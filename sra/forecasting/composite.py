"""Композитный прогноз дневной массы: едоки × грамм/едока, vs прямой прогноз.

Идея (важна для КЗ): посещаемость наблюдается автоматически («Социальный
кошелёк»), поэтому спрос можно прогнозировать ДЕКОМПОЗИЦИЕЙ:
  масса = едоки × грамм/едока.
Вопрос «лучше ли это прямого прогноза массы» НЕ постулируется — измеряется
на данных: MAE, конформальное покрытие q80 и СТОИМОСТЬ РЕШЕНИЯ (новостный
формула кухни: P* = квантиль τ = c_s/(c_w+c_s) распределения спроса;
cost = c_w·max(P−y,0)+c_s·max(y−P,0)). Оценка по стоимости решения —
первый шаг к decision-focused learning (P3): модели сравниваются по тому,
сколько они стоят кухне, а не только по MAE.

Оценка честная: rolling-origin окна, причинные правила (те же, что в diners),
конформальные интервалы по калибровочному хвосту. На FI-датасете проверяется
на реальном ряде отходов (PW); ряд «выданная масса» проверяется на данных
школы (нет — честный DATA_NOT_READY для этого поля).
"""

from __future__ import annotations

from dataclasses import dataclass

from ..errors import ValidationError
from .diners import _quantile, default_menu_fn

MIN_TRAIN = 10
MIN_CALIB = 8
N_ORIGINS = 3
RULES = ("ma5", "weekday3", "menu_hist")
CHAIN = {"ma5": ("ma5", "weekday3"),
         "weekday3": ("weekday3", "ma5"),
         "menu_hist": ("menu_hist", "weekday3", "ma5")}


@dataclass
class DayPoint:
    date: object
    weekday: int
    menu: str | None
    mass: float
    diners: float


def build_daily_points(sessions, observations, *, field: str = "plate_waste_g",
                       menu_fn=None) -> list[DayPoint]:
    """Дневной ряд: масса поля по сессии + едоки + меню; пропуск≠0."""
    if field not in ("plate_waste_g", "served_mass_g"):
        raise ValidationError(f"field: допустимо plate_waste_g|served_mass_g, "
                              f"получено {field!r}", code="E_BAD_FIELD")
    menu_fn = menu_fn or default_menu_fn
    mass_by_sid: dict[str, float] = {}
    for o in observations:
        v = getattr(o, field)
        if v is not None:
            mass_by_sid[o.session_id] = mass_by_sid.get(o.session_id, 0.0) + v
    pts = []
    for s in sessions:
        mass = mass_by_sid.get(s.session_id)
        diners = s.attendance_count if s.attendance_count is not None else s.meals_served
        if not mass or not diners or diners <= 0:
            continue  # точка непригодна для обоих подходов (честно пропускается)
        pts.append(DayPoint(date=s.service_date, weekday=s.service_date.weekday(),
                            menu=menu_fn(s), mass=float(mass), diners=float(diners)))
    pts.sort(key=lambda p: p.date)
    return pts


def _rule_preds(series: list[dict]) -> list[dict]:
    """Причинные прогнозы правилами: предсказание строго из прошлого."""
    seen: list[float] = []
    wd_map: dict[int, list[float]] = {}
    menu_map: dict[str, list[float]] = {}
    out = []
    for x in series:
        p = {}
        p["ma5"] = (sum(seen[-5:]) / min(5, len(seen))) if seen else None
        wl = wd_map.get(x["weekday"], [])
        p["weekday3"] = (sum(wl[-3:]) / min(3, len(wl))) if wl else None
        mh = menu_map.get(x["menu"]) if x["menu"] is not None else None
        p["menu_hist"] = (sum(mh) / len(mh)) if mh else None
        out.append({"preds": p, "y": x["y"]})
        seen.append(x["y"])
        wd_map.setdefault(x["weekday"], []).append(x["y"])
        if x["menu"] is not None:
            menu_map.setdefault(x["menu"], []).append(x["y"])
    return out


def _chain(preds: dict, rule: str):
    for r in CHAIN[rule]:
        if preds.get(r) is not None:
            return preds[r]
    return None


def _origin_evals(points: list[DayPoint], *, n_train: int, calib_days: int,
                  tau: float, c_w: float, c_s: float) -> dict:
    """Одно окно: калибровка → тест для всех моделей. Возвращает по модели
    {mae, dec_cost, coverage, n_test, n_skipped}."""
    # три ряда: масса (прямой), едоки и г/едока (композит)
    direct = _rule_preds([{"weekday": p.weekday, "menu": p.menu, "y": p.mass}
                          for p in points])
    comp_d = _rule_preds([{"weekday": p.weekday, "menu": p.menu, "y": p.diners}
                          for p in points])
    comp_g = _rule_preds([{"weekday": p.weekday, "menu": p.menu,
                           "y": p.mass / p.diners} for p in points])

    calib_idx = range(n_train - calib_days, n_train)
    test_idx = range(n_train, len(points))
    out = {}
    for rule in RULES:
        for model in (f"direct_{rule}", f"composite_{rule}"):
            def raw_pred(i):
                if model.startswith("direct_"):
                    return _chain(direct[i]["preds"], rule)
                d = _chain(comp_d[i]["preds"], rule)
                g = _chain(comp_g[i]["preds"], rule)
                return None if (d is None or g is None) else d * g
            cal_res, cal_signed = [], []
            for i in calib_idx:
                p = raw_pred(i)
                if p is not None:
                    cal_res.append(abs(points[i].mass - p))
                    cal_signed.append(points[i].mass - p)
            if len(cal_res) < MIN_CALIB:
                out[model] = {"status": "UNAVAILABLE",
                              "reason": f"калибровочных точек < {MIN_CALIB}"}
                continue
            q80 = _quantile(cal_res, 0.8)
            q_tau = _quantile(cal_signed, tau)
            errs, covered, skipped, costs = [], 0, 0, []
            for i in test_idx:
                p = raw_pred(i)
                if p is None:
                    skipped += 1
                    continue
                y = points[i].mass
                errs.append(abs(y - p))
                covered += 1 if abs(y - p) <= q80 else 0
                plan = p + q_tau
                costs.append((c_w * max(plan - y, 0.0)
                              + c_s * max(y - plan, 0.0)) / 1000.0)
            if not errs:
                out[model] = {"status": "UNAVAILABLE",
                              "reason": "нет предсказуемых тестовых точек"}
                continue
            out[model] = {"status": "OK", "mae": sum(errs) / len(errs),
                          "dec_cost_kzt": sum(costs) / len(costs),
                          "coverage": covered / len(errs), "q80": q80,
                          "n_test": len(list(test_idx)),
                          "n_skipped": skipped}
    return out


def backtest_composite(sessions, observations, *, field: str = "plate_waste_g",
                       test_days: int = 20, calib_days: int = 20,
                       n_origins: int = N_ORIGINS, c_w: float = 500.0,
                       c_s: float = 5000.0, menu_fn=None) -> dict:
    """Сравнение композита и прямого прогноза: MAE + стоимость решения."""
    tau = c_s / (c_w + c_s)  # новостный критический квантиль
    pts = build_daily_points(sessions, observations, field=field, menu_fn=menu_fn)
    min_needed = test_days + MIN_TRAIN + calib_days
    if len(pts) < min_needed:
        return {"status": "DATA_NOT_READY",
                "reason": (f"нужно >= {min_needed} дней с массой и едоками; есть "
                           f"{len(pts)}; поле: {field}"),
                "n_points": len(pts)}
    n = len(pts)
    origins = []
    for k in range(n_origins):
        nt = n - test_days - k * test_days
        if nt - calib_days >= MIN_TRAIN:
            origins.append(nt)
    per_model: dict[str, list[dict]] = {}
    for nt in origins:
        res = _origin_evals(pts, n_train=nt, calib_days=calib_days,
                            tau=tau, c_w=c_w, c_s=c_s)
        for m, r in res.items():
            per_model.setdefault(m, []).append(r)
    models = []
    for m, rs in sorted(per_model.items()):
        ok = [r for r in rs if r["status"] == "OK"]
        if not ok:
            models.append({"name": m, "status": "UNAVAILABLE",
                           "reason": rs[-1].get("reason", "нет окон")})
            continue
        models.append({
            "name": m, "status": "OK",
            "mae_mean": sum(r["mae"] for r in ok) / len(ok),
            "dec_cost_kzt_mean": sum(r["dec_cost_kzt"] for r in ok) / len(ok),
            "coverage_mean": sum(r["coverage"] for r in ok) / len(ok),
            "n_windows": len(ok), "n_test_per_window": ok[0]["n_test"],
            "n_skipped_total": sum(r.get("n_skipped", 0) for r in ok),
        })
    ok_models = [m for m in models if m["status"] == "OK"]
    best_mae = min(ok_models, key=lambda x: x["mae_mean"])["name"] if ok_models else None
    best_cost = (min(ok_models, key=lambda x: x["dec_cost_kzt_mean"])["name"]
                 if ok_models else None)
    comp_best = min((m for m in ok_models if m["name"].startswith("composite")),
                    key=lambda x: x["mae_mean"], default=None)
    dir_best = min((m for m in ok_models if m["name"].startswith("direct")),
                   key=lambda x: x["mae_mean"], default=None)
    verdict = None
    if comp_best and dir_best:
        denom = dir_best["mae_mean"]
        diff_pct = (0.0 if denom <= 1e-12 else
                    (comp_best["mae_mean"] - denom) / denom * 100.0)
        verdict = {"composite_best": comp_best["name"], "direct_best": dir_best["name"],
                   "composite_vs_direct_mae_pct": diff_pct,
                   "note": ("декомпозиция лучше" if diff_pct < 0
                            else "прямой прогноз лучше" if diff_pct > 0 else "равны")}
    return {"status": "OK" if ok_models else "DATA_NOT_READY",
            "field": field,
            "n_points": n, "n_origins": len(origins),
            "test_days": test_days, "calib_days": calib_days,
            "first_test_date": pts[origins[-1]].date.isoformat(),
            "last_test_date": pts[-1].date.isoformat(),
            "decision_params": {"c_w": c_w, "c_s": c_s, "tau_newsvendor": tau,
                                "note": "P* = прогноз + квантиль τ калибровочных "
                                        "остатков; cost в тыс. KZT_DEMO/день"},
            "models": models,
            "best_by_mae": best_mae,
            "best_by_decision_cost": best_cost,
            "verdict": verdict,
            "notes": [
                "все правила причинные; интервалы — конформальные q80 по "
                "калибровочному хвосту",
                "оценка по стоимости решения (новостная формула) — модели "
                "сравниваются по расходам кухни, а не только по MAE; это "
                "оценочная (не обучающая) форма decision-focused подхода",
                "выводы на OPEN-данных не переносятся на школу КЗ автоматически",
            ]}


