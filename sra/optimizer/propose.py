"""Формирование предложения решения на основе оптимизатора (этап 4).

Контур отказов (каждый путь возвращает NO_CHANGE с машиночитаемыми причинами):
  - REAL-среда: контур эксплуатации не введён; предложение не формируется;
  - политика отсутствует/недействительна/не применима к среде;
  - прогноз недоступен (COLD_START) или недостаточно данных;
  - дрейф обнаружен → надёжность прогноза недостаточна;
  - базовая линия «готовить как обычно» неизвестна;
  - распределение спроса недоступно (мало калибровочных остатков);
  - неразрешимость в разрешённых границах / тайм-аут решателя;
  - улучшение незначимо (меньше порога политики) или ниже разрешения сетки.

В DEMO допустимая политика (обязательно SYNTHETIC) даёт предложение
ADJUST_PREPARED_VOLUME со статусом SIMULATED_NOT_EXECUTABLE: это расчёт
сценария, а не разрешение на действие. Согласование конкретной версии
требуется отдельно; реального вмешательства это не разрешает.
"""

from __future__ import annotations

import uuid
from datetime import date

from .. import enums
from ..decide import DecisionProposal, REASON_DESCRIPTIONS, propose_action
from ..errors import NotFoundError, ValidationError
from ..forecasting.pipeline import decision_forecast
from ..forecasting.score import empirical_quantile
from ..forecasting.series import eligible_points, make_snapshot_fn
from .core import DemandScenarios, habitual_baseline, scenario_cost_deltas, solve_prepared_volume
from .policies import check_policy_usable, validate_decision_policy

# Новые коды причин NO_CHANGE (этап 4).
COND_FORECAST_UNAVAILABLE = "FORECAST_UNAVAILABLE"
COND_NO_DEMAND_DISTRIBUTION = "NO_DEMAND_DISTRIBUTION"
COND_NO_BASELINE = "NO_BASELINE"
COND_FORECAST_DRIFT = "FORECAST_UNRELIABLE_DRIFT"
COND_POLICY_INVALID = "POLICY_PARAMS_INVALID"
COND_POLICY_NOT_USABLE = "POLICY_NOT_USABLE_IN_ENV"
COND_INFEASIBLE = "INFEASIBLE_WITHIN_APPROVED_BOUNDS"
COND_SOLVER_TIMEOUT = "SOLVER_TIMEOUT"
COND_NO_IMPROVEMENT = "NO_MEANINGFUL_IMPROVEMENT"
COND_BELOW_RESOLUTION = "CHANGE_BELOW_RESOLUTION"
COND_MODE_BLOCKED_STAGE4 = "MODE_BLOCKED_STAGE4_REAL"

REASON_DESCRIPTIONS.update({
    "OPEN_ENV_FOREIGN_DATA": "среда OPEN: чужие открытые измерения — действия "
                             "не предлагаются и не выполняются",
    COND_FORECAST_UNAVAILABLE: "прогноз на целевую дату недоступен (холодный старт/мало истории)",
    COND_NO_DEMAND_DISTRIBUTION: "недостаточно калибровочных остатков для распределения спроса",
    COND_NO_BASELINE: "неизвестна базовая линия «готовить как обычно» (нет истории prepared)",
    COND_FORECAST_DRIFT: "обнаружен дрейф: надёжность прогноза недостаточна для решения",
    COND_POLICY_INVALID: "параметры политики решения отсутствуют или нарушают валидацию",
    COND_POLICY_NOT_USABLE: "политика не применима в данной среде (статус/природа значений)",
    COND_INFEASIBLE: "в границах разрешённого изменения нет кандидата с допустимым риском нехватки",
    COND_SOLVER_TIMEOUT: "бюджет времени решателя исчерпан",
    COND_NO_IMPROVEMENT: "ожидаемое улучшение ниже порога значимости политики",
    COND_BELOW_RESOLUTION: "лучший кандидат отличается от базовой линии меньше разрешения сетки",
    COND_MODE_BLOCKED_STAGE4: "оптимизационные предложения в REAL-среде заблокированы до "
                              "введения проверенных политик и контура одобрения",
})

MIN_BASELINE_POINTS = 3
MIN_RESIDUALS = 3  # согласовано с forecasting (MIN_RESIDUALS_FOR_INTERVAL)


def _no_change(store, *, school_id, target_date, mode, codes: list[str],
               extra_notes: list[str] | None = None,
               policy_refs: list[str] | None = None) -> DecisionProposal:
    return DecisionProposal(
        proposal_id=uuid.uuid4().hex,
        mode=mode,
        environment=store.environment,
        decision_at=store._clock(),
        school_id=school_id,
        target_date=str(target_date),
        action="NO_CHANGE",
        action_status="NOT_EXECUTABLE",
        reasons=list(dict.fromkeys(codes)),
        missing_conditions=[REASON_DESCRIPTIONS[c] for c in dict.fromkeys(codes)],
        policy_refs=policy_refs or [],
        constraint_checks={"stage": "optimizer_v1", "executed": False},
        notes=[*(extra_notes or []),
               "NO_CHANGE — отказ от нового вмешательства; он НЕ подтверждает "
               "безопасность текущей практики кухни"]
    )


def propose_adjustment(store, policies, *, school_id: str, meal_type: str,
                       target_date: str, policy_id: str, actor: str,
                       decision_hour: int | None = None,
                       requested_mode: str | None = None) -> DecisionProposal:
    """Предложение ограниченного изменения объёма приготовления.

    В REAL-среде возвращает NO_CHANGE (контур эксплуатации не введён).
    В DEMO рассчитывает симуляционное предложение по явно синтетической политике.
    """
    target = date.fromisoformat(target_date)

    # 1. REAL-среда: предложение действия не формируется (этап 4 — симуляция).
    if store.environment == enums.ENV_REAL:
        base = propose_action(store, policies, school_id=school_id, target_date=target_date,
                              requested_mode=requested_mode or enums.MODE_OPERATIONAL)
        codes = list(dict.fromkeys(base.reasons + [COND_MODE_BLOCKED_STAGE4]))
        return _no_change(store, school_id=school_id, target_date=target,
                          mode=requested_mode or enums.MODE_OPERATIONAL, codes=codes,
                          extra_notes=["этап 4: в REAL-среде выполняется только расчёт "
                                       "сценариев (SHADOW-аналитика); предложение действия "
                                       "появится не раньше этапов 5–6 при выполненных "
                                       "условиях ворот"],
                          policy_refs=base.policy_refs)

    # 1а. OPEN-среда: чужие открытые данные — расчёт метрик/прогнозов да, действия нет.
    if store.environment == enums.ENV_OPEN:
        base = propose_action(store, policies, school_id=school_id, target_date=target_date,
                              requested_mode=requested_mode or enums.MODE_OPERATIONAL)
        codes = list(dict.fromkeys(base.reasons + [COND_MODE_BLOCKED_STAGE4, "OPEN_ENV_FOREIGN_DATA"]))
        return _no_change(store, school_id=school_id, target_date=target,
                          mode=requested_mode or enums.MODE_OPERATIONAL, codes=codes,
                          extra_notes=["среда OPEN: это чужие открытые измерения "
                                       "(source_ref указывает источник); система не предлагает "
                                       "и не выполняет действий в чужой школе"],
                          policy_refs=base.policy_refs)

    # 2. DEMO: политика решения.
    mode = enums.MODE_DEMO
    try:
        row = policies.get(policy_id)
    except NotFoundError:
        return _no_change(store, school_id=school_id, target_date=target, mode=mode,
                          codes=[COND_POLICY_INVALID],
                          extra_notes=[f"политика {policy_id!r} не найдена"])
    payload = row["payload_json"]
    problems = validate_decision_policy(payload.get("values", {}))
    if problems:
        return _no_change(store, school_id=school_id, target_date=target, mode=mode,
                          codes=[COND_POLICY_INVALID],
                          extra_notes=[f"политика {policy_id}: {p}" for p in problems],
                          policy_refs=[_pref(row)])
    problems = check_policy_usable(row, environment=store.environment)
    if problems:
        return _no_change(store, school_id=school_id, target_date=target, mode=mode,
                          codes=[COND_POLICY_NOT_USABLE],
                          extra_notes=[f"политика {policy_id}: {p}" for p in problems],
                          policy_refs=[_pref(row)])
    values = payload["values"]
    policy_refs = [_pref(row)]

    # 3. Прогноз на момент решения (as-of).
    snapshot_fn = make_snapshot_fn(store, school_id, meal_type)
    fc = decision_forecast(
        snapshot_fn, target, model_name=values["forecast_model"],
        calib_days=int(values["calib_days"]),
        decision_hour=int(decision_hour if decision_hour is not None else 18))
    forecast_ref = (f"{fc['model_name']}@{fc['cutoff_iso']}|hist={fc['n_history']}"
                    f"|calib={fc['n_calib_used']}")
    if fc["status"] != enums.FORECAST_OK or fc["pred_g"] is None:
        return _no_change(store, school_id=school_id, target_date=target, mode=mode,
                          codes=[COND_FORECAST_UNAVAILABLE],
                          extra_notes=[f"прогноз: {fc}"],
                          policy_refs=policy_refs)
    if fc["drift"]["status"] == enums.DRIFT_DETECTED:
        return _no_change(store, school_id=school_id, target_date=target, mode=mode,
                          codes=[COND_FORECAST_DRIFT],
                          extra_notes=[f"дрейф: {fc['drift']}"],
                          policy_refs=policy_refs)
    if len(fc["residuals_g"]) < MIN_RESIDUALS:
        return _no_change(store, school_id=school_id, target_date=target, mode=mode,
                          codes=[COND_NO_DEMAND_DISTRIBUTION],
                          extra_notes=[f"калибровочных остатков: {len(fc['residuals_g'])}"],
                          policy_refs=policy_refs)

    # 4. Базовая линия «готовить как обычно» (as-of на момент решения).
    cutoff_snap = snapshot_fn(fc["cutoff_iso"])
    hist_all = [p for p in cutoff_snap.points if p.service_date < target]
    hist_all.sort(key=lambda p: p.service_date)
    p0 = habitual_baseline(hist_all, days=int(values["baseline_days"]))
    n_prepared = sum(1 for p in hist_all if p.prepared_mass_g is not None)
    if p0 is None or n_prepared < MIN_BASELINE_POINTS:
        return _no_change(store, school_id=school_id, target_date=target, mode=mode,
                          codes=[COND_NO_BASELINE],
                          extra_notes=[f"точек с prepared_mass в истории: {n_prepared}"],
                          policy_refs=policy_refs)

    # 5. Решение задачи.
    scenarios = DemandScenarios.build(fc["pred_g"], fc["residuals_g"])
    delta_max = float(values["delta_max_pct_of_baseline"]) * p0
    solve = solve_prepared_volume(
        baseline_g=p0, scenarios=scenarios,
        cost_waste_per_kg=float(values["cost_waste_per_kg"]),
        cost_shortage_per_kg=float(values["cost_shortage_per_kg"]),
        shortage_risk_max=float(values["shortage_risk_max"]),
        delta_max_g=delta_max, grid_step_g=float(values["grid_step_g"]))
    if solve["status"] == "TIMEOUT":
        return _no_change(store, school_id=school_id, target_date=target, mode=mode,
                          codes=[COND_SOLVER_TIMEOUT], extra_notes=[solve["reason"]],
                          policy_refs=policy_refs)
    if solve["status"] == "INFEASIBLE":
        notes = [solve["reason"]]
        if not solve["checks"]["shortage_risk_max"].get("baseline_pass", True):
            notes.append("ВНИМАНИЕ: текущая практика, по оценке, нарушает допустимый риск "
                         "нехватки — передайте ответственному за столовую; система "
                         "самостоятельно границу не расширяет")
        return _no_change(store, school_id=school_id, target_date=target, mode=mode,
                          codes=[COND_INFEASIBLE], extra_notes=notes,
                          policy_refs=policy_refs)
    if solve["status"] == "NO_CHANGE_PREFERRED":
        return _no_change(store, school_id=school_id, target_date=target, mode=mode,
                          codes=[COND_BELOW_RESOLUTION], extra_notes=[solve["reason"]],
                          policy_refs=policy_refs)
    if solve["improvement_rel"] < float(values["min_improvement_rel"]):
        return _no_change(store, school_id=school_id, target_date=target, mode=mode,
                          codes=[COND_NO_IMPROVEMENT],
                          extra_notes=[f"относительное улучшение {solve['improvement_rel']:.4f} "
                                       f"< порога {values['min_improvement_rel']}"],
                          policy_refs=policy_refs)

    # 6. Предложение (DEMO: SIMULATED, не исполняемое без согласования версии).
    deltas = scenario_cost_deltas(
        solve["chosen_g"], p0, scenarios,
        cost_waste_per_kg=float(values["cost_waste_per_kg"]),
        cost_shortage_per_kg=float(values["cost_shortage_per_kg"]))
    n = len(deltas)
    effect_interval = {
        "nature": enums.SIMULATED,
        "unit": f"{values['currency']}/сценарий",
        "definition": "стоимость сценария спроса при выбранном P минус при NO_CHANGE "
                      "(< 0 — экономия против текущей практики); разброс по сценариям",
        "q10": deltas[int(0.10 * (n - 1))],
        "q50": deltas[n // 2],
        "q90": deltas[int(0.90 * (n - 1))],
        "n_scenarios": n,
        "caveat": "не денежная экономия; контрфактическая симуляция на эмпирических "
                  "сценариях спроса",
    }
    notes = [
        "СИМУЛЯЦИЯ (DEMO): политика и данные синтетические; предложение не даёт права "
        "на реальное вмешательство",
        "действие меняет ТОЛЬКО общий объём приготовления; нормативная порция и меню "
        "не затрагиваются",
        "сценарии спроса построены по выданным массам; на цензурированных днях полный "
        "спрос выше — риск нехватки может быть занижен",
        "ожидаемый эффект — контрфактическая оценка, а не измеренный результат",
        "исполнение требует согласования КОНКРЕТНОЙ версии предложения уполномоченным",
    ]
    return DecisionProposal(
        proposal_id=uuid.uuid4().hex,
        mode=mode,
        environment=store.environment,
        decision_at=store._clock(),
        school_id=school_id,
        target_date=str(target),
        action="ADJUST_PREPARED_VOLUME",
        action_status="SIMULATED_NOT_EXECUTABLE",
        reasons=[],
        missing_conditions=[],
        action_params={
            "planned_prepared_mass_g": solve["chosen_g"],
            "baseline_mass_g": p0,
            "delta_g": solve["delta_g"],
            "delta_max_g": delta_max,
            "expected_outcome_chosen": solve["chosen_metrics"],
            "expected_outcome_baseline": solve["baseline_metrics"],
            "cost_baseline": solve["cost_baseline"],
            "cost_chosen": solve["cost_chosen"],
            "improvement": solve["improvement"],
            "improvement_rel": solve["improvement_rel"],
            "forecast": {
                "pred_g": fc["pred_g"],
                "q10_g": empirical_quantile(fc["residuals_g"], 0.1),
                "q90_g": empirical_quantile(fc["residuals_g"], 0.9),
                "n_scenarios": len(fc["residuals_g"]),
                "cutoff_iso": fc["cutoff_iso"],
            },
        },
        forecast_ref=forecast_ref,
        policy_refs=policy_refs,
        expected_effect_interval=effect_interval,
        constraint_checks={**solve["checks"],
                           "data_versions": {"dataset_version": store.get_dataset_version(),
                                             "forecast": forecast_ref}},
        approval=None,
        notes=notes,
    )


def _pref(row: dict) -> str:
    return f"{row['policy_id']}@v{row['version']}"
