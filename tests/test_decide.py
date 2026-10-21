"""Тесты защитного модуля решений: отказ NO_CHANGE и блокировки режимов."""

import shutil
import tempfile
import unittest
from pathlib import Path

from sra import enums
from sra.contracts import parse_session_row
from sra.decide import (COND_APPROVAL, COND_MODEL, COND_MODE_BLOCKED, COND_REAL_DATA,
                        COND_VERIFIED_POLICY, propose_action)
from sra.policies import PolicyRegistry
from sra.store import EvidenceStore
from tests.helpers import make_session

CLOCK = lambda: "2026-10-21T12:00:00+00:00"  # noqa: E731


class DecideTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="sra-decide-"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestNoChangeDefault(DecideTestCase):
    def test_demo_env_no_data_always_no_change(self):
        store = EvidenceStore(self.tmp / "demo.db", enums.ENV_DEMO, clock=CLOCK)
        try:
            p = propose_action(store, PolicyRegistry(store), school_id="SCH-X",
                               target_date="2026-10-22").to_dict()
            self.assertEqual(p["action"], "NO_CHANGE")
            self.assertEqual(p["action_status"], "NOT_EXECUTABLE")
            self.assertEqual(p["mode"], enums.MODE_DEMO)
            for cond in (COND_REAL_DATA, COND_VERIFIED_POLICY, COND_MODEL, COND_APPROVAL):
                self.assertIn(cond, p["reasons"])
            self.assertTrue(any("НЕ подтверждает безопасность" in n for n in p["notes"]))
            # нет исполняемых параметров действия
            self.assertIsNone(p["action_params"])
            self.assertIsNone(p["approval"])
        finally:
            store.close()

    def test_shadow_requested_blocked(self):
        store = EvidenceStore(self.tmp / "real.db", enums.ENV_REAL, clock=CLOCK)
        try:
            p = propose_action(store, PolicyRegistry(store), school_id="SCH-X",
                               target_date="2026-10-22", requested_mode=enums.MODE_SHADOW).to_dict()
            self.assertIn(COND_MODE_BLOCKED, p["reasons"])
            self.assertEqual(p["action"], "NO_CHANGE")
        finally:
            store.close()

    def test_operational_requested_blocked_even_with_data_and_policy(self):
        store = EvidenceStore(self.tmp / "real.db", enums.ENV_REAL, clock=CLOCK)
        try:
            store.import_sessions(
                [parse_session_row(make_session(session_id="S1"), row_number=0)], actor="a")
            reg = PolicyRegistry(store)
            reg.register(policy_id="POL-KZ-1", version=1, jurisdiction="KZ",
                         source_ref="документ-источник", valid_from="2026-01-01",
                         valid_until="2027-01-01", payload={"min_service_level": 95},
                         actor="a", data_nature=enums.REAL_OBSERVED)
            reg.verify_policy("POL-KZ-1", 1, verifier="Проверяющий", method="сверка с документом",
                              actor="a")
            p = propose_action(store, reg, school_id="SCH-1", target_date="2026-10-22",
                               jurisdiction="KZ", requested_mode=enums.MODE_OPERATIONAL).to_dict()
            # данные и политика есть, но модель и одобрения отсутствуют + режим заблокирован
            self.assertNotIn(COND_REAL_DATA, p["reasons"])
            self.assertNotIn(COND_VERIFIED_POLICY, p["reasons"])
            self.assertEqual(p["policy_refs"], ["POL-KZ-1@v1"])
            self.assertIn(COND_MODEL, p["reasons"])
            self.assertIn(COND_APPROVAL, p["reasons"])
            self.assertIn(COND_MODE_BLOCKED, p["reasons"])
            self.assertEqual(p["action"], "NO_CHANGE")
        finally:
            store.close()

    def test_missing_real_data_even_if_synthetic_present(self):
        """Синтетика не считается реальными данными для решения."""
        store = EvidenceStore(self.tmp / "demo.db", enums.ENV_DEMO, clock=CLOCK)
        try:
            store.import_sessions([parse_session_row(
                make_session(session_id="S1", evidence_status="SYNTHETIC",
                             source_ref="synthetic:v1:seed=42"), row_number=0)], actor="a")
            p = propose_action(store, PolicyRegistry(store), school_id="SCH-1",
                               target_date="2026-10-22").to_dict()
            self.assertIn(COND_REAL_DATA, p["reasons"])
        finally:
            store.close()


if __name__ == "__main__":
    unittest.main()
