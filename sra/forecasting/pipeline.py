"""Прогнозное состояние на момент решения (для оптимизатора и сравнений).

Отличие от backtest'а: возвращает прогноз + калибровочные остатки + диагностику
дрейфа для ОДНОЙ целевой даты, всё строго из as-of среза на момент решения
(D−1, школьный часовой пояс). Используется оптимизатором этапа 4 и
сравнением стратегий; сам backtest этапа 3 не трогается (он протестирован).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .. import enums
from .baselines import build_model
from .drift import detect_drift
from .series import eligible_points

MIN_RESIDUALS_FOR_INTERVAL = 3


def decision_moment_iso(service_date, tz_name: str, hour: int) -> str:
    """Момент решения: сутки назад от даты обслуживания в школьном поясе."""
    tz = ZoneInfo(tz_name)
    dm = (datetime(service_date.year, service_date.month, service_date.day,
                   hour, 0, tzinfo=tz) - timedelta(days=1))
    return dm.isoformat()


def decision_forecast(snapshot_fn, target_date, *, model_name: str = "seasonal_naive",
                      calib_days: int = 8, seasonal_weeks: int = 3,
                      moving_avg_window: int = 4, decision_hour: int = 18,
                      tz_name: str = "Asia/Almaty",
                      exclude_censored: bool = True) -> dict:
    """Прогноз целевой даты только из данных, доступных на момент решения.

    Возвращает статус OK | COLD_START, точечный прогноз, хронологически
    упорядоченные калибровочные остатки (y − pred) и эвристическую диагностику
    дрейфа (ранние против последних остатков калибровки).
    """
    model = build_model(model_name, seasonal_weeks=seasonal_weeks,
                        moving_avg_window=moving_avg_window)
    cutoff_iso = decision_moment_iso(target_date, tz_name, decision_hour)
    snap = snapshot_fn(cutoff_iso)
    ordered = [p for p in eligible_points(snap.points, exclude_censored=exclude_censored)
               if p.service_date < target_date]
    ordered.sort(key=lambda p: p.service_date)

    # Калибровочные остатки walk-forward: каждый день калибруется своей историей.
    residuals: list[float] = []
    skipped = 0
    for cp in ordered[-calib_days:]:
        hist = [p for p in ordered if p.service_date < cp.service_date]
        if not hist:
            skipped += 1
            continue
        model.fit(hist)
        pred = model.predict(cp.service_date)
        if pred is None or cp.served_mass_g is None:
            skipped += 1
            continue
        residuals.append(cp.served_mass_g - pred)

    # Точечный прогноз целевой даты.
    model.fit(ordered)
    pred = model.predict(target_date)
    if pred is None:
        return {"status": enums.FORECAST_COLD_START, "target_date": target_date.isoformat(),
                "cutoff_iso": cutoff_iso, "model_name": model_name,
                "model_params": model.params, "pred_g": None,
                "residuals_g": residuals, "n_calib_used": len(residuals),
                "n_calib_skipped": skipped, "n_history": len(ordered),
                "drift": {"status": enums.DRIFT_INSUFFICIENT,
                          "note": "прогноз недоступен, дрейф не оценивался"}}

    # Дрейф: ранние остатки против последних k.
    if len(residuals) >= 6:
        k = max(2, len(residuals) // 3)
        drift = detect_drift(residuals[:-k], residuals[-k:], k=k)
    else:
        drift = {"status": enums.DRIFT_INSUFFICIENT,
                 "note": f"мало калибровочных остатков ({len(residuals)})"}

    return {"status": enums.FORECAST_OK, "target_date": target_date.isoformat(),
            "cutoff_iso": cutoff_iso, "model_name": model_name,
            "model_params": model.params, "pred_g": pred,
            "residuals_g": residuals, "n_calib_used": len(residuals),
            "n_calib_skipped": skipped, "n_history": len(ordered),
            "exclude_censored": exclude_censored, "drift": drift}
