"""Promotion gate — формальное правило принятия модели прогноза в работу.

Проблема (docs/AUDIT.md §B.3): до сих пор «лучшая модель» выбиралась неявно
по MAE внутри отчёта backtest'а, без формального правила и без фиксации
решения в модели-карточке. Отсюда promotion_gate: кандидат переводится в
статус принятой модели ТОЛЬКО если выполняются все критерии:

1. Достаточность данных: число окон rolling-origin >= gate.min_origins и
   суммарное число тестовых точек >= gate.min_points.
2. Обе модели (кандидат и действующая) отчитали статус OK.
3. Улучшение: MAE кандидата строго ниже MAE действующей модели минимум на
   gate.min_improvement_pct процентов (защита от «победы на шуме»).
4. Стабильность: кандидат лучше действующей минимум в
   gate.win_fraction_origins доле окон (по умолчанию 2/3) — защита от
   единичного везучего окна (см. урок RIDGE: одиночный holdout дал ложную
   победу; rolling-origin вскрыл нестабильность).
5. Покрытие интервала (если у обеих моделей есть coverage): в коридоре
   [gate.coverage_min, gate.coverage_max] — интервал должен быть полезным,
   а не пустым (coverage~0) и не фиктивно широким (coverage~1).

Вердикт PROMOTED/REJECTED сопровождается списком причин по каждому
несостоявшемуся критерию. При PROMOTED записывается модель-карточка
(model_cards) с полным аудитом решения; при REJECTED карточка НЕ пишется
(в orig данных ничего не меняется).

Инварианты: вердикт никогда не строится по процентам-агрегатам (MAE —
абсолютная величина); решения исполняются только через отдельный механизм
одобрения, promotion_gate лишь фиксирует готовность МОДЕЛИ, а не действий.
"""
from __future__ import annotations

from dataclasses import dataclass, field

VERDICT_PROMOTED = "PROMOTED"
VERDICT_REJECTED = "REJECTED"

_CRITERIA_ORDER = ("data_sufficiency", "improvement", "stability", "coverage")


@dataclass
class GatePolicy:
    """Пороговые значения правила принятия. Фиксируются в карточке решения."""

    min_improvement_pct: float = 5.0   # кандидат должен быть лучше на >= 5%
    min_origins: int = 3               # минимум окон rolling-origin
    min_points: int = 30               # минимум суммарных тестовых точек
    win_fraction_origins: float = 2.0 / 3.0  # доля окон, где кандидат лучше
    coverage_min: float = 0.60         # нижняя граница покрытия интервала
    coverage_max: float = 0.95         # верхняя граница (фиктивно широкий интервал)

    def to_dict(self) -> dict:
        return {
            "min_improvement_pct": self.min_improvement_pct,
            "min_origins": self.min_origins,
            "min_points": self.min_points,
            "win_fraction_origins": self.win_fraction_origins,
            "coverage_min": self.coverage_min,
            "coverage_max": self.coverage_max,
        }


def _candidate_from_report(report: dict, *, skip_names: set[str]) -> dict | None:
    """Лучшая по MAE модель отчёта backtest'а (среди допущенных к сравнению)."""
    models = [m for m in report.get("models", [])
              if m.get("status") == "OK" and m.get("name") not in skip_names]
    if not models:
        return None
    return min(models, key=lambda m: m["mae_mean"])


def _incumbent_from_report(report: dict, *, name: str) -> dict | None:
    """Действующая (контрольная) модель по имени в отчёте."""
    for m in report.get("models", []):
        if m.get("name") == name and m.get("status") == "OK":
            return m
    return None


def evaluate_promotion(candidate: dict, incumbent: dict, *,
                       evaluation: dict, gate: GatePolicy | None = None) -> dict:
    """Формальная оценка кандидата против действующей модели.

    candidate/incumbent — записи моделей отчёта backtest'а (status=OK):
    {name, mae_mean, mae_per_origin, [intervals -> coverage]}.
    evaluation — блок {'n_origins': int, 'test_days': int, ...} отчёта.
    Возвращает {'verdict', 'criteria': [...], 'reasons': [...], 'gate', 'numbers'}.
    """
    gate = gate or GatePolicy()
    criteria: list[dict] = []
    reasons: list[str] = []

    # 1. Достаточность данных. test_days в отчётах — на ОКНО, итого
    # протестированных точек = n_origins × test_days (rolling-origin).
    n_origins = int(evaluation.get("n_origins") or 0)
    per_window = int(evaluation.get("test_days") or 0)
    n_points = (per_window * n_origins if per_window
                else int(evaluation.get("n_points") or 0))
    ok_data = n_origins >= gate.min_origins and n_points >= gate.min_points
    criteria.append({"name": "data_sufficiency", "passed": ok_data,
                     "n_origins": n_origins, "min_origins": gate.min_origins,
                     "n_points": n_points, "min_points": gate.min_points})
    if not ok_data:
        reasons.append(
            f"недостаточно данных для решения: окон {n_origins} < {gate.min_origins} "
            f"или точек {n_points} < {gate.min_points}")

    # 2-3. Улучшение по MAE (абсолютные величины, не проценты-агрегаты).
    c_mae = float(candidate["mae_mean"])
    i_mae = float(incumbent["mae_mean"])
    denom = i_mae if i_mae > 1e-12 else 1e-12
    improvement_pct = (i_mae - c_mae) / denom * 100.0
    ok_impr = improvement_pct >= gate.min_improvement_pct
    criteria.append({"name": "improvement", "passed": ok_impr,
                     "candidate_mae": c_mae, "incumbent_mae": i_mae,
                     "improvement_pct": round(improvement_pct, 4),
                     "min_improvement_pct": gate.min_improvement_pct})
    if not ok_impr:
        reasons.append(
            f"улучшение MAE {improvement_pct:.2f}% < порога "
            f"{gate.min_improvement_pct:.2f}% "
            f"({c_mae:.4f} против {i_mae:.4f})")

    # 4. Стабильность по окнам.
    c_origins = list(candidate.get("mae_per_origin") or [])
    i_origins = list(incumbent.get("mae_per_origin") or [])
    n_cmp = min(len(c_origins), len(i_origins))
    if n_cmp == 0 or len(c_origins) != len(i_origins):
        wins, n_cmp = 0, max(n_cmp, 0)
        ok_stab = False
        reasons.append("пооконные MAE недоступны: сравнение окон невозможно")
    else:
        wins = sum(1 for c, i in zip(c_origins, i_origins) if c < i)
        ok_stab = (n_cmp > 0 and
                   wins / n_cmp >= gate.win_fraction_origins - 1e-9)
        if not ok_stab:
            reasons.append(
                f"нестабильно: кандидат лучше действующей в {wins} из {n_cmp} окон "
                f"(порог {gate.win_fraction_origins:.2f} доли окон)")
    criteria.append({"name": "stability", "passed": ok_stab,
                     "wins": wins, "origins_compared": n_cmp,
                     "win_fraction_origins": gate.win_fraction_origins})

    # 5. Покрытие интервала (если есть у обеих моделей).
    c_cov = _coverage_of(candidate)
    i_cov = _coverage_of(incumbent)
    if c_cov is not None and i_cov is not None:
        ok_cov = gate.coverage_min <= c_cov <= gate.coverage_max
        criteria.append({"name": "coverage", "passed": ok_cov,
                         "candidate_coverage": c_cov,
                         "incumbent_coverage": i_cov,
                         "allowed": [gate.coverage_min, gate.coverage_max]})
        if not ok_cov:
            reasons.append(
                f"покрытие интервала кандидата {c_cov:.2f} вне коридора "
                f"[{gate.coverage_min:.2f}, {gate.coverage_max:.2f}]")

    verdict = VERDICT_PROMOTED if all(c["passed"] for c in criteria) else VERDICT_REJECTED
    return {"verdict": verdict, "criteria": criteria, "reasons": reasons,
            "gate": gate.to_dict(),
            "numbers": {"candidate": candidate.get("name"),
                        "incumbent": incumbent.get("name"),
                        "candidate_mae": c_mae, "incumbent_mae": i_mae,
                        "improvement_pct": round(improvement_pct, 4)}}


def _coverage_of(model: dict) -> float | None:
    """Покрытие интервала модели, если отчёт его содержит."""
    iv = model.get("intervals") or {}
    cov = iv.get("coverage") if isinstance(iv, dict) else None
    if not isinstance(cov, (int, float)):
        cov = model.get("coverage_mean")  # формат отчёта composite
    if isinstance(cov, (int, float)):
        return float(cov)
    return None


def gate_from_report(report: dict, *, incumbent_name: str,
                     candidate_skip: set[str] | None = None,
                     gate: GatePolicy | None = None) -> dict:
    """Удобная обёртка: выбирает кандидата/действующую из отчёта и оценивает.

    incumbent_name — имя контрольной модели в отчёте (статус-кво);
    candidate_skip — имена, не допущенные в кандидаты (обычно сами базовые
    правила: кандидат должен быть моделью прогноза, а не тем же правилом).
    """
    skip = candidate_skip if candidate_skip is not None else {incumbent_name}
    candidate = _candidate_from_report(report, skip_names=skip)
    incumbent = _incumbent_from_report(report, name=incumbent_name)
    if candidate is None or incumbent is None:
        return {"verdict": VERDICT_REJECTED,
                "reasons": ["отчёт не содержит сопоставимых моделей: "
                            f"кандидат={candidate is not None}, "
                            f"действующая ({incumbent_name})={incumbent is not None}"],
                "gate": (gate or GatePolicy()).to_dict(), "criteria": [],
                "numbers": {}}
    res = evaluate_promotion(candidate, incumbent,
                             evaluation=report.get("evaluation", {}), gate=gate)
    res["report_meta"] = {k: report.get(k) for k in
                          ("first_test_date", "last_test_date", "status")}
    return res
