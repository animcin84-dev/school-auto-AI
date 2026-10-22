"""Сравнение стратегий управления объёмом на тестовом окне (SIMULATED).

Вопрос (раздел 8 задания): оценить predict-then-optimize относительно простых
правил ПЕРЕД усложнением. Стратегии:
  - no_change            — готовить как обычно (базовая линия P0, as-of);
  - fixed_quantile_rule  — простое правило: готовить pred + q90(остатков);
  - optimizer            — predict-then-optimize: минимизация ожидаемой стоимости
                           при жёстких ограничениях политики (этап 4);
  - kitchen_actual       — СПРАВОЧНО (hindsight): фактическая масса приготовления
                           той же сессии; не стратегия, а «как было на самом деле».

Все стратегии оцениваются на одних днях тестового окна по фактической выданной
массе y: отходы max(P−y,0), нехватка max(y−P,0), стоимость из той же политики.
Прогноз строится только из данных, доступных на момент решения. Результат —
SIMULATED: это сценарная оценка качества решений, не измеренная экономия.
"""

from __future__ import annotations

from .. import enums
from ..forecasting.pipeline import decision_forecast
from ..forecasting.score import empirical_quantile
from ..forecasting.series import make_snapshot_fn
from .core import (DemandScenarios, habitual_baseline, scenario_cost_deltas,
                   solve_prepared_volume)

MIN_RESIDUALS = 3


def _cost(p: float, y: float, cw: float, cs: float) -> float:
    return (max(p - y, 0.0) * cw + max(y - p, 0.0) * cs) / 1000.0


def compare_strategies(store, *, school_id: str, meal_type: str, test_days: int = 10,
                       calib_days: int = 8, baseline_days: int = 8,
                       decision_hour: int = 18, model_name: str = "seasonal_naive",
                       seasonal_weeks: int = 3, cost_waste_per_kg: float,
                       cost_shortage_per_kg: float, shortage_risk_max: float = 0.10,
                       delta_max_pct: float = 0.15, grid_step_g: float = 250.0) -> dict:
    """Прогоняет стратегии по последним `test_days` точкам ряда (хронологически)."""
    snapshot_fn = make_snapshot_fn(store, school_id, meal_type)
    full = snapshot_fn("9999-12-31T23:59:59+00:00")
    ordered = sorted(full.points, key=lambda p: p.service_date)
    if len(ordered) < test_days + 3:
        return {"status": enums.DATA_NOT_READY,
                "reason": f"точек {len(ordered)} меньше, чем нужно для окна ({test_days}+3)",
                "strategies": {}}
    test_pts = ordered[-test_days:]

    rows: dict[str, list[dict]] = {s: [] for s in
                                   ("no_change", "fixed_quantile_rule", "optimizer")}
    skipped = {"COLD_START": 0, "NO_DISTRIBUTION": 0, "NO_BASELINE": 0, "NO_Y": 0}
    optimizer_actions = {"adjust": 0, "keep": 0}

    for tp in test_pts:
        y = tp.served_mass_g
        if y is None:
            skipped["NO_Y"] += 1
            continue
        fc = decision_forecast(snapshot_fn, tp.service_date, model_name=model_name,
                               calib_days=calib_days, decision_hour=decision_hour,
                               seasonal_weeks=seasonal_weeks)
        if fc["status"] != "OK" or fc["pred_g"] is None:
            skipped["COLD_START"] += 1
            continue
        res = fc["residuals_g"]
        if len(res) < MIN_RESIDUALS:
            skipped["NO_DISTRIBUTION"] += 1
            continue
        cut_snap = snapshot_fn(fc["cutoff_iso"])
        hist = [p for p in cut_snap.points if p.service_date < tp.service_date]
        hist.sort(key=lambda p: p.service_date)
        p0 = habitual_baseline(hist, days=baseline_days)
        if p0 is None:
            skipped["NO_BASELINE"] += 1
            continue

        scenarios = DemandScenarios.build(fc["pred_g"], res)
        q90 = empirical_quantile(res, 0.9)
        p_fixed = fc["pred_g"] + q90
        solve = solve_prepared_volume(
            baseline_g=p0, scenarios=scenarios,
            cost_waste_per_kg=cost_waste_per_kg,
            cost_shortage_per_kg=cost_shortage_per_kg,
            shortage_risk_max=shortage_risk_max,
            delta_max_g=delta_max_pct * p0, grid_step_g=grid_step_g)
        p_opt = solve["chosen_g"] if solve["status"] == "OK" else p0
        if solve["status"] == "OK":
            optimizer_actions["adjust"] += 1
        else:
            optimizer_actions["keep"] += 1

        day = {"date": tp.service_date.isoformat(), "y_g": y, "pred_g": fc["pred_g"],
               "baseline_g": p0, "censored": tp.censored}
        rows["no_change"].append({**day, "P_g": p0, "cost": _cost(p0, y, cost_waste_per_kg,
                                                                  cost_shortage_per_kg)})
        rows["fixed_quantile_rule"].append({**day, "P_g": p_fixed,
                                            "cost": _cost(p_fixed, y, cost_waste_per_kg,
                                                          cost_shortage_per_kg)})
        rows["optimizer"].append({**day, "P_g": p_opt,
                                  "cost": _cost(p_opt, y, cost_waste_per_kg,
                                                cost_shortage_per_kg)})

    strategies = {}
    for name, rs in rows.items():
        if not rs:
            strategies[name] = {"n_days": 0, "status": "NO_DAYS_EVALUATED"}
            continue
        strategies[name] = {
            "n_days": len(rs),
            "mean_prepared_g": sum(r["P_g"] for r in rs) / len(rs),
            "mean_waste_g": sum(max(r["P_g"] - r["y_g"], 0.0) for r in rs) / len(rs),
            "mean_shortage_g": sum(max(r["y_g"] - r["P_g"], 0.0) for r in rs) / len(rs),
            "shortfall_day_rate": sum(1 for r in rs if r["y_g"] > r["P_g"]) / len(rs),
            "mean_cost": sum(r["cost"] for r in rs) / len(rs),
            "total_cost": sum(r["cost"] for r in rs),
            "days": rs,
        }

    # Справочно: фактическая практика кухни (hindsight).
    kp = []
    for tp in test_pts:
        if tp.served_mass_g is not None and tp.prepared_mass_g is not None:
            pr = tp.prepared_mass_g
            kp.append(_cost(pr, tp.served_mass_g, cost_waste_per_kg, cost_shortage_per_kg))
    kitchen_actual = ({"n_days": len(kp),
                       "mean_waste_g": (sum(max(tp.prepared_mass_g - tp.served_mass_g, 0.0)
                                            for tp in test_pts
                                            if tp.served_mass_g is not None
                                            and tp.prepared_mass_g is not None) / len(kp)),
                       "total_cost": sum(kp), "hindsight_only": True} if kp else None)

    return {
        "status": "OK",
        "artifact_nature": (enums.SIMULATED if store.environment == enums.ENV_DEMO
                            else enums.PREDICTED),
        "environment": store.environment,
        "school_id": school_id,
        "meal_type": meal_type,
        "window": {"test_days": test_days,
                   "test_range": [test_pts[0].service_date.isoformat(),
                                  test_pts[-1].service_date.isoformat()],
                   "calib_days": calib_days},
        "params": {"forecast_model": model_name, "cost_waste_per_kg": cost_waste_per_kg,
                   "cost_shortage_per_kg": cost_shortage_per_kg,
                   "shortage_risk_max": shortage_risk_max,
                   "delta_max_pct": delta_max_pct, "grid_step_g": grid_step_g},
        "skipped_days": skipped,
        "optimizer_decisions": optimizer_actions,
        "strategies": strategies,
        "kitchen_actual_reference": kitchen_actual,
        "notes": [
            "SIMULATED: сценарная оценка на синтетических/исторических данных, "
            "не измеренная экономия",
            "y = выданная масса; на цензурированных днях занижает полный спрос",
            "стратегии используют только информацию, доступную на момент решения; "
            "kitchen_actual — hindsight-справка",
        ],
    }
