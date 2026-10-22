"""Прогноз числа едоков (посещаемости) — отдельный трек с ML-кандидатами.

Зачем отдельный трек: спрос = едоки × грамм/едока. Посещаемость стабильнее
граммов и в КЗ наблюдается автоматически («Социальный кошелёк» фиксирует
факты выдачи); литература называет её главным предиктором для столовых.

Правила честности (не нарушаются никакой моделью):
  - признаки строятся ПРИЧИННО (только прошлое относительно точки);
  - оценка — expanding-origin backtest: train → калибровка → тест;
  - интервал — split-conformal по хвостовым остаткам train (q80);
  - кандидат сравнивается с действующими базовыми в ОДНОМ отчёте,
    выбор по измеренному MAE, а не по обещаниям;
  - отсутствующий пакет → статус UNAVAILABLE с причиной, не ошибка.

Модели:
  moving_average5 — среднее последних 5 точек (действующая);
  weekday3        — среднее последних 3 точек того же дня недели (действующая);
  menu_history    — среднее предыдущих точек с тем же идентификатором меню
                    (fallback: weekday3); меню по умолчанию извлекается из
                    notes по шаблону "MENU=<id>" (совместимо с FI-датасетом);
  ridge           — L2-регуляризованная линейная регрессия на признаках
                    [1, menu_hist, weekday_hist, lag1, ma5, trend] — закрытая
                    формула, stdlib, детерминирована;
  sklearn_gbdt    — HistGradientBoostingRegressor (нужен пакет scikit-learn;
                    scripts/setup-ml.sh), параметры зафиксированы в отчёте.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .. import enums

_MENU_RE = re.compile(r"MENU=(\d+)")
MIN_TRAIN_ROWS = 10
MIN_TEST_POINTS = 5


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
        pts.append(DinersPoint(session_id=s.session_id, date=s.service_date,
                               weekday=s.service_date.weekday(),
                               menu=menu_fn(s), y=float(y)))
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


def _quantile80(xs: list[float]) -> float:
    xs = sorted(xs)
    if not xs:
        return float("nan")
    k = 0.8 * (len(xs) - 1)
    lo, hi = int(k), min(int(k) + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def _solve_ridge(X, y, lam: float) -> list[float]:
    k = len(X[0])
    A = [[sum(row[a] * row[b] for row in X) + (lam if a == b else 0.0)
          for b in range(k)] for a in range(k)]
    b = [sum(row[a] * yy for row, yy in zip(X, y)) for a in range(k)]
    for c in range(k):  # Гаусс с частичным выбором
        piv = max(range(c, k), key=lambda r: abs(A[r][c]))
        A[c], A[piv] = A[piv], A[c]
        b[c], b[piv] = b[piv], b[c]
        for r in range(c + 1, k):
            f = A[r][c] / A[c][c]
            for cc in range(c, k):
                A[r][cc] -= f * A[c][cc]
            b[r] -= f * b[c]
    w = [0.0] * k
    for r in range(k - 1, -1, -1):
        w[r] = (b[r] - sum(A[r][c] * w[c] for c in range(r + 1, k))) / A[r][r]
    return w


class _RidgeModel:
    """L2-линейная регрессия со стандартизацией (закрытая форма, stdlib).

    Перехват НЕ стандартизуется (иначе столбец констант вырождается в нули):
    признаки стандартизуются, затем добавляется столбец единиц; лямбда не
    действует на перехват.
    """

    kind = "ridge"

    def __init__(self, lam: float = 3.0):
        self.lam = lam
        self.weights = None
        self.mu = None
        self.sd = None

    def _vec(self, f: dict) -> list[float]:
        return [f["f_menu"], f["f_wd"], f["f_lag"], f["f_ma5"], f["trend"]]

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
        Xs = [[1.0] + [(row[c] - self.mu[c]) / self.sd[c] for c in range(len(X[0]))]
              for row in X]
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
        z = [(v - m) / s for v, m, s in zip(self._vec(f), self.mu, self.sd)]
        row = [1.0] + z
        return sum(a * b for a, b in zip(self.weights, row))

    def params(self) -> dict:
        return {"lam": self.lam, "features": ["menu_hist", "weekday_hist",
                                              "lag1", "ma5", "trend"],
                "standardized": True}


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


def _eval_model(name: str, model, *, feats, n_train, calib_days, needs_all):
    """Честная схема: fit на train без хвоста → q80 по хвостовым остаткам →
    тест. Для базовых (без fit) residuals те же хвостовые точки."""
    if model is None:  # базовая без обучения: предсказание по признаку
        pass
    rows_all = feats
    train_fit = [f for f in rows_all[:max(0, n_train - calib_days)]]
    calib = rows_all[max(0, n_train - calib_days):n_train]
    test = rows_all[n_train:]
    if needs_all:
        train_fit = [f for f in train_fit
                     if all(f[k] is not None for k in ("f_menu", "f_wd", "f_lag", "f_ma5"))]
        calib = [f for f in calib
                 if all(f[k] is not None for k in ("f_menu", "f_wd", "f_lag", "f_ma5"))]
    if model is not None and len(train_fit) < MIN_TRAIN_ROWS:
        return {"name": name, "status": "UNAVAILABLE",
                "reason": (f"строк с полными признаками для обучения меньше "
                           f"{MIN_TRAIN_ROWS}: {len(train_fit)}")}
    if len(calib) < 5:
        return {"name": name, "status": "UNAVAILABLE",
                "reason": f"калибровочных точек меньше 5: {len(calib)}"}

    if model is not None:
        try:
            m = model.fit(train_fit)
        except ImportError as e:
            # честный отказ без маскировки: причина в отчёте, остальные модели работают
            return {"name": name, "kind": getattr(model, "kind", "candidate"),
                    "status": "UNAVAILABLE",
                    "reason": f"зависимость недоступна: {e}; установите: scripts/setup-ml.sh"}
    else:
        m = None

    def pred_of(f):
        if m is not None:
            return m.predict(f)
        # базовые: действующий признак (с честным fallback)
        if name.startswith("moving_average"):
            v = f["f_ma5"]
        elif name.startswith("weekday"):
            v = f["f_wd"]
        elif name.startswith("menu_history"):
            v = f["f_menu"] if f["f_menu"] is not None else f["f_wd"]
        else:
            v = None
        return v

    cal_res = [abs(f["y"] - pred_of(f)) for f in calib if pred_of(f) is not None]
    if len(cal_res) < 5:
        return {"name": name, "status": "UNAVAILABLE",
                "reason": f"предсказуемых калибровочных точек меньше 5"}
    q80 = _quantile80(cal_res)

    errs, covered, n_skip = [], 0, 0
    for f in test:
        p = pred_of(f)
        if p is None:
            n_skip += 1
            continue
        e = abs(f["y"] - p)
        errs.append(e)
        covered += 1 if e <= q80 else 0
    if not errs:
        return {"name": name, "status": "UNAVAILABLE",
                "reason": "нет предсказуемых тестовых точек"}
    mae = sum(errs) / len(errs)
    out = {
        "name": name,
        "kind": (m.kind if m is not None else "baseline"),
        "status": "OK",
        "n_test": len(test), "n_predicted": len(errs), "n_skipped_features": n_skip,
        "mae": mae,
        "interval": {"method": "split-conformal q80 (калибровка на хвосте train)",
                     "q80": q80, "coverage": covered / len(errs),
                     "width": 2 * q80},
        "params": (m.params() if m is not None else
                   {"rule": name}),
    }
    return out


def backtest_diners(sessions, *, menu_fn=None, test_days: int = 30,
                    calib_days: int = 20) -> dict:
    """Backtest прогноза едоков: действующие базовые vs ML-кандидаты."""
    pts = build_diners_series(sessions, menu_fn=menu_fn)
    if len(pts) < test_days + MIN_TRAIN_ROWS + calib_days:
        return {"status": "DATA_NOT_READY",
                "reason": (f"нужно >= {test_days + MIN_TRAIN_ROWS + calib_days} точек "
                           f"(train+calib+test); есть {len(pts)}"),
                "n_points": len(pts)}
    feats = _causal_features(pts)
    n = len(feats)
    n_train = n - test_days

    candidates = [
        ("moving_average5", None, False),
        ("weekday3", None, False),
        ("menu_history", None, False),
        ("ridge", _RidgeModel(), True),
        ("sklearn_gbdt", None, True),  # создаётся ниже с guard-импортом
    ]
    results = []
    for name, model, needs_all in candidates:
        if name == "sklearn_gbdt":
            try:
                model = _SklearnGBDT()
            except Exception as e:  # ImportError и любые другие
                results.append({"name": name, "kind": "sklearn_gbdt",
                                "status": "UNAVAILABLE",
                                "reason": f"scikit-learn недоступен: {e}"})
                continue
        results.append(_eval_model(name, model, feats=feats, n_train=n_train,
                                   calib_days=calib_days, needs_all=needs_all))
    ok = [r for r in results if r["status"] == "OK"]
    best = min(ok, key=lambda r: r["mae"])["name"] if ok else None
    return {
        "status": "OK" if ok else "DATA_NOT_READY",
        "reason": None if ok else "все модели UNAVAILABLE (см. models)",
        "target": "attendance_count (иначе meals_served); пропуск≠0",
        "n_points": n, "n_train": n_train, "n_test": n - n_train,
        "test_range": [pts[n_train].date.isoformat(), pts[-1].date.isoformat()],
        "models": results,
        "best_by_mae": best,
        "notes": [
            "модель для эксплуатации выбирается по измеренному backtest-MAE, "
            "а не по обещаниям; параметры всех кандидатов зафиксированы",
            "признаки причинные; интервалы калибруются раздельно по хвосту train",
            "MAE на одном датасете — ориентир; точность на школе КЗ не заявляется",
        ],
    }
