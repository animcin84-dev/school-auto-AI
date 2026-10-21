"""Фазы исследования школы: CALIBRATION → BASELINE → SHADOW → INTERVENTION → FOLLOWUP.

Журнал переходов — append-only (таблица phase_events). Ворота переходов
проверяются кодом:
  - порядок строго вперёд, по одной фазе (перескоки запрещены);
  - BASELINE: есть хотя бы одна сессия школы и ссылка на протокол измерений
    (protocol_ref обязателен — выдумывать протокол запрещено);
  - SHADOW: данных достаточно для прогнозов (check_readiness) и зарегистрирована
    применимая для среды политика решения;
  - INTERVENTION: ЗАБЛОКИРОВАН в этапе 5а — требует согласия школы, VERIFIED
    протокола и политик, утверждённого набора действий, одобренного плана
    оценки с критериями остановки и условий этапа 6; список условий возвращается
    явно, а не придумывается выполненным;
  - FOLLOWUP: достижим только после INTERVENTION, поэтому тоже недостижим.
"""

from __future__ import annotations

from . import enums
from .errors import GuardError, NotFoundError
from .forecasting.backtest import check_readiness
from .forecasting.series import build_target_series
from .optimizer.policies import check_policy_usable

PHASE_ORDER = [enums.PHASE_CALIBRATION, enums.PHASE_BASELINE, enums.PHASE_SHADOW,
               enums.PHASE_INTERVENTION, enums.PHASE_FOLLOWUP]

# Пороги достаточности данных для входа в SHADOW. Это начальные инженерные
# значения для проверки кода; реальный порог задаётся протоколом этапа 5.
SHADOW_GATE_READINESS = {"min_train": 5, "calib_days": 5, "test_days": 3}

INTERVENTION_CONDITIONS = [
    "письменное согласие школы на вмешательство и использование данных (документ с происхождением)",
    "проверенный (VERIFIED) протокол измерений: точки взвешивания, тара, границы категорий",
    "проверенные (VERIFIED) политики, применимые к юрисдикции и дате",
    "утверждённый уполномоченным набор разрешённых действий",
    "одобренный план оценки эффекта, включая критерии остановки",
    "выполнение условий этапа 6 (docs/ARCHITECTURE_AND_GATES.md)",
]


class PhaseManager:
    def __init__(self, store, policies=None):
        self.store = store
        self.policies = policies

    def current(self, school_id: str) -> dict | None:
        return self.store.get_school_phase(school_id)

    def transition(self, school_id: str, to_phase: str, *, actor: str,
                   protocol_ref: str | None = None, policy_id: str | None = None,
                   meal_type: str = "LUNCH") -> dict:
        """Переход к следующей фазе. Порядок и ворота проверяются до записи."""
        cur = self.current(school_id)
        cur_phase = cur["phase"] if cur else None
        if cur_phase is None:
            expected = PHASE_ORDER[0]
        else:
            idx = PHASE_ORDER.index(cur_phase)
            if idx + 1 >= len(PHASE_ORDER):
                raise GuardError(f"фаза {cur_phase} конечная; переходы дальше не определены",
                                 code="E_PHASE_FINAL")
            expected = PHASE_ORDER[idx + 1]
        if to_phase != expected:
            raise GuardError(
                f"переход {cur_phase or 'START'} → {to_phase} нарушает порядок фаз; "
                f"ожидается следующий шаг: {expected}",
                code="E_PHASE_ORDER")
        self._gate(school_id, to_phase, protocol_ref=protocol_ref,
                   policy_id=policy_id, meal_type=meal_type)
        return self.store.add_phase_event(school_id=school_id, phase=to_phase,
                                          actor=actor, protocol_ref=protocol_ref,
                                          notes=self._gate_note(to_phase, policy_id))

    def _gate_note(self, to_phase: str, policy_id: str | None) -> str | None:
        if to_phase == enums.PHASE_SHADOW and policy_id:
            return f"политика решения: {policy_id}"
        return None

    def _gate(self, school_id: str, to_phase: str, *, protocol_ref: str | None,
              policy_id: str | None, meal_type: str) -> None:
        if to_phase == enums.PHASE_CALIBRATION:
            return
        if to_phase == enums.PHASE_BASELINE:
            sessions = self.store.fetch_sessions(school_id=school_id)
            if not sessions:
                raise GuardError(
                    "BASELINE требует хотя бы одной импортированной сессии школы",
                    code="E_GATE_NO_DATA")
            if not (protocol_ref or "").strip():
                raise GuardError(
                    "BASELINE требует protocol_ref — ссылку на протокол измерений; "
                    "протокол не может быть выдуман системой",
                    code="E_GATE_PROTOCOL_REQUIRED")
            return
        if to_phase == enums.PHASE_SHADOW:
            series = build_target_series(self.store, school_id, meal_type)
            readiness = check_readiness(series.points, **SHADOW_GATE_READINESS)
            if readiness["status"] != enums.METRIC_OK:
                raise GuardError(
                    "SHADOW требует достаточных данных для прогнозов: "
                    + "; ".join(readiness["reasons"]),
                    code="E_GATE_DATA_NOT_READY", details=readiness)
            if self.policies is None or not (policy_id or "").strip():
                raise GuardError(
                    "SHADOW требует зарегистрированную политику решения (policy_id)",
                    code="E_GATE_POLICY_REQUIRED")
            try:
                row = self.policies.get(policy_id)
            except NotFoundError:
                raise GuardError(f"политика решения {policy_id!r} не найдена",
                                 code="E_GATE_POLICY_REQUIRED")
            problems = check_policy_usable(row, environment=self.store.environment)
            if problems:
                raise GuardError("политика решения не применима: " + "; ".join(problems),
                                 code="E_GATE_POLICY_NOT_USABLE")
            return
        if to_phase in (enums.PHASE_INTERVENTION, enums.PHASE_FOLLOWUP):
            raise GuardError(
                f"переход в {to_phase} заблокирован: этап 5а реализует только SHADOW-контур. "
                "Необходимые условия (все обязательны): " + "; ".join(INTERVENTION_CONDITIONS),
                code="E_INTERVENTION_BLOCKED_STAGE5",
                details={"conditions": INTERVENTION_CONDITIONS})
        raise GuardError(f"неизвестная фаза: {to_phase!r}", code="E_BAD_PHASE")
