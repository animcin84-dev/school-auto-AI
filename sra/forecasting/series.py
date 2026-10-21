"""Целевой ряд прогноза и снапшоты данных «на момент решения».

Определения этапа 3 (фиксируются в отчёте backtest'а):
  - ЦЕЛЬ: served_mass_g — суммарная масса выданной еды за приём пищи (сессия).
    Это НАБЛЮДАЕМАЯ величина. На цензурированных днях (нехватка еды) она
    занижает полный спрос; точность прогноза выдачи НЕ доказывает прогноз
    полного спроса — цензурирование показывается отдельно.
  - ЕДИНИЦА: школа × дата × meal_type (сессия), агрегат по блюдам.
  - ГОРИЗОНТ: 1 приём пищи.
  - МОМЕНТ РЕШЕНИЯ: D−1 в decision_hour по школьному часовому поясу.

Сессия включается в ряд только при ПОЛНОМ покрытии блюд (все наблюдения
сессии имеют served_mass_g); частичные дни исключаются с явным учётом
(без импутации — пропуск не заполняем).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from .. import enums

# Режущая точка «всё, что есть» для финальных данных (datetime максимум далёк).
FAR_FUTURE_CUTOFF = "9999-12-31T23:59:59+00:00"


@dataclass
class TargetPoint:
    """Одна точка целевого ряда (одна сессия с полным покрытием блюд)."""

    session_id: str
    service_date: date
    served_mass_g: float | None
    prepared_mass_g: float | None
    n_obs: int
    n_served_obs: int
    censored: bool                 # demand_censored == true (выдача занижена нехваткой)
    censored_unknown: bool
    meals_served: int | None


@dataclass
class TargetSeries:
    points: list[TargetPoint] = field(default_factory=list)   # отсортированы по дате
    excluded: list[dict] = field(default_factory=list)        # явный учёт исключений
    school_id: str = ""
    meal_type: str = ""


def build_target_series(store, school_id: str, meal_type: str,
                        *, cutoff_iso: str | None = None) -> TargetSeries:
    """Строит целевой ряд из as-of среза хранилища (по умолчанию — финальные данные)."""
    cutoff = cutoff_iso or FAR_FUTURE_CUTOFF
    sessions = [r for r in store.asof("session", cutoff)
                if r["payload"].get("school_id") == school_id
                and r["payload"].get("meal_type") == meal_type]
    obs_by_session: dict[str, list[dict]] = {}
    for r in store.asof("observation", cutoff):
        obs_by_session.setdefault(r["payload"].get("session_id"), []).append(r["payload"])

    points: list[TargetPoint] = []
    excluded: list[dict] = []
    for s in sorted(sessions, key=lambda r: r["payload"]["service_date"]):
        p = s["payload"]
        obs = obs_by_session.get(p.get("session_id"), [])
        n_obs = len(obs)
        served = [o["served_mass_g"] for o in obs if o.get("served_mass_g") is not None]
        prepared = [o["prepared_mass_g"] for o in obs if o.get("prepared_mass_g") is not None]
        tp = TargetPoint(
            session_id=p["session_id"],
            service_date=date.fromisoformat(p["service_date"]),
            served_mass_g=(sum(served) if served else None),
            prepared_mass_g=(sum(prepared) if prepared else None),
            n_obs=n_obs,
            n_served_obs=len(served),
            censored=(p.get("demand_censored") == "true"),
            censored_unknown=(p.get("demand_censored") == "unknown"),
            meals_served=p.get("meals_served"),
        )
        if n_obs == 0:
            excluded.append({"session_id": tp.session_id, "date": tp.service_date.isoformat(),
                             "reason": "NO_OBSERVATIONS"})
        elif tp.n_served_obs < n_obs:
            excluded.append({"session_id": tp.session_id, "date": tp.service_date.isoformat(),
                             "reason": "PARTIAL_SERVED_COVERAGE",
                             "detail": f"{tp.n_served_obs}/{n_obs} блюд с served_mass_g; "
                                       "импутация запрещена"})
        else:
            points.append(tp)
    points.sort(key=lambda t: t.service_date)
    return TargetSeries(points=points, excluded=excluded,
                        school_id=school_id, meal_type=meal_type)


def make_snapshot_fn(store, school_id: str, meal_type: str):
    """Возвращает функцию cutoff_iso -> TargetSeries для backtest'а.

    Каждый вызов берёт свежий as-of срез: история обучения строго из данных,
    доступных к моменту решения (защита от утечки будущего, включая поздние
    исправления записей).
    """
    def snapshot(cutoff_iso: str) -> TargetSeries:
        return build_target_series(store, school_id, meal_type, cutoff_iso=cutoff_iso)
    return snapshot


def eligible_points(points: list[TargetPoint], *, exclude_censored: bool = True) -> list[TargetPoint]:
    """Отбор точек для обучения: served известен; цензурированные дни исключаются
    по умолчанию (выдача занижена нехваткой и нерепрезентативна для спроса)."""
    out = []
    for p in points:
        if p.served_mass_g is None:
            continue
        if exclude_censored and p.censored:
            continue
        out.append(p)
    return out
