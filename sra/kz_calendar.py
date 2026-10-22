"""Ориентировочный учебный календарь школ Казахстана.

ЧЕСТНОСТЬ: даты каникул/праздников здесь ОРИЕНТИРОВОЧНЫЕ (типовые окна);
точные даты задаются приказами УО/школы и меняются по годам. Для точности
заполните data/kz-calendar.json (см. README раздел «Календарь»), источник —
приказ управления образования. Модуль используется для:
  - контроля пропусков измерений (учебный день без сессии — подозрителен);
  - подавления ложных «нет данных» в каникулы;
  - отчётов о полноте данных.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

# Государственные праздники РК (фиксированные, месяц-день); переносимые
# отмечать в конфиге по конкретному году.
DEFAULT_HOLIDAYS = ["01-01", "01-02", "03-08", "03-21", "03-22", "03-23",
                    "05-01", "05-07", "05-09", "07-06", "08-30", "10-25",
                    "12-16"]
# Типовые окна каникул (месяц-день, включительно); ЛЕТНИЕ — отдельно.
DEFAULT_VACATIONS = [{"from": "12-25", "to": "01-05"},   # зимние (ориентир)
                     {"from": "03-24", "to": "03-29"},   # весенние (ориентир)
                     {"from": "10-26", "to": "10-31"}]   # осенние (ориентир)
DEFAULT_SUMMER = {"from": "06-01", "to": "08-31"}

DEFAULT_CONFIG = {
    "holidays": DEFAULT_HOLIDAYS,
    "vacations": DEFAULT_VACATIONS,
    "summer": DEFAULT_SUMMER,
    "note": ("ориентировочные даты; точные — по приказу УО/школы, "
             "редактируйте data/kz-calendar.json и указывайте источник"),
}


def load_calendar(path=None) -> dict:
    """Конфиг календаря: явный путь → SRA_KZ_CALENDAR → data/kz-calendar.json
    (ближайший существующий вверх от cwd) → значения по умолчанию."""
    if path is None:
        import os
        path = os.environ.get("SRA_KZ_CALENDAR")
    if path is None:
        p = Path.cwd()
        for cand in [p / "data" / "kz-calendar.json"] +                 [pp / "data" / "kz-calendar.json" for pp in p.parents]:
            if cand.exists():
                path = cand
                break
    if path is not None:
        p = Path(path)
        if p.exists():
            cfg = json.loads(p.read_text(encoding="utf-8"))
            cfg.setdefault("note", DEFAULT_CONFIG["note"])
            cfg["_source"] = str(p)
            return cfg
        raise FileNotFoundError(f"файл календаря не найден: {p}")
    cfg = dict(DEFAULT_CONFIG)
    cfg["_source"] = "defaults (файл конфига не найден; даты ОРИЕНТИРОВОЧНЫЕ)"
    return cfg


def _md(d: date) -> str:
    return f"{d.month:02d}-{d.day:02d}"


def _in_window(d: date, w: dict) -> bool:
    """Окно MM-DD..MM-DD, возможно через Новый год (12-25..01-05)."""
    frm, to = w["from"], w["to"]
    if frm <= to:
        return frm <= _md(d) <= to
    return _md(d) >= frm or _md(d) <= to


def non_school_reason(d: date, cal: dict | None = None) -> str | None:
    """Причина неучебного дня или None (учебный день). Выходные — тоже причина."""
    cal = cal or DEFAULT_CONFIG
    if d.weekday() >= 5:
        return "выходной (сб/вс)"
    if _in_window(d, cal.get("summer", DEFAULT_SUMMER)):
        return "летние каникулы (ориентир)"
    for h in cal.get("holidays", []):
        if _md(d) == h:
            return f"государственный праздник {h}"
    for w in cal.get("vacations", []):
        if _in_window(d, w):
            return f"каникулы {w['from']}..{w['to']} (ориентир)"
    return None


def is_school_day(d: date, cal: dict | None = None) -> tuple[bool, str | None]:
    r = non_school_reason(d, cal)
    return (r is None, r)


def school_days_between(d_from: date, d_to: date, cal: dict | None = None) -> int:
    n, d = 0, d_from
    while d <= d_to:
        if is_school_day(d, cal)[0]:
            n += 1
        d += timedelta(days=1)
    return n
