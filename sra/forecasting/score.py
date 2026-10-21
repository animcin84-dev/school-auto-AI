"""Метрики качества прогноза: квантили, pinball loss, MAE, покрытие интервалов.

Все функции чистые и детерминированные — используются в backtest'е и
покрыты тестами с ручными контрольными значениями.
"""

from __future__ import annotations

import math


def empirical_quantile(values: list[float], q: float) -> float | None:
    """Эмпирический квантиль с линейной интерполяцией (позиция (n−1)·q).

    Пустой список → None. Это эмпирическая оценка по остаткам калибровки:
    никаких conformal-гарантий не заявляется.
    """
    if not values:
        return None
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    if not 0.0 <= q <= 1.0:
        raise ValueError(f"квантиль q вне [0,1]: {q}")
    pos = (len(s) - 1) * q
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return s[lo]
    frac = pos - lo
    return s[lo] * (1.0 - frac) + s[hi] * frac


def pinball_loss(y: float, p: float, q: float) -> float:
    """Pinball (quantile) loss: max(q·(y−p), (q−1)·(y−p)).

    Асимметричная штрафная функция: занижение квантиля дороже при q→1.
    """
    return max(q * (y - p), (q - 1.0) * (y - p))


def mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def mae(pairs: list[tuple[float, float]]) -> float | None:
    """MAE по парам (y, pred)."""
    if not pairs:
        return None
    return sum(abs(y - p) for y, p in pairs) / len(pairs)
