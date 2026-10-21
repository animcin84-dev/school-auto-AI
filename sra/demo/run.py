"""Изолированное демо: генерация SYNTHETIC → импорт в DEMO-БД → метрики → NO_CHANGE.

Изоляция:
  - демо живёт только в указанном каталоге (--dir, по умолчанию data/demo);
  - БД демо создаётся с маркером среды DEMO; REAL-данные в неё не проходят
    (guard в EvidenceStore), SYNTHETIC не проходят в REAL-хранилище;
  - reset удаляет ТОЛЬКО файлы демо с фиксированными именами в каталоге демо;
    реальные данные (любые другие пути/файлы) не затрагиваются.
"""

from __future__ import annotations

import json
from pathlib import Path

from .. import enums, metrics, validation
from ..decide import propose_action
from ..policies import PolicyRegistry
from ..store import EvidenceStore
from .synthetic import generate

DEMO_FILES = ("demo.sqlite3", "sessions.csv", "observations.csv", "manifest.json", "demo_report.json")

# Синтетическая политика стоимости для демонстрации ветки CostWastePerMeal.
# Значения ИСКУССТВЕННЫЕ; политика остаётся UNVERIFIED и не даёт реальных прав.
DEMO_COST_POLICY = {
    "policy_id": "POLICY-DEMO-COST-001",
    "version": 1,
    "jurisdiction": "DEMO",
    "payload": {"cost_per_kg": 500.0, "currency": "KZT_DEMO"},
}

# Синтетическая политика РЕШЕНИЯ (этап 4): все параметры искусственные,
# политика UNVERIFIED; предложение оптимизатора в DEMO — SIMULATED и не даёт
# права на реальное вмешательство.
DEMO_OPT_POLICY = {
    "policy_id": "POLICY-DEMO-OPT-001",
    "version": 1,
    "jurisdiction": "DEMO",
    "payload": {
        "cost_waste_per_kg": 500.0,          # KZT_DEMO за кг предотвратимых потерь
        "cost_shortage_per_kg": 5000.0,      # доминирование: нехватка дороже потерь
        "currency": "KZT_DEMO",
        "shortage_risk_max": 0.10,           # жёсткая граница риска нехватки
        "delta_max_pct_of_baseline": 0.15,   # максимум ±15% от привычного объёма
        "baseline_days": 8,
        "forecast_model": "seasonal_naive",
        "seasonal_weeks": 3,
        "calib_days": 8,
        "grid_step_g": 250.0,
        "min_improvement_rel": 0.02,
    },
}


def run_demo(out_dir: str | Path = "data/demo", *, seed: int = 42, n_days: int = 60,
             reset: bool = False) -> dict:
    out = Path(out_dir)
    db_path = out / "demo.sqlite3"
    if db_path.exists():
        if not reset:
            raise RuntimeError(
                f"демо уже создано в {out}; используйте --reset (удалит только файлы демо: "
                f"{', '.join(DEMO_FILES)})")
        _reset_demo(out)

    # 1. Генерация синтетики (детерминированная).
    manifest = generate(out, seed=seed, n_days=n_days)

    # 2. DEMO-хранилище (маркер среды DEMO; реальные данные сюда не проходят).
    store = EvidenceStore(db_path, enums.ENV_DEMO)

    # 3. Импорт + валидация (валидация идёт ДО записи; батч атомарен).
    session_recs = _validated_import(store, out / "sessions.csv", "sessions", actor="demo")
    obs_recs = _validated_import(store, out / "observations.csv", "observations", actor="demo")

    # 4. Явно обозначенная синтетическая политика стоимости (остаётся UNVERIFIED).
    policies = PolicyRegistry(store)
    policies.register(
        policy_id=DEMO_COST_POLICY["policy_id"], version=DEMO_COST_POLICY["version"],
        jurisdiction=DEMO_COST_POLICY["jurisdiction"], source_ref=manifest["source_ref"],
        valid_from="2000-01-01", valid_until="2099-12-31",
        payload=DEMO_COST_POLICY["payload"], actor="demo", data_nature=enums.SYNTHETIC)
    cost_source = metrics.CostSource(
        policy_id=DEMO_COST_POLICY["policy_id"],
        cost_per_kg=DEMO_COST_POLICY["payload"]["cost_per_kg"],
        currency=DEMO_COST_POLICY["payload"]["currency"],
        evidence_status=enums.SYNTHETIC)

    # 5. Метрики по всей области демо (SIMULATED, т.к. входы SYNTHETIC).
    sessions = store.fetch_sessions(school_id=manifest["school_id"])
    observations = store.fetch_observations(school_id=manifest["school_id"])
    metric_report = metrics.build_metric_report(sessions, observations, cost_source=cost_source)

    # 6. Решение: защитный чек-лист -> NO_CHANGE с причинами.
    proposal = propose_action(store, policies, school_id=manifest["school_id"],
                              target_date=sessions[-1].service_date.isoformat()).to_dict()

    # 7. Базовые прогнозы (этап 3) на тех же синтетических данных.
    # Тестовое окно меньше стандартного: демо-ряд короткий; при нехватке точек
    # честный статус DATA_NOT_READY.
    from ..forecasting.backtest import run_backtest
    from ..forecasting.series import make_snapshot_fn
    forecast_report = run_backtest(
        snapshot_fn=make_snapshot_fn(store, manifest["school_id"], "LUNCH"),
        test_days=min(5, max(1, manifest["n_sessions"] // 6)),
        calib_days=5, min_train=5,
        environment=enums.ENV_DEMO)
    forecast_cards = []
    if forecast_report.get("status") == enums.METRIC_OK:
        for m in forecast_report.get("models", []):
            if m.get("status") != enums.FORECAST_OK:
                continue
            forecast_cards.append(store.save_model_card(
                model_id=f"sra.forecast.{m['name']}", model_name=m["name"],
                params=m["params"],
                data_meta={"school_id": manifest["school_id"], "meal_type": "LUNCH",
                           "windows": forecast_report["windows"],
                           "dataset_version": store.get_dataset_version()},
                metrics={"mae_g": m.get("mae_g"), "pinball": m.get("pinball"),
                         "interval": m.get("interval")},
                actor="demo", environment=enums.ENV_DEMO))
    forecast_report["model_cards"] = forecast_cards

    # 7b. Симуляционное предложение оптимизатора (этап 4) на следующий учебный день.
    from datetime import date as _date, timedelta as _timedelta
    from ..optimizer.propose import propose_adjustment
    from ..optimizer.compare import compare_strategies
    policies.register(
        policy_id=DEMO_OPT_POLICY["policy_id"], version=DEMO_OPT_POLICY["version"],
        jurisdiction=DEMO_OPT_POLICY["jurisdiction"], source_ref=manifest["source_ref"],
        valid_from="2000-01-01", valid_until="2099-12-31",
        payload=DEMO_OPT_POLICY["payload"], actor="demo", data_nature=enums.SYNTHETIC)
    last_day = _date.fromisoformat(sessions[-1].service_date.isoformat())
    next_school_day = last_day + _timedelta(days=1)
    while next_school_day.weekday() >= 5:
        next_school_day += _timedelta(days=1)
    optimizer_decision = propose_adjustment(
        store, policies, school_id=manifest["school_id"], meal_type="LUNCH",
        target_date=next_school_day.isoformat(),
        policy_id=DEMO_OPT_POLICY["policy_id"], actor="demo").to_dict()
    opt_values = DEMO_OPT_POLICY["payload"]
    strategy_comparison = compare_strategies(
        store, school_id=manifest["school_id"], meal_type="LUNCH",
        test_days=min(8, max(3, manifest["n_sessions"] // 8)),
        calib_days=int(opt_values["calib_days"]),
        baseline_days=int(opt_values["baseline_days"]),
        model_name=opt_values["forecast_model"],
        seasonal_weeks=int(opt_values["seasonal_weeks"]),
        cost_waste_per_kg=opt_values["cost_waste_per_kg"],
        cost_shortage_per_kg=opt_values["cost_shortage_per_kg"],
        shortage_risk_max=opt_values["shortage_risk_max"],
        delta_max_pct=opt_values["delta_max_pct_of_baseline"],
        grid_step_g=opt_values["grid_step_g"])

    # 7c. SHADOW-контур (этап 5а): фазы (синтетические переходы DEMO), shadow-дни,
    # мониторинг качества и проект плана оценки. Ворота фаз могут честно
    # заблокировать SHADOW на коротком ряде — демо это фиксирует, а не обходит.
    from ..errors import GuardError
    from ..monitoring import monitor_shadow
    from ..phases import PhaseManager
    from ..shadow import run_shadow
    pm = PhaseManager(store, policies)
    school = manifest["school_id"]
    gate_block = None
    try:
        if pm.current(school) is None:
            pm.transition(school, enums.PHASE_CALIBRATION, actor="demo")
        if pm.current(school)["phase"] == enums.PHASE_CALIBRATION:
            pm.transition(school, enums.PHASE_BASELINE, actor="demo",
                          protocol_ref="synthetic-protocol:v1 (DEMO; не реальный протокол)")
        if pm.current(school)["phase"] == enums.PHASE_BASELINE:
            pm.transition(school, enums.PHASE_SHADOW, actor="demo",
                          policy_id=DEMO_OPT_POLICY["policy_id"])
    except GuardError as e:
        gate_block = e.to_dict()
    study_phase = pm.current(school)
    in_shadow = study_phase["phase"] == enums.PHASE_SHADOW
    if in_shadow:
        shadow_report = run_shadow(store, policies, school_id=school,
                                   meal_type="LUNCH", policy_id=DEMO_OPT_POLICY["policy_id"],
                                   days=min(10, max(1, manifest["n_sessions"] // 4)),
                                   actor="demo")
        monitoring_report = monitor_shadow(store, school, "LUNCH")
    else:
        shadow_report = {"status": enums.DATA_NOT_READY,
                         "reason": f"фаза SHADOW не достигнута: {gate_block}"}
        monitoring_report = {"status": enums.DATA_NOT_READY,
                             "reason": "фаза SHADOW не достигнута"}
    evaluation_plan = None
    recs = [r for r in store.list_shadow_records(school, "LUNCH")
            if r["pred_g"] is not None and r["y_g"] is not None]
    if len(recs) >= 8:
        from statistics import mean as _mean, stdev as _stdev
        from ..evaluation.design import build_evaluation_plan
        sigma = _stdev([r["y_g"] - r["pred_g"] for r in recs])
        baseline_waste = _mean([max(r["actual_prepared_g"] - r["y_g"], 0.0)
                                for r in recs if r["actual_prepared_g"] is not None])
        evaluation_plan = build_evaluation_plan(
            school_id=school, design="two_arm_parallel",
            sigma_g=sigma, baseline_waste_g_per_day=baseline_waste,
            expected_effect_rel=0.20)
    else:
        evaluation_plan = {"status": enums.DATA_NOT_READY,
                           "reason": "shadow-дней с прогнозом и фактом меньше 8"}

    # 8. Самопроверка инвариантов хранилища.
    selfcheck = store.selfcheck()

    report = {
        "data_nature": enums.SYNTHETIC,
        "environment": enums.ENV_DEMO,
        "mode": enums.MODE_DEMO,
        "warnings": [
            "ДЕМО: все данные SYNTHETIC, метрики SIMULATED; это проверка кода, "
            "а не польза в реальной школе",
            "результат решения NO_CHANGE: в этапе 1 нет модели, проверенных политик "
            "и одобрений; предложение действия не формируется",
            "качество на синтетике не переносится на реальные школы",
        ],
        "manifest": manifest,
        "validation": {"sessions": session_recs, "observations": obs_recs},
        "metric_report": metric_report,
        "decision": proposal,
        "forecast": forecast_report,
        "optimizer_decision": optimizer_decision,
        "strategy_comparison": strategy_comparison,
        "study_phase": study_phase,
        "shadow": shadow_report,
        "monitoring": monitoring_report,
        "evaluation_plan": evaluation_plan,
        "selfcheck": selfcheck,
    }
    (out / "demo_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    store.close()
    return report


def _validated_import(store: EvidenceStore, path: Path, kind: str, *, actor: str) -> dict:
    from ..csvio import parse_file
    records = parse_file(path, kind)
    if kind == "sessions":
        report = validation.validate_sessions(records, existing_ids=store.session_ids())
        if not report.ok:
            raise RuntimeError(f"валидация {kind} не прошла в демо: "
                               f"{[i.to_dict() for i in report.errors][:3]}")
        return store.import_sessions(report.accepted, actor=actor,
                                     import_meta={"source": "synthetic_generator"})
    report = validation.validate_observations(
        records, known_sessions=store.session_ids(),
        existing_observation_ids=store.observation_ids(),
        existing_pairs=store.observation_pairs())
    if not report.ok:
        raise RuntimeError(f"валидация {kind} не прошла в демо: "
                           f"{[i.to_dict() for i in report.errors][:3]}")
    return store.import_observations(report.accepted, actor=actor,
                                     import_meta={"source": "synthetic_generator"})


def reset_demo(out_dir: str | Path = "data/demo") -> list[str]:
    """Удаляет ТОЛЬКО файлы демо с фиксированными именами. Реальные данные не затрагивает."""
    out = Path(out_dir)
    removed = []
    for name in DEMO_FILES:
        p = out / name
        if p.exists():
            p.unlink()
            removed.append(name)
    return removed


def _reset_demo(out: Path) -> None:
    reset_demo(out)
