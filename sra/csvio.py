"""CSV-ввод/вывод: чтение с проверкой заголовка, конвертация единиц, шаблоны.

Канонические заголовки — files templates/sessions.csv и templates/observations.csv.
Допустимое расширение: необязательная последняя колонка `notes`. Любые другие
лишние колонки — ошибка (защита от молчаливой потери данных).
"""

from __future__ import annotations

import csv
from pathlib import Path

from . import contracts, enums
from .errors import ValidationError

SESSION_HEADER = list(contracts.SESSION_FIELDS)
OBSERVATION_HEADER = list(contracts.OBSERVATION_FIELDS)

_HEADER_TO_KIND = {
    tuple(SESSION_HEADER): "sessions",
    tuple(OBSERVATION_HEADER): "observations",
}


def expected_header(kind: str) -> list[str]:
    return SESSION_HEADER if kind == "sessions" else OBSERVATION_HEADER


def check_header(header: list[str], kind: str) -> None:
    """Строгая проверка заголовка: канонический шаблон + опциональные поля
    (SESSION: notes, menu_id; OBSERVATION: notes) в любом порядке после
    обязательных. Прочие лишние колонки отклоняются."""
    base = expected_header(kind)
    if header == base:
        return
    optional = (list(contracts.SESSION_OPTIONAL_FIELDS) if kind == "sessions"
                else list(contracts.OBSERVATION_OPTIONAL_FIELDS))
    if set(base) <= set(header) and set(header) - set(base) <= set(optional):
        return
    extra = [c for c in header if c not in base + optional]
    missing = [c for c in base if c not in header]
    order_bad = extra == [] and missing == []
    raise ValidationError(
        "заголовок CSV не соответствует контракту "
        f"(kind={kind}): отсутствуют={missing}, лишние={extra}"
        + ("; порядок колонок отличается от шаблона" if order_bad else ""),
        code="E_BAD_HEADER",
        details={"expected": base, "got": header},
    )


def read_rows(path: str | Path, kind: str) -> list[dict]:
    """Читает CSV и возвращает список dict с ключом 'row_number' (1 = заголовок)."""
    p = Path(path)
    if not p.exists():
        raise ValidationError(f"файл не найден: {p}", code="E_FILE_NOT_FOUND")
    with p.open("r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.reader(fh)
        try:
            header = next(reader)
        except StopIteration:
            raise ValidationError(f"файл пуст: {p}", code="E_EMPTY_FILE")
        header = [h.strip() for h in header]
        check_header(header, kind)
        rows: list[dict] = []
        for i, raw in enumerate(reader, start=2):
            if not raw or all((c or "").strip() == "" for c in raw):
                continue  # полностью пустая строка — не запись
            if len(raw) != len(header):
                raise ValidationError(
                    f"строка {i}: число полей ({len(raw)}) не совпадает с заголовком ({len(header)})",
                    code="E_ROW_LENGTH",
                )
            row = {h: (c or "").strip() for h, c in zip(header, raw)}
            row["row_number"] = i
            rows.append(row)
    return rows


def parse_file(path: str | Path, kind: str, *, unit: str = "g"):
    """Читает и парсит файл в типизированные записи. Порядок строк сохраняется."""
    rows = read_rows(path, kind)
    parse = contracts.parse_session_row if kind == "sessions" else contracts.parse_observation_row
    return [parse(r, row_number=r["row_number"], unit=unit) for r in rows]


def convert_mass_unit(rec, *, from_unit: str) -> None:
    """Явная конвертация массовых полей записи в канонические граммы.

    Вызывается только при импорте с --unit kg. Добавляет quality_flag,
    чтобы конвертация была видна в происхождении данных.
    """
    if from_unit == "g":
        return
    if from_unit != "kg":
        raise ValidationError(f"неизвестная единица: {from_unit!r}", code="E_BAD_UNIT")
    factor = 1000.0
    fields = contracts.MASS_FIELDS if hasattr(rec, "prepared_mass_g") else []
    for f in fields:
        v = getattr(rec, f)
        if v is not None:
            setattr(rec, f, v * factor)
    if hasattr(rec, "quality_flags"):
        rec.quality_flags.append("UNIT_CONVERTED_KG_TO_G")
    rec.unit_declared = "g (converted from kg)"


def write_template(path: str | Path, kind: str) -> None:
    """Записывает пустой шаблон (только заголовок) — для новых полей/школ."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8", newline="") as fh:
        csv.writer(fh).writerow(expected_header(kind))


def source_ref_seed(source_ref: str) -> str:
    """Хелпер: выделить метку синтетического источника ('synthetic:v1:seed=42')."""
    return source_ref if source_ref.startswith("synthetic:") else ""
