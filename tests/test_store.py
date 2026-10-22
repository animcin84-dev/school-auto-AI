"""Тесты Evidence Store: среды, ревизии, VERIFIED-процедура, as-of, самопроверка."""

import json
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

from sra import enums
from sra.contracts import parse_observation_row, parse_session_row
from sra.errors import GuardError, NotFoundError, ValidationError
from sra.store import EvidenceStore
from tests.helpers import make_observation, make_session

CLOCK = lambda: "2026-10-21T12:00:00+00:00"  # noqa: E731


class StoreTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="sra-test-"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def real_store(self, name="real.sqlite3") -> EvidenceStore:
        return EvidenceStore(self.tmp / name, enums.ENV_REAL, clock=CLOCK)

    def demo_store(self, name="demo.sqlite3") -> EvidenceStore:
        return EvidenceStore(self.tmp / name, enums.ENV_DEMO, clock=CLOCK)

    def _session(self, **over):
        return parse_session_row(make_session(**over), row_number=0)

    def _observation(self, **over):
        return parse_observation_row(make_observation(**over), row_number=0)


class TestEnvironmentGuards(StoreTestCase):
    def test_marker_consistency(self):
        store = self.real_store()
        store.close()
        with self.assertRaises(GuardError) as cm:
            EvidenceStore(self.tmp / "real.sqlite3", enums.ENV_DEMO, clock=CLOCK)
        self.assertEqual(cm.exception.code, "E_ENV_MISMATCH")

    def test_synthetic_rejected_in_real(self):
        store = self.real_store()
        try:
            with self.assertRaises(GuardError) as cm:
                store.import_sessions(
                    [self._session(evidence_status="SYNTHETIC")], actor="t")
            self.assertEqual(cm.exception.code, "E_SYNTHETIC_IN_REAL_ENV")
        finally:
            store.close()

    def test_real_rejected_in_demo(self):
        store = self.demo_store()
        try:
            with self.assertRaises(GuardError) as cm:
                store.import_sessions([self._session()], actor="t")
            self.assertEqual(cm.exception.code, "E_REAL_IN_DEMO_ENV")
        finally:
            store.close()

    def test_verified_import_rejected_everywhere(self):
        store = self.real_store()
        try:
            with self.assertRaises(ValidationError) as cm:
                store.import_sessions([self._session(evidence_status="VERIFIED")], actor="t")
            self.assertEqual(cm.exception.code, "E_BAD_RAW_EVIDENCE")
        finally:
            store.close()


class TestImportAndRevisions(StoreTestCase):
    def test_import_creates_revision_and_audit(self):
        store = self.real_store()
        try:
            res = store.import_sessions([self._session()], actor="alice")
            self.assertEqual(res["imported"], 1)
            self.assertEqual(res["dataset_version"], 1)
            cur = store.get_current("session", "S1")
            self.assertEqual(cur["evidence_status"], enums.REAL_OBSERVED)
            self.assertEqual(cur["created_by"], "alice")
            audit_actions = [e["action"] for e in store.audit_tail(100)]
            self.assertIn("RECORD_CREATED", audit_actions)
            self.assertIn("IMPORT_APPLIED", audit_actions)
        finally:
            store.close()

    def test_duplicate_session_rejected(self):
        store = self.real_store()
        try:
            store.import_sessions([self._session()], actor="a")
            with self.assertRaises(ValidationError) as cm:
                store.import_sessions([self._session()], actor="a")
            self.assertEqual(cm.exception.code, "E_DUPLICATE_ID")
        finally:
            store.close()

    def test_observations_require_known_session(self):
        store = self.real_store()
        try:
            with self.assertRaises(ValidationError) as cm:
                store.import_observations([self._observation()], actor="a")
            self.assertEqual(cm.exception.code, "E_UNKNOWN_SESSION")
        finally:
            store.close()

    def test_revise_creates_chain(self):
        store = self.real_store()
        try:
            store.import_sessions([self._session(meals_served="90")], actor="a")
            res = store.revise("session", "S1", {"meals_served": 85},
                               reason="опечатка при вводе", actor="b",
                               recorded_at="2026-10-02T10:00:00+05:00")
            self.assertEqual(res["supersedes"], 1)
            self.assertEqual(res["revision_id"], 2)
            cur = store.get_current("session", "S1")
            self.assertEqual(cur["payload"]["meals_served"], 85)
            # история сохранена
            row = store._conn.execute("SELECT payload FROM revisions WHERE revision_id=1").fetchone()
            self.assertEqual(json.loads(row["payload"])["meals_served"], 90)
            # смена id запрещена
            with self.assertRaises(ValidationError):
                store.revise("session", "S1", {"session_id": "S2"},
                             reason="r", actor="b", recorded_at=CLOCK())
        finally:
            store.close()

    def test_revision_invalid_value_rejected(self):
        store = self.real_store()
        try:
            store.import_observations
            store.import_sessions([self._session()], actor="a")
            store.import_observations([self._observation()], actor="a")
            with self.assertRaises(ValidationError) as cm:
                store.revise("observation", "O1", {"plate_waste_g": -5},
                             reason="r", actor="a", recorded_at=CLOCK())
            self.assertEqual(cm.exception.code, "E_REVISION_INVALID")
        finally:
            store.close()

    def test_asof_hides_future_corrections(self):
        """Утечка будущего: исправление, появившееся позже, не видно в прошлом срезе."""
        store = self.real_store()
        try:
            store.import_sessions([self._session(meals_served="90")], actor="a")
            store.revise("session", "S1", {"meals_served": 70},
                         reason="уточнение веса", actor="a",
                         recorded_at="2026-10-05T10:00:00+05:00")
            before = store.asof("session", "2026-10-03T00:00:00+05:00")
            after = store.asof("session", "2026-10-06T00:00:00+05:00")
            self.assertEqual(len(before), 1)
            self.assertEqual(before[0]["payload"]["meals_served"], 90)
            self.assertEqual(after[0]["payload"]["meals_served"], 70)
        finally:
            store.close()


class TestVerificationProcedure(StoreTestCase):
    def test_synthetic_cannot_be_verified(self):
        store = self.demo_store()
        try:
            store.import_sessions([self._session(evidence_status="SYNTHETIC",
                                                 source_ref="synthetic:v1:seed=1")], actor="a")
            with self.assertRaises(GuardError) as cm:
                store.verify("session", "S1", verifier="V", method="cross-check",
                             source_ref="doc", actor="a")
            self.assertEqual(cm.exception.code, "E_SYNTHETIC_CANNOT_BE_VERIFIED")
        finally:
            store.close()

    def test_verify_real_record(self):
        store = self.real_store()
        try:
            store.import_sessions([self._session()], actor="a")
            res = store.verify("session", "S1", verifier="Иванова А.",
                               method="сверка с журналом", source_ref="журнал-2026-10",
                               actor="a")
            self.assertEqual(res["evidence_status"], enums.VERIFIED)
            cur = store.get_current("session", "S1")
            self.assertEqual(cur["evidence_status"], enums.VERIFIED)
            # повторная проверка не нужна и запрещена
            with self.assertRaises(GuardError) as cm:
                store.verify("session", "S1", verifier="V", method="m", source_ref="s", actor="a")
            self.assertEqual(cm.exception.code, "E_ALREADY_VERIFIED")
        finally:
            store.close()

    def test_revising_verified_requires_reverification(self):
        store = self.real_store()
        try:
            store.import_sessions([self._session()], actor="a")
            store.verify("session", "S1", verifier="V", method="m", source_ref="s", actor="a")
            res = store.revise("session", "S1", {"meals_served": 80},
                               reason="уточнение", actor="a",
                               recorded_at="2026-10-03T10:00:00+05:00")
            self.assertEqual(res["evidence_status"], enums.REAL_OBSERVED)
            self.assertTrue(res["pending_verification"])
            self.assertIn("REVERIFICATION_REQUIRED", res["quality_flags"])
            cur = store.get_current("session", "S1")
            self.assertEqual(cur["evidence_status"], enums.REAL_OBSERVED)
            # повторная проверка возможна и возвращает VERIFIED
            res2 = store.verify("session", "S1", verifier="V2", method="m2",
                                source_ref="s2", actor="a")
            self.assertEqual(res2["evidence_status"], enums.VERIFIED)
        finally:
            store.close()


class TestAsofMixedOffsets(StoreTestCase):
    """As-of сравнивает время в UTC: разные offset в recorded_at корректны.

    Ревизия recorded_at = 2026-10-02T12:00:00+00:00 (= 17:00 +05) НЕ видна
    в срезе 2026-10-02T16:00:00+05:00, хотя строковое сравнение было бы неверно.
    """

    def test_asof_uses_utc_semantics(self):
        store = self.real_store()
        try:
            store.import_sessions([self._session(meals_served="90")], actor="a")
            store.revise("session", "S1", {"meals_served": 70},
                         reason="уточнение", actor="a",
                         recorded_at="2026-10-02T12:00:00+00:00")
            before = store.asof("session", "2026-10-02T16:00:00+05:00")
            after = store.asof("session", "2026-10-02T18:00:00+05:00")
            self.assertEqual(before[0]["payload"]["meals_served"], 90)
            self.assertEqual(after[0]["payload"]["meals_served"], 70)
        finally:
            store.close()

    def test_asof_rejects_naive_cutoff(self):
        store = self.real_store()
        try:
            from sra.errors import ValidationError
            with self.assertRaises(ValidationError) as cm:
                store.asof("session", "2026-10-02T16:00:00")
            self.assertEqual(cm.exception.code, "E_BAD_CUTOFF")
        finally:
            store.close()


class TestModelCards(StoreTestCase):
    def test_versions_autoincrement_and_audit(self):
        store = self.real_store()
        try:
            c1 = store.save_model_card(model_id="sra.forecast.naive", model_name="naive",
                                       params={}, data_meta={"school_id": "SCH-1"},
                                       metrics={"mae_g": 20.0}, actor="t")
            c2 = store.save_model_card(model_id="sra.forecast.naive", model_name="naive",
                                       params={"weeks": 3}, data_meta={}, actor="t")
            self.assertEqual(c1["version"], 1)
            self.assertEqual(c2["version"], 2)
            cards = store.get_model_cards("sra.forecast.naive")
            self.assertEqual([c["version"] for c in cards], [1, 2])
            self.assertEqual(cards[0]["metrics"]["mae_g"], 20.0)
            actions = [e["action"] for e in store.audit_tail(100)]
            self.assertEqual(actions.count("MODEL_CARD_SAVED"), 2)
        finally:
            store.close()

    def test_get_dataset_version(self):
        store = self.real_store()
        try:
            v0 = store.get_dataset_version()
            store.import_sessions([self._session()], actor="a")
            self.assertEqual(store.get_dataset_version(), v0 + 1)
        finally:
            store.close()


class TestSelfcheck(StoreTestCase):
    def test_selfcheck_ok_on_healthy_store(self):
        store = self.real_store()
        try:
            store.import_sessions([self._session()], actor="a")
            store.import_observations([self._observation()], actor="a")
            results = store.selfcheck()
            self.assertTrue(all(r["ok"] for r in results),
                            msg=json.dumps(results, ensure_ascii=False))
        finally:
            store.close()

    def test_selfcheck_detects_tampering(self):
        store = self.real_store()
        try:
            store.import_sessions([self._session()], actor="a")
            store.verify("session", "S1", verifier="V", method="m", source_ref="s", actor="a")
            # прямая порча данных в обход API: удаляем строку проверки
            store._conn.execute("DELETE FROM verifications")
            store._conn.commit()
            results = store.selfcheck()
            failed = {r["check"] for r in results if not r["ok"]}
            self.assertIn("verified_has_verification_row", failed)
        finally:
            store.close()

    def test_stats_and_not_found(self):
        store = self.real_store()
        try:
            with self.assertRaises(NotFoundError):
                store.get_current("session", "NOPE")
        finally:
            store.close()


if __name__ == "__main__":
    unittest.main()
