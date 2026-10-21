"""Реестр политик: версионированные параметры с источником, юрисдикцией,
сроком действия и статусом проверки.

Правила:
  - конкретные нормативные значения НЕИЗВЕСТНЫ и сюда не выдумываются;
  - каждая политика: источник (source_ref), юрисдикция, версия, срок действия,
    статус UNVERIFIED/VERIFIED/RETIRED;
  - VERIFIED для политики — отдельная процедура verify_policy();
  - для реальных решений допускаются только VERIFIED и действующие политики;
  - демо может регистрировать синтетические политики — они остаются
    SYNTHETIC/UNVERIFIED и не дают права на реальное вмешательство.
"""

from __future__ import annotations

import json
from datetime import date

from . import enums
from .errors import GuardError, NotFoundError, ValidationError


class PolicyRegistry:
    """Работает поверх EvidenceStore (та же БД, таблица policies)."""

    def __init__(self, store):
        self.store = store

    def register(self, *, policy_id: str, version: int, jurisdiction: str, source_ref: str,
                 valid_from: str, valid_until: str, payload: dict, actor: str,
                 data_nature: str = enums.REAL_OBSERVED) -> dict:
        """Регистрирует новую версию политики.

        data_nature — природа ЗНАЧЕНИЙ в payload: REAL_OBSERVED (значения взяты
        из документа-источника) или SYNTHETIC (искусственные значения для демо).
        """
        if not policy_id or not source_ref:
            raise ValidationError("политика требует policy_id и source_ref", code="E_POLICY_PARAMS")
        if data_nature not in (enums.REAL_OBSERVED, enums.SYNTHETIC):
            raise ValidationError("data_nature политики: REAL_OBSERVED | SYNTHETIC",
                                  code="E_POLICY_NATURE")
        date.fromisoformat(valid_from)
        date.fromisoformat(valid_until)
        now = self.store._clock()
        with self.store._conn:
            self.store._conn.execute(
                "INSERT INTO policies(policy_id, version, jurisdiction, source_ref, valid_from, "
                "valid_until, verification_status, payload, created_at, created_by) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (policy_id, version, jurisdiction, source_ref, valid_from, valid_until,
                 enums.POLICY_UNVERIFIED,
                 json.dumps({"values": payload, "data_nature": data_nature}, ensure_ascii=False),
                 now, actor))
            self.store._audit(actor, "POLICY_REGISTERED", entity_type="policy", entity_id=policy_id,
                              details={"version": version, "jurisdiction": jurisdiction,
                                       "source_ref": source_ref, "data_nature": data_nature,
                                       "status": enums.POLICY_UNVERIFIED})
        return {"policy_id": policy_id, "version": version,
                "verification_status": enums.POLICY_UNVERIFIED}

    def verify_policy(self, policy_id: str, version: int, *, verifier: str, method: str,
                      actor: str) -> dict:
        """Проверка политики проверяющим. Синтетические политики НЕ проверяемы:
        их значения искусственны и не могут стать нормативом."""
        row = self._get(policy_id, version)
        payload = json.loads(row["payload"])
        if payload.get("data_nature") == enums.SYNTHETIC:
            raise GuardError(
                f"политика {policy_id} v{version} имеет SYNTHETIC-значения и не может стать "
                "VERIFIED; реальный норматив требует документа-источника",
                code="E_SYNTHETIC_CANNOT_BE_VERIFIED")
        now = self.store._clock()
        with self.store._conn:
            self.store._conn.execute(
                "UPDATE policies SET verification_status = ? WHERE policy_id = ? AND version = ?",
                (enums.POLICY_VERIFIED, policy_id, version))
            self.store._audit(actor, "POLICY_VERIFIED", entity_type="policy", entity_id=policy_id,
                              details={"version": version, "verifier": verifier, "method": method})
        return {"policy_id": policy_id, "version": version,
                "verification_status": enums.POLICY_VERIFIED}

    def retire(self, policy_id: str, version: int, *, actor: str, reason: str) -> dict:
        with self.store._conn:
            self.store._conn.execute(
                "UPDATE policies SET verification_status = ? WHERE policy_id = ? AND version = ?",
                (enums.POLICY_RETIRED, policy_id, version))
            self.store._audit(actor, "POLICY_RETIRED", entity_type="policy", entity_id=policy_id,
                              details={"version": version, "reason": reason})
        return {"policy_id": policy_id, "version": version, "verification_status": enums.POLICY_RETIRED}

    def _get(self, policy_id: str, version: int) -> dict:
        row = self.store._conn.execute(
            "SELECT * FROM policies WHERE policy_id = ? AND version = ?",
            (policy_id, version)).fetchone()
        if row is None:
            raise NotFoundError(f"политика {policy_id} v{version} не найдена")
        return dict(row)

    def find_applicable(self, *, jurisdiction: str, on_date: str,
                        require_verified: bool = True) -> list[dict]:
        """Действующие на дату политики юрисдикции; по умолчанию только VERIFIED."""
        out = []
        for row in self.store._conn.execute("SELECT * FROM policies").fetchall():
            if row["jurisdiction"] != jurisdiction:
                continue
            if require_verified and row["verification_status"] != enums.POLICY_VERIFIED:
                continue
            if not (row["valid_from"] <= on_date <= row["valid_until"]):
                continue
            out.append(dict(row))
        return out
