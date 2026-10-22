"""Импорт CSV внешнего источника по ЯВНОЙ карте колонок (универсальный).

Зачем: формат выгрузки «Социального кошелька» (и любого школьного журнала)
подтверждается по фактическому образцу. Вместо догадок — карта соответствий,
которую пишет человек по реальному файлу:

{
  "kind": "sessions",
  "columns": {"session_id": "Код обеда", "school_id": "Школа",
              "service_date": "Дата", ...},
  "constants": {"timezone": "Asia/Almaty", "meal_type": "LUNCH",
                "evidence_status": "REAL_OBSERVED",
                "source_ref": "social-wallet:export:2026-10-22"}
}

Каждое значение получает происхождение (source_ref обязателен); статусы
проверяются guard'ом среды (OPEN/DEMO/REAL). Разбор честный: ошибки строк
собираются со ссылками на строки, батч атомарен. Никаких догадок о формате:
не указанное в карте поле = ошибка с перечнем, а не молчаливый ноль.
"""

from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .. import enums
from ..contracts import (
    ObservationRecord, SessionRecord, parse_date_str, parse_datetime_offset,
    parse_enum, parse_mass, parse_optional_int, parse_timezone, require_nonempty)
from ..errors import ValidationError

REQUIRED = {
    "sessions": ("session_id", "school_id", "service_date", "timezone",
                 "meal_type", "evidence_status", "source_ref", "observed_at",
                 "recorded_at"),
    "observations": ("observation_id", "session_id", "dish_id",
                     "evidence_status", "source_ref", "observed_at",
                     "recorded_at"),
}
_MASS_FIELDS = ("prepared_mass_g", "served_mass_g", "production_waste_g",
                "serving_waste_g", "plate_waste_g", "avoidable_waste_g")
_COUNT_FIELDS = ("attendance_count", "meals_requested", "meals_served")
_INT_FIELDS = _COUNT_FIELDS
_FIELD_PARSERS = {
    "session_id": lambda v, rn: require_nonempty(v, field_name="session_id", row_number=rn),
    "school_id": lambda v, rn: require_nonempty(v, field_name="school_id", row_number=rn),
    "observation_id": lambda v, rn: require_nonempty(v, field_name="observation_id", row_number=rn),
    "dish_id": lambda v, rn: require_nonempty(v, field_name="dish_id", row_number=rn),
    "source_ref": lambda v, rn: require_nonempty(v, field_name="source_ref", row_number=rn),
    "service_date": lambda v, rn: parse_date_str(v, field_name="service_date", row_number=rn),
    "timezone": lambda v, rn: parse_timezone(v, field_name="timezone", row_number=rn),
    "meal_type": lambda v, rn: require_nonempty(v, field_name="meal_type", row_number=rn),
    "evidence_status": lambda v, rn: parse_enum(v, enums.RAW_ALLOWED_EVIDENCE,
                                                field_name="evidence_status",
                                                row_number=rn),
    "demand_censored": lambda v, rn: parse_enum(v, enums.CENSORED_VALUES,
                                                field_name="demand_censored",
                                                row_number=rn,
                                                empty=enums.CENSOR_UNKNOWN),
    "study_phase": lambda v, rn: parse_enum(v, enums.STUDY_PHASES,
                                            field_name="study_phase",
                                            row_number=rn, empty=None),
    "avoidability_status": lambda v, rn: parse_enum(v, enums.AVOIDABILITY_STATUSES,
                                                    field_name="avoidability_status",
                                                    row_number=rn,
                                                    empty=enums.AV_UNCLASSIFIED),
    "observed_at": lambda v, rn: parse_datetime_offset(v, field_name="observed_at",
                                                       row_number=rn),
    "recorded_at": lambda v, rn: parse_datetime_offset(v, field_name="recorded_at",
                                                       row_number=rn),
}
for _f in _MASS_FIELDS:
    _FIELD_PARSERS[_f] = (lambda f: (lambda v, rn: parse_mass(
        v, field_name=f, row_number=rn)))(_f)
for _f in _INT_FIELDS:
    _FIELD_PARSERS[_f] = (lambda f: (lambda v, rn: parse_optional_int(
        v, field_name=f, row_number=rn)))(_f)

KNOWN_FIELDS = set(_FIELD_PARSERS)


def load_map(path) -> dict:
    m = json.loads(Path(path).read_text(encoding="utf-8"))
    kind = m.get("kind")
    if kind not in REQUIRED:
        raise ValidationError(f"map.kind: sessions|observations, получено {kind!r}",
                              code="E_BAD_KIND")
    columns = m.get("columns") or {}
    constants = m.get("constants") or {}
    unknown = [f for f in list(columns) + list(constants) if f not in KNOWN_FIELDS]
    if unknown:
        raise ValidationError(f"карта содержит неизвестные поля: {sorted(unknown)}; "
                              f"допустимо: {sorted(KNOWN_FIELDS)}",
                              code="E_BAD_MAP_FIELD")
    missing = [f for f in REQUIRED[kind]
               if f not in columns and f not in constants]
    overlap = sorted(set(columns) & set(constants))
    if overlap:
        raise ValidationError(f"поля заданы и колонкой, и константой: {overlap}",
                              code="E_MAP_CONFLICT")
    derive = m.get("derive") or {}
    unknown_derive = [f for f in derive if f not in KNOWN_FIELDS]
    if unknown_derive:
        raise ValidationError(f"derive содержит неизвестные поля: {unknown_derive}",
                              code="E_BAD_MAP_FIELD")
    # производимые поля удовлетворяют требование обязательности
    missing = [f for f in missing if f not in derive]
    if missing:
        raise ValidationError(f"обязательные поля не заданы (колонка/константа/"
                              f"derive): {missing}", code="E_MAP_INCOMPLETE")
    if "observed_at" in derive:
        spec = derive["observed_at"]
        if "date_field" not in spec or "time" not in spec:
            raise ValidationError("derive.observed_at требует {date_field, time}",
                                  code="E_BAD_MAP_FIELD")
        if spec["date_field"] not in columns:
            raise ValidationError("derive.observed_at.date_field должен быть "
                                  "колонкой карты", code="E_BAD_MAP_FIELD")
    if "recorded_at" in derive and not derive["recorded_at"].get("at_import"):
        raise ValidationError('derive.recorded_at: только {"at_import": true} '
                              "(время записи = момент импорта)",
                              code="E_BAD_MAP_FIELD")
    if set(derive) & set(columns) or set(derive) & set(constants):
        raise ValidationError("поле задано дважды (derive + колонка/константа)",
                              code="E_MAP_CONFLICT")
    return m


def parse_csv(path, m: dict) -> tuple[list, dict]:
    """Читает CSV по карте → (записи, отчёт). Ошибки собираются, батч атомарен."""
    kind = m["kind"]
    columns = m["columns"]
    constants = m["constants"]
    derive = m.get("derive") or {}
    import_now = datetime.now(timezone.utc)
    records, errors = [], []
    delimiter = (m.get("options") or {}).get("delimiter", ",")
    if len(delimiter) != 1:
        raise ValidationError("options.delimiter должен быть одним символом",
                              code="E_BAD_MAP_FIELD")
    with open(path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh, delimiter=delimiter)
        if reader.fieldnames is None:
            raise ValidationError("CSV пуст (нет заголовка)", code="E_NO_DATA_ROWS")
        headers = {h.strip() for h in reader.fieldnames}
        for field, col in columns.items():
            if col not in headers:
                raise ValidationError(f"колонка {col!r} (для {field}) отсутствует "
                                      f"в файле; есть: {sorted(headers)}",
                                      code="E_COLUMN_NOT_FOUND")
        for rn, row in enumerate(reader, start=2):
            vals = {}
            for field in KNOWN_FIELDS & set(columns):
                raw = row.get(columns[field])
                vals[field] = raw if raw is not None else ""
            for field, v in constants.items():
                vals[field] = v
            vals.setdefault("demand_censored", enums.CENSOR_UNKNOWN)
            vals.setdefault("avoidability_status", enums.AV_UNCLASSIFIED)
            if "observed_at" in derive:
                spec = derive["observed_at"]
                try:
                    tz_name = vals.get("timezone") or "UTC"
                    d_val = vals.get(spec["date_field"], "")
                    hh, mm = str(spec["time"]).split(":")
                    vals["observed_at"] = datetime(
                        *parse_date_str(d_val, field_name=spec["date_field"],
                                        row_number=rn).timetuple()[:3],
                        int(hh), int(mm), tzinfo=ZoneInfo(tz_name)).isoformat()
                except Exception as e:
                    errors.append({"row": rn, "code": "E_DERIVE_FAILED",
                                   "message": f"observed_at: {e}"})
                    continue
            if derive.get("recorded_at", {}).get("at_import"):
                vals["recorded_at"] = import_now.isoformat()
            try:
                if kind == "sessions":
                    rec = SessionRecord(
                        session_id=_FIELD_PARSERS["session_id"](vals.get("session_id", ""), rn),
                        school_id=_FIELD_PARSERS["school_id"](vals.get("school_id", ""), rn),
                        service_date=_FIELD_PARSERS["service_date"](vals.get("service_date", ""), rn),
                        timezone=_FIELD_PARSERS["timezone"](vals.get("timezone", ""), rn),
                        meal_type=_FIELD_PARSERS["meal_type"](vals.get("meal_type", ""), rn),
                        attendance_count=(_FIELD_PARSERS["attendance_count"](vals["attendance_count"], rn)
                                          if vals.get("attendance_count") not in (None, "") else None),
                        meals_requested=(_FIELD_PARSERS["meals_requested"](vals["meals_requested"], rn)
                                         if vals.get("meals_requested") not in (None, "") else None),
                        meals_served=(_FIELD_PARSERS["meals_served"](vals["meals_served"], rn)
                                      if vals.get("meals_served") not in (None, "") else None),
                        demand_censored=_FIELD_PARSERS["demand_censored"](vals.get("demand_censored", ""), rn),
                        evidence_status=_FIELD_PARSERS["evidence_status"](vals.get("evidence_status", ""), rn),
                        study_phase=(_FIELD_PARSERS["study_phase"](vals["study_phase"], rn)
                                     if vals.get("study_phase") not in (None, "") else None),
                        source_ref=_FIELD_PARSERS["source_ref"](vals.get("source_ref", ""), rn),
                        observed_at=_FIELD_PARSERS["observed_at"](vals.get("observed_at", ""), rn),
                        recorded_at=_FIELD_PARSERS["recorded_at"](vals.get("recorded_at", ""), rn))
                else:
                    masses = {}
                    for f in _MASS_FIELDS:
                        if vals.get(f) not in (None, ""):
                            masses[f] = _FIELD_PARSERS[f](vals[f], rn)
                    rec = ObservationRecord(
                        observation_id=_FIELD_PARSERS["observation_id"](vals.get("observation_id", ""), rn),
                        session_id=_FIELD_PARSERS["session_id"](vals.get("session_id", ""), rn),
                        dish_id=_FIELD_PARSERS["dish_id"](vals.get("dish_id", ""), rn),
                        prepared_mass_g=masses.get("prepared_mass_g"),
                        served_mass_g=masses.get("served_mass_g"),
                        production_waste_g=masses.get("production_waste_g"),
                        serving_waste_g=masses.get("serving_waste_g"),
                        plate_waste_g=masses.get("plate_waste_g"),
                        avoidable_waste_g=masses.get("avoidable_waste_g"),
                        avoidability_status=_FIELD_PARSERS["avoidability_status"](vals.get("avoidability_status", ""), rn),
                        evidence_status=_FIELD_PARSERS["evidence_status"](vals.get("evidence_status", ""), rn),
                        source_ref=_FIELD_PARSERS["source_ref"](vals.get("source_ref", ""), rn),
                        observed_at=_FIELD_PARSERS["observed_at"](vals.get("observed_at", ""), rn),
                        recorded_at=_FIELD_PARSERS["recorded_at"](vals.get("recorded_at", ""), rn))
                records.append(rec)
            except ValidationError as e:
                errors.append({"row": rn, "code": e.code, "message": str(e)})
    return records, {"kind": kind, "rows_total": len(records) + len(errors),
                     "rows_parsed": len(records),
                     "errors": errors[:50],
                     "errors_total": len(errors),
                     "note": "батч атомарен: при наличии ошибок импорт не выполняется"}
