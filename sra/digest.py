"""Дайджест для директора/бракеражной комиссии (AUDIT §D.16).

Одним вызовом собирает ЧЕСТНУЮ сводку за окно (по умолчанию 7 учебных дней):
метрики окна (агрегаты числителей/знаменателей, не проценты), качество
данных, проверку порций по Санправилам, алерты нацстандарта и ML-статус.
Каждая секция собирается независимо: если секция недоступна — в дайджесте
причина, а не число и не молчание.

ТОЛЬКО ЧТЕНИЕ: дайджест ничего не пишет в хранилище, не создаёт предложений
и не исполняет действий. Русский текст — explain_digest (sra/explain.py).
"""
from __future__ import annotations

from datetime import date, timedelta

from .norms import check_portions
from .status import ml_status


def _window_metric(sessions, observations, date_from: date, date_to: date,
                   meal_type: str) -> dict:
    """Σ отходов тарелок / Σ едоков по окну (агрегаты, не среднее процентов)."""
    ses = [s for s in sessions
           if s.meal_type == meal_type
           and date_from <= s.service_date <= date_to]
    ses_ids = {s.session_id for s in ses}
    pw = sum(o.plate_waste_g for o in observations
             if o.session_id in ses_ids and o.plate_waste_g is not None)
    n_pw_obs = sum(1 for o in observations
                   if o.session_id in ses_ids and o.plate_waste_g is not None)
    diners = sum((s.attendance_count if s.attendance_count is not None
                  else s.meals_served) or 0 for s in ses)
    days = len(ses)
    base = {"days_with_sessions": days,
            "days_with_plate_waste": n_pw_obs,
            "plate_waste_g_total": round(pw, 1),
            "diners_total": diners}
    if not ses:
        return {"status": "UNAVAILABLE",
                "reason": f"за окно нет сессий ({date_from}…{date_to})", **base}
    if diners <= 0 or pw <= 0:
        return {"status": "UNAVAILABLE",
                "reason": ("недостаточно измерений в окне: нужна и масса "
                           "отходов, и число едоков; пустое поле = «не "
                           "измеряли»"), **base}
    return {"status": "OK",
            "plate_waste_per_diner_g": round(pw / diners, 1), **base}


def build_digest(store, school_id: str, *, meal_type: str = "LUNCH",
                 days: int = 7, uneaten_threshold_pct: float = 30.0,
                 age_group: str = "6-11") -> dict:
    """Read-only дайджест: окно, качество, порции, алерты, ML-статус."""
    from .monitoring import nutrition_alerts
    from .quality import data_quality_report

    sessions_all = store.fetch_sessions(school_id=school_id)
    sessions = [s for s in sessions_all if s.meal_type == meal_type]
    observations = store.fetch_observations(school_id=school_id)

    if sessions:
        last_day = max(s.service_date for s in sessions)
        date_from = last_day - timedelta(days=days - 1)
        window = {"date_from": date_from.isoformat(),
                  "date_to": last_day.isoformat(), "requested_days": days}
    else:
        window = {"date_from": None, "date_to": None, "requested_days": days}

    window_metric = _window_metric(sessions, observations,
                                   date_from if sessions else date.today(),
                                   last_day if sessions else date.today(),
                                   meal_type)

    # Качество: берём только компактную сводку проверок (полный отчёт —
    # отдельной командой quality-check).
    q = data_quality_report(store, school_id)
    quality_brief = {
        "status": q.get("status"),
        "reason": q.get("reason"),
        "checks": [{"name": c.get("check"), "ok": c.get("ok"),
                    "message": (c.get("details") or {}).get("note")}
                   for c in (q.get("checks") or [])],
    }

    norms_report = check_portions(observations, age_group=age_group,
                                  sessions=sessions_all, per="total")
    alerts = nutrition_alerts(store, school_id,
                              uneaten_threshold_pct=uneaten_threshold_pct,
                              meal_type=meal_type)
    ml = ml_status(store, school_id, meal_type=meal_type)

    return {
        "read_only": True,
        "environment": store.environment,
        "school_id": school_id,
        "meal_type": meal_type,
        "data_nature": (sessions[0].evidence_status if sessions else None),
        "window": window,
        "window_metric": window_metric,
        "quality": quality_brief,
        "norms": norms_report,
        "alerts": alerts,
        "ml_status": ml,
        "disclaimer": ("сводка только читает данные; система не исполняет "
                       "действий — решения за бракеражной комиссией и "
                       "администрацией школы"),
    }
