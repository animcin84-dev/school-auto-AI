"""Дизайн оценки реального эффекта (этап 5а): требуемый объём выборки и план.

Математика: простая двухгрупповая сравнительная оценка (или до/после с парными
разностями) по дневным агрегатам. Требуемый размер на группу:
    n = k · (z_{1−α/2} + z_{power})² · σ² / effect²,
где k = 2 для двух групп и k = 1 для парного дизайна.

Ограничения честности:
  - z-квантили стандартного нормального распределения заданы константами
    (scipy недоступен в этапе stdlib); значения проверены тестами вручную;
  - σ берётся из остатков прогноза исторического окна — это ЗАМЕНА, а не
    изменчивость дизайна; план требует пересмотра статистиком до вмешательства;
  - план имеет статус DRAFT_REQUIRES_APPROVAL и НЕ является разрешением на
    вмешательство;
  - критерии остановки остаются незаполненными (None), пока их не задаст
    проверенная политика школы — выдумывать пороги запрещено.
"""

from __future__ import annotations

from math import ceil

from .. import enums
from ..errors import ValidationError

# Стандартные нормальные квантили (проверены тестами с ручными значениями).
Z_ALPHA = {0.05: 1.959964, 0.01: 2.5758293}   # z_{1−α/2}
Z_POWER = {0.80: 0.8416212, 0.90: 1.2815516}  # z_{power}

DESIGNS = ("two_arm_parallel", "before_after")


def required_n_per_arm(*, sigma_g: float, effect_g: float, alpha: float = 0.05,
                       power: float = 0.80, paired: bool = False) -> int:
    if sigma_g <= 0:
        raise ValidationError("sigma_g должен быть > 0", code="E_BAD_SIGMA")
    if effect_g <= 0:
        raise ValidationError("effect_g должен быть > 0", code="E_BAD_EFFECT")
    if alpha not in Z_ALPHA:
        raise ValidationError(f"alpha поддерживается из {sorted(Z_ALPHA)}", code="E_BAD_ALPHA")
    if power not in Z_POWER:
        raise ValidationError(f"power поддерживается из {sorted(Z_POWER)}", code="E_BAD_POWER")
    factor = 1.0 if paired else 2.0
    zsum = Z_ALPHA[alpha] + Z_POWER[power]
    return ceil(factor * (zsum * sigma_g / effect_g) ** 2)


def build_evaluation_plan(*, school_id: str, design: str, sigma_g: float,
                          baseline_waste_g_per_day: float,
                          expected_effect_rel: float, alpha: float = 0.05,
                          power: float = 0.80,
                          school_days_per_week: int = 5) -> dict:
    if design not in DESIGNS:
        raise ValidationError(f"design поддерживается из {list(DESIGNS)}", code="E_BAD_DESIGN")
    if not 0.0 < expected_effect_rel < 1.0:
        raise ValidationError("expected_effect_rel должен быть в (0, 1)", code="E_BAD_EFFECT")
    effect_g = expected_effect_rel * baseline_waste_g_per_day
    paired = design == "before_after"
    n_per_arm = required_n_per_arm(sigma_g=sigma_g, effect_g=effect_g,
                                   alpha=alpha, power=power, paired=paired)
    sessions_total = n_per_arm if paired else 2 * n_per_arm
    if school_days_per_week <= 0:
        raise ValidationError("school_days_per_week должен быть > 0", code="E_BAD_CALENDAR")
    weeks = ceil(sessions_total / school_days_per_week)
    return {
        "status": "DRAFT_REQUIRES_APPROVAL",
        "artifact_nature": enums.PREDICTED,
        "school_id": school_id,
        "design": design,
        "params": {"sigma_g": sigma_g, "alpha": alpha, "power": power,
                   "baseline_waste_g_per_day": baseline_waste_g_per_day,
                   "expected_effect_rel": expected_effect_rel,
                   "school_days_per_week": school_days_per_week},
        "effect_g_per_day": effect_g,
        "n_per_arm": n_per_arm,
        "sessions_total": sessions_total,
        "estimated_weeks_min": weeks,
        "sigma_interpretation": ("парные разности дней (до/после)" if paired
                                 else "межгрупповое SD дневных агрегатов"),
        "sigma_source_note": "σ оценена по остаткам прогноза исторического окна; это "
                             "замена, а не изменчивость дизайна — до вмешательства план "
                             "пересматривается статистиком",
        "assumptions": [
            "приближённая нормальность дневных агрегатов",
            "независимость дней: автокорреляция и меню-циклы могут нарушать это "
            "предположение и смещать оценку объёма выборки",
            "σ стабильна во времени; дрейф обнуляет расчёт",
            "в период оценки нет других одновременных изменений работы столовой",
            "ожидаемый эффект — ПРЕДПОЛОЖЕНИЕ заказчика плана, а не измеренный факт",
        ],
        "stopping_criteria_template": {
            "service_level_drop_pct": None,
            "max_consecutive_shortfall_days": None,
            "note": "значения задаёт проверенная политика школы до вмешательства; "
                    "система не выдумывает пороги",
        },
        "warnings": [
            "план НЕ является разрешением на вмешательство: требуются согласие школы, "
            "VERIFIED-протокол и политики, одобрение уполномоченных (этап 6)",
            "оценка по этому дизайну показывает величину эффекта с неопределённостью, "
            "а не гарантию экономии",
            "результаты одной школы не экстраполируются на другие школы или страну",
        ],
    }
