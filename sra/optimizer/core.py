"""Оптимизация объёма приготовления (этап 4, симуляционный контур).

Формулировка задачи (фиксируется в предложении решения):
  - ПЕРЕМЕННАЯ: P — плановая суммарная масса приготовления на сессию
    (школа × дата × тип приёма), граммы. Нормативная порция на ребёнка НЕ
    является переменной: действие меняет только общий объём, состав меню и
    размер порции не затрагиваются.
  - ДЕЙСТВИЯ: NO_CHANGE (готовить как обычно, базовая линия P0) либо
    ADJUST_PREPARED_VOLUME с |P − P0| ≤ Δ_max (заранее разрешённая граница
    изменения — параметр политики).
  - НЕОПРЕДЕЛЁННОСТЬ: спрос D представлен эмпирическим набором сценариев
    (точечный прогноз + калибровочные остатки). Сценарии занижают полный
    спрос на цензурированных днях — ограничение фиксируется в предложении.
  - ЦЕЛЬ: минимум ожидаемой стоимости
        c_w · E[max(P − D, 0)]/1000 + c_s · E[max(D − P, 0)]/1000
    где c_w — стоимость предотвращаемых потерь на кг, c_s — штраф нехватки
    на кг. Доминирование c_s > c_w обязательно (доступность питания дороже).
  - ЖЁСТКИЕ ОГРАНИЧЕНИЯ:
        P(D > P) ≤ α          — допустимый риск нехватки (параметр политики);
        |P − P0| ≤ Δ_max      — граница разрешённого изменения;
        P ≥ 0.
  - РЕШАТЕЛЬ: детерминированный перебор сетки кандидатов с точным расчётом
    ожидаемых величин по сценарием (без случайного сэмплирования); бюджет
    времени ограничен; превышение → NO_CHANGE.

NO_CHANGE — допустимое решение задачи: если изменение не даёт значимого
улучшения или нарушает границы, предложение действия не формируется.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from statistics import median

RISK_EPS = 1e-12


@dataclass
class DemandScenarios:
    """Эмпирическое распределение спроса (граммы) для целевой сессии."""

    point_g: float
    residuals_g: list[float]
    values_g: list[float] = field(default_factory=list)
    source: str = "точечный прогноз + эмпирические калибровочные остатки (walk-forward)"

    @classmethod
    def build(cls, point_g: float, residuals_g: list[float]) -> "DemandScenarios":
        values = sorted(point_g + r for r in residuals_g)
        return cls(point_g=point_g, residuals_g=list(residuals_g), values_g=values)


def expected_outcome(p_g: float, scenarios: DemandScenarios) -> dict:
    """Точные ожидаемые величины по дискретному набору сценариев (детерминированно)."""
    n = len(scenarios.values_g)
    waste = sum(max(p_g - v, 0.0) for v in scenarios.values_g) / n
    shortage = sum(max(v - p_g, 0.0) for v in scenarios.values_g) / n
    risk = sum(1 for v in scenarios.values_g if v > p_g + RISK_EPS) / n
    return {"waste_g_mean": waste, "shortage_g_mean": shortage,
            "shortage_risk": risk, "n_scenarios": n}


def total_cost(p_g: float, scenarios: DemandScenarios, *,
               cost_waste_per_kg: float, cost_shortage_per_kg: float) -> float:
    o = expected_outcome(p_g, scenarios)
    return (o["waste_g_mean"] * cost_waste_per_kg
            + o["shortage_g_mean"] * cost_shortage_per_kg) / 1000.0


def scenario_cost_deltas(p_g: float, p0_g: float, scenarios: DemandScenarios, *,
                         cost_waste_per_kg: float,
                         cost_shortage_per_kg: float) -> list[float]:
    """Стоимость сценария при P минус при P0 (< 0 — экономия против NO_CHANGE)."""
    out = []
    for v in scenarios.values_g:
        c_p = (max(p_g - v, 0.0) * cost_waste_per_kg
               + max(v - p_g, 0.0) * cost_shortage_per_kg) / 1000.0
        c_p0 = (max(p0_g - v, 0.0) * cost_waste_per_kg
                + max(v - p0_g, 0.0) * cost_shortage_per_kg) / 1000.0
        out.append(c_p - c_p0)
    return sorted(out)


def habitual_baseline(history, *, days: int) -> float | None:
    """Оценка «готовить как обычно»: медиана prepared_mass последних `days` точек.

    history — точки с известной prepared_mass_g, отсортированные по дате.
    Возвращает None, если точек нет (базовая линия неизвестна → NO_CHANGE).
    """
    pts = [p.prepared_mass_g for p in history
           if p.service_date is not None and p.prepared_mass_g is not None]
    if not pts:
        return None
    pts = pts[-days:]
    return median(pts)


def solve_prepared_volume(*, baseline_g: float, scenarios: DemandScenarios,
                          cost_waste_per_kg: float, cost_shortage_per_kg: float,
                          shortage_risk_max: float, delta_max_g: float,
                          grid_step_g: float, time_budget_s: float = 2.0,
                          max_candidates: int = 4000) -> dict:
    """Детерминированный перебор допустимых кандидатов; возвращает решение задачи.

    Статусы: OK (выбран кандидат), NO_CHANGE_PREFERRED (улучшение незначимо),
    INFEASIBLE (в границах разрешённого изменения нет кандидата с допустимым
    риском нехватки), TIMEOUT (бюджет времени исчерпан).
    Ограждения никогда не расширяются молча.
    """
    deadline = time.monotonic() + time_budget_s
    lo = max(0.0, baseline_g - delta_max_g)
    hi = baseline_g + delta_max_g
    checks = {
        "shortage_risk_max": {"limit": shortage_risk_max, "pass": False},
        "delta_bound_g": {"limit": delta_max_g, "pass": False},
        "nonnegative_mass_g": {"min": 0.0, "pass": False},
        "time_budget_s": {"budget": time_budget_s, "pass": False, "elapsed_s": None},
        "grid": {"step_g": grid_step_g, "n_candidates": 0},
    }
    if grid_step_g <= 0:
        return {"status": "BAD_PARAMS", "reason": "grid_step_g должен быть > 0",
                "chosen_g": None, "checks": checks}
    span = hi - lo
    step = grid_step_g
    n_steps = int(span / step) + 1
    if n_steps > max_candidates:
        step = span / (max_candidates - 1) if span > 0 else grid_step_g
        n_steps = max_candidates
        checks["grid"]["note"] = "шаг увеличен, чтобы уложиться в лимит кандидатов"
    candidates = [lo + i * step for i in range(n_steps)]
    if span > 0 and candidates[-1] < hi - 1e-9:
        candidates.append(hi)
    checks["grid"]["n_candidates"] = len(candidates)

    base_out = expected_outcome(baseline_g, scenarios)
    cost_base = (base_out["waste_g_mean"] * cost_waste_per_kg
                 + base_out["shortage_g_mean"] * cost_shortage_per_kg) / 1000.0
    checks["time_budget_s"]["pass"] = True  # будет перекрыто при TIMEOUT

    best: tuple[float, float] | None = None  # (cost, P)
    feasible_count = 0
    for cand in candidates:
        if time.monotonic() > deadline:
            checks["time_budget_s"]["pass"] = False
            checks["time_budget_s"]["elapsed_s"] = time_budget_s
            return {"status": "TIMEOUT",
                    "reason": f"бюджет решателя {time_budget_s}s исчерпан на "
                              f"{len(candidates)} кандидатах",
                    "chosen_g": None, "baseline_g": baseline_g,
                    "baseline_metrics": base_out, "cost_baseline": cost_base,
                    "checks": checks}
        o = expected_outcome(cand, scenarios)
        if o["shortage_risk"] > shortage_risk_max + RISK_EPS:
            continue
        feasible_count += 1
        cost = (o["waste_g_mean"] * cost_waste_per_kg
                + o["shortage_g_mean"] * cost_shortage_per_kg) / 1000.0
        if best is None or cost < best[0] - 1e-12:
            best = (cost, cand)

    checks["shortage_risk_max"]["baseline_risk"] = base_out["shortage_risk"]
    checks["shortage_risk_max"]["baseline_pass"] = (
        base_out["shortage_risk"] <= shortage_risk_max + RISK_EPS)
    if best is None:
        return {"status": "INFEASIBLE",
                "reason": "в границах разрешённого изменения нет кандидата с допустимым "
                          "риском нехватки; расширять границы молча запрещено",
                "chosen_g": None, "baseline_g": baseline_g,
                "baseline_metrics": base_out, "cost_baseline": cost_base,
                "checks": checks}
    cost_best, p_best = best
    delta = p_best - baseline_g
    checks["shortage_risk_max"].update({
        "chosen_risk": expected_outcome(p_best, scenarios)["shortage_risk"],
        "pass": expected_outcome(p_best, scenarios)["shortage_risk"] <= shortage_risk_max + RISK_EPS})
    checks["delta_bound_g"].update({"chosen_abs_delta_g": abs(delta),
                                    "pass": abs(delta) <= delta_max_g + 1e-9})
    checks["nonnegative_mass_g"]["pass"] = p_best >= 0.0

    improvement = cost_base - cost_best
    result = {
        "status": "OK",
        "chosen_g": p_best, "baseline_g": baseline_g, "delta_g": delta,
        "baseline_metrics": base_out, "chosen_metrics": expected_outcome(p_best, scenarios),
        "cost_baseline": cost_base, "cost_chosen": cost_best,
        "improvement": improvement,
        "improvement_rel": improvement / max(cost_base, 1e-9),
        "n_feasible_candidates": feasible_count,
        "checks": checks,
    }
    # Консервативное правило: изменение меньше шага сетки не рекомендуется.
    if abs(delta) < step - 1e-9:
        result["status"] = "NO_CHANGE_PREFERRED"
        result["reason"] = ("лучший допустимый кандидат отличается от базовой линии "
                            f"меньше шага сетки ({step:.1f} г) — изменение ниже "
                            "разрешения рекомендации")
    return result
