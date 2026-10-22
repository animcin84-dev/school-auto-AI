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

# Поля, обязательные в aggregate-режиме (порция в строке -> сессия дня):
# session_id/attendance_count/meals_served вычисляются при агрегации.
REQUIRED_AGG = ("school_id", "service_date", "meal_type", "timezone",
                "evidence_status", "source_ref", "observed_at", "recorded_at")
# поля уровня группы: значения всех строк группы обязаны совпадать
_GROUP_CONSISTENT = ("evidence_status", "timezone", "source_ref",
                     "demand_censored", "study_phase", "menu_id")

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
    "menu_id": lambda v, rn: (str(v).strip()[:24] or None),
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
    agg = (m.get("options") or {}).get("aggregate")
    if agg is not None:
        if kind != "sessions":
            raise ValidationError("options.aggregate допустим только для "
                                  "kind=sessions (агрегация порций в сессию дня)",
                                  code="E_AGGREGATE_CONFLICT")
        if not isinstance(agg, dict) or set(agg) - {"pupil_column"}:
            raise ValidationError('options.aggregate: допустимый ключ — '
                                  '"pupil_column" (колонка с идентификатором '
                                  "ученика для distinct-посещаемости)",
                                  code="E_BAD_MAP_FIELD")
    columns = m.get("columns") or {}
    constants = m.get("constants") or {}
    unknown = [f for f in list(columns) + list(constants) if f not in KNOWN_FIELDS]
    if unknown:
        raise ValidationError(f"карта содержит неизвестные поля: {sorted(unknown)}; "
                              f"допустимо: {sorted(KNOWN_FIELDS)}",
                              code="E_BAD_MAP_FIELD")
    required = REQUIRED_AGG if agg is not None else REQUIRED[kind]
    missing = [f for f in required
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
    if agg is not None and "recorded_at" in columns:
        raise ValidationError("aggregate: recorded_at не может быть колонкой строк "
                              '(используйте derive {"recorded_at": {"at_import": true}} '
                              "или константу)",
                              code="E_AGGREGATE_CONFLICT")
    return m


def _parse_csv_aggregate(path, m: dict, agg: dict) -> tuple[list, dict]:
    """Aggregate-режим: одна строка CSV = одна ВЫДАННАЯ ПОРЦИЯ (например,
    подтверждение «Социального кошелька»); строки группы
    (school_id, service_date, meal_type) сворачиваются в ОДНУ сессию дня:

    - meals_served = число строк группы (порций);
    - attendance_count = число УНИКАЛЬНЫХ учеников (distinct pupil_column),
      если pupil_column задан, иначе None (посещаемость не выдумываем);
    - session_id детерминирован: SES-<школа>-<дата>-<ПРИЁМ>;
    - поля уровня группы (evidence_status, timezone, source_ref,
      demand_censored, study_phase, menu_id) обязаны совпадать во всех
      строках группы — расхождение = ошибка строки (батч атомарен);
    - observed_at: при разных значениях в группе берётся минимум
      (раньший момент) с предупреждением W_AGG_OBSERVED_MIN.

    Инвариант сохраняется: посещаемость (ученики) != спрос (порции);
    цензурирование demand_censored приходит только из карты, не выдумывается.
    """
    columns = m["columns"]
    constants = m["constants"]
    derive = m.get("derive") or {}
    import_now = datetime.now(timezone.utc)
    pupil_column = agg.get("pupil_column")
    errors: list[dict] = []
    warnings: list[dict] = []
    delimiter = (m.get("options") or {}).get("delimiter", ",")
    if len(delimiter) != 1:
        raise ValidationError("options.delimiter должен быть одним символом",
                              code="E_BAD_MAP_FIELD")
    groups: dict[tuple, dict] = {}
    n_rows = 0
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
        if pupil_column and pupil_column not in headers:
            raise ValidationError(f"колонка {pupil_column!r} (pupil_column) "
                                  f"отсутствует в файле; есть: {sorted(headers)}",
                                  code="E_COLUMN_NOT_FOUND")
        for rn, row in enumerate(reader, start=2):
            n_rows += 1
            vals = {}
            for field in KNOWN_FIELDS & set(columns):
                raw = row.get(columns[field])
                vals[field] = raw if raw is not None else ""
            for field, v in constants.items():
                vals[field] = v
            vals.setdefault("demand_censored", enums.CENSOR_UNKNOWN)
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
                parsed = {
                    "school_id": _FIELD_PARSERS["school_id"](vals.get("school_id", ""), rn),
                    "service_date": _FIELD_PARSERS["service_date"](vals.get("service_date", ""), rn),
                    "meal_type": _FIELD_PARSERS["meal_type"](vals.get("meal_type", ""), rn),
                    "timezone": _FIELD_PARSERS["timezone"](vals.get("timezone", ""), rn),
                    "evidence_status": _FIELD_PARSERS["evidence_status"](vals.get("evidence_status", ""), rn),
                    "source_ref": _FIELD_PARSERS["source_ref"](vals.get("source_ref", ""), rn),
                    "observed_at": _FIELD_PARSERS["observed_at"](vals.get("observed_at", ""), rn),
                    "recorded_at": _FIELD_PARSERS["recorded_at"](vals.get("recorded_at", ""), rn),
                    "demand_censored": _FIELD_PARSERS["demand_censored"](vals.get("demand_censored", ""), rn),
                    "study_phase": (_FIELD_PARSERS["study_phase"](vals["study_phase"], rn)
                                    if vals.get("study_phase") not in (None, "") else None),
                    "menu_id": (str(vals.get("menu_id")).strip()[:24] or None)
                    if vals.get("menu_id") not in (None, "") else None,
                    "pupil": (row.get(pupil_column) or "").strip()
                    if pupil_column else "",
                }
            except ValidationError as e:
                errors.append({"row": rn, "code": e.code, "message": str(e)})
                continue
            key = (parsed["school_id"], parsed["service_date"],
                   parsed["meal_type"].upper())
            g = groups.get(key)
            if g is None:
                g = groups[key] = {**{k: v for k, v in parsed.items()
                                      if k != "pupil"},
                                   "rows": 0, "pupils": set(),
                                   "row_nums": []}
            for f in _GROUP_CONSISTENT:
                if parsed[f] != g[f]:
                    errors.append({
                        "row": rn, "code": "E_AGG_CONFLICT_VALUE",
                        "message": (f"поле {f} различается внутри группы "
                                    f"{key[0]}/{key[1].isoformat()}/{key[2]}: "
                                    f"{parsed[f]!r} против {g[f]!r} (строка "
                                    f"{g['row_nums'][0]})")})
            g["rows"] += 1
            if parsed["pupil"]:
                g["pupils"].add(parsed["pupil"])
            if parsed["observed_at"] < g["observed_at"]:
                g["observed_at"] = parsed["observed_at"]
                g["observed_min_mixed"] = True
            g["row_nums"].append(rn)
    if errors:
        return [], {"kind": "sessions-aggregate", "rows_total": n_rows,
                    "rows_parsed": 0, "sessions_built": 0,
                    "warnings": warnings, "errors": errors[:50],
                    "errors_total": len(errors),
                    "note": "батч атомарен: при наличии ошибок импорт не выполняется"}
    records = []
    for key, g in groups.items():
        if g.get("observed_min_mixed"):
            warnings.append({
                "code": "W_AGG_OBSERVED_MIN",
                "message": (f"группа {key[0]}/{key[1].isoformat()}/{key[2]}: "
                            "observed_at строк различается — взят минимальный "
                            f"момент ({g['observed_at'].isoformat()})")})
        records.append(SessionRecord(
            session_id=f"SES-{key[0]}-{key[1].isoformat()}-{key[2]}",
            school_id=key[0], service_date=key[1], timezone=g["timezone"],
            meal_type=key[2],
            attendance_count=(len(g["pupils"]) if pupil_column and g["pupils"]
                              else None),
            meals_requested=None, meals_served=g["rows"],
            demand_censored=g["demand_censored"],
            evidence_status=g["evidence_status"], study_phase=g["study_phase"],
            menu_id=g["menu_id"], source_ref=g["source_ref"],
            observed_at=g["observed_at"], recorded_at=g["recorded_at"]))
    report = {"kind": "sessions-aggregate", "rows_total": n_rows,
              "rows_parsed": n_rows, "sessions_built": len(records),
              "warnings": warnings, "errors": [], "errors_total": 0,
              "attendance_semantics": ("distinct pupil_column" if pupil_column
                                       else "pupil_column не задан -> None"),
              "note": ("одна строка = одна порция; группы (школа, дата, приём) "
                       "-> сессия дня; meals_served = строк, attendance = "
                       "уникальные ученики")}
    return records, report


def parse_csv(path, m: dict) -> tuple[list, dict]:
    """Читает CSV по карте → (записи, отчёт). Ошибки собираются, батч атомарен."""
    agg = (m.get("options") or {}).get("aggregate")
    if agg is not None:
        return _parse_csv_aggregate(path, m, agg)
    kind = m["kind"]
    columns = m["columns"]
    constants = m["constants"]
    derive = m.get("derive") or {}
    import_now = datetime.now(timezone.utc)
    records, errors = [], []
    warnings: list[dict] = []
    if "observed_at" in (m.get("derive") or {}) and \
            "timezone" not in (m.get("columns") or {}) and \
            "timezone" not in (m.get("constants") or {}):
        warnings.append({
            "code": "W_DERIVE_UTC_FALLBACK",
            "message": "derive.observed_at: зона не задана картой — используется UTC; "
                       "добавьте константу timezone (например Asia/Almaty)",
        })
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
                        menu_id=(vals.get("menu_id") or None),
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
                     "warnings": warnings,
                     "errors": errors[:50],
                     "errors_total": len(errors),
                     "note": "батч атомарен: при наличии ошибок импорт не выполняется"}
