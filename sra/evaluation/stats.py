"""Минимальная статистика на стандартной библиотеке (сторонних пакетов нет).

Реализовано:
  - двухвыборочный t-критерий Уэлча (не предполагает равенства дисперсий):
    статистика t, поправка Уэлча–Саттертуэйта на df, точное двустороннее
    p через регуляризованную неполную бета-функцию (продолжительная дробь);
  - критерий Манна–Уитни с поправкой на связки (нормальное приближение,
    p через math.erfc) — устойчивая к форме распределения проверка сдвига.

Ограничения (честно): приближение Манна–Уитни нормальным рассчитано на
n1·n2 >= ~20 без экстремального числа связок; при малых выборках p
выводится с предупреждением. Это инженерная статистика для разведочного
анализа, не пакет для публикаций.
"""

from __future__ import annotations

import math

from ..errors import ValidationError


def mean(xs: list[float]) -> float:
    return sum(xs) / len(xs)


def sample_sd(xs: list[float]) -> float:
    """Несмещённое выборочное СКО (n−1). Для len<2 — 0.0 (вызов проверит n)."""
    n = len(xs)
    if n < 2:
        return 0.0
    m = mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (n - 1))


def _betacf(a: float, b: float, x: float, *, max_iter: int = 300, eps: float = 3e-12):
    """Продолжительная дробь Ленца для неполной бета-функции (Numerical Recipes)."""
    tiny = 1e-300
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < tiny:
        d = tiny
    d = 1.0 / d
    h = d
    for m in range(1, max_iter + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < tiny:
            d = tiny
        c = 1.0 + aa / c
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < tiny:
            d = tiny
        c = 1.0 + aa / c
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < eps:
            break
    return h


def betainc(a: float, b: float, x: float) -> float:
    """Регуляризованная неполная бета-функция I_x(a, b)."""
    if not (0.0 <= x <= 1.0):
        raise ValidationError(f"betainc: x вне [0,1]: {x}", code="E_BAD_BETAINC_ARG")
    if x == 0.0:
        return 0.0
    if x == 1.0:
        return 1.0
    lbeta = math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
    bt = math.exp(lbeta + a * math.log(x) + b * math.log1p(-x))
    if x < (a + 1.0) / (a + b + 2.0):
        return bt * _betacf(a, b, x) / a
    return 1.0 - bt * _betacf(b, a, 1.0 - x) / b


def t_two_sided_p(t: float, df: float) -> float:
    """Точное двустороннее p для t-распределения с df степенями свободы."""
    if df <= 0:
        raise ValidationError("t_two_sided_p: df должно быть положительным",
                              code="E_BAD_DF")
    x = df / (df + t * t)
    return betainc(df / 2.0, 0.5, x)


def welch_ttest(xs: list[float], ys: list[float]) -> dict:
    """Двухвыборочный t-критерий Уэлча (H0: равные средние)."""
    n1, n2 = len(xs), len(ys)
    if n1 < 2 or n2 < 2:
        raise ValidationError("welch_ttest: нужно >=2 значений в каждой группе",
                              code="E_SMALL_SAMPLE")
    v1, v2 = sample_sd(xs) ** 2, sample_sd(ys) ** 2
    se2 = v1 / n1 + v2 / n2
    if se2 <= 0:
        # все значения в обеих группах одинаковы: различий нет по построению
        return {"n1": n1, "n2": n2, "mean1": mean(xs), "mean2": mean(ys),
                "diff": 0.0, "t": 0.0, "df": float(n1 + n2 - 2), "p": 1.0,
                "note": "нулевая дисперсия в обеих группах"}
    # Уэлч–Саттертуэйт: df = (v1/n1+v2/n2)² / [v1²/(n1²(n1−1)) + v2²/(n2²(n2−1))]
    df = se2 ** 2 / (v1 ** 2 / (n1 ** 2 * (n1 - 1)) + v2 ** 2 / (n2 ** 2 * (n2 - 1)))
    m1, m2 = mean(xs), mean(ys)
    t = (m1 - m2) / math.sqrt(se2)
    return {"n1": n1, "n2": n2, "mean1": m1, "mean2": m2, "diff": m1 - m2,
            "t": t, "df": df, "p": t_two_sided_p(t, df)}


def mann_whitney_u(xs: list[float], ys: list[float]) -> dict:
    """Критерий Манна–Уитни с поправкой на связки (нормальное приближение)."""
    n1, n2 = len(xs), len(ys)
    if n1 < 1 or n2 < 1:
        raise ValidationError("mann_whitney_u: пустая группа", code="E_SMALL_SAMPLE")
    pooled = [(v, 0) for v in xs] + [(v, 1) for v in ys]
    pooled.sort(key=lambda p: p[0])
    ranks = [0.0] * len(pooled)
    i = 0
    tie_term = 0.0
    while i < len(pooled):
        j = i
        while j + 1 < len(pooled) and pooled[j + 1][0] == pooled[i][0]:
            j += 1
        avg_rank = (i + j) / 2.0 + 1.0
        t = j - i + 1
        tie_term += t ** 3 - t
        for k in range(i, j + 1):
            ranks[k] = avg_rank
        i = j + 1
    r1 = sum(r for r, (v, g) in zip(ranks, pooled) if g == 0)
    u1 = r1 - n1 * (n1 + 1) / 2.0
    u2 = n1 * n2 - u1
    u = min(u1, u2)
    n = n1 + n2
    var = n1 * n2 / 12.0 * ((n + 1) - tie_term / (n * (n - 1)) if n > 1 else (n + 1))
    if var <= 0:
        return {"u": u, "z": 0.0, "p": 1.0, "note": "нулевая дисперсия рангов"}
    z = (u - n1 * n2 / 2.0) / math.sqrt(var)
    p = math.erfc(abs(z) / math.sqrt(2.0))  # 2·(1−Φ(|z|))
    return {"u": u, "z": z, "p": p}
