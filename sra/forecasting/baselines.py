"""Базовые модели прогноза этапа 3 + hindsight-базовая линия политики кухни.

Все модели:
  - детерминированные, без обучения «весов» (малый объём данных, зрелые методы);
  - общий интерфейс: fit(history) → predict(target_date) → float | None
    (None = холодный старт: недостаточно истории, фиктивного значения НЕТ).

KitchenPolicyModel — это НЕ прогноз: она возвращает фактическую массу
приготовления той же сессии (модель видит день, который «предсказывает»).
Это базовая линия «как кухня реально готовила» для честного сравнения;
для реальных forward-решений она неприменима и помечена hindsight_only.
"""

from __future__ import annotations

from statistics import median

from .series import TargetPoint


class NaiveModel:
    """Наивный прогноз: последнее наблюдаемое значение (по дате)."""

    name = "naive"

    def __init__(self):
        self.params: dict = {}
        self._last: TargetPoint | None = None

    def fit(self, history: list[TargetPoint]) -> None:
        self._last = max(history, key=lambda p: p.service_date) if history else None

    def predict(self, target_date) -> float | None:
        return self._last.served_mass_g if self._last is not None else None


class SeasonalNaiveModel:
    """Сезонный наивный: медиана значений того же дня недели за последние `weeks` недель."""

    name = "seasonal_naive"

    def __init__(self, weeks: int = 3):
        if weeks < 1:
            raise ValueError("weeks >= 1")
        self.params = {"weeks": weeks}
        self._by_weekday: dict[int, list[TargetPoint]] = {}

    def fit(self, history: list[TargetPoint]) -> None:
        by_wd: dict[int, list[TargetPoint]] = {}
        for p in history:
            by_wd.setdefault(p.service_date.weekday(), []).append(p)
        for wd in by_wd:
            by_wd[wd].sort(key=lambda p: p.service_date)
        self._by_weekday = by_wd

    def predict(self, target_date) -> float | None:
        pts = self._by_weekday.get(target_date.weekday(), [])[-self.params["weeks"]:]
        if not pts:
            return None
        return median([p.served_mass_g for p in pts])


class MovingAverageModel:
    """Скользящее среднее за `window` последних сессий (требует полной истории окна)."""

    name = "moving_average"

    def __init__(self, window: int = 4):
        if window < 1:
            raise ValueError("window >= 1")
        self.params = {"window": window}
        self._hist: list[TargetPoint] = []

    def fit(self, history: list[TargetPoint]) -> None:
        self._hist = sorted(history, key=lambda p: p.service_date)

    def predict(self, target_date) -> float | None:
        pts = self._hist[-self.params["window"]:]
        if len(pts) < self.params["window"]:
            return None  # холодный старт: окно не заполнено
        return sum(p.served_mass_g for p in pts) / len(pts)


class KitchenPolicyModel:
    """Фактическая политика кухни: prepared_mass той же сессии (hindsight)."""

    name = "kitchen_policy_prepared"
    hindsight_only = True

    def __init__(self):
        self.params: dict = {"hindsight": "фактическая prepared_mass сессии-цели"}
        self._by_date: dict = {}

    def fit(self, history: list[TargetPoint]) -> None:
        self._by_date = {p.service_date: p.prepared_mass_g for p in history}

    def predict(self, target_date) -> float | None:
        return self._by_date.get(target_date)


def build_model(name: str, *, seasonal_weeks: int = 3, moving_avg_window: int = 4):
    """Фабрика моделей по имени. Неизвестное имя — явная ошибка."""
    if name == NaiveModel.name:
        return NaiveModel()
    if name == SeasonalNaiveModel.name:
        return SeasonalNaiveModel(weeks=seasonal_weeks)
    if name == MovingAverageModel.name:
        return MovingAverageModel(window=moving_avg_window)
    if name == KitchenPolicyModel.name:
        return KitchenPolicyModel()
    raise KeyError(f"неизвестная модель прогноза: {name!r}")
