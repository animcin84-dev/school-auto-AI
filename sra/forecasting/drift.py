"""Детектор дрейфа (эвристический, этап 3).

Правило: сравнивается медиана последних k остатков теста с медианой
калибровочных остатков; порог — factor · масштаб MAD (1.4826·MAD).
Это НАЧАЛЬНАЯ эвристика: порог и окно подлежат валидации на реальных
данных (этап 5); при scale=0 любое устойчивое отклонение считается дрейфом.
Детектор информирует о необходимости пересмотра модели, но сам не меняет
поведение решений; решение при дрейфе — по-прежнему NO_CHANGE до проверки.
"""

from __future__ import annotations

from statistics import median

from .. import enums

MAD_SCALE = 1.4826  # согласованность с σ для нормального распределения


def detect_drift(calib_residuals: list[float], test_residuals: list[float],
                 *, k: int = 5, factor: float = 3.0) -> dict:
    if len(calib_residuals) < 2 or len(test_residuals) < 2:
        return {"status": enums.DRIFT_INSUFFICIENT,
                "details": {"n_calib": len(calib_residuals), "n_test": len(test_residuals)},
                "note": "мало остатков для детекции"}
    med_c = median(calib_residuals)
    mad = median([abs(r - med_c) for r in calib_residuals])
    scale = MAD_SCALE * mad
    last = test_residuals[-k:]
    med_t = median(last)
    dev = abs(med_t - med_c)
    threshold = factor * scale
    detected = (dev > threshold) if scale > 0 else (dev > 1e-9)
    return {
        "status": enums.DRIFT_DETECTED if detected else enums.DRIFT_OK,
        "details": {"median_calib": med_c, "mad_scale": scale,
                    "median_last_k": med_t, "k": len(last),
                    "threshold": threshold, "deviation": dev},
        "note": "эвристика; не блокирует расчёты, но требует внимания ответственного "
                "при DRIFT_DETECTED",
    }
