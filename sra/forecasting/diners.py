"""Прогноз числа едоков (посещаемости) — отдельный трек с ML-кандидатами.

Зачем отдельный трек: спрос = едоки × грамм/едока. Посещаемость стабильнее
граммов и в КЗ наблюдается автоматически («Социальный кошелёк» фиксирует
факты выдачи); литература называет её главным предиктором для столовых.

Правила честности (не нарушаются никакой моделью):
  - признаки строятся ПРИЧИННО (только прошлое относительно точки);
  - оценка — rolling-origin backtest: несколько несмещённых окон
    (train → калибровка → тест), агрегат по окнам, детали в отчёте;
  - интервалы — split-conformal: статичный q80 по хвосту train, скользящее
    окно остатков (каузально, обновляется по ходу теста) и ACI (адаптивная
    α: ошибка покрытия → интервал расширяется); все три показываются рядом,
    выбор стратегии — по измеренным coverage/width;
  - кандидат сравнивается с действующими базовыми в ОДНОМ отчёте;
  - отсутствующий пакет → статус UNAVAILABLE с причиной, не ошибка.

Модели: moving_average5, weekday3, menu_history (действующие правила);
ridge (закрытая форма, stdlib); sklearn_gbdt (HistGradientBoosting,
scripts/setup-ml.sh; без пакета — UNAVAILABLE с причиной).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .. import enums

_MENU_RE = re.compile(r"MENU=(\d+)")
MIN_TRAIN_ROWS = 10
MIN_CALIB = 5
N_ORIGINS = 3
ROLLING_WINDOW = 20
ACI_GAMMA = 0.05
ACI_ALPHA = 0.20


def default_menu_fn(session):
    m = _MENU_RE.search(getattr(session, "notes", "") or "")
    return m.group(1) if m else None


@dataclass
class DinersPoint:
    session_id: str
    date: object
    weekday: int
    menu: str | None
    y: float


def build_diners_series(sessions, *, menu_fn=None) -> list[DinersPoint]:
    """Точки ряда едоков: attendance_count, иначе meals_served; пропуск≠0."""
    menu_fn = menu_fn or default_menu_fn
    pts = []
    for s in sessions:
        y = s.attendance_count if s.attendance_count is not None else s.meals_served
        if y is None or y <= 0:
            continue
        menu = getattr(s, "menu_id", None) or menu_fn(s)  # поле; notes — fallback
        pts.append(DinersPoint(session_id=s.session_id, date=s.service_date,
                               weekday=s.service_date.weekday(),
                               menu=menu, y=float(y)))
    pts.sort(key=lambda p: p.date)
    return pts


def _causal_features(pts: list[DinersPoint]) -> list[dict]:
    """Признаки только из прошлого (строго до текущей точки)."""
    menu_map: dict[str, list[float]] = {}
    wd_map: dict[int, list[float]] = {}
    din_seen: list[float] = []
    out = []
    for i, p in enumerate(pts):
        mh = menu_map.get(p.menu) if p.menu is not None else None
        f_menu = sum(mh) / len(mh) if mh else None
        wl = wd_map.get(p.weekday, [])
        f_wd = sum(wl[-3:]) / min(3, len(wl)) if wl else None
        f_lag = din_seen[-1] if din_seen else None
        f_ma5 = sum(din_seen[-5:]) / min(5, len(din_seen)) if din_seen else None
        out.append({"i": i, "f_menu": f_menu, "f_wd": f_wd, "f_lag": f_lag,
                    "f_ma5": f_ma5, "trend": float(i), "y": p.y})
        if p.menu is not None:
            menu_map.setdefault(p.menu, []).append(p.y)
        wd_map.setdefault(p.weekday, []).append(p.y)
        din_seen.append(p.y)
    return out


def _quantile(xs: list[float], q: float) -> float:
    xs = sorted(xs)
    if not xs:
        return float("nan")
    k = q * (len(xs) - 1)
    lo, hi = int(k), min(int(k) + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


class _RidgeModel:
    """L2-линейная регрессия со стандартизацией (закрытая форма, stdlib).

    Перехват НЕ стандартизуется и не регуляризуется (иначе столбец констант
    вырождается): признаки стандартизуются, затем добавляется столбец единиц.
    """

    kind = "ridge"

    def __init__(self, lam: float = 3.0, use_trend: bool = True):
        self.lam = lam
        self.use_trend = use_trend
        self.weights = None
        self.mu = None
        self.sd = None

    def _vec(self, f: dict) -> list[float]:
        base = [f["f_menu"], f["f_wd"], f["f_lag"], f["f_ma5"]]
        return base + ([f["trend"]] if self.use_trend else [])

    def fit(self, train_feats: list[dict]):
        X = [self._vec(f) for f in train_feats]
        y = [f["y"] for f in train_feats]
        k = len(X[0]) + 1  # + перехват
        self.mu = [sum(col) / len(col) for col in zip(*X)]
        self.sd = []
        for col_idx in range(len(X[0])):
            diffs = [row[col_idx] - self.mu[col_idx] for row in X]
            var = sum(d * d for d in diffs) / max(1, len(diffs) - 1)
            self.sd.append(var ** 0.5 or 1.0)
        Xs = [[1.0] + [(row[c] - self.mu[c]) / self.sd[c]
                       for c in range(len(X[0]))] for row in X]
        A = [[sum(r[a] * r[b] for r in Xs) + (self.lam if a == b and a else 0.0)
              for b in range(k)] for a in range(k)]
        b = [sum(r[a] * yy for r, yy in zip(Xs, y)) for a in range(k)]
        for c in range(k):
            piv = max(range(c, k), key=lambda r: abs(A[r][c]))
            A[c], A[piv] = A[piv], A[c]
            b[c], b[piv] = b[piv], b[c]
            for r in range(c + 1, k):
                f_ = A[r][c] / A[c][c]
                for cc in range(c, k):
                    A[r][cc] -= f_ * A[c][cc]
                b[r] -= f_ * b[c]
        w = [0.0] * k
        for r in range(k - 1, -1, -1):
            w[r] = (b[r] - sum(A[r][c] * w[c] for c in range(r + 1, k))) / A[r][r]
        self.weights = w
        return self

    def predict(self, f: dict) -> float:
        row = [1.0] + [(v - m) / s
                       for v, m, s in zip(self._vec(f), self.mu, self.sd)]
        return sum(a * b for a, b in zip(self.weights, row))

    def params(self) -> dict:
        feats = ["menu_hist", "weekday_hist", "lag1", "ma5"] + (
            ["trend"] if self.use_trend else [])
        return {"lam": self.lam, "features": feats,
                "standardized": True, "intercept": "not regularized"}


class _SklearnGBDT:
    kind = "sklearn_gbdt"

    def __init__(self, max_iter: int = 200, max_depth: int = 3,
                 learning_rate: float = 0.05, random_state: int = 42):
        self.params_d = {"max_iter": max_iter, "max_depth": max_depth,
                         "learning_rate": learning_rate,
                         "random_state": random_state}
        self.model = None

    def _vec(self, f: dict) -> list[float]:
        return [f["f_menu"], f["f_wd"], f["f_lag"], f["f_ma5"], f["trend"]]

    def fit(self, train_feats: list[dict]):
        from sklearn.ensemble import HistGradientBoostingRegressor
        self.model = HistGradientBoostingRegressor(**self.params_d)
        self.model.fit([self._vec(f) for f in train_feats],
                       [f["y"] for f in train_feats])
        return self

    def predict(self, f: dict) -> float:
        return float(self.model.predict([self._vec(f)])[0])

    def params(self) -> dict:
        return dict(self.params_d)


def _baseline_pred(name: str, f: dict):
    if name.startswith("moving_average"):
        return f["f_ma5"]
    if name.startswith("weekday"):
        return f["f_wd"]
    if name.startswith("menu_history"):
        return f["f_menu"] if f["f_menu"] is not None else f["f_wd"]
    return None


def _interval_report_full(q_calib: float, calib_res: list[float],
                          pred_errs: list[tuple[float, float]]) -> dict:
    """Полный вариант: static / rolling / ACI (α адаптируется по покрытию)."""
    n = len(pred_errs)
    cov_s = sum(1 for p, y in pred_errs if abs(p - y) <= q_calib) / n
    static = {"coverage": cov_s, "mean_width": 2 * q_calib,
              "note": "q80 по хвосту train, фиксирован"}
    pool: list[float] = []
    covered_r, widths_r = 0, []
    for p, y in pred_errs:
        q = _quantile(pool, 0.8) if len(pool) >= 5 else q_calib
        e = abs(p - y)
        covered_r += 1 if e <= q else 0
        widths_r.append(2 * q)
        pool.append(e)
        if len(pool) > ROLLING_WINDOW:
            pool.pop(0)
    rolling = {"coverage": covered_r / n, "mean_width": sum(widths_r) / n,
               "window": ROLLING_WINDOW}
    alpha = ACI_ALPHA
    covered_a, widths_a = 0, []
    for p, y in pred_errs:
        q = _quantile(calib_res, 1.0 - min(max(alpha, 0.01), 0.99))
        e = abs(p - y)
        miss = 0.0 if e <= q else 1.0
        covered_a += 1 if miss == 0.0 else 0
        widths_a.append(2 * q)
        alpha = alpha + ACI_GAMMA * (ACI_ALPHA - miss)
        alpha = min(max(alpha, 0.01), 0.99)
    aci = {"coverage": covered_a / n, "mean_width": sum(widths_a) / n,
           "gamma": ACI_GAMMA, "final_alpha": alpha,
           "note": "Adaptive Conformal Inference: интервал расширяется после промахов"}
    return {"static_q80": static, "rolling_q80": rolling, "aci": aci,
            "target_coverage": 1.0 - ACI_ALPHA}


def _eval_model_at_origin(name: str, model, *, feats, n_train, calib_days,
                          needs_all):
    """Одно несмещённое окно: fit(train−calib) → калибровка → тест."""
    if model is not None:
        train_fit = [f for f in feats[:max(0, n_train - calib_days)]
                     if all(f[k] is not None for k in ("f_menu", "f_wd", "f_lag", "f_ma5"))]
        if len(train_fit) < MIN_TRAIN_ROWS:
            return {"status": "UNAVAILABLE",
                    "reason": (f"строк с полными признаками для обучения меньше "
                               f"{MIN_TRAIN_ROWS}: {len(train_fit)}")}
        try:
            m = model.fit(train_fit)
        except ImportError as e:
            return {"status": "UNAVAILABLE",
                    "reason": (f"зависимость недоступна: {e}; "
                               "установите: scripts/setup-ml.sh")}
    else:
        m = None

    def pred_of(f):
        if m is not None:
            return m.predict(f)
        return _baseline_pred(name, f)

    calib = feats[n_train - calib_days:n_train]
    cal_res = [abs(f["y"] - pred_of(f)) for f in calib if pred_of(f) is not None]
    if len(cal_res) < MIN_CALIB:
        return {"status": "UNAVAILABLE",
                "reason": f"калибровочных точек меньше {MIN_CALIB}: {len(cal_res)}"}
    q_calib = _quantile(cal_res, 0.8)

    test = feats[n_train:]
    pred_errs = [(pred_of(f), f["y"]) for f in test if pred_of(f) is not None]
    if not pred_errs:
        return {"status": "UNAVAILABLE", "reason": "нет предсказуемых тестовых точек"}
    mae = sum(abs(p - y) for p, y in pred_errs) / len(pred_errs)
    intervals = _interval_report_full(q_calib, cal_res, pred_errs)
    return {"status": "OK", "mae": mae, "n_test": len(test),
            "n_predicted": len(pred_errs),
            "test_range": [feats[n_train]["i"], feats[-1]["i"]],
            "q80_train_tail": q_calib, "intervals": intervals,
            "params": (m.params() if m is not None else {"rule": name})}


def backtest_diners(sessions, *, menu_fn=None, test_days: int = 30,
                    calib_days: int = 20, n_origins: int = N_ORIGINS) -> dict:
    """Rolling-origin backtest: базовые правила vs ML, три стратегии интервалов."""
    pts = build_diners_series(sessions, menu_fn=menu_fn)
    min_needed = test_days + MIN_TRAIN_ROWS + calib_days
    if len(pts) < min_needed:
        return {"status": "DATA_NOT_READY",
                "reason": (f"нужно >= {min_needed} точек (train+calib+test); "
                           f"есть {len(pts)}"),
                "n_points": len(pts)}
    feats = _causal_features(pts)
    n = len(feats)
    origins = []
    step = test_days
    for k in range(n_origins):
        nt = n - test_days - k * step
        if nt - calib_days >= MIN_TRAIN_ROWS and nt > calib_days:
            origins.append(nt)
    if not origins:
        return {"status": "DATA_NOT_READY",
                "reason": "нет валидных окон rolling-origin",
                "n_points": n}

    candidates = [
        ("moving_average5", None, False),
        ("weekday3", None, False),
        ("menu_history", None, False),
        ("ridge", _RidgeModel(use_trend=True), True),
        ("ridge_notrend", _RidgeModel(use_trend=False), True),
        ("sklearn_gbdt", "SKLEARN", True),
    ]
    results = []
    for name, model, needs_all in candidates:
        if model == "SKLEARN":
            try:
                model = _SklearnGBDT()
            except Exception as e:
                results.append({"name": name, "kind": "sklearn_gbdt",
                                "status": "UNAVAILABLE",
                                "reason": f"scikit-learn недоступен: {e}"})
                continue
        per_origin = []
        for nt in origins:
            r = _eval_model_at_origin(name, model if not isinstance(model, str) else None,
                                      feats=feats, n_train=nt,
                                      calib_days=calib_days, needs_all=needs_all)
            per_origin.append(r)
        ok = [r for r in per_origin if r["status"] == "OK"]
        if not ok:
            results.append({"name": name,
                            "kind": (model.kind if hasattr(model, "kind") else "baseline"),
                            "status": "UNAVAILABLE",
                            "reason": per_origin[-1].get("reason", "все окна недоступны")})
            continue
        maes = [r["mae"] for r in ok]
        # интервалы: усредняем по окнам
        agg_intervals = {}
        for strat in ("static_q80", "rolling_q80", "aci"):
            vals = [r["intervals"][strat] for r in ok if strat in r["intervals"]]
            if vals:
                agg_intervals[strat] = {
                    "coverage": sum(v["coverage"] for v in vals) / len(vals),
                    "mean_width": sum(v["mean_width"] for v in vals) / len(vals),
                }
        results.append({
            "name": name,
            "kind": (model.kind if hasattr(model, "kind") else "baseline"),
            "status": "OK",
            "mae_mean": sum(maes) / len(maes),
            "mae_per_origin": maes,
            "mae_spread": max(maes) - min(maes),
            "n_windows": len(ok),
            "n_test_per_window": ok[0]["n_test"],
            "intervals": agg_intervals,
            "target_coverage": 1.0 - ACI_ALPHA,
            "params": (model.params() if hasattr(model, "params") and model is not None
                       and not isinstance(model, str) else {"rule": name}),
        })
    ok_models = [r for r in results if r["status"] == "OK"]
    best = min(ok_models, key=lambda r: r["mae_mean"])["name"] if ok_models else None
    # лучшая стратегия интервалов: ближайшее покрытие к цели, при равном — уже
    best_intervals = None
    if ok_models:
        b = min(ok_models, key=lambda r: r["mae_mean"])
        target = 1.0 - ACI_ALPHA
        best_intervals = min(b["intervals"].items(),
                             key=lambda kv: (abs(kv[1]["coverage"] - target),
                                             kv[1]["mean_width"]))[0]
    return {
        "status": "OK" if ok_models else "DATA_NOT_READY",
        "reason": None if ok_models else "все модели UNAVAILABLE (см. models)",
        "target": "attendance_count (иначе meals_served); пропуск≠0",
        "evaluation": {"scheme": "rolling-origin: train → калибровка → тест",
                       "n_origins": len(origins), "test_days": test_days,
                       "calib_days": calib_days},
        "n_points": n,
        "first_test_date": pts[origins[-1]].date.isoformat(),
        "last_test_date": pts[-1].date.isoformat(),
        "models": results,
        "best_by_mae": best,
        "best_interval_strategy_for_best_model": best_intervals,
        "notes": [
            "модель для эксплуатации выбирается по измеренному backtest-MAE; "
            "параметры всех кандидатов зафиксированы в model cards",
            "три стратегии интервалов оцениваются рядом: static / rolling / ACI; "
            "выбор — по ближайшему измеренному покрытию к цели при меньшей ширине",
            "ridge с линейным трендом экстраполирует и нестабилен на дальних "
            "окнах — поэтому рядом оценивается ridge_notrend (вариант без тренда)",
            "признаки причинные; точность на школе КЗ не заявляется до REAL-данных",
        ],
    }
