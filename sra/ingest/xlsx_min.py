"""Минимальный ридер .xlsx на стандартной библиотеке (zipfile + xml.etree).

Сторонние пакеты запрещены правилами проекта, поэтому читаем OOXML напрямую.
Поддерживается то, что генерирует Excel/LibreOffice для простых таблиц:
общие строки (sharedStrings), inline-строки, числовые/логические/строковые
ячейки, пропуски ячеек, разреженные строки. Форматирование (стили) не
читается: дата, сохранённая Excel как серийное число, распознаётся
отдельным хелпером excel_serial_to_date / parse_any_date (без стилей
надёжного способа отличить дату от числа нет — решение остаётся за
вызывающим кодом и фиксируется в отчёте импорта).
"""

from __future__ import annotations

import re
import zipfile
from datetime import date, datetime, timedelta
from xml.etree import ElementTree as ET

_RID = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
_LOCALNAME = re.compile(r"\{.*\}(.*)$")
_CELLREF = re.compile(r"([A-Z]+)([0-9]+)")
EXCEL_EPOCH = date(1899, 12, 30)  # серийная система Excel (учёт високосной 1900)


def _local(tag: str) -> str:
    m = _LOCALNAME.match(tag)
    return m.group(1) if m else tag


def _cell_pos(ref: str | None):
    if not ref:
        return None
    m = _CELLREF.match(ref)
    if not m:
        return None
    col = 0
    for ch in m.group(1):
        col = col * 26 + (ord(ch) - 64)
    return col - 1, int(m.group(2)) - 1  # (столбец, строка), 0-based


def _all_text(el) -> str:
    return "".join(t.text or "" for t in el.iter() if _local(t.tag) == "t")


def _read_shared_strings(z: zipfile.ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in z.namelist():
        return []
    root = ET.fromstring(z.read("xl/sharedStrings.xml"))
    return [_all_text(si) for si in root if _local(si.tag) == "si"]


def _coerce_number(raw: str):
    try:
        f = float(raw)
    except (TypeError, ValueError):
        return raw
    if f.is_integer() and "." not in raw and "e" not in raw.lower():
        return int(f)
    return f


def _read_sheet(xml_bytes: bytes, shared: list[str]) -> list[list]:
    root = ET.fromstring(xml_bytes)
    rows_by_idx: dict[int, list] = {}
    for row in (e for e in root.iter() if _local(e.tag) == "row"):
        r_idx = int(row.get("r", "0")) - 1
        cells: dict[int, object] = {}
        for c in (e for e in row if _local(e.tag) == "c"):
            pos = _cell_pos(c.get("r"))
            if pos is None:
                continue
            col = pos[0]
            t = c.get("t", "n")
            value = None
            if t == "inlineStr":
                is_el = next((e for e in c if _local(e.tag) == "is"), None)
                value = _all_text(is_el) if is_el is not None else None
            else:
                v_el = next((e for e in c if _local(e.tag) == "v"), None)
                if v_el is not None and v_el.text is not None:
                    raw = v_el.text
                    if t == "s":
                        try:
                            value = shared[int(raw)]
                        except (ValueError, IndexError):
                            value = raw
                    elif t == "b":
                        value = raw not in ("0", "")
                    else:  # "n" — число; "str" — строковый результат формулы; "e" — ошибка
                        value = _coerce_number(raw) if t != "str" else raw
            cells[col] = value
        width = (max(cells) + 1) if cells else 0
        rows_by_idx[r_idx] = [cells.get(i) for i in range(width)]
    n = (max(rows_by_idx) + 1) if rows_by_idx else 0
    width = max((len(r) for r in rows_by_idx.values()), default=0)
    out = []
    for i in range(n):
        row = rows_by_idx.get(i) or []
        out.append(row + [None] * (width - len(row)))
    return out


def read_workbook(path) -> dict:
    """Читает xlsx → {"sheets": [{"name": str, "rows": [[ячейка,...],...]}]}.

    Ячейка: None | bool | int | float | str. Пустые строки — [].
    """
    sheets = []
    with zipfile.ZipFile(path) as z:
        shared = _read_shared_strings(z)
        wb = ET.fromstring(z.read("xl/workbook.xml"))
        rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
        rid_to_target = {rel.get("Id"): rel.get("Target") for rel in rels}
        for sheet in (e for e in wb.iter() if _local(e.tag) == "sheet"):
            name = sheet.get("name")
            rid = sheet.get(_RID)
            target = rid_to_target.get(rid, "")
            target = target.lstrip("/")
            if not target.startswith("xl/"):
                target = "xl/" + target
            if target not in z.namelist():
                raise ValueError(f"лист {name!r}: файл {target!r} отсутствует в архиве")
            sheets.append({"name": name, "rows": _read_sheet(z.read(target), shared)})
    return {"sheets": sheets}


def excel_serial_to_date(serial: float) -> date:
    return EXCEL_EPOCH + timedelta(days=int(serial))


_DATE_FORMATS = ("%Y-%m-%d", "%Y.%m.%d", "%d.%m.%Y", "%d/%m/%Y", "%Y/%m/%d")
_MONTH_FORMATS = ("%Y-%m", "%m/%Y", "%B %Y", "%b %Y")


def parse_any_date(value) -> date | None:
    """Терпеливый парсер дат: серийное число Excel, ISO, d.m.Y, 'Mon YYYY'.

    Возвращает None, если значение не распознано как дата — вызывающий код
    обязан честно пропустить такую строку с причиной (не выдумывать дату).
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        # серийные даты Excel для 1954–2064 гг.; прочие числа датой не считаем
        if 20000 <= float(value) <= 60000:
            return excel_serial_to_date(value)
        return None
    s = str(value).strip()
    if not s:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    for fmt in _MONTH_FORMATS:  # агрегат за месяц → первый день месяца
        try:
            return datetime.strptime(s, fmt).date().replace(day=1)
        except ValueError:
            pass
    try:
        return date.fromisoformat(s[:10])
    except ValueError:
        return None


def normalize_header(value) -> str:
    if value is None:
        return ""
    return str(value).strip().lower().replace(" ", "").replace("_", "")


def map_columns(rows: list[list], wanted: dict[str, tuple[str, ...]], *,
                min_hits: int = 2, max_scan_rows: int = 10) -> dict:
    """Ищет строку заголовка и отображает ключи → индексы столбцов.

    wanted: {ключ: варианты нормализованных имён}. Совпадение — точное по
    normalize_header. Возвращает {"header_row", "columns", "header_raw",
    "unmatched_keys", "matched"}; unmatched_keys — честный список ненайденного.
    """
    best = None
    for i, row in enumerate(rows[:max_scan_rows]):
        norm = [normalize_header(v) for v in row]
        cols: dict[str, int] = {}
        for key, variants in wanted.items():
            for variant in variants:
                if variant in norm:
                    cols[key] = norm.index(variant)
                    break
        hits = len(cols)
        if best is None or hits > best[1]:
            best = (i, hits, cols)
    if best is None or best[1] < min_hits:
        return {"header_row": None, "columns": {}, "header_raw": [],
                "unmatched_keys": sorted(wanted), "matched": {}}
    i, hits, cols = best
    header_raw = [v for v in rows[i]]
    matched = {}
    norm = [normalize_header(v) for v in rows[i]]
    for key, variants in wanted.items():
        for variant in variants:
            if variant in norm:
                matched[key] = norm.index(variant)
                break
    unmatched = sorted(set(wanted) - set(matched))
    return {"header_row": i, "columns": matched, "header_raw": header_raw,
            "unmatched_keys": unmatched, "matched": dict(matched)}
