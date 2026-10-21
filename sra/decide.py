"""Защитный модуль решений (Decision Optimizer, этап 4 в полном объёме).

Этап 1 реализует КОНТРАКТ решения и отказ-по-умолчанию:
  - предложение действия требует: достаточных REAL-данных, применимой VERIFIED
    политики, готовой проверенной модели прогноза и отдельного одобрения;
  - любое отсутствующее условие => NO_CHANGE со списком причин;
  - NO_CHANGE — это отказ от НОВОГО вмешательства; он не подтверждает
    безопасность текущей практики;
  - SHADOW/OPERATIONAL-режимы в этапе 1 заблокированы (нет модели и политик);
  - имитация «успешного» действия отсутствует по построению: у предложения нет
    исполняемого действия, пока не выполнены условия.

Полный оптимизатор (этап 4) заменит чек-лист расчётом, но сохранит контракт
DecisionProposal и правило NO_CHANGE при невыполнимых предпосылках.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from . import enums

# Коды недостающих условий (стабильный словарь причин NO_CHANGE).
COND_REAL_DATA = "MISSING_REAL_DATA"
COND_VERIFIED_POLICY = "MISSING_VERIFIED_POLICY"
COND_MODEL = "MISSING_VERIFIED_MODEL"
COND_APPROVAL = "MISSING_APPROVAL"
COND_MODE_BLOCKED = "MODE_BLOCKED_STAGE1"

REASON_DESCRIPTIONS = {
    COND_REAL_DATA: "нет достаточных REAL_OBSERVED-измерений в области решения",
    COND_VERIFIED_POLICY: "нет проверенной (VERIFIED) политики, применимой к юрисдикции и дате",
    COND_MODEL: "нет обученной и проверенной модели прогноза (этапы 3–4)",
    COND_APPROVAL: "нет одобрения уполномоченного человека на конкретную версию действия",
    COND_MODE_BLOCKED: "режим SHADOW/OPERATIONAL заблокирован до завершения этапов 3–5",
}


@dataclass
class DecisionProposal:
    """Контракт рекомендации (основа для этапов 3–4).

    Поля будущих этапов (forecast_ref, action_params, expected_effect_interval,
    constraint_checks) уже присутствуют как явные None/пустые значения —
    это дизайн контракта, а не реализованная функциональность.
    """
    proposal_id: str
    mode: str                       # DEMO | SHADOW | OPERATIONAL
    environment: str                # REAL | DEMO
    decision_at: str                # момент принятия решения
    school_id: str
    target_date: str                # дата обслуживания, на которую план
    action: str                     # NO_CHANGE | ADJUST_PREPARED_VOLUME | ...
    action_status: str              # NOT_EXECUTABLE | PENDING_APPROVAL | ...
    reasons: list[str] = field(default_factory=list)
    missing_conditions: list[str] = field(default_factory=list)
    action_params: dict | None = None
    forecast_ref: str | None = None
    policy_refs: list[str] = field(default_factory=list)
    expected_effect_interval: dict | None = None
    constraint_checks: dict = field(default_factory=dict)
    approval: dict | None = None    # ссылка на согласование конкретной версии
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "proposal_id": self.proposal_id, "mode": self.mode,
            "environment": self.environment, "decision_at": self.decision_at,
            "school_id": self.school_id, "target_date": self.target_date,
            "action": self.action, "action_status": self.action_status,
            "reasons": self.reasons, "missing_conditions": self.missing_conditions,
            "action_params": self.action_params, "forecast_ref": self.forecast_ref,
            "policy_refs": self.policy_refs,
            "expected_effect_interval": self.expected_effect_interval,
            "constraint_checks": self.constraint_checks, "approval": self.approval,
            "notes": self.notes,
        }


def propose_action(store, policies, *, school_id: str, target_date: str,
                   jurisdiction: str = "KZ", requested_mode: str | None = None,
                   decision_at: str | None = None,
                   min_real_sessions: int = 1) -> DecisionProposal:
    """Формирует предложение действия. В этапе 1 всегда NO_CHANGE:
    обязательные условия (модель, политики, одобрения) ещё не реализованы.

    min_real_sessions — предварительный инженерный порог достаточности данных;
    реальный порог задаётся протоколом этапа 5, не здесь.
    """
    env = store.environment
    if requested_mode in (enums.MODE_SHADOW, enums.MODE_OPERATIONAL):
        mode = requested_mode
    else:
        mode = enums.MODE_DEMO if env == enums.ENV_DEMO else enums.MODE_SHADOW
    reasons: list[str] = []
    policy_refs: list[str] = []

    # 1. Реальные данные в области решения.
    n_real = sum(1 for s in store.fetch_sessions(school_id=school_id, date_to=target_date)
                 if s.evidence_status in (enums.REAL_OBSERVED, enums.VERIFIED))
    has_data = n_real >= min_real_sessions
    if not has_data:
        reasons.append(COND_REAL_DATA)

    # 2. Проверенная действующая политика.
    applicable = policies.find_applicable(jurisdiction=jurisdiction, on_date=target_date)
    if not applicable:
        reasons.append(COND_VERIFIED_POLICY)
    else:
        policy_refs = [f"{p['policy_id']}@v{p['version']}" for p in applicable]

    # 3. Модель прогноза (этап 3–4) — в этапе 1 отсутствует по построению.
    reasons.append(COND_MODEL)

    # 4. Одобрение — отсутствует по построению (нет контура согласований).
    reasons.append(COND_APPROVAL)

    # 5. Блокировка режимов сверх DEMO до завершения этапов 3–5.
    if mode in (enums.MODE_SHADOW, enums.MODE_OPERATIONAL):
        reasons.append(COND_MODE_BLOCKED)

    notes = [
        "NO_CHANGE — отказ от нового вмешательства; он НЕ подтверждает безопасность "
        "текущей практики кухни",
        "расчёт метрик/прогнозов продолжает работать независимо от отказа в действии",
    ]
    if env == enums.ENV_DEMO:
        notes.append("среда DEMO: данные и политики синтетические; результат — симуляция сценария")
    return DecisionProposal(
        proposal_id=uuid.uuid4().hex,
        mode=mode,
        environment=env,
        decision_at=decision_at or store._clock(),
        school_id=school_id,
        target_date=target_date,
        action="NO_CHANGE",
        action_status="NOT_EXECUTABLE",
        reasons=reasons,
        missing_conditions=[REASON_DESCRIPTIONS[r] for r in reasons],
        policy_refs=policy_refs,
        constraint_checks={"checklist_v1": "not_applicable_in_stage1"},
        notes=notes,
    )
