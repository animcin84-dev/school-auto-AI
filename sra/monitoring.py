"""Мониторинг качества прогнозов и shadow-решений (этап 5а).

Считает по накопленным shadow-записям (хронологически):
  - MAE последних k дней;
  - фактическое покрытие эмпирического интервала q10–q90 против номинального
    (0.8); отклонение больше допуска — предупреждение (интервалы НЕ объявляются
    калиброванными без проверки);
  - дрейф по последовательности остатков (ранняя половина против поздней);
  - средний контрфакт фактического дня cost_delta_vs_actual (SIMULATED).

Статусы: OK | WARN_COVERAGE | DRIFT_DETECTED | DATA_NOT_READY.
Дрейф имеет высший приоритет: он отключает предложения на уровне
propose_adjustment (проверяется в этап 4), мониторинг лишь сообщает.
"""

from __future__ import annotations

from . import enums
from .forecasting.drift import detect_drift
from .forecasting.score import mean

# Номинальное покрытие соответствует паре квантилей 0.1/0.9.
INTERVAL_NOMINAL_COVERAGE = 0.80
COVERAGE_TOLERANCE = 0.15
MIN_RECORDS = 5


def monitor_shadow(store, school_id: str, meal_type: str,
                   *, recent_k: int = 10) -> dict:
    all_records = store.list_shadow_records(school_id, meal_type)
    recs = [r for r in all_records
            if r["pred_g"] is not None and r["y_g"] is not None]
    if len(recs) < MIN_RECORDS:
        return {"status": enums.DATA_NOT_READY,
                "n_quality_records": len(recs),
                "n_total_records": len(all_records),
                "reason": f"для мониторинга нужно ≥{MIN_RECORDS} shadow-дней с "
                          "прогнозом и фактом"}
    residuals = [r["y_g"] - r["pred_g"] for r in recs]
    recent = residuals[-recent_k:]
    mae_recent = mean([abs(x) for x in recent])
    with_interval = [r for r in recs
                     if r["pred_q10"] is not None and r["pred_q90"] is not None]
    coverage = (sum(1 for r in with_interval
                    if r["pred_q10"] <= r["y_g"] <= r["pred_q90"]) / len(with_interval)
                if with_interval else None)
    half = len(residuals) // 2
    drift = detect_drift(residuals[:half], residuals[half:],
                         k=max(2, len(residuals) - half))
    cost_deltas = [r["cost_delta_vs_actual"] for r in recs
                   if r["cost_delta_vs_actual"] is not None]

    if drift["status"] == enums.DRIFT_DETECTED:
        status = enums.DRIFT_DETECTED
        reason = drift["note"]
    elif coverage is not None and abs(coverage - INTERVAL_NOMINAL_COVERAGE) > COVERAGE_TOLERANCE:
        status = "WARN_COVERAGE"
        reason = (f"покрытие {coverage:.3f} отклоняется от номинального "
                  f"{INTERVAL_NOMINAL_COVERAGE} больше допуска {COVERAGE_TOLERANCE}; "
                  "интервалы не считаются калиброванными")
    else:
        status = "OK"
        reason = None
    return {
        "status": status,
        "reason": reason,
        "n_quality_days": len(recs),
        "n_total_records": len(all_records),
        "mae_recent_g": mae_recent,
        "recent_k": len(recent),
        "interval_coverage": coverage,
        "nominal_coverage": INTERVAL_NOMINAL_COVERAGE,
        "drift": drift,
        "cost_delta_vs_actual_mean": mean(cost_deltas) if cost_deltas else None,
        "n_cost_counterfactual_days": len(cost_deltas),
        "note": "мониторинг информативен: DRIFT_DETECTED автоматически отключает "
                "предложения (propose_adjustment); cost_delta — SIMULATED-контрфакты, "
                "не измеренная экономия",
    }


def nutrition_alerts(store, school_id: str, *,
                     uneaten_threshold_pct: float = 30.0,
                     meal_type: str = "LUNCH") -> list[dict]:
    """Алерты по питанию на основе метрик (этап: план P0-4).

    Порог 30% — из нацстандарта РК 2025: если более 30% блюд остаются
    нетронутыми, меню пересматривается (zakon.kz 6490135; класс NORM_KZ).
    Порог конфигурируем. Нет данных → нет алерта, но и нет молчаливого OK:
    возвращается элемент с status=UNAVAILABLE и причиной.
    """
    from .metrics import build_metric_report
    sessions = [s for s in store.fetch_sessions(school_id=school_id)
                if s.meal_type == meal_type]
    observations = [o for o in store.fetch_observations(school_id=school_id)
                    if o.session_id in {s.session_id for s in sessions}]
    rep = build_metric_report(sessions, observations)
    alerts: list[dict] = []
    ui = next((m for m in rep["metrics"] if m["name"] == "UneatenIndex"), None)
    if ui is None:
        alerts.append({"code": "UNEATEN_INDEX_UNAVAILABLE", "severity": "INFO",
                       "status": "UNAVAILABLE",
                       "reason": "метрика UneatenIndex не построена"})
        return alerts
    if ui["status"] != "OK":
        alerts.append({"code": "UNEATEN_INDEX_UNAVAILABLE", "severity": "INFO",
                       "status": "UNAVAILABLE", "reason": ui.get("reason") or ui["status"],
                       "note": "нет данных — нет числа; причина вместо молчания"})
        return alerts
    value = ui["value"]
    if value > uneaten_threshold_pct:
        alerts.append({
            "code": "UNEATEN_INDEX_HIGH", "severity": "WARN",
            "status": "OK", "value_pct": value,
            "threshold_pct": uneaten_threshold_pct,
            "note": ("превышен порог нацстандарта РК: при >30% нетронутых блюд "
                     "меню подлежит пересмотру; решение — за бракеражной "
                     "комиссией школы, не за системой"),
        })
    else:
        alerts.append({"code": "UNEATEN_INDEX_OK", "severity": "INFO",
                       "status": "OK", "value_pct": value,
                       "threshold_pct": uneaten_threshold_pct})
    return alerts
