"""Контракты данных: строгий парсинг строк CSV в типизированные записи.

Принципы:
  - пустое поле = НЕИЗВЕСТНО (None), а не ноль и не «unknown по умолчанию»
    (кроме полей, у которых UNKNOWN — явное значение словаря);
  - числа конечны, массы неотрицательны, счётчики целые;
  - время события/появления записи — ISO-8601 с явным offset (наивное время отклоняется);
  - структурный парсинг отделён от семантической валидации (validation.py).

Поле `notes` — документированное расширение серверного контракта (docs/DATA_CONTRACT.md
разрешает серверные расширения); оно используется как обязательное место для
документированного объяснения аномалий (например served_mass > prepared_mass).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime
from zoneinfo import ZoneInfo

from . import enums
from .errors import ValidationError

# --- Канонические списки полей (совпадают с templates/*.csv) -----------------
SESSION_FIELDS = (
    "session_id", "school_id", "service_date", "timezone", "meal_type",
    "attendance_count", "meals_requested", "meals_served", "demand_censored",
    "evidence_status", "study_phase", "source_ref", "observed_at", "recorded_at",
)
SESSION_OPTIONAL_FIELDS = ("notes",)

OBSERVATION_FIELDS = (
    "observation_id", "session_id", "dish_id",
    "prepared_mass_g", "served_mass_g", "production_waste_g", "serving_waste_g",
    "plate_waste_g", "avoidable_waste_g", "avoidability_status",
    "evidence_status", "source_ref", "observed_at", "recorded_at",
)
OBSERVATION_OPTIONAL_FIELDS = ("notes",)

MASS_FIELDS = (
    "prepared_mass_g", "served_mass_g", "production_waste_g",
    "serving_waste_g", "plate_waste_g", "avoidable_waste_g",
)
COUNT_FIELDS_SESSION = ("attendance_count", "meals_requested", "meals_served")

# Единицы: каноническое хранение — граммы. Импорт в kg допускается только
# с явной конвертацией (csvio.convert_mass_unit) и пометкой quality_flags.
CANONICAL_UNIT = "g"


# --- Низкоуровневые парсеры ---------------------------------------------------
def parse_optional_float(value: str, *, field_name: str, row_number: int) -> float | None:
    if value is None or value.strip() == "":
        return None
    try:
        num = float(value.strip().replace(",", "."))
    except ValueError:
        raise ValidationError(
            f"строка {row_number}: поле '{field_name}': не число ({value!r})",
            code="E_BAD_NUMBER",
        )
    if not math.isfinite(num):
        raise ValidationError(
            f"строка {row_number}: поле '{field_name}': значение не конечно ({value!r})",
            code="E_NONFINITE",
        )
    return num


def parse_mass(value: str, *, field_name: str, row_number: int) -> float | None:
    """Масса в граммах: конечное неотрицательное число или неизвестно (None)."""
    num = parse_optional_float(value, field_name=field_name, row_number=row_number)
    if num is not None and num < 0:
        raise ValidationError(
            f"строка {row_number}: поле '{field_name}': отрицательная масса ({num})",
            code="E_NEGATIVE_MASS",
        )
    return num


def parse_optional_int(value: str, *, field_name: str, row_number: int) -> int | None:
    if value is None or value.strip() == "":
        return None
    try:
        num = float(value.strip())
    except ValueError:
        raise ValidationError(
            f"строка {row_number}: поле '{field_name}': не число ({value!r})",
            code="E_BAD_NUMBER",
        )
    if not math.isfinite(num) or num != int(num):
        raise ValidationError(
            f"строка {row_number}: поле '{field_name}': счётчик должен быть целым ({value!r})",
            code="E_BAD_INT",
        )
    num = int(num)
    if num < 0:
        raise ValidationError(
            f"строка {row_number}: поле '{field_name}': отрицательный счётчик ({num})",
            code="E_NEGATIVE_COUNT",
        )
    return num


def parse_date_str(value: str, *, field_name: str, row_number: int) -> date:
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        raise ValidationError(
            f"строка {row_number}: поле '{field_name}': дата не в формате YYYY-MM-DD ({value!r})",
            code="E_BAD_DATE",
        )


def parse_datetime_offset(value: str, *, field_name: str, row_number: int) -> datetime:
    """ISO-8601 с явным offset. Наивное время (без offset) отклоняется."""
    try:
        dt = datetime.fromisoformat(value.strip())
    except ValueError:
        raise ValidationError(
            f"строка {row_number}: поле '{field_name}': не ISO-8601 ({value!r})",
            code="E_BAD_DATETIME",
        )
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValidationError(
            f"строка {row_number}: поле '{field_name}': время без часового пояса/offset ({value!r}); "
            "требуется явный offset (например +05:00)",
            code="E_NAIVE_TIME",
        )
    return dt


def parse_timezone(value: str, *, field_name: str, row_number: int) -> str:
    tz = value.strip()
    try:
        ZoneInfo(tz)
    except Exception:
        raise ValidationError(
            f"строка {row_number}: поле '{field_name}': неизвестный часовой пояс ({tz!r})",
            code="E_BAD_TZ",
        )
    return tz


def parse_enum(value: str, allowed, *, field_name: str, row_number: int, empty=None):
    v = value.strip()
    if v == "":
        if empty is not None:
            return empty
        raise ValidationError(
            f"строка {row_number}: поле '{field_name}': пустое значение обязательного поля",
            code="E_EMPTY_REQUIRED",
        )
    if v not in allowed:
        raise ValidationError(
            f"строка {row_number}: поле '{field_name}': значение {v!r} вне словаря "
            f"(допустимо: {sorted(allowed)})",
            code="E_BAD_ENUM",
        )
    return v


def require_nonempty(value: str, *, field_name: str, row_number: int) -> str:
    v = value.strip() if value is not None else ""
    if not v:
        raise ValidationError(
            f"строка {row_number}: поле '{field_name}': пустое значение обязательного поля",
            code="E_EMPTY_REQUIRED",
        )
    return v


# --- Записи ---------------------------------------------------------------------
@dataclass
class SessionRecord:
    """Один приём пищи в одной школе (строка sessions.csv)."""

    session_id: str
    school_id: str
    service_date: date
    timezone: str
    meal_type: str
    attendance_count: int | None
    meals_requested: int | None
    meals_served: int | None
    demand_censored: str  # true/false/unknown (UNKNOWN — явное значение)
    evidence_status: str
    study_phase: str | None
    source_ref: str
    observed_at: datetime
    recorded_at: datetime
    notes: str | None = None
    quality_flags: list[str] = field(default_factory=list)
    row_number: int = 0
    unit_declared: str = CANONICAL_UNIT

    REQUIRED = (
        "session_id", "school_id", "service_date", "timezone", "meal_type",
        "evidence_status", "source_ref", "observed_at", "recorded_at",
    )

    def to_payload(self) -> dict:
        return {
            "session_id": self.session_id,
            "school_id": self.school_id,
            "service_date": self.service_date.isoformat(),
            "timezone": self.timezone,
            "meal_type": self.meal_type,
            "attendance_count": self.attendance_count,
            "meals_requested": self.meals_requested,
            "meals_served": self.meals_served,
            "demand_censored": self.demand_censored,
            "evidence_status": self.evidence_status,
            "study_phase": self.study_phase,
            "source_ref": self.source_ref,
            "observed_at": self.observed_at.isoformat(),
            "recorded_at": self.recorded_at.isoformat(),
            "notes": self.notes,
            "unit_declared": self.unit_declared,
        }

    @classmethod
    def from_payload(cls, p: dict, *, quality_flags: list[str] | None = None) -> "SessionRecord":
        return cls(
            session_id=p["session_id"],
            school_id=p["school_id"],
            service_date=date.fromisoformat(p["service_date"]),
            timezone=p["timezone"],
            meal_type=p["meal_type"],
            attendance_count=p["attendance_count"],
            meals_requested=p["meals_requested"],
            meals_served=p["meals_served"],
            demand_censored=p["demand_censored"],
            evidence_status=p["evidence_status"],
            study_phase=p["study_phase"],
            source_ref=p["source_ref"],
            observed_at=datetime.fromisoformat(p["observed_at"]),
            recorded_at=datetime.fromisoformat(p["recorded_at"]),
            notes=p.get("notes"),
            quality_flags=list(quality_flags or []),
        )


@dataclass
class ObservationRecord:
    """Измерение блюда в сессии (строка observations.csv); не тарелка ученика."""

    observation_id: str
    session_id: str
    dish_id: str
    prepared_mass_g: float | None
    served_mass_g: float | None
    production_waste_g: float | None
    serving_waste_g: float | None
    plate_waste_g: float | None
    avoidable_waste_g: float | None
    avoidability_status: str
    evidence_status: str
    source_ref: str
    observed_at: datetime
    recorded_at: datetime
    notes: str | None = None
    quality_flags: list[str] = field(default_factory=list)
    row_number: int = 0
    unit_declared: str = CANONICAL_UNIT

    REQUIRED = (
        "observation_id", "session_id", "dish_id",
        "evidence_status", "source_ref", "observed_at", "recorded_at",
    )

    def to_payload(self) -> dict:
        return {
            "observation_id": self.observation_id,
            "session_id": self.session_id,
            "dish_id": self.dish_id,
            "prepared_mass_g": self.prepared_mass_g,
            "served_mass_g": self.served_mass_g,
            "production_waste_g": self.production_waste_g,
            "serving_waste_g": self.serving_waste_g,
            "plate_waste_g": self.plate_waste_g,
            "avoidable_waste_g": self.avoidable_waste_g,
            "avoidability_status": self.avoidability_status,
            "evidence_status": self.evidence_status,
            "source_ref": self.source_ref,
            "observed_at": self.observed_at.isoformat(),
            "recorded_at": self.recorded_at.isoformat(),
            "notes": self.notes,
            "unit_declared": self.unit_declared,
        }

    @classmethod
    def from_payload(cls, p: dict, *, quality_flags: list[str] | None = None) -> "ObservationRecord":
        return cls(
            observation_id=p["observation_id"],
            session_id=p["session_id"],
            dish_id=p["dish_id"],
            prepared_mass_g=p["prepared_mass_g"],
            served_mass_g=p["served_mass_g"],
            production_waste_g=p["production_waste_g"],
            serving_waste_g=p["serving_waste_g"],
            plate_waste_g=p["plate_waste_g"],
            avoidable_waste_g=p["avoidable_waste_g"],
            avoidability_status=p["avoidability_status"],
            evidence_status=p["evidence_status"],
            source_ref=p["source_ref"],
            observed_at=datetime.fromisoformat(p["observed_at"]),
            recorded_at=datetime.fromisoformat(p["recorded_at"]),
            notes=p.get("notes"),
            quality_flags=list(quality_flags or []),
        )


# --- Парсинг строк CSV (сырой dict → типизированная запись) --------------------
def parse_session_row(row: dict, *, row_number: int, unit: str = CANONICAL_UNIT) -> SessionRecord:
    g = lambda k: row.get(k, "")
    rec = SessionRecord(
        session_id=require_nonempty(g("session_id"), field_name="session_id", row_number=row_number),
        school_id=require_nonempty(g("school_id"), field_name="school_id", row_number=row_number),
        service_date=parse_date_str(g("service_date"), field_name="service_date", row_number=row_number),
        timezone=parse_timezone(g("timezone"), field_name="timezone", row_number=row_number),
        meal_type=parse_enum(
            g("meal_type"), enums.MEAL_TYPES_V1,
            field_name="meal_type", row_number=row_number,
        ),
        attendance_count=parse_optional_int(g("attendance_count"), field_name="attendance_count", row_number=row_number),
        meals_requested=parse_optional_int(g("meals_requested"), field_name="meals_requested", row_number=row_number),
        meals_served=parse_optional_int(g("meals_served"), field_name="meals_served", row_number=row_number),
        demand_censored=parse_enum(
            g("demand_censored"), enums.CENSORED_VALUES,
            field_name="demand_censored", row_number=row_number, empty=enums.CENSOR_UNKNOWN,
        ),
        evidence_status=parse_enum(
            g("evidence_status"), enums.EVIDENCE_STATUSES,
            field_name="evidence_status", row_number=row_number,
        ),
        study_phase=parse_enum(
            g("study_phase"), enums.STUDY_PHASES,
            field_name="study_phase", row_number=row_number, empty=None,
        ),
        source_ref=require_nonempty(g("source_ref"), field_name="source_ref", row_number=row_number),
        observed_at=parse_datetime_offset(g("observed_at"), field_name="observed_at", row_number=row_number),
        recorded_at=parse_datetime_offset(g("recorded_at"), field_name="recorded_at", row_number=row_number),
        notes=(row.get("notes") or "").strip() or None,
        row_number=row_number,
        unit_declared=unit,
    )
    return rec


def parse_observation_row(row: dict, *, row_number: int, unit: str = CANONICAL_UNIT) -> ObservationRecord:
    g = lambda k: row.get(k, "")
    kw = dict(row_number=row_number)
    rec = ObservationRecord(
        observation_id=require_nonempty(g("observation_id"), field_name="observation_id", **kw),
        session_id=require_nonempty(g("session_id"), field_name="session_id", **kw),
        dish_id=require_nonempty(g("dish_id"), field_name="dish_id", **kw),
        prepared_mass_g=parse_mass(g("prepared_mass_g"), field_name="prepared_mass_g", **kw),
        served_mass_g=parse_mass(g("served_mass_g"), field_name="served_mass_g", **kw),
        production_waste_g=parse_mass(g("production_waste_g"), field_name="production_waste_g", **kw),
        serving_waste_g=parse_mass(g("serving_waste_g"), field_name="serving_waste_g", **kw),
        plate_waste_g=parse_mass(g("plate_waste_g"), field_name="plate_waste_g", **kw),
        avoidable_waste_g=parse_mass(g("avoidable_waste_g"), field_name="avoidable_waste_g", **kw),
        avoidability_status=parse_enum(
            g("avoidability_status"), enums.AVOIDABILITY_STATUSES,
            field_name="avoidability_status", row_number=row_number,
            empty=enums.AV_UNCLASSIFIED,
        ),
        evidence_status=parse_enum(
            g("evidence_status"), enums.EVIDENCE_STATUSES,
            field_name="evidence_status", row_number=row_number,
        ),
        source_ref=require_nonempty(g("source_ref"), field_name="source_ref", row_number=row_number),
        observed_at=parse_datetime_offset(g("observed_at"), field_name="observed_at", row_number=row_number),
        recorded_at=parse_datetime_offset(g("recorded_at"), field_name="recorded_at", row_number=row_number),
        notes=(row.get("notes") or "").strip() or None,
        row_number=row_number,
        unit_declared=unit,
    )
    return rec
