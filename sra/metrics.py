"""Авторитетные серверные метрики по docs/DATA_CONTRACT.md.

Правила вычисления:
  - проценты школы получаются агрегированием ЧИСЛИТЕЛЕЙ и ЗНАМЕНАТЕЛЕЙ,
    а не усреднением процентов блюд;
  - счётчики приёма пищи (meals_served и т.п.) учитываются один раз на сессию,
    даже если в сессии несколько блюд (защита от дублирования);
  - TotalMeasuredWaste считается только по блюдам, у которых измерены все три
    непересекающиеся категории;
  - WastePerMeal использует только ЯВНО классифицированные preventable-отходы
    и требует достаточной полноты классификации (параметр политики);
  - ServiceLevel = min(served / непосредственно наблюдаемый requested, 1) × 100;
    цензурирование показывается отдельно;
  - CostWastePerMeal — только при наличии источника стоимости; стоимость потерь
    НЕ является денежной экономией;
  - при пропусках или нулевом знаменателе — UNAVAILABLE с причиной;
  - каждый результат несёт происхождение: число входов, счётчики статусов
    доказательств и derived_status (SIMULATED, если хоть один вход SYNTHETIC).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import enums
from .contracts import ObservationRecord, SessionRecord


@dataclass
class MetricResult:
    name: str
    status: str                       # OK | UNAVAILABLE
    unit: str
    value: float | None = None
    numerator: float | None = None
    denominator: float | None = None
    n_inputs: int = 0
    evidence_counts: dict = field(default_factory=dict)   # {REAL_OBSERVED: n, SYNTHETIC: n, ...}
    derived_status: str = enums.DERIVED_COMPUTED          # COMPUTED | SIMULATED
    reason: str | None = None         # причина UNAVAILABLE
    notes: list[str] = field(default_factory=list)
    censoring: dict | None = None     # только для ServiceLevel

    def to_dict(self) -> dict:
        return {
            "name": self.name, "status": self.status, "unit": self.unit,
            "value": self.value, "numerator": self.numerator, "denominator": self.denominator,
            "n_inputs": self.n_inputs, "evidence_counts": self.evidence_counts,
            "derived_status": self.derived_status, "reason": self.reason,
            "notes": self.notes, **({"censoring": self.censoring} if self.censoring else {}),
        }


def _provenance(rows: list) -> tuple[int, dict, str]:
    counts: dict[str, int] = {}
    for r in rows:
        counts[r.evidence_status] = counts.get(r.evidence_status, 0) + 1
    derived = enums.DERIVED_SIMULATED if counts.get(enums.SYNTHETIC) else enums.DERIVED_COMPUTED
    return len(rows), counts, derived


def _unavailable(name: str, reason: str, rows: list, unit: str = "percent") -> MetricResult:
    n, counts, derived = _provenance(rows)
    return MetricResult(name=name, status=enums.METRIC_UNAVAILABLE, unit=unit,
                        reason=reason, n_inputs=n, evidence_counts=counts, derived_status=derived)


def uneaten_index(observations: list[ObservationRecord]) -> MetricResult:
    """UneatenIndex = Σ plate_waste / Σ served_mass × 100% (по блюдам с обеими величинами)."""
    rows = [o for o in observations if o.plate_waste_g is not None and o.served_mass_g is not None]
    if not rows:
        return _unavailable("UneatenIndex", "MISSING_FIELDS: нет блюд с измеренными "
                            "plate_waste_g и served_mass_g одновременно", observations)
    num = sum(o.plate_waste_g for o in rows)
    den = sum(o.served_mass_g for o in rows)
    n, counts, derived = _provenance(rows)
    if den <= 0:
        return MetricResult(name="UneatenIndex", status=enums.METRIC_UNAVAILABLE, unit="percent",
                            numerator=num, denominator=den, n_inputs=n, evidence_counts=counts,
                            derived_status=derived, reason="ZERO_DENOMINATOR: сумма served_mass_g равна 0")
    return MetricResult(name="UneatenIndex", status=enums.METRIC_OK, unit="percent",
                        value=100.0 * num / den, numerator=num, denominator=den,
                        n_inputs=n, evidence_counts=counts, derived_status=derived)


def overproduction_rate(observations: list[ObservationRecord]) -> MetricResult:
    """OverproductionRate = (Σ prepared − Σ served) / Σ prepared × 100%."""
    rows = [o for o in observations if o.prepared_mass_g is not None and o.served_mass_g is not None]
    if not rows:
        return _unavailable("OverproductionRate", "MISSING_FIELDS: нет блюд с измеренными "
                            "prepared_mass_g и served_mass_g одновременно", observations)
    num = sum(o.prepared_mass_g - o.served_mass_g for o in rows)
    den = sum(o.prepared_mass_g for o in rows)
    n, counts, derived = _provenance(rows)
    if den <= 0:
        return MetricResult(name="OverproductionRate", status=enums.METRIC_UNAVAILABLE, unit="percent",
                            numerator=num, denominator=den, n_inputs=n, evidence_counts=counts,
                            derived_status=derived, reason="ZERO_DENOMINATOR: сумма prepared_mass_g равна 0")
    notes = []
    mismatched = [o for o in observations
                  if o.prepared_mass_g is not None and o.served_mass_g is not None
                  and o.served_mass_g > o.prepared_mass_g]
    if mismatched:
        notes.append("в области расчёта есть строки served > prepared "
                     "(флаг BASE_MISMATCH_EXPLAINED); сверьте базу измерения")
    return MetricResult(name="OverproductionRate", status=enums.METRIC_OK, unit="percent",
                        value=100.0 * num / den, numerator=num, denominator=den,
                        n_inputs=n, evidence_counts=counts, derived_status=derived, notes=notes)


def total_measured_waste(observations: list[ObservationRecord]) -> MetricResult:
    """TotalMeasuredWaste = Σ(prod+serv+plate) только по блюдам со всеми тремя категориями."""
    rows = [o for o in observations if all(
        getattr(o, f) is not None
        for f in ("production_waste_g", "serving_waste_g", "plate_waste_g"))]
    if not rows:
        return _unavailable("TotalMeasuredWaste", "MISSING_FIELDS: нет блюд со всеми тремя "
                            "измеренными категориями отходов (production/serving/plate)",
                            observations, unit="g")
    num = sum(o.production_waste_g + o.serving_waste_g + o.plate_waste_g for o in rows)
    n, counts, derived = _provenance(rows)
    return MetricResult(name="TotalMeasuredWaste", status=enums.METRIC_OK, unit="g",
                        value=num, numerator=num, denominator=None, n_inputs=n,
                        evidence_counts=counts, derived_status=derived,
                        notes=["включены только блюда со всеми тремя непересекающимися категориями"])


def waste_per_meal(observations: list[ObservationRecord], sessions: list[SessionRecord], *,
                   min_classified_coverage: float = 0.5) -> MetricResult:
    """WastePerMeal = Σ(явно классифицированных avoidable) / Σ meals_served (сессия один раз).

    min_classified_coverage — параметр политики полноты классификации; значение по
    умолчанию — предварительное инженерное и должно быть заменено проверенной политикой.
    """
    meals_rows = [s for s in sessions if s.meals_served is not None]
    den = sum(s.meals_served for s in meals_rows)
    cls = [o for o in observations
           if o.avoidable_waste_g is not None and o.avoidability_status == enums.AV_CLASSIFIED]
    with_waste = [o for o in observations if any(
        getattr(o, f) is not None
        for f in ("production_waste_g", "serving_waste_g", "plate_waste_g"))]
    if with_waste:
        coverage = len([o for o in with_waste
                        if o.avoidability_status == enums.AV_CLASSIFIED]) / len(with_waste)
    else:
        coverage = None
    scope_rows = observations + meals_rows
    if not cls:
        return _unavailable("WastePerMeal", "MISSING_FIELDS: нет явно классифицированных "
                            "avoidable_waste_g (avoidability_status=CLASSIFIED)", scope_rows, unit="g/meal")
    if not meals_rows:
        return _unavailable("WastePerMeal", "MISSING_FIELDS: нет сессий с meals_served",
                            scope_rows, unit="g/meal")
    if coverage is not None and coverage < min_classified_coverage:
        return _unavailable("WastePerMeal",
                            f"INSUFFICIENT_CLASSIFICATION_COVERAGE: покрытие классификации "
                            f"{coverage:.3f} < порога {min_classified_coverage}", scope_rows, unit="g/meal")
    num = sum(o.avoidable_waste_g for o in cls)
    n, counts, derived = _provenance(scope_rows)
    if den <= 0:
        return MetricResult(name="WastePerMeal", status=enums.METRIC_UNAVAILABLE, unit="g/meal",
                            numerator=num, denominator=den, n_inputs=n, evidence_counts=counts,
                            derived_status=derived, reason="ZERO_DENOMINATOR: сумма meals_served равна 0")
    return MetricResult(name="WastePerMeal", status=enums.METRIC_OK, unit="g/meal",
                        value=num / den, numerator=num, denominator=den, n_inputs=n,
                        evidence_counts=counts, derived_status=derived,
                        notes=[f"покрытие классификации: {coverage:.3f}" if coverage is not None else "нет измерений отходов"])


def service_level(sessions: list[SessionRecord]) -> MetricResult:
    """ServiceLevel = min(Σ served / Σ наблюдаемый requested, 1) × 100; цензура отдельно."""
    rows = [s for s in sessions
            if s.meals_requested is not None and s.meals_served is not None]
    if not rows:
        return _unavailable("ServiceLevel", "MISSING_FIELDS: нет сессий с непосредственно "
                            "наблюдаемыми meals_requested и meals_served; посещаемость спросом "
                            "не заменяется", sessions)
    den = sum(s.meals_requested for s in rows)
    num = sum(s.meals_served for s in rows)
    n, counts, derived = _provenance(rows)
    if den <= 0:
        return MetricResult(name="ServiceLevel", status=enums.METRIC_UNAVAILABLE, unit="percent",
                            numerator=num, denominator=den, n_inputs=n, evidence_counts=counts,
                            derived_status=derived, reason="ZERO_DENOMINATOR: сумма meals_requested равна 0")
    censoring = {enums.CENSOR_TRUE: 0, enums.CENSOR_FALSE: 0, enums.CENSOR_UNKNOWN: 0}
    for s in rows:
        censoring[s.demand_censored] = censoring.get(s.demand_censored, 0) + 1
    notes = []
    if censoring[enums.CENSOR_TRUE] or censoring[enums.CENSOR_UNKNOWN]:
        notes.append("есть цензурированные/неизвестные сессии: нехватка еды скрывает часть спроса, "
                     "уровень сервиса может быть завышен")
    return MetricResult(name="ServiceLevel", status=enums.METRIC_OK, unit="percent",
                        value=min(num / den, 1.0) * 100.0, numerator=num, denominator=den,
                        n_inputs=n, evidence_counts=counts, derived_status=derived,
                        censoring=censoring, notes=notes)


@dataclass
class CostSource:
    """Источник стоимости предотвращённых потерь. Валиден для расчёта только
    при evidence_status=VERIFIED; иначе результат помечается SIMULATED."""
    policy_id: str
    cost_per_kg: float
    currency: str
    evidence_status: str  # VERIFIED | SYNTHETIC | ...


def cost_waste_per_meal(observations: list[ObservationRecord], sessions: list[SessionRecord],
                        cost_source: CostSource | None) -> MetricResult:
    """CostWastePerMeal: стоимость явно классифицированных preventable-отходов на приём пищи.

    Не является денежной экономией. Без доказательного источника стоимости — UNAVAILABLE.
    Источник с не-VERIFIED статусом даёт SIMULATED-результат (демо/сценарий).
    """
    if cost_source is None:
        return _unavailable("CostWastePerMeal", "COST_SOURCE_ABSENT: нет источника стоимости "
                            "с доказательством (политика/прайс); стоимость потерь не может "
                            "быть придумана", observations, unit="currency/meal")
    base = waste_per_meal(observations, sessions)
    if base.status != enums.METRIC_OK:
        result = _unavailable("CostWastePerMeal",
                              f"BASE_METRIC_UNAVAILABLE: WastePerMeal недоступен "
                              f"({base.reason})", observations, unit="currency/meal")
        result.notes.append(f"основание: WastePerMeal недоступен ({base.reason})")
        return result
    cost_value = (base.numerator / 1000.0) * cost_source.cost_per_kg / base.denominator
    n, counts, derived = _provenance(observations + sessions)
    derived = (enums.DERIVED_COMPUTED if cost_source.evidence_status == enums.VERIFIED
               else enums.DERIVED_SIMULATED)
    notes = ["стоимость предотвращённых потерь НЕ является денежной экономией (не AVF)",
             f"источник стоимости: {cost_source.policy_id} ({cost_source.evidence_status})"]
    if derived == enums.DERIVED_SIMULATED:
        notes.append("источник стоимости не VERIFIED — результат SIMULATED")
    return MetricResult(name="CostWastePerMeal", status=enums.METRIC_OK, unit="currency/meal",
                        value=cost_value,
                        numerator=(base.numerator / 1000.0) * cost_source.cost_per_kg,
                        denominator=base.denominator, n_inputs=n, evidence_counts=counts,
                        derived_status=derived, notes=notes)


def build_metric_report(sessions: list[SessionRecord], observations: list[ObservationRecord],
                        *, cost_source: CostSource | None = None,
                        min_classified_coverage: float = 0.5) -> dict:
    """Полный отчёт метрик по согласованной области (одна школа × период)."""
    metrics = [
        uneaten_index(observations),
        overproduction_rate(observations),
        total_measured_waste(observations),
        waste_per_meal(observations, sessions, min_classified_coverage=min_classified_coverage),
        service_level(sessions),
        cost_waste_per_meal(observations, sessions, cost_source),
    ]
    any_synthetic = any(m.evidence_counts.get(enums.SYNTHETIC) for m in metrics)
    return {
        "scope": {"sessions": len(sessions), "observations": len(observations)},
        "nature": ("SIMULATED: в расчёт вошли SYNTHETIC-входы" if any_synthetic
                   else "COMPUTED: все входы REAL_OBSERVED/VERIFIED"),
        "metrics": [m.to_dict() for m in metrics],
    }
