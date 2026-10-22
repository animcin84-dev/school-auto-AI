"""ml_status — read-only сводка ML-состояния хранилища (без вычислений).

Проблема (docs/AUDIT.md §B.9): новые модули (diners/composite/quality/
alerts/promotion) не имели ни человекочитаемых объяснений, ни одного места,
где видно «что сейчас знает система». ml_status отвечает на вопросы:

- сколько данных и какого качества (сессии/наблюдения, menu_id, даты);
- достаточно ли точек для backtest'ов diners (30/30×3) и composite (20/20×3)
  — ЧЕСТНО: считается по фактическим точкам каждого модуля;
- какие модели-карточки сохранены (последние версии + измеренный MAE);
- какое последнее решение promotion gate зафиксировано.

ТОЛЬКО ЧТЕНИЕ: ничего не считает заново, ничего не пишет, предложений не
создаёт. Вердикты о точности на будущих данных не даются.
"""
from __future__ import annotations

from .forecasting.composite import MIN_TRAIN as COMPOSITE_MIN_TRAIN
from .forecasting.diners import MIN_TRAIN_ROWS as DINERS_MIN_TRAIN_ROWS

DINERS_WINDOW_DAYS = 30      # конфигурация по умолчанию CLI forecast-diners
DINERS_MIN_NEEDED = DINERS_MIN_TRAIN_ROWS + 2 * DINERS_WINDOW_DAYS
COMPOSITE_WINDOW_DAYS = 20   # конфигурация по умолчанию CLI forecast-composite
COMPOSITE_MIN_NEEDED = COMPOSITE_MIN_TRAIN + 2 * COMPOSITE_WINDOW_DAYS


def _sufficiency(n: int, needed: int) -> str:
    return (f"OK ({n} точек >= {needed})"
            if n >= needed else f"DATA_NOT_READY ({n} точек < {needed})")


def ml_status(store, school_id: str, *, meal_type: str = "LUNCH") -> dict:
    """Собирает read-only сводку. Никаких записей и прогнозов."""
    sessions_all = store.fetch_sessions(school_id=school_id)
    sessions = [s for s in sessions_all if s.meal_type == meal_type]
    observations = store.fetch_observations(school_id=school_id)

    # diners: точки = сессии с числом едоков > 0 (посещаемость или выдача)
    n_diner_points = sum(
        1 for s in sessions
        if ((s.attendance_count if s.attendance_count is not None
             else s.meals_served) or 0) > 0)
    # composite: точки = дни, где есть и едоки, и масса (из наблюдений)
    obs_mass_by_session: set[str] = set()
    for o in observations:
        if (o.served_mass_g or 0) > 0 or (o.plate_waste_g or 0) > 0:
            obs_mass_by_session.add(o.session_id)
    n_composite_points = sum(1 for s in sessions
                             if s.session_id in obs_mass_by_session
                             and ((s.attendance_count if s.attendance_count
                                   is not None else s.meals_served) or 0) > 0)

    # последние версии карточек моделей (по model_id)
    latest: dict[str, dict] = {}
    promotion_latest: dict | None = None
    for c in store.get_model_cards():
        model_id = c["model_id"]
        if model_id.startswith("sra.promotion."):
            if promotion_latest is None or c["version"] > promotion_latest["version"]:
                promotion_latest = c
            continue
        if model_id not in latest or c["version"] > latest[model_id]["version"]:
            latest[model_id] = c
    latest_cards = [
        {"model_id": c["model_id"], "version": c["version"],
         "model_name": c["model_name"], "metrics": c["metrics"],
         "created_at": c["created_at"]}
        for c in sorted(latest.values(), key=lambda x: x["model_id"])]

    dates = sorted(s.service_date for s in sessions)
    warnings: list[str] = []
    if sessions and not any(s.menu_id for s in sessions):
        warnings.append("menu_id не заполнен ни в одной сессии: модели с "
                        "меню-историей (menu_history) будут недоступны")
    if sessions_all and not sessions:
        warnings.append(f"сессий с meal_type={meal_type} нет "
                        f"(всего сессий школы: {len(sessions_all)})")

    return {
        "read_only": True,
        "environment": store.environment,
        "school_id": school_id,
        "meal_type": meal_type,
        "dataset_version": store.get_dataset_version(),
        "sessions": len(sessions),
        "sessions_with_menu_id": sum(1 for s in sessions if s.menu_id),
        "sessions_with_attendance": sum(1 for s in sessions
                                        if s.attendance_count is not None),
        "observations": len(observations),
        "observations_with_plate_waste": sum(1 for o in observations
                                             if o.plate_waste_g is not None),
        "date_from": dates[0].isoformat() if dates else None,
        "date_to": dates[-1].isoformat() if dates else None,
        "diner_points": n_diner_points,
        "diners_sufficiency": _sufficiency(n_diner_points, DINERS_MIN_NEEDED),
        "composite_points": n_composite_points,
        "composite_sufficiency": _sufficiency(n_composite_points,
                                              COMPOSITE_MIN_NEEDED),
        "latest_cards": latest_cards,
        "latest_promotion": ({"model_id": promotion_latest["model_id"],
                              "version": promotion_latest["version"],
                              "metrics": promotion_latest["metrics"]}
                             if promotion_latest else None),
        "warnings": warnings,
    }
