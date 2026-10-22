"""Адаптер латвийского датасета Zenodo 18441715 (Резекне, 1–7 классы, дек. 2023).

Источник: https://zenodo.org/records/18441715 — фильтрованные данные прямых
взвешиваний в 4 школах, лицензия CC BY 4.0 (подтверждена в Read_me записи).
Модель обслуживания — частично pre-served (основное блюдо уже на тарелке);
измерялись ТОЛЬКО индивидуальные отходы с тарелок учеников (общие ёмкости
исключены авторами); лишние порции отсутствующих фильтровались.

Переменные (по Read_me, «indicative» — фактические заголовки проверяются
по файлу и попадают в отчёт):
  School_ID, Measurement_period, Student_group, School_level, Age_group,
  Meal_type, Served_food_g, Plate_waste_g.

Отображение на контракт: сессия = (школа, дата/начало периода, LUNCH,
Europe/Riga); наблюдение = группа: dish_id="PRE-SERVED-<группа>" с
served_mass_g=Served_food_g и plate_waste_g=Plate_waste_g. Prepared-массы
в датасете нет → UneatenIndex/прогноз — UNAVAILABLE с причинами. Если
Measurement_period не содержит даты, а только месяц/период — используется
первый день периода С ЯВНОЙ пометкой допущения в notes/quality_flags
(агрегат периода не является дневным измерением).
"""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from .. import enums
from ..contracts import ObservationRecord, SessionRecord
from .xlsx_min import map_columns, normalize_header, parse_any_date

SOURCE_REF = "zenodo:18441715:v1"
SCHOOL_PREFIX_DEFAULT = "LV-ZEN-18441715"
TIMEZONE = "Europe/Riga"

WANTED = {
    "school_id": ("schoolid", "school", "schoolcode"),
    "period": ("measurementperiod", "period", "date", "day", "observationperiod"),
    "group": ("studentgroup", "group", "grades", "grade", "class"),
    "level": ("schoollevel", "level"),
    "age": ("agegroup", "age"),
    "meal": ("mealtype", "meal"),
    "served": ("servedfoodg", "servedfood", "servedg", "servedmassg"),
    "plate": ("platewasteg", "platewaste", "wasteg", "platewasteamountg"),
}

LIMITATIONS = [
    "измерены только индивидуальные отходы с тарелок: общие ёмкости (салат, суп, "
    "хлеб) исключены авторами — TotalMeasuredWaste здесь НЕ сопоставим с полной "
    "массой отходов кухни",
    "prepared-массы нет: UneatenIndex, OverproductionRate, CostWastePerMeal и "
    "прогноз спроса останутся UNAVAILABLE/DATA_NOT_READY с причинами",
    "Served_food_g/Plate_waste_g — агрегаты по группам; число порций в день из "
    "файла не следует (пропуски фильтровались авторами) → WastePerMeal не считать",
    "если Measurement_period — только месяц/период, service_date = первый день "
    "периода (явное допущение, помечается флагом PERIOD_AGGREGATE_AS_DATE)",
    "это ЧУЖИЕ реальные измерения (Латвия, не школа КЗ): не переносить значения "
    "на другие школы и не использовать для заявлений об эффекте",
]


def _group_label(raw) -> str:
    s = str(raw).strip() if raw is not None else ""
    if not s:
        return "ALL"
    return "".join(ch if (ch.isalnum() or ch in "-") else "-" for ch in s)[:24]


def map_workbook(wb: dict, *, school_prefix: str = SCHOOL_PREFIX_DEFAULT,
                 recorded_at: datetime | None = None) -> dict:
    if not wb.get("sheets"):
        raise ValueError("в рабочей книге нет листов")
    sheet = wb["sheets"][0]
    recorded = recorded_at or datetime.now(timezone.utc)
    tzinfo = ZoneInfo(TIMEZONE)
    cm = map_columns(sheet["rows"], WANTED, min_hits=2)
    report: dict = {
        "dataset": ("zenodo:18441715 (Filtered Plate Waste Measurement Data from "
                    "Latvian Schools, Rezekne, December 2023)"),
        "sheet": sheet["name"],
        "source_ref": SOURCE_REF,
        "school_prefix": school_prefix,
        "environment": enums.ENV_OPEN,
        "license": "CC BY 4.0 (Read_me записи)",
        "header_row": cm["header_row"],
        "header_raw": cm["header_raw"],
        "matched_columns": cm["columns"],
        "unmatched_keys": cm["unmatched_keys"],
        "rows_total": 0, "rows_imported": 0, "rows_skipped": [],
        "limitations": LIMITATIONS,
    }
    if cm["header_row"] is None:
        report["rows_skipped"].append({"reason": "строка заголовка не найдена"})
        return {"sessions": [], "observations": [], "report": report}

    cols = cm["columns"]
    sessions: list[SessionRecord] = []
    observations: list[ObservationRecord] = []
    session_index: dict[tuple, SessionRecord] = {}
    for i, row in enumerate(sheet["rows"]):
        if i <= cm["header_row"]:
            continue
        if not row or all(v is None or v == "" for v in row):
            continue
        report["rows_total"] += 1
        get = lambda key: (row[cols[key]] if key in cols and cols[key] < len(row) else None)
        school_raw = get("school_id")
        d = parse_any_date(get("period"))
        served = get("served")
        plate = get("plate")
        if school_raw is None or str(school_raw).strip() == "":
            report["rows_skipped"].append({"row": i + 1, "reason": "пуст School_ID"})
            continue
        if d is None:
            report["rows_skipped"].append({"row": i + 1,
                                           "reason": "период/дата не распознаны",
                                           "raw_period": str(get("period"))})
            continue
        if plate is None or isinstance(plate, (str, bool)) or float(plate) < 0:
            report["rows_skipped"].append({"row": i + 1, "date": d.isoformat(),
                                           "reason": f"Plate_waste_g не число: {plate!r}"})
            continue
        school_id = f"{school_prefix}-{_group_label(school_raw)}"
        iso = d.isoformat()
        observed = datetime(d.year, d.month, d.day, 13, 0, tzinfo=tzinfo)
        session_id = f"SES-{school_id}-{iso}-LUNCH"
        key = (school_id, iso)
        period_raw = str(get("period")).strip() if get("period") is not None else ""
        month_aggregate = not any(sep in period_raw for sep in ("-", ".")
                                  ) or len(normalize_header(period_raw)) <= 7
        if key not in session_index:
            session_index[key] = SessionRecord(
                session_id=session_id, school_id=school_id, service_date=d,
                timezone=TIMEZONE, meal_type="LUNCH",
                attendance_count=None, meals_requested=None, meals_served=None,
                demand_censored=enums.CENSOR_UNKNOWN, evidence_status=enums.REAL_OBSERVED,
                study_phase=None, source_ref=SOURCE_REF, observed_at=observed,
                recorded_at=recorded,
                notes=(f"чужое измерение (Zenodo 18441715, Резекне, Латвия); "
                       f"Measurement_period={period_raw!r}"))
            if month_aggregate:
                session_index[key].quality_flags.append("PERIOD_AGGREGATE_AS_DATE")
                session_index[key].notes += ("; допущение: service_date = первый день "
                                             "периода (агрегат, не дневное измерение)")
            sessions.append(session_index[key])
        group = _group_label(get("group"))
        observations.append(ObservationRecord(
            observation_id=f"OBS-{session_id}-G{group}", session_id=session_id,
            dish_id=f"PRE-SERVED-{group}",
            prepared_mass_g=None,
            served_mass_g=(float(served) if served is not None and
                           not isinstance(served, (str, bool)) else None),
            production_waste_g=None, serving_waste_g=None,
            plate_waste_g=float(plate),
            avoidable_waste_g=None, avoidability_status=enums.AV_UNCLASSIFIED,
            evidence_status=enums.REAL_OBSERVED, source_ref=SOURCE_REF,
            observed_at=observed, recorded_at=recorded,
            notes=(f"Student_group={get('group')!r}; School_level={get('level')!r}; "
                   "модель: частично pre-served; взвешивание индивидуальных тарелок")))
        report["rows_imported"] += 1
    report["n_sessions"] = len(sessions)
    report["n_observations"] = len(observations)
    return {"sessions": sessions, "observations": observations, "report": report}
