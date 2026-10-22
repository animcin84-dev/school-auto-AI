"""Адаптер финского датасета Zenodo 20825233 (школа, Западная Финляндия, 2024–2025).

Источник: https://zenodo.org/records/20825233 — ежедневные измерения одной
школы, 155 учебных дней (08.2024–04.2025), лицензия — открытая запись
(тип лицензии подтверждается на странице записи при скачивании).

Переменные (по описанию записи):
  PW         — суммарные отходы на тарелках за день, кг;
  PW/g/diner — то же на едока, г;
  KSW        — отходы кухни И раздачи вместе, кг (в контракт НЕ кладётся:
               категории production/serving в датасете неразделимы);
  KSW/g/diner, DINERS (едоки за день), MENU (идентификатор меню, цикл 1–25).

Фазы: baseline = первые 135 строк данных, intervention = последние 20
(описание записи; файла-колонки фаз по описанию нет) — правило фиксируется
в отчёте импорта.

Отображение на контракт: одна сессия = учебный день (LUNCH, Europe/Helsinki),
одно наблюдение dish_id="ALL" (агрегат дня). served/prepared массы датасетом
НЕ измерялись → прогноз спроса в граммах на этих данных невозможен: это
честное ограничение, а не сбой. DINERS → attendance_count и meals_served
(все едоки получили обед; спрос при этом не наблюдается: закончиться еда
могла незаметно — demand_censored=unknown).
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from .. import enums
from ..contracts import ObservationRecord, SessionRecord
from .xlsx_min import map_columns, parse_any_date

SOURCE_REF = "zenodo:20825233:v1"
SCHOOL_DEFAULT = "FI-ZEN-20825233"
TIMEZONE = "Europe/Helsinki"
N_BASELINE_ROWS = 135  # описание записи: 135 baseline + 20 intervention

# Фактическая шапка файла v1: DATE (сериал Excel), MENU, "KSW kg", "PW kg",
# "PW g", DINERS, "PW/g/diner", "KSW/g/diner" (+ пустые служебные колонки).
WANTED = {
    "date": ("date", "day", "paiva", "pv", "datum"),
    "menu": ("menu", "menuitem", "menuid"),
    "pw_kg": ("pwkg", "pw"),
    "pw_g": ("pwg",),
    "ksw_kg": ("kswkg", "ksw"),
    "ksw_g": ("kswg",),
    "pw_per_diner": ("pw/g/diner", "pwperdiner"),
    "ksw_per_diner": ("ksw/g/diner", "kswperdiner"),
    "diners": ("diners",),
}

LIMITATIONS = [
    "served/prepared массы не измерялись: UneatenIndex, OverproductionRate, "
    "CostWastePerMeal и прогноз спроса в граммах останутся UNAVAILABLE/DATA_NOT_READY "
    "с причинами — это честный результат",
    "KSW (кухня+раздача вместе) не импортируется: контракт требует раздельные "
    "категории production/serving; исходные значения остаются в xlsx-источнике",
    "DINERS — едоки за день; спрос не наблюдается (demand_censored=unknown), "
    "пропуски и отказы от еды на индивидуальном уровне не видны",
    "это ЧУЖИЕ реальные измерения (Финляндия, не школа КЗ): не переносить "
    "значения на другие школы и не использовать для заявлений об эффекте",
]


def map_workbook(wb: dict, *, school_id: str = SCHOOL_DEFAULT,
                 recorded_at: datetime | None = None) -> dict:
    """Читает workbook из read_workbook() → записи контракта + отчёт."""
    if not wb.get("sheets"):
        raise ValueError("в рабочей книге нет листов")
    sheet = wb["sheets"][0]
    recorded = recorded_at or datetime.now(timezone.utc)
    tzinfo = ZoneInfo(TIMEZONE)
    cm = map_columns(sheet["rows"], WANTED, min_hits=2)
    report: dict = {
        "dataset": "zenodo:20825233 (Food waste dataset: secondary school in Finland 2024-2025)",
        "sheet": sheet["name"],
        "source_ref": SOURCE_REF,
        "school_id": school_id,
        "environment": enums.ENV_OPEN,
        "header_row": cm["header_row"],
        "header_raw": cm["header_raw"],
        "matched_columns": cm["columns"],
        "unmatched_keys": cm["unmatched_keys"],
        "rows_total": 0, "rows_imported": 0, "rows_skipped": [],
        "phase_rule": (f"по позиции строки данных: первые {N_BASELINE_ROWS} — BASELINE, "
                       "далее INTERVENTION (описание записи; колонки фаз в файле нет)"),
        "limitations": LIMITATIONS,
        "license_note": ("запись открыта; точный тип лицензии подтверждается на странице "
                         "записи при скачивании (ожидается CC BY — процитировать авторов)"),
    }
    if cm["header_row"] is None:
        report["rows_skipped"].append({"reason": "строка заголовка не найдена"})
        return {"sessions": [], "observations": [], "report": report}

    cols = cm["columns"]
    sessions: list[SessionRecord] = []
    observations: list[ObservationRecord] = []
    data_rows = 0
    for i, row in enumerate(sheet["rows"]):
        if i <= cm["header_row"]:
            continue
        if not row or all(v is None or v == "" for v in row):
            continue
        report["rows_total"] += 1
        get = lambda key: (row[cols[key]] if key in cols and cols[key] < len(row) else None)
        d = parse_any_date(get("date"))
        pw_kg = get("pw_kg")
        pw_g_col = get("pw_g")
        diners = get("diners")
        if d is None:
            report["rows_skipped"].append({"row": i + 1, "reason": "дата не распознана",
                                           "raw_date": str(get("date"))})
            continue
        if pw_g_col is not None and not isinstance(pw_g_col, (str, bool)) \
                and float(pw_g_col) >= 0:
            pw_g = round(float(pw_g_col), 3)
            if pw_kg is not None and not isinstance(pw_kg, (str, bool)):
                if abs(float(pw_kg) * 1000.0 - pw_g) > 1.0:
                    report.setdefault("crosscheck_warnings", []).append(
                        {"row": i + 1, "date": d.isoformat(),
                         "detail": f"PW kg*1000={round(float(pw_kg)*1000, 3)} != PW g={pw_g}"})
        elif pw_kg is not None and not isinstance(pw_kg, (str, bool)) and float(pw_kg) >= 0:
            pw_g = round(float(pw_kg) * 1000.0, 3)
        else:
            report["rows_skipped"].append({"row": i + 1, "date": d.isoformat(),
                                           "reason": f"PW нет числа: kg={pw_kg!r}, g={pw_g_col!r}"})
            continue
        diners_int = int(diners) if diners is not None and not isinstance(diners, (str, bool)) else None
        menu = get("menu")
        phase = enums.PHASE_BASELINE if data_rows < N_BASELINE_ROWS else enums.PHASE_INTERVENTION
        data_rows += 1
        iso = d.isoformat()
        observed = datetime(d.year, d.month, d.day, 12, 0, tzinfo=tzinfo)
        session_id = f"SES-{school_id}-{iso}-LUNCH"
        sessions.append(SessionRecord(
            session_id=session_id, school_id=school_id, service_date=d,
            timezone=TIMEZONE, meal_type="LUNCH",
            attendance_count=diners_int, meals_requested=None, meals_served=diners_int,
            demand_censored=enums.CENSOR_UNKNOWN, evidence_status=enums.REAL_OBSERVED,
            study_phase=phase, source_ref=SOURCE_REF, observed_at=observed,
            recorded_at=recorded,
            notes=(f"чужое измерение (Zenodo 20825233, Финляндия); DINERS={diners_int} "
                   f"(едоки за день); MENU={menu} (идентификатор меню, цикл 1–25)")))
        observations.append(ObservationRecord(
            observation_id=f"OBS-{session_id}-ALL", session_id=session_id,
            dish_id="ALL", prepared_mass_g=None, served_mass_g=None,
            production_waste_g=None, serving_waste_g=None, plate_waste_g=pw_g,
            avoidable_waste_g=None, avoidability_status=enums.AV_UNCLASSIFIED,
            evidence_status=enums.REAL_OBSERVED, source_ref=SOURCE_REF,
            observed_at=observed, recorded_at=recorded,
            notes=(f"PW за день (kg-колонка файла: {pw_kg}); dish_id=ALL — агрегат дня, "
                   "разбивка по блюдам в датасете отсутствует")))
        report["rows_imported"] += 1
    report["n_sessions"] = len(sessions)
    report["n_observations"] = len(observations)
    return {"sessions": sessions, "observations": observations, "report": report}
