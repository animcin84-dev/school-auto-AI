"""Семантическая валидация записей поверх структурного парсинга (contracts.py).

Ошибки (E_*) — строка отклоняется; предупреждения (W_*) — строка принимается
с quality_flag (код предупреждения фиксируется в происхождении записи).
Импорт батча атомарен: если есть хотя бы одна ошибка, батч не записывается.

Ключевые правила контракта (docs/DATA_CONTRACT.md):
  - served > prepared не исправляется молча: отклоняется, либо принимается
    только с документированным объяснением в `notes` (+флаг BASE_MISMATCH_EXPLAINED);
  - синтетика не может пройти как VERIFIED: сырой импорт допускает только
    REAL_OBSERVED / SYNTHETIC; VERIFIED создаётся отдельной процедурой;
  - observation ссылается только на известную сессию;
  - дубликаты id и пар (session, dish) отклоняются — исправление идёт через ревизию;
  - recorded_at >= observed_at (запись не может появиться раньше события);
  - avoidable_waste проверяется на непревышение измеренных категорий, когда все
    три категории измерены; иначе — предупреждение (проверить невозможно);
  - согласованность счётчиков (served vs requested, цензурирование) —
    предупреждения: семантику протокола подтверждает школа, не валидатор.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import enums
from .contracts import MASS_FIELDS, ObservationRecord, SessionRecord
from .errors import ValidationError


@dataclass
class RowIssue:
    row_number: int
    entity_id: str
    field: str
    code: str
    message: str
    severity: str  # "error" | "warning"

    def to_dict(self) -> dict:
        return {
            "row_number": self.row_number, "entity_id": self.entity_id,
            "field": self.field, "code": self.code,
            "message": self.message, "severity": self.severity,
        }


@dataclass
class ValidationReport:
    accepted: list = field(default_factory=list)   # типизированные записи
    issues: list[RowIssue] = field(default_factory=list)

    @property
    def errors(self) -> list[RowIssue]:
        return [i for i in self.issues if i.severity == "error"]

    @property
    def warnings(self) -> list[RowIssue]:
        return [i for i in self.issues if i.severity == "warning"]

    @property
    def ok(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict:
        return {
            "accepted_count": len(self.accepted),
            "error_count": len(self.errors),
            "warning_count": len(self.warnings),
            "issues": [i.to_dict() for i in self.issues],
        }


def _issue(rec, field_name: str, code: str, message: str, severity: str = "error") -> RowIssue:
    eid = getattr(rec, "session_id", None) or getattr(rec, "observation_id", "?")
    return RowIssue(rec.row_number, eid, field_name, code, message, severity)


def _apply_warning_flags(rec, issues: list[RowIssue]) -> None:
    """Коды предупреждений принятой записи фиксируются в её quality_flags."""
    for i in issues:
        if i.severity == "warning" and i.code not in rec.quality_flags:
            rec.quality_flags.append(i.code)


def _check_time_order(rec) -> list[RowIssue]:
    out = []
    if rec.recorded_at < rec.observed_at:
        out.append(_issue(rec, "recorded_at", "E_TIME_ORDER",
                          f"recorded_at ({rec.recorded_at.isoformat()}) раньше observed_at "
                          f"({rec.observed_at.isoformat()})"))
    return out


def _check_raw_evidence(rec) -> list[RowIssue]:
    if rec.evidence_status not in enums.RAW_ALLOWED_EVIDENCE:
        return [_issue(rec, "evidence_status", "E_BAD_EVIDENCE",
                       f"сырая запись не может иметь статус {rec.evidence_status}; допустимо "
                       f"{sorted(enums.RAW_ALLOWED_EVIDENCE)}; VERIFIED создаётся процедурой verify()")]
    return []


def _check_mass_fields(rec) -> list[RowIssue]:
    """Защита в глубину: отрицательные массы (парсинг уже отсёк нечисловое/бесконечное)."""
    out = []
    for f in MASS_FIELDS:
        v = getattr(rec, f, None)
        if v is not None and v < 0:
            out.append(_issue(rec, f, "E_NEGATIVE_MASS", f"отрицательная масса: {v}"))
    return out


def validate_session_record(rec: SessionRecord) -> list[RowIssue]:
    issues: list[RowIssue] = []
    issues += _check_raw_evidence(rec)
    issues += _check_time_order(rec)
    # served > requested при наблюдаемом requested возможно при регистрации на
    # месте (заходы без предзаказа) — не жёсткое противоречие, но требует сверки.
    if rec.meals_requested is not None and rec.meals_served is not None:
        if rec.meals_served > rec.meals_requested:
            issues.append(_issue(rec, "meals_served", "W_SERVED_GT_REQUESTED",
                                 f"meals_served ({rec.meals_served}) > meals_requested "
                                 f"({rec.meals_requested}); проверить правила регистрации "
                                 "выдач/заходов", severity="warning"))
    # served < requested при censored=false: спрос наблюдается непосредственно,
    # поэтому это не обязательно противоречие, но семантику цензурирования
    # нужно подтвердить протоколом школы — предупреждение, не отказ.
    if (rec.demand_censored == enums.CENSOR_FALSE
            and rec.meals_requested is not None and rec.meals_served is not None
            and rec.meals_served < rec.meals_requested):
        issues.append(_issue(rec, "demand_censored", "W_CENSOR_CHECK",
                             "meals_served < meals_requested при demand_censored=false; "
                             "убедитесь, что спрос полностью наблюдаем и цензура "
                             "действительно отсутствует", severity="warning"))
    if rec.demand_censored == enums.CENSOR_UNKNOWN:
        # unknown допустим (например, спрос не наблюдался), но фиксируем флагом.
        rec.quality_flags.append("CENSORED_UNSET")
    if rec.study_phase is None:
        rec.quality_flags.append("PHASE_UNSET")
    return issues


def validate_observation_record(rec: ObservationRecord, *, known_sessions: set[str]) -> list[RowIssue]:
    issues: list[RowIssue] = []
    issues += _check_raw_evidence(rec)
    issues += _check_time_order(rec)
    issues += _check_mass_fields(rec)

    if rec.session_id not in known_sessions:
        issues.append(_issue(rec, "session_id", "E_UNKNOWN_SESSION",
                             f"сессия {rec.session_id!r} не найдена среди импортированных"))

    # served > prepared: не исправлять молча. Без объяснения — отклонить.
    if (rec.served_mass_g is not None and rec.prepared_mass_g is not None
            and rec.served_mass_g > rec.prepared_mass_g):
        if rec.notes:
            rec.quality_flags.append("BASE_MISMATCH_EXPLAINED")
            issues.append(_issue(rec, "served_mass_g", "W_BASE_MISMATCH_EXPLAINED",
                                 "served > prepared принят с документированным объяснением "
                                 f"в notes: {rec.notes!r}", severity="warning"))
        else:
            issues.append(_issue(rec, "served_mass_g", "E_SERVED_GT_PREPARED",
                                 f"served_mass_g ({rec.served_mass_g}) > prepared_mass_g "
                                 f"({rec.prepared_mass_g}) без документированного объяснения "
                                 "в notes (доп. партии/различие базы)"))

    # Непересекаемость категорий и границы классификации avoidable.
    cats = [rec.production_waste_g, rec.serving_waste_g, rec.plate_waste_g]
    if rec.avoidable_waste_g is not None:
        if all(c is not None for c in cats):
            if rec.avoidable_waste_g > sum(cats) + 1e-9:
                issues.append(_issue(rec, "avoidable_waste_g", "E_AVOIDABLE_GT_CATEGORIES",
                                     f"avoidable_waste_g ({rec.avoidable_waste_g}) превышает сумму "
                                     f"измеренных категорий ({sum(cats)}); нарушены границы категорий"))
        else:
            issues.append(_issue(rec, "avoidable_waste_g", "W_AVOIDABLE_UNVERIFIABLE",
                                 "avoidable_waste_g задан, но не все категории отходов измерены — "
                                 "непересекаемость проверить невозможно", severity="warning"))
        if rec.avoidability_status == enums.AV_UNCLASSIFIED:
            issues.append(_issue(rec, "avoidability_status", "E_AVOIDABLE_UNCLASSIFIED",
                                 "avoidable_waste_g задан при avoidability_status=UNCLASSIFIED"))
    if rec.avoidability_status == enums.AV_CLASSIFIED and rec.avoidable_waste_g is None:
        issues.append(_issue(rec, "avoidable_waste_g", "E_CLASSIFIED_WITHOUT_VALUE",
                             "avoidability_status=CLASSIFIED, но avoidable_waste_g не задан"))
    return issues


def validate_sessions(records: list[SessionRecord], *, existing_ids: set[str]) -> ValidationReport:
    """Валидация батча сессий: контекст хранилища + внутрипакетные дубликаты."""
    report = ValidationReport()
    seen_ids = set(existing_ids)
    for rec in records:
        issues = validate_session_record(rec)
        if rec.session_id in seen_ids:
            issues.append(_issue(rec, "session_id", "E_DUPLICATE_ID",
                                 f"дубликат session_id {rec.session_id!r}; исправление — "
                                 "через ревизию, а не повторный импорт"))
        seen_ids.add(rec.session_id)
        report.issues.extend(issues)
        if not any(i.severity == "error" for i in issues):
            _apply_warning_flags(rec, issues)
            report.accepted.append(rec)
    return report


def validate_observations(records: list[ObservationRecord], *,
                          known_sessions: set[str],
                          existing_observation_ids: set[str],
                          existing_pairs: set[tuple[str, str]]) -> ValidationReport:
    """Валидация батча наблюдений (known_sessions включает сессии текущего батча)."""
    report = ValidationReport()
    seen_ids = set(existing_observation_ids)
    seen_pairs = set(existing_pairs)
    for rec in records:
        issues = validate_observation_record(rec, known_sessions=known_sessions)
        if rec.observation_id in seen_ids:
            issues.append(_issue(rec, "observation_id", "E_DUPLICATE_ID",
                                 f"дубликат observation_id {rec.observation_id!r}"))
        seen_ids.add(rec.observation_id)
        pair = (rec.session_id, rec.dish_id)
        if pair in seen_pairs:
            issues.append(_issue(rec, "dish_id", "E_DUPLICATE_DISH_SESSION",
                                 f"повторное измерение блюда {rec.dish_id!r} в сессии "
                                 f"{rec.session_id!r}; исправление — через ревизию"))
        seen_pairs.add(pair)
        report.issues.extend(issues)
        if not any(i.severity == "error" for i in issues):
            _apply_warning_flags(rec, issues)
            report.accepted.append(rec)
    return report


def revalidate_record(rec) -> list[RowIssue]:
    """Контекстно-независимые правила при ревизии существующей записи.

    Проверки ссылочной целостности/дубликатов здесь не нужны: сущность уже
    в хранилище, а изменение ссылок при ревизии запрещено в store.revise().
    """
    if isinstance(rec, SessionRecord):
        return validate_session_record(rec)
    if isinstance(rec, ObservationRecord):
        return validate_observation_record(rec, known_sessions={rec.session_id})
    raise ValidationError("неизвестный тип записи", code="E_BAD_ENTITY")
