"""Параметры политик решений (этап 4) и их валидация.

Все числовые параметры решения должны приходить из версионированной политики
(источник, юрисдикция, срок действия, статус проверки), а не быть случайными
константами кода. В DEMO допускаются явно синтетические политики
(data_nature=SYNTHETIC) — они остаются UNVERIFIED и не дают права на реальное
вмешательство.
"""

from __future__ import annotations

from .. import enums
from ..errors import ValidationError

# Имена параметров политики решения (стабильный контракт).
REQUIRED_PARAMS = (
    "cost_waste_per_kg",          # стоимость предотвращаемых потерь, ед. валюты/кг
    "cost_shortage_per_kg",       # штраф нехватки (доступность питания), ед. валюты/кг
    "currency",
    "shortage_risk_max",          # α: жёсткая граница риска нехватки
    "delta_max_pct_of_baseline",  # Δ_max: граница разрешённого изменения объёма
    "baseline_days",              # окно оценки «готовить как обычно»
    "forecast_model",             # имя модели прогноза (этап 3)
    "calib_days",                 # окно калибровки остатков
    "grid_step_g",                # шаг сетки решателя
    "min_improvement_rel",        # минимальное относительное улучшение для действия
)

FORECAST_MODEL_NAMES = {"naive", "seasonal_naive", "moving_average"}


def validate_decision_policy(values: dict) -> list[str]:
    """Возвращает список проблем (пустой список = параметры допустимы)."""
    problems: list[str] = []
    missing = [k for k in REQUIRED_PARAMS if k not in values]
    if missing:
        problems.append(f"отсутствуют параметры: {missing}")
        return problems
    if values["cost_waste_per_kg"] <= 0:
        problems.append("cost_waste_per_kg должен быть > 0")
    if values["cost_shortage_per_kg"] <= values["cost_waste_per_kg"]:
        problems.append("cost_shortage_per_kg должен строго доминировать "
                        "(доступность питания дороже потерь)")
    a = values["shortage_risk_max"]
    if not 0.0 < a < 0.5:
        problems.append("shortage_risk_max должен быть в (0, 0.5)")
    d = values["delta_max_pct_of_baseline"]
    if not 0.0 < d <= 0.5:
        problems.append("delta_max_pct_of_baseline должен быть в (0, 0.5]")
    if int(values["baseline_days"]) < 3:
        problems.append("baseline_days должен быть >= 3")
    if int(values["calib_days"]) < 3:
        problems.append("calib_days должен быть >= 3")
    if values["forecast_model"] not in FORECAST_MODEL_NAMES:
        problems.append(f"forecast_model должен быть из {sorted(FORECAST_MODEL_NAMES)}")
    if values["grid_step_g"] <= 0:
        problems.append("grid_step_g должен быть > 0")
    if values["min_improvement_rel"] < 0:
        problems.append("min_improvement_rel не может быть отрицательным")
    if not str(values["currency"]).strip():
        problems.append("currency не может быть пустым")
    return problems


def check_policy_usable(row: dict, *, environment: str) -> list[str]:
    """Проверка применимости политики В ДАННОЙ среде.

    DEMO: значения обязаны быть помечены SYNTHETIC (искусственная политика).
    REAL: требуется VERIFIED-статус проверки политики и REAL_OBSERVED-значения
    (таких политик в этапе 4 не существует — решение остаётся NO_CHANGE).
    """
    payload = row.get("payload_json") or {}
    problems: list[str] = []
    nature = payload.get("data_nature")
    status = row.get("verification_status")
    if environment == enums.ENV_DEMO:
        if nature != enums.SYNTHETIC:
            problems.append("в DEMO-среде допускаются только политики с "
                            "data_nature=SYNTHETIC")
        if status == enums.POLICY_RETIRED:
            problems.append("политика выведена из действия (RETIRED)")
    else:
        if status != enums.POLICY_VERIFIED:
            problems.append(f"в среде {environment} требуется VERIFIED-политика; текущий "
                            f"статус: {status}")
        if nature != enums.REAL_OBSERVED:
            problems.append(f"в среде {environment} значения политики должны иметь "
                            "документ-источник (data_nature=REAL_OBSERVED)")
    return problems
