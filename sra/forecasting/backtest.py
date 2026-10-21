"""Хронологический backtest базовых прогнозов (этап 3).

Протокол:
  1. Данные делятся ПО ВРЕМЕНИ: пул обучения → калибровка (calib_days) → тест
     (test_days). Параметры моделей фиксируются вызывающим; подбор на тесте
     запрещён (для этого есть калибровочный период).
  2. Для каждого калибровочного и тестового дня прогноз строится ТОЛЬКО из
     данных с recorded_at ≤ момент решения (D−1, decision_hour, школьный
     часовой пояс) — as-of срез хранилища. Поздние исправления записей не
     утекают в историю.
  3. Интервалы: эмпирические квантили остатков калибровки (walk-forward).
     Никаких conformal-гарантий: предпосылки (обменяемость/стационарность)
     во временных рядах обычно нарушаются; покрытие проверяется на тесте.
  4. Метрики: MAE, pinball loss по квантилям, покрытие/ширина интервала,
     разрез по цензурированным дням, прокси качества последующего решения
     (SIMULATED-эвристика: готовить q_high → отходы/нехватка на тесте).
  5. «Фактическая политика кухни» — hindsight-базовая линия для сравнения
     (prepared той же сессии); это не прогноз.

Статусы:
  - DATA_NOT_READY: истории меньше min_train + calib_days + test_days —
    метрик НЕТ, причин по-честному;
  - COLD_START: день не предсказан конкретной моделью (мало истории);
  - артефакт в DEMO-среде помечен SIMULATED, в REAL — PREDICTED.

Качество на SYNTHETIC-данных показывает работу кода и поведение в сценарии,
а не ожидаемую точность в реальной школе.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .. import enums
from .baselines import build_model
from .drift import detect_drift
from .score import empirical_quantile, mae, mean, pinball_loss
from .series import eligible_points

MIN_INTERVAL_RESIDUALS = 3   # меньше остатков калибровки — интервал недоступен
DEFAULT_QUANTILES = (0.1, 0.5, 0.9)


def decision_moment_iso(service_date, tz_name: str, hour: int) -> str:
    """Момент решения: сутки назад от даты обслуживания в школьном поясе."""
    tz = ZoneInfo(tz_name)
    dm = (datetime(service_date.year, service_date.month, service_date.day,
                   hour, 0, tzinfo=tz) - timedelta(days=1))
    return dm.isoformat()


def check_readiness(points, *, min_train: int, calib_days: int, test_days: int) -> dict:
    """Готовность данных к backtest'у: явный DATA_NOT_READY вместо фиктивной модели."""
    n = len(points)
    need = min_train + calib_days + test_days
    reasons = []
    if n < need:
        reasons.append(
            f"INSUFFICIENT_HISTORY: точек с полным покрытием {n}, требуется {need} "
            f"(min_train={min_train} + calib_days={calib_days} + test_days={test_days})")
    censored = sum(1 for p in points if p.censored)
    if n > 0 and censored == n:
        reasons.append("ALL_CENSORED: все доступные дни цензурированы; спрос "
                       "систематически занижен нехваткой еды")
    dates = [p.service_date.isoformat() for p in points]
    return {
        "status": enums.METRIC_OK if not reasons else enums.DATA_NOT_READY,
        "reasons": reasons,
        "n_points": n,
        "n_required": need,
        "censored_days": censored,
        "date_from": dates[0] if dates else None,
        "date_to": dates[-1] if dates else None,
    }


def _history_for(snapshot_fn, target_date, *, tz_name: str, hour: int,
                 exclude_censored: bool):
    """История, доступная на момент решения для целевого дня: as-of срез +
    фильтр по дате + отбор пригодных точек."""
    cutoff = decision_moment_iso(target_date, tz_name, hour)
    snap = snapshot_fn(cutoff)
    hist = [p for p in eligible_points(snap.points, exclude_censored=exclude_censored)
            if p.service_date < target_date]
    return hist


def _metric_block(rows: list[dict], quantiles) -> dict:
    """Сводные метрики по набору дневных строк (y_g, pred_g, pred_q*)."""
    pairs = [(r["y_g"], r["pred_g"]) for r in rows]
    out = {
        "n_days": len(rows),
        "mae_g": mae(pairs),
        "pinball": {},
        "interval": {"coverage": None, "mean_width_g": None,
                     "quantiles": list(quantiles),
                     "source": "эмпирические квантили остатков калибровки; "
                               "гарантии покрытия НЕ заявляются"},
    }
    for q in quantiles:
        key = f"pred_q{int(round(q * 100))}"
        vals = [pinball_loss(r["y_g"], r[key], q) for r in rows if r.get(key) is not None]
        out["pinball"][str(q)] = mean(vals)
    cov_rows = [r for r in rows
                if r.get("pred_q10") is not None and r.get("pred_q90") is not None]
    if cov_rows:
        cov = sum(1 for r in cov_rows if r["pred_q10"] <= r["y_g"] <= r["pred_q90"])
        out["interval"]["coverage"] = cov / len(cov_rows)
        out["interval"]["mean_width_g"] = mean(
            [r["pred_q90"] - r["pred_q10"] for r in cov_rows])
        out["interval"]["n_days"] = len(cov_rows)
    return out


def run_backtest(*, snapshot_fn, test_days: int = 10, calib_days: int = 8,
                 min_train: int = 10, decision_hour: int = 18,
                 tz_name: str = "Asia/Almaty", seasonal_weeks: int = 3,
                 moving_avg_window: int = 4, exclude_censored: bool = True,
                 quantiles=DEFAULT_QUANTILES, decision_quantile: float = 0.9,
                 environment: str = enums.ENV_REAL,
                 model_names: list[str] | None = None) -> dict:
    """Запускает хронологический backtest. Возвращает отчёт (dict, JSON-совместимый)."""
    full = snapshot_fn("9999-12-31T23:59:59+00:00")
    points = full.points
    readiness = check_readiness(points, min_train=min_train,
                                calib_days=calib_days, test_days=test_days)
    base = {
        "artifact_nature": (enums.SIMULATED if environment == enums.ENV_DEMO
                            else enums.PREDICTED),
        "environment": environment,
        "school_id": full.school_id,
        "meal_type": full.meal_type,
        "target": {
            "definition": "served_mass_g — суммарная масса выданной еды за сессию "
                          "(агрегат по блюдам; наблюдаемая величина)",
            "unit": "g",
            "horizon": "1 приём пищи",
            "decision_moment": f"D-1 {decision_hour}:00 {tz_name} "
                               "(история строго с recorded_at ≤ момента решения)",
            "censoring_note": "на цензурированных днях served занижает полный спрос; "
                              "точность прогноза выдачи НЕ доказывает прогноз спроса",
        },
        "status": readiness["status"],
        "readiness": readiness,
    }
    if readiness["status"] != enums.METRIC_OK:
        base.update({
            "models": [],
            "kitchen_policy": None,
            "notes": ["данных недостаточно: метрики не считались, фиктивной модели нет",
                      *(f"причина: {r}" for r in readiness["reasons"])],
        })
        return base

    ordered = sorted(points, key=lambda p: p.service_date)
    test_pts = ordered[-test_days:]
    calib_pts = ordered[-test_days - calib_days:-test_days]
    train_count = len(ordered) - test_days - calib_days
    censored_all = sum(1 for p in ordered if p.censored)

    names = model_names or ["naive", "seasonal_naive", "moving_average"]
    models = {}
    for name in names:
        try:
            models[name] = build_model(name, seasonal_weeks=seasonal_weeks,
                                       moving_avg_window=moving_avg_window)
        except KeyError as e:
            raise KeyError(str(e)) from e

    # --- Калибровка: walk-forward остатки (каждый день калибруется своей историей) ---
    residuals: dict[str, list[float]] = {n: [] for n in models}
    calib_cold: dict[str, int] = {n: 0 for n in models}
    for cp in calib_pts:
        hist = _history_for(snapshot_fn, cp.service_date, tz_name=tz_name,
                            hour=decision_hour, exclude_censored=exclude_censored)
        for name, model in models.items():
            model.fit(hist)
            pred = model.predict(cp.service_date)
            if pred is None:
                calib_cold[name] += 1
                continue
            residuals[name].append(cp.served_mass_g - pred)

    # --- Тест: дни, доступные истории, поквантильные прогнозы ---
    per_model: list[dict] = []
    test_residuals: dict[str, list[float]] = {}
    for name, model in models.items():
        res = residuals[name]
        qmap = {q: (empirical_quantile(res, q)
                    if len(res) >= MIN_INTERVAL_RESIDUALS else None)
                for q in quantiles}
        day_rows: list[dict] = []
        cold = 0
        for tp in test_pts:
            hist = _history_for(snapshot_fn, tp.service_date, tz_name=tz_name,
                                hour=decision_hour, exclude_censored=exclude_censored)
            model.fit(hist)
            pred = model.predict(tp.service_date)
            row = {"date": tp.service_date.isoformat(), "y_g": tp.served_mass_g,
                   "censored": tp.censored}
            if pred is None:
                row["status"] = enums.FORECAST_COLD_START
                cold += 1
                day_rows.append(row)
                continue
            row["status"] = enums.FORECAST_OK
            row["pred_g"] = pred
            for q in quantiles:
                key = f"pred_q{int(round(q * 100))}"
                row[key] = (pred + qmap[q]) if qmap[q] is not None else None
            day_rows.append(row)
        ok_rows = [r for r in day_rows
                   if r.get("status") == enums.FORECAST_OK and r["y_g"] is not None]
        test_res = [r["y_g"] - r["pred_g"] for r in ok_rows]
        test_residuals[name] = test_res
        block = _metric_block(ok_rows, quantiles)
        nc = [r for r in ok_rows if not r["censored"]]
        cen = [r for r in ok_rows if r["censored"]]
        block["by_censoring"] = {
            "non_censored": {"n_days": len(nc), "mae_g": mae([(r["y_g"], r["pred_g"]) for r in nc])},
            "censored": {"n_days": len(cen), "mae_g": mae([(r["y_g"], r["pred_g"]) for r in cen]),
                         "note": "y занижен нехваткой; метрика смещена"},
        }
        # Прокси последующего решения: готовить pred_q(decision_quantile).
        dq_key = f"pred_q{int(round(decision_quantile * 100))}"
        proxy_rows = [r for r in ok_rows if r.get(dq_key) is not None]
        if proxy_rows:
            waste = [max(r[dq_key] - r["y_g"], 0.0) for r in proxy_rows]
            shortfall = [1.0 if r["y_g"] > r[dq_key] else 0.0 for r in proxy_rows]
            block["decision_proxy"] = {
                "rule": f"готовить {dq_key} (SIMULATED-эвристика, не этап 4)",
                "prepared_quantile": decision_quantile,
                "n_days": len(proxy_rows),
                "waste_proxy_mean_g": mean(waste),
                "shortfall_rate": mean(shortfall),
            }
        else:
            block["decision_proxy"] = {"rule": f"готовить {dq_key}", "status": "UNAVAILABLE",
                                       "reason": "интервал недоступен (мало остатков калибровки)"}
        interval_ok = len(res) >= MIN_INTERVAL_RESIDUALS
        per_model.append({
            "name": name,
            "params": model.params,
            "status": enums.FORECAST_OK if ok_rows else enums.DATA_NOT_READY,
            "n_calib_residuals": len(res),
            "n_calib_cold_start": calib_cold[name],
            "interval_status": (enums.FORECAST_OK if interval_ok else "INSUFFICIENT_CALIBRATION"),
            "n_test_days": len(day_rows),
            "n_cold_start": cold,
            **block,
            "drift": detect_drift(res, test_res, k=min(5, len(test_res)) if test_res else 5),
            "days": day_rows,
        })

    # --- Базовая линия: фактическая политика кухни (hindsight, не прогноз) ---
    kp_rows = []
    for tp in test_pts:
        if tp.prepared_mass_g is None or tp.served_mass_g is None:
            continue
        prepared = tp.prepared_mass_g
        kp_rows.append({
            "date": tp.service_date.isoformat(),
            "prepared_g": prepared,
            "y_g": tp.served_mass_g,
            "overproduction_proxy_g": max(prepared - tp.served_mass_g, 0.0),
            "shortfall_vs_served": tp.served_mass_g > prepared,
            "abs_err_g": abs(prepared - tp.served_mass_g),
        })
    kitchen_policy = {
        "name": "kitchen_policy_prepared",
        "hindsight_only": True,
        "note": "НЕ прогноз: фактическая prepared_mass сессии-цели; базовая линия "
                "«как кухня реально готовила» для сравнения",
        "n_days": len(kp_rows),
        "mae_implicit_g": mae([(r["y_g"], r["prepared_g"]) for r in kp_rows]),
        "overproduction_proxy_mean_g": mean([r["overproduction_proxy_g"] for r in kp_rows]),
        "shortfall_rate": (mean([1.0 if r["shortfall_vs_served"] else 0.0
                                 for r in kp_rows]) if kp_rows else None),
    }

    return {
        **base,
        "windows": {
            "train_points": train_count,
            "calib_days": calib_days,
            "test_days": test_days,
            "calib_range": [calib_pts[0].service_date.isoformat(),
                            calib_pts[-1].service_date.isoformat()],
            "test_range": [test_pts[0].service_date.isoformat(),
                           test_pts[-1].service_date.isoformat()],
            "train_rule": "расширяющееся окно: каждый день калибруется/предсказывается "
                          "только своей прошлой историей",
        },
        "data": {
            "n_points": len(ordered),
            "date_from": ordered[0].service_date.isoformat(),
            "date_to": ordered[-1].service_date.isoformat(),
            "censored_days": censored_all,
            "censored_unknown_days": sum(1 for p in ordered if p.censored_unknown),
            "exclude_censored_from_train": exclude_censored,
            "excluded_partial_days": full.excluded,
        },
        "models": per_model,
        "kitchen_policy": kitchen_policy,
        "notes": [
            "параметры моделей фиксированы до теста; подбор по финальному тесту запрещён",
            "интервалы — эмпирические квантили остатков калибровки; покрытие проверяется "
            "на тесте, conformal-гарантии не заявляются",
            "прокси решения — SIMULATED-эвристика; этап 4 (оптимизатор) заменит её",
            "качество на синтетических данных — проверка кода, не точность в школе",
        ],
    }
