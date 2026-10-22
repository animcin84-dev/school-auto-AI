"""SHADOW-контур (этап 5а): ежедневные предложения БЕЗ исполнения.

Для каждого дня окна заново строится предложение оптимизатора строго из
данных, доступных на момент решения (as-of D−1), затем день сопоставляется
с фактом (y = выданная масса, фактическая подготовка кухни). Результат
сохраняется как shadow-запись; это симуляционный протокол, из которого потом
считаются мониторинг качества и дизайн оценки. Исполнение действий не
выполняется и не имитируется (action_executions не пополняется).

Идемпотентность: content_key исключает proposal_id и время решения, поэтому
повторный прогон того же окна на тех же данных не создаёт дублей.
"""

from __future__ import annotations

import hashlib
import json

from . import enums
from .errors import ValidationError
from .forecasting.series import build_target_series
from .optimizer.propose import propose_adjustment
from .store import proposal_hash


def content_key(*, school_id: str, meal_type: str, target_date: str, action: str,
                planned_g: float | None, policy_refs: list[str],
                forecast_ref: str | None) -> str:
    """Стабильный ключ содержания shadow-дня (без id и времени)."""
    canonical = json.dumps({
        "school_id": school_id, "meal_type": meal_type, "target_date": target_date,
        "action": action, "planned_g": planned_g, "policy_refs": policy_refs,
        "forecast_ref": forecast_ref,
    }, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def run_shadow(store, policies, *, school_id: str, meal_type: str, policy_id: str,
               days: int = 7, actor: str = "shadow",
               decision_hour: int = 18) -> dict:
    if days < 1:
        raise ValidationError("days должен быть >= 1", code="E_BAD_WINDOW")
    series = build_target_series(store, school_id, meal_type)
    pts = [p for p in series.points
           if p.served_mass_g is not None and p.prepared_mass_g is not None]
    if not pts:
        return {"status": enums.DATA_NOT_READY,
                "reason": "нет дней с измеренными served и prepared массами",
                "days": 0, "inserted": 0, "records_total": 0}
    window = pts[-days:]
    prow = policies.get(policy_id)
    values = prow["payload_json"].get("values", {})
    cost_w = float(values["cost_waste_per_kg"])
    cost_s = float(values["cost_shortage_per_kg"])

    inserted = 0
    actions = {"ADJUST_PREPARED_VOLUME": 0, "NO_CHANGE": 0}
    for tp in window:
        proposal = propose_adjustment(
            store, policies, school_id=school_id, meal_type=meal_type,
            target_date=tp.service_date.isoformat(), policy_id=policy_id,
            actor=actor, decision_hour=decision_hour).to_dict()
        action = proposal["action"]
        planned_g = baseline_g = pred_g = q10 = q90 = None
        cost_delta = None
        if action == "ADJUST_PREPARED_VOLUME":
            actions[action] += 1
            ap = proposal["action_params"]
            planned_g = ap["planned_prepared_mass_g"]
            baseline_g = ap["baseline_mass_g"]
            fd = ap.get("forecast") or {}
            pred_g, q10, q90 = fd.get("pred_g"), fd.get("q10_g"), fd.get("q90_g")
            # Контрфакт фактического дня: стоимость при планировании P* против
            # фактической подготовки кухни (реализация, не сценарное среднее).
            y = tp.served_mass_g
            pa = tp.prepared_mass_g
            cost_delta = (cost_w * max(planned_g - y, 0.0)
                          + cost_s * max(y - planned_g, 0.0)
                          - (cost_w * max(pa - y, 0.0) + cost_s * max(y - pa, 0.0))
                          ) / 1000.0
        else:
            actions["NO_CHANGE"] += 1
        key = content_key(school_id=school_id, meal_type=meal_type,
                          target_date=tp.service_date.isoformat(), action=action,
                          planned_g=planned_g, policy_refs=proposal["policy_refs"],
                          forecast_ref=proposal.get("forecast_ref"))
        res = store.add_shadow_record(
            school_id=school_id, meal_type=meal_type,
            target_date=tp.service_date.isoformat(), content_key=key,
            action=action, proposal_hash=proposal_hash(proposal),
            planned_g=planned_g, baseline_g=baseline_g, pred_g=pred_g,
            pred_q10=q10, pred_q90=q90, y_g=tp.served_mass_g,
            actual_prepared_g=tp.prepared_mass_g,
            cost_delta_vs_actual=cost_delta, payload=proposal)
        if res["inserted"]:
            inserted += 1
    total = len(store.list_shadow_records(school_id, meal_type))
    return {
        "status": "OK",
        "artifact_nature": (enums.SIMULATED if store.environment == enums.ENV_DEMO
                            else enums.PREDICTED),
        "window": [window[0].service_date.isoformat(), window[-1].service_date.isoformat()],
        "days": len(window),
        "inserted": inserted,
        "records_total": total,
        "actions": actions,
        "note": "SHADOW: предложения рассчитаны со статусом NOT_EXECUTABLE; кухня не "
                "изменялась; cost_delta_vs_actual — контрфакт фактического дня (SIMULATED), "
                "не измеренная экономия",
    }
