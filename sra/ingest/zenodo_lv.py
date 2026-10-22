"""Адаптер латвийского датасета Zenodo 18441715 (Резекне, 1–7 классы, дек. 2023).

Источник: https://zenodo.org/records/18441715, лицензия CC BY 4.0 (в файле:
лист Meta-data → «Term of use/license: CC BY 4.0»). Прямые взвешивания
индивидуальных тарелок; частично pre-served модель; общие ёмкости исключены
авторами; лишние порции отсутствующих отфильтрованы.

ФАКТИЧЕСКАЯ структура файла v1 (отличается от «indicative» Read_me):
  лист "Meta-data" — метаданные (читается в отчёт, не импортируется);
  листы "School 1".."School 4" — блоки дней:
    строка дня:      "Monday lunch (December 11, 2023)" | число участников;
    строки блюд:     название | Food served (total, g) | Plate Waste (total, g);
    строка итога:    "Monday total" | served | waste  (кросс-чек суммы блюд);
    сводный блок:    Grades 1-4 / Grades 5-7 (агрегат ЗА НЕДЕЛЮ, без дат) —
                     читается в отчёт, НЕ импортируется (нечего крепить к сессии).

Отображение на контракт: сессия = (школа, дата, LUNCH, Europe/Riga) с
attendance_count = участники дня; наблюдение = блюдо дня: dish_id = нормализованное
название, served_mass_g, plate_waste_g. Prepared-массы нет → OverproductionRate и
прогноз — UNAVAILABLE/DATA_NOT_READY с причинами. UneatenIndex, ServiceLevel,
WastePerMeal считаются системой по своим правилам доступности.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from .. import enums
from ..contracts import ObservationRecord, SessionRecord
from .xlsx_min import parse_any_date

SOURCE_REF = "zenodo:18441715:v1"
SCHOOL_PREFIX_DEFAULT = "LV-ZEN-18441715"
TIMEZONE = "Europe/Riga"

_DAY_RE = re.compile(
    r"^(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\s+(?:lunch|Lunch)\s*"
    r"\((January|February|March|April|May|June|July|August|September|October|"
    r"November|December)\s+(\d{1,2}),\s*(\d{4})\)")
_MONTHS = {m: i + 1 for i, m in enumerate(
    ("January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December"))}
_TOTAL_RE = re.compile(r"^\w+day\s+total$", re.I)

LIMITATIONS = [
    "измерены только индивидуальные отходы с тарелок: общие ёмкости (салат, суп, "
    "хлеб) исключены авторами — значения не сопоставимы с полными отходами кухни",
    "prepared-массы нет: OverproductionRate и прогноз спроса останутся "
    "UNAVAILABLE/DATA_NOT_READY с причинами",
    "сводный блок Grades 1-4/5-7 — агрегат за неделю без дат: в отчёт, но не в БД",
    "это ЧУЖИЕ реальные измерения (Латвия, не школа КЗ): не переносить значения "
    "на другие школы и не использовать для заявлений об эффекте",
]


def _num(v):
    if v is None or isinstance(v, bool) or isinstance(v, str):
        return None
    return float(v)


def _dish_label(raw) -> str:
    s = " ".join(str(raw).split())  # схлопнуть \n и пробелы
    out = []
    for ch in s:
        out.append(ch if (ch.isalnum() or ch in " -") else "-")
    return "".join(out)[:40].strip("-") or "DISH"


def _parse_sheet(rows: list[list], school_id: str, *, recorded: datetime,
                 tzinfo, report: dict):
    """Парсит лист школы → (sessions, observations); кросс-чек итогов дня."""
    sessions: dict[str, SessionRecord] = {}
    observations: list[ObservationRecord] = []
    current_day = None  # (session_id, date, running_served, running_waste, row)
    for i, row in enumerate(rows):
        cells = [c for c in row if c not in (None, "")]
        if not cells:
            continue
        first = str(cells[0]).strip() if cells[0] is not None else ""
        m = _DAY_RE.match(first)
        if m:
            d = date(int(m.group(4)), _MONTHS[m.group(2)], int(m.group(3)))
            participants = _num(cells[1]) if len(cells) > 1 else None
            iso = d.isoformat()
            session_id = f"SES-{school_id}-{iso}-LUNCH"
            observed = datetime(d.year, d.month, d.day, 13, 0, tzinfo=tzinfo)
            if session_id in sessions:
                report["rows_skipped"].append({"row": i + 1, "reason": "дубликат дня",
                                               "date": iso})
                current_day = None
                continue
            sessions[session_id] = SessionRecord(
                session_id=session_id, school_id=school_id, service_date=d,
                timezone=TIMEZONE, meal_type="LUNCH",
                attendance_count=(int(participants) if participants is not None else None),
                meals_requested=None, meals_served=None,
                demand_censored=enums.CENSOR_UNKNOWN, evidence_status=enums.REAL_OBSERVED,
                study_phase=None, source_ref=SOURCE_REF, observed_at=observed,
                recorded_at=recorded,
                notes=(f"чужое измерение (Zenodo 18441715, Резекне, Латвия); "
                       f"{first}; участников взвешивания: {participants}"))
            current_day = [session_id, 0.0, 0.0, i]
            continue
        if current_day is None:
            continue
        if _TOTAL_RE.match(first):
            total_served, total_waste = _num(cells[1]) if len(cells) > 1 else None, \
                _num(cells[2]) if len(cells) > 2 else None
            if total_served is not None and abs(total_served - current_day[1]) > 0.5:
                report.setdefault("crosscheck_warnings", []).append(
                    {"sheet_row": current_day[3] + 1, "detail":
                        f"{first}: served в файле {total_served} != сумма блюд "
                        f"{round(current_day[1], 3)}"})
            if total_waste is not None and abs(total_waste - current_day[2]) > 0.5:
                report.setdefault("crosscheck_warnings", []).append(
                    {"sheet_row": current_day[3] + 1, "detail":
                        f"{first}: waste в файле {total_waste} != сумма блюд "
                        f"{round(current_day[2], 3)}"})
            current_day = None
            continue
        served = _num(cells[1]) if len(cells) > 1 else None
        waste = _num(cells[2]) if len(cells) > 2 else None
        if waste is None and served is None:
            continue  # служебная строка сводного блока и пр.
        session_id = current_day[0]
        dish = _dish_label(cells[0])
        observations.append(ObservationRecord(
            observation_id=f"OBS-{session_id}-{dish}", session_id=session_id,
            dish_id=dish, prepared_mass_g=None,
            served_mass_g=served, production_waste_g=None, serving_waste_g=None,
            plate_waste_g=(waste if waste is not None else None),
            avoidable_waste_g=None, avoidability_status=enums.AV_UNCLASSIFIED,
            evidence_status=enums.REAL_OBSERVED, source_ref=SOURCE_REF,
            observed_at=sessions[session_id].observed_at, recorded_at=recorded,
            notes="прямое взвешивание индивидуальных тарелок; частично pre-served "
                  "модель; агрегат по школе за день"))
        current_day[1] += served or 0.0
        current_day[2] += waste or 0.0
    return list(sessions.values()), observations


def map_workbook(wb: dict, *, school_prefix: str = SCHOOL_PREFIX_DEFAULT,
                 recorded_at: datetime | None = None) -> dict:
    if not wb.get("sheets"):
        raise ValueError("в рабочей книге нет листов")
    recorded = recorded_at or datetime.now(timezone.utc)
    tzinfo = ZoneInfo(TIMEZONE)
    report: dict = {
        "dataset": ("zenodo:18441715 (Filtered Plate Waste Measurement Data from "
                    "Latvian Schools, Rezekne, December 2023)"),
        "source_ref": SOURCE_REF,
        "school_prefix": school_prefix,
        "environment": enums.ENV_OPEN,
        "license": "CC BY 4.0 (лист Meta-data файла и Read_me записи)",
        "sheets_seen": [], "rows_total": 0, "rows_imported": 0, "rows_skipped": [],
        "crosscheck_warnings": [],
        "summary_block_note": ("сводный блок Grades 1-4/5-7 (агрегат за неделю) "
                               "читается в отчёт, но не импортируется: нет дат"),
        "limitations": LIMITATIONS,
    }
    # meta-лист: полезно для аудита (заголовок, лицензия, цитирование)
    meta_rows = []
    sessions_all: list[SessionRecord] = []
    observations_all: list[ObservationRecord] = []
    for sheet in wb["sheets"]:
        name = sheet["name"]
        report["sheets_seen"].append(name)
        if name.lower().startswith("meta"):
            meta_rows = [r for r in sheet["rows"]
                         if r and any(c not in (None, "") for c in r)]
            continue
        m = re.match(r"^School\s*(\d+)$", name, re.I)
        if not m:
            report["rows_skipped"].append({"sheet": name,
                                           "reason": "лист не School N и не Meta-data"})
            continue
        school_id = f"{school_prefix}-S{m.group(1)}"
        sessions, observations = _parse_sheet(sheet["rows"], school_id,
                                              recorded=recorded, tzinfo=tzinfo,
                                              report=report)
        report["rows_total"] += len(observations)
        report["rows_imported"] += len(observations)
        sessions_all.extend(sessions)
        observations_all.extend(observations)
    report["meta_excerpt"] = [[str(c)[:60] for c in r if c not in (None, "")][:2]
                              for r in meta_rows[:10]]
    report["n_sessions"] = len(sessions_all)
    report["n_observations"] = len(observations_all)
    return {"sessions": sessions_all, "observations": observations_all,
            "report": report}
