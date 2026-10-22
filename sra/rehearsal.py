"""Репетиция учебного года на синтетике (AUDIT §D.18).

Прогоняет ПОЛНЫЙ протокол на калиброванном генераторе как тренинг школы до
реального пилота: генерация года с фазами (CALIBRATION → BASELINE → SHADOW →
INTERVENTION → FOLLOWUP в данных) → импорт в DEMO-хранилище → фазовые
переходы через PhaseManager (настоящие ворота: BASELINE требует протокол,
SHADOW — политику и достаточность, INTERVENTION ЗАБЛОКИРОВАН с перечнем
условий) → сравнение фаз → backtest'ы diners/composite → проверка порций по
Санправилам → алерты → дайджест.

Границы репетиции:
- ВСЁ SYNTHETIC/DEMO: ни одно число не переносится на реальную школу;
- эффект вмешательства в данных — сценарий генератора, НЕ доказательство;
- INTERVENTION-переход намеренно не исполняется: репетиция показывает школе,
  какие условия надо подготовить (согласия, VERIFIED-протокол, политики,
  одобренный план) — это и есть тренинг;
- каждая секция независима: сбой секции фиксируется с причиной, остальные
  продолжаются.
"""
from __future__ import annotations

import dataclasses
import json
from pathlib import Path

from . import enums

# Сценарий года (учебные дни): границы фаз по порядковому номеру дня.
PHASE_PLAN = (
    (enums.PHASE_CALIBRATION, 5),
    (enums.PHASE_BASELINE, 60),
    (enums.PHASE_SHADOW, 20),
    (enums.PHASE_INTERVENTION, 60),
    (enums.PHASE_FOLLOWUP, None),   # остаток года
)


def _phase_of(day_index: int) -> str:
    i = 0
    for phase, length in PHASE_PLAN:
        if length is None or day_index < i + length:
            return phase
        i += length
    return PHASE_PLAN[-1][0]


def _intervention_start() -> int:
    """День начала фазы INTERVENTION = сумма длин предыдущих фаз."""
    total = 0
    for phase, length in PHASE_PLAN:
        if phase == enums.PHASE_INTERVENTION:
            return total
        total += length or 0
    return total


def run_rehearsal(out_dir, *, seed: int = 2026, days: int = 170,
                  enrollment: int = 120, meal_type: str = "LUNCH") -> dict:
    """Полный протокол года на синтетике → отчёт + rehearsal_report.json."""
    from dataclasses import replace as dc_replace

    from .csvio import parse_file
    from .demo.calibrated import SCHOOL_ID, generate
    from .demo.run import DEMO_COST_POLICY
    from .evaluation.compare_phases import compare_phases
    from .forecasting.composite import backtest_composite
    from .forecasting.diners import backtest_diners
    from .monitoring import nutrition_alerts
    from .norms import check_portions
    from .policies import PolicyRegistry
    from .phases import PhaseManager
    from .store import EvidenceStore

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    report: dict = {
        "data_nature": enums.SYNTHETIC,
        "environment": enums.ENV_DEMO,
        "school_id": SCHOOL_ID,
        "seed": seed,
        "planned_days": days,
        "phase_plan": [{"phase": ph, "days": ln} for ph, ln in PHASE_PLAN],
        "warnings": [
            "РЕПЕТИЦИЯ: все данные SYNTHETIC, сценарий эффекта задан "
            "генератором; это тренинг протокола, не доказательство эффекта",
            "переход INTERVENTION через PhaseManager заблокирован НАМЕРЕННО: "
            "школа должна подготовить условия (согласия, VERIFIED-протокол, "
            "политики, одобренный план оценки)",
        ],
        "steps": {},
    }

    # 1. Генерация года с границей эффекта на старте INTERVENTION-фазы.
    change_day = _intervention_start()
    manifest = generate(out / "data", seed=seed, n_days=days,
                        enrollment=enrollment, mode_change_day=change_day)
    report["steps"]["generate"] = {"ok": True,
                                   "manifest_source": manifest["source_ref"]}

    # 2. Переклейка фаз по сценарию года (данные генератора помечены
    #    BASELINE; репетиция раскладывает их по PHASE_PLAN).
    sessions = parse_file(out / "data" / "sessions.csv", "sessions")
    sessions.sort(key=lambda s: s.service_date)
    relabeled = [dc_replace(s, study_phase=_phase_of(i))
                 for i, s in enumerate(sessions)]
    observations = parse_file(out / "data" / "observations.csv", "observations")

    # 3. DEMO-хранилище + импорт (валидация до записи, батч атомарен).
    db_path = out / "rehearsal.sqlite3"
    store = EvidenceStore(db_path, enums.ENV_DEMO)
    try:
        store.import_sessions(relabeled, actor="rehearsal",
                              import_meta={"source": "synthetic-rehearsal"})
        store.import_observations(observations, actor="rehearsal",
                                  import_meta={"source": "synthetic-rehearsal"})
        report["steps"]["import"] = {
            "ok": True, "sessions": len(relabeled),
            "observations": len(observations)}

        # 4. Политика стоимости (SYNTHETIC, только для DEMO-метрик).
        policies = PolicyRegistry(store)
        policies.register(
            policy_id=DEMO_COST_POLICY["policy_id"],
            version=DEMO_COST_POLICY["version"],
            jurisdiction=DEMO_COST_POLICY["jurisdiction"],
            source_ref=manifest["source_ref"],
            valid_from="2000-01-01", valid_until="2099-12-31",
            payload=DEMO_COST_POLICY["payload"], actor="rehearsal",
            data_nature=enums.SYNTHETIC)

        # 5. Фазовые переходы через настоящие ворота протокола.
        pm = PhaseManager(store, policies)
        protocol_ref = f"synthetic-rehearsal:seed={seed}"
        transitions = []
        for phase in (enums.PHASE_CALIBRATION, enums.PHASE_BASELINE,
                      enums.PHASE_SHADOW, enums.PHASE_INTERVENTION):
            try:
                if phase == enums.PHASE_SHADOW:
                    pm.transition(SCHOOL_ID, phase, actor="rehearsal",
                                  protocol_ref=protocol_ref,
                                  policy_id=DEMO_COST_POLICY["policy_id"],
                                  meal_type=meal_type)
                else:
                    pm.transition(SCHOOL_ID, phase, actor="rehearsal",
                                  protocol_ref=protocol_ref,
                                  meal_type=meal_type)
                transitions.append({"phase": phase, "result": "OK"})
            except Exception as e:  # GuardError — ожидаем для INTERVENTION
                transitions.append({
                    "phase": phase, "result": "BLOCKED",
                    "code": getattr(e, "code", type(e).__name__),
                    "conditions": (getattr(e, "details", {}) or {})
                    .get("conditions") or [str(e)]})
        report["steps"]["phases"] = {"ok": True, "transitions": transitions}

        fetched = [s for s in store.fetch_sessions(school_id=SCHOOL_ID)
                   if s.meal_type == meal_type]
        obs_all = store.fetch_observations(school_id=SCHOOL_ID)

        # 6. Сравнение фаз (сценарные BASELINE vs INTERVENTION).
        try:
            report["steps"]["compare_phases"] = compare_phases(
                store, SCHOOL_ID, min_days_per_phase=5)
        except Exception as e:
            report["steps"]["compare_phases"] = {
                "status": "UNAVAILABLE", "reason": str(e)}

        # 7. Backtest'ы (год даёт достаточно точек — в отличие от 30-дневки).
        try:
            report["steps"]["diners_backtest"] = backtest_diners(
                fetched, test_days=30, calib_days=30)
        except Exception as e:
            report["steps"]["diners_backtest"] = {
                "status": "UNAVAILABLE", "reason": str(e)}
        try:
            report["steps"]["composite_backtest"] = backtest_composite(
                fetched, obs_all, field="served_mass_g",
                test_days=20, calib_days=20)
        except Exception as e:
            report["steps"]["composite_backtest"] = {
                "status": "UNAVAILABLE", "reason": str(e)}

        # 8. Нормы порций + алерты.
        report["steps"]["norms"] = check_portions(
            obs_all, age_group="6-11", sessions=fetched, per="total")
        report["steps"]["alerts"] = nutrition_alerts(
            store, SCHOOL_ID, meal_type=meal_type)

        # 9. Дайджест по хранилищу репетиции.
        from .digest import build_digest
        from .explain import explain_digest
        digest = build_digest(store, SCHOOL_ID, meal_type=meal_type)
        digest["explanation"] = explain_digest(digest)
        report["steps"]["digest"] = digest
    finally:
        store.close()

    out_report = out / "rehearsal_report.json"
    out_report.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                          encoding="utf-8")
    report["report_file"] = str(out_report)
    return report
