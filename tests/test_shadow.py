"""Тесты этапа 5а: фазы с воротами, SHADOW-контур, мониторинг.

Контрольные значения мониторинга (ручной счёт):
  набор A: pred=1000, остатки [10,0,-10,10,0,-70] (y=[1010,1000,990,1010,1000,930]),
    q10=940, q90=1060 → MAE = 100/6 ≈ 16.667; покрытие = 5/6 ≈ 0.833
    (в допуске ±0.15 от номинала 0.8); медианы половин равны, MAD > 0 → статус OK.
    (Покрытие 1.0 дало бы WARN: перекрытие — тоже некалиброванность.)
  набор B: тот же y, q10=995, q90=1005 → покрытие 0 → WARN_COVERAGE.
  набор C: остатки [0]*6 + [200]*6 → дрейф → статус DRIFT_DETECTED.

Дизайн (ручной счёт): required_n(σ=1000, effect=400, α=0.05, power=0.80):
  z_sum = 1.959964 + 0.8416212 = 2.8015852; (2.8015852·1000/400)² = 49.0555;
  две группы → ×2 = 98.111 → ceil 99; парный дизайн → ceil(49.0555) = 50.
"""

import shutil
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from sra import enums
from sra.csvio import parse_file
from sra.demo.synthetic import generate
from sra.errors import GuardError
from sra.monitoring import monitor_shadow
from sra.phases import INTERVENTION_CONDITIONS, PHASE_ORDER, PhaseManager
from sra.policies import PolicyRegistry
from sra.shadow import run_shadow
from sra.store import EvidenceStore
from sra.validation import validate_observations, validate_sessions

OPT_POLICY = {
    "policy_id": "POLICY-DEMO-OPT-001",
    "payload": {
        "cost_waste_per_kg": 500.0, "cost_shortage_per_kg": 5000.0,
        "currency": "KZT_DEMO", "shortage_risk_max": 0.10,
        "delta_max_pct_of_baseline": 0.15, "baseline_days": 8,
        "forecast_model": "seasonal_naive", "seasonal_weeks": 3,
        "calib_days": 8, "grid_step_g": 250.0, "min_improvement_rel": 0.02,
    },
}


class ShadowFixture(unittest.TestCase):
    """DEMO-хранилище с синтетикой seed=42 и зарегистрированной политикой."""

    n_days = 60

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="sra-shadow-"))
        generate(cls.tmp / "data", seed=42, n_days=cls.n_days)
        cls.store = EvidenceStore(cls.tmp / "demo.sqlite3", enums.ENV_DEMO)
        for kind in ("sessions", "observations"):
            recs = parse_file(cls.tmp / "data" / f"{kind}.csv", kind)
            if kind == "sessions":
                rep = validate_sessions(recs, existing_ids=set())
                cls.store.import_sessions(rep.accepted, actor="t")
            else:
                rep = validate_observations(recs, known_sessions=cls.store.session_ids(),
                                            existing_observation_ids=set(),
                                            existing_pairs=set())
                cls.store.import_observations(rep.accepted, actor="t")
        cls.policies = PolicyRegistry(cls.store)
        cls.policies.register(policy_id=OPT_POLICY["policy_id"], version=1,
                              jurisdiction="DEMO", source_ref="synthetic:v1:seed=42",
                              valid_from="2000-01-01", valid_until="2099-12-31",
                              payload=OPT_POLICY["payload"], actor="t",
                              data_nature=enums.SYNTHETIC)

    @classmethod
    def tearDownClass(cls):
        cls.store.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)


class TestPhaseGates(ShadowFixture):
    def test_order_and_full_path_to_shadow(self):
        pm = PhaseManager(self.store, self.policies)
        school = "SCH-DEMO-001"
        self.assertIsNone(pm.current(school))
        # перескок в SHADOW/INTERVENTION с нуля запрещён
        with self.assertRaises(GuardError) as cm:
            pm.transition(school, enums.PHASE_SHADOW, actor="t")
        self.assertEqual(cm.exception.code, "E_PHASE_ORDER")
        pm.transition(school, enums.PHASE_CALIBRATION, actor="t")
        # BASELINE без протокола — отказ
        with self.assertRaises(GuardError) as cm:
            pm.transition(school, enums.PHASE_BASELINE, actor="t")
        self.assertEqual(cm.exception.code, "E_GATE_PROTOCOL_REQUIRED")
        pm.transition(school, enums.PHASE_BASELINE, actor="t",
                      protocol_ref="synthetic-protocol:v1")
        # SHADOW без политики — отказ
        pm2 = PhaseManager(self.store, None)
        with self.assertRaises(GuardError) as cm:
            pm2.transition(school, enums.PHASE_SHADOW, actor="t")
        self.assertEqual(cm.exception.code, "E_GATE_POLICY_REQUIRED")
        # SHADOW с неприменимой (REAL-nature) политикой — отказ
        self.policies.register(policy_id="POLICY-REAL-NATURE", version=1,
                               jurisdiction="DEMO", source_ref="doc",
                               valid_from="2000-01-01", valid_until="2099-12-31",
                               payload=OPT_POLICY["payload"], actor="t",
                               data_nature=enums.REAL_OBSERVED)
        with self.assertRaises(GuardError) as cm:
            pm.transition(school, enums.PHASE_SHADOW, actor="t",
                          policy_id="POLICY-REAL-NATURE")
        self.assertEqual(cm.exception.code, "E_GATE_POLICY_NOT_USABLE")
        # корректный путь
        pm.transition(school, enums.PHASE_SHADOW, actor="t",
                      policy_id=OPT_POLICY["policy_id"])
        cur = pm.current(school)
        self.assertEqual(cur["phase"], enums.PHASE_SHADOW)
        baseline_event = self.store._conn.execute(
            "SELECT protocol_ref FROM phase_events WHERE school_id=? AND phase=? "
            "ORDER BY phase_event_id DESC LIMIT 1",
            (school, enums.PHASE_BASELINE)).fetchone()
        self.assertEqual(baseline_event["protocol_ref"], "synthetic-protocol:v1")
        # INTERVENTION заблокирован с явным списком условий
        with self.assertRaises(GuardError) as cm:
            pm.transition(school, enums.PHASE_INTERVENTION, actor="t")
        self.assertEqual(cm.exception.code, "E_INTERVENTION_BLOCKED_STAGE5")
        self.assertEqual(cm.exception.details["conditions"], INTERVENTION_CONDITIONS)
        # FOLLOWUP недостижим (следующая после BASELINE... после SHADOW тоже блок)
        with self.assertRaises(GuardError):
            pm.transition(school, enums.PHASE_FOLLOWUP, actor="t")
        # журнал: все события сохранены, порядок полон
        events = self.store._conn.execute(
            "SELECT phase FROM phase_events WHERE school_id=? ORDER BY phase_event_id",
            (school,)).fetchall()
        self.assertEqual([e["phase"] for e in events],
                         [enums.PHASE_CALIBRATION, enums.PHASE_BASELINE,
                          enums.PHASE_SHADOW])
        actions = [e["action"] for e in self.store.audit_tail(100)]
        self.assertEqual(actions.count("PHASE_TRANSITION"), 3)

    def test_shadow_gate_data_insufficient(self):
        tmp = Path(tempfile.mkdtemp(prefix="sra-phase-tiny-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        generate(tmp / "data", seed=1, n_days=8)
        store = EvidenceStore(tmp / "demo.sqlite3", enums.ENV_DEMO)
        try:
            for kind in ("sessions", "observations"):
                recs = parse_file(tmp / "data" / f"{kind}.csv", kind)
                if kind == "sessions":
                    rep = validate_sessions(recs, existing_ids=set())
                    store.import_sessions(rep.accepted, actor="t")
                else:
                    rep = validate_observations(recs, known_sessions=store.session_ids(),
                                                existing_observation_ids=set(),
                                                existing_pairs=set())
                    store.import_observations(rep.accepted, actor="t")
            policies = PolicyRegistry(store)
            policies.register(policy_id=OPT_POLICY["policy_id"], version=1,
                              jurisdiction="DEMO", source_ref="synthetic:v1:seed=1",
                              valid_from="2000-01-01", valid_until="2099-12-31",
                              payload=OPT_POLICY["payload"], actor="t",
                              data_nature=enums.SYNTHETIC)
            pm = PhaseManager(store, policies)
            school = "SCH-DEMO-001"
            pm.transition(school, enums.PHASE_CALIBRATION, actor="t")
            pm.transition(school, enums.PHASE_BASELINE, actor="t",
                          protocol_ref="p")
            with self.assertRaises(GuardError) as cm:
                pm.transition(school, enums.PHASE_SHADOW, actor="t",
                              policy_id=OPT_POLICY["policy_id"])
            self.assertEqual(cm.exception.code, "E_GATE_DATA_NOT_READY")
            self.assertIn("INSUFFICIENT_HISTORY", cm.exception.details["reasons"][0])
        finally:
            store.close()

    def test_phase_order_list_matches_enums(self):
        self.assertEqual(PHASE_ORDER[0], enums.PHASE_CALIBRATION)
        self.assertEqual(PHASE_ORDER[-1], enums.PHASE_FOLLOWUP)
        self.assertEqual(len(PHASE_ORDER), 5)


class TestShadowLoop(ShadowFixture):
    def test_run_idempotent_and_no_execution(self):
        """Сценарий в одном тесте: первый прогон (возможно, после других тестов
        класса) → второй прогон не вставляет дублей → расширение окна."""
        school = "SCH-DEMO-001"
        r1 = run_shadow(self.store, self.policies, school_id=school, meal_type="LUNCH",
                        policy_id=OPT_POLICY["policy_id"], days=7, actor="t")
        self.assertEqual(r1["status"], "OK")
        self.assertEqual(r1["days"], 7)
        self.assertEqual(r1["records_total"], 7)
        self.assertEqual(sum(r1["actions"].values()), 7)
        # повторный прогон — без вставок (content_key идемпотентен)
        r2 = run_shadow(self.store, self.policies, school_id=school, meal_type="LUNCH",
                        policy_id=OPT_POLICY["policy_id"], days=7, actor="t")
        self.assertEqual(r2["inserted"], 0)
        self.assertEqual(r2["records_total"], 7)
        # исполнений нет: SHADOW не трогает action_executions
        n_exec = self.store._conn.execute(
            "SELECT COUNT(*) n FROM action_executions").fetchone()["n"]
        self.assertEqual(n_exec, 0)
        # расширение окна: старые дни пропущены, новые вставлены
        r3 = run_shadow(self.store, self.policies, school_id=school, meal_type="LUNCH",
                        policy_id=OPT_POLICY["policy_id"], days=10, actor="t")
        self.assertEqual(r3["inserted"], 3)
        self.assertEqual(r3["records_total"], 10)

    def test_record_content_and_cost_delta_formula(self):
        school = "SCH-DEMO-001"
        run_shadow(self.store, self.policies, school_id=school, meal_type="LUNCH",
                   policy_id=OPT_POLICY["policy_id"], days=7, actor="t")
        recs = [r for r in self.store.list_shadow_records(school, "LUNCH")][:7]
        self.assertEqual(len(recs), 7)
        cw, cs = 500.0, 5000.0
        checked = 0
        for r in recs:
            self.assertIsNotNone(r["y_g"])
            self.assertIsNotNone(r["actual_prepared_g"])
            if r["action"] == "ADJUST_PREPARED_VOLUME":
                ap = r["payload"]["action_params"]
                # planned в пределах разрешённого изменения
                self.assertLessEqual(abs(r["planned_g"] - r["baseline_g"]),
                                     ap["delta_max_g"] + 1e-9)
                # формула контрфакта фактического дня
                y, p, pa = r["y_g"], r["planned_g"], r["actual_prepared_g"]
                expected = (cw * max(p - y, 0.0) + cs * max(y - p, 0.0)
                            - (cw * max(pa - y, 0.0) + cs * max(y - pa, 0.0))) / 1000.0
                self.assertAlmostEqual(r["cost_delta_vs_actual"], expected, places=6)
                self.assertIsNotNone(r["pred_g"])
                self.assertIsNotNone(r["pred_q10"])
                self.assertIsNotNone(r["pred_q90"])
                checked += 1
            else:
                self.assertIsNone(r["planned_g"])
        self.assertGreater(checked, 0, "на этом сценарии должны быть дни с предложением")
        # payload — полное предложение с оговорками
        adjust = next(r for r in recs if r["action"] == "ADJUST_PREPARED_VOLUME")
        self.assertTrue(any("СИМУЛЯЦИЯ" in n for n in adjust["payload"]["notes"]))

    def test_data_not_ready_without_data(self):
        tmp = Path(tempfile.mkdtemp(prefix="sra-shadow-empty-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        store = EvidenceStore(tmp / "demo.sqlite3", enums.ENV_DEMO)
        try:
            policies = PolicyRegistry(store)
            policies.register(policy_id=OPT_POLICY["policy_id"], version=1,
                              jurisdiction="DEMO", source_ref="s",
                              valid_from="2000-01-01", valid_until="2099-12-31",
                              payload=OPT_POLICY["payload"], actor="t",
                              data_nature=enums.SYNTHETIC)
            r = run_shadow(store, policies, school_id="SCH-X", meal_type="LUNCH",
                           policy_id=OPT_POLICY["policy_id"], days=5, actor="t")
            self.assertEqual(r["status"], enums.DATA_NOT_READY)
        finally:
            store.close()


class _CraftRecords(ShadowFixture):
    """Вставляет синтетические shadow-записи с заданными числами."""

    def craft(self, school, preds, ys, q10s, q90s):
        for i, (pred, y, q10, q90) in enumerate(zip(preds, ys, q10s, q90s)):
            d = (date(2026, 10, 5) + timedelta(days=i)).isoformat()
            self.store.add_shadow_record(
                school_id=school, meal_type="LUNCH", target_date=d,
                content_key=f"craft-{school}-{i}", action="ADJUST_PREPARED_VOLUME",
                proposal_hash="x", planned_g=pred, baseline_g=pred + 500,
                pred_g=pred, pred_q10=q10, pred_q90=q90, y_g=y,
                actual_prepared_g=y + 400, cost_delta_vs_actual=None,
                payload={"notes": []})

    def setUp(self):
        self.schools = []

    def tearDown(self):
        pass


class TestMonitoringHandValues(ShadowFixture):
    def _mk_store(self):
        # отдельные DEMO-БД на класс теста: чистые записи
        tmp = Path(tempfile.mkdtemp(prefix="sra-mon-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        store = EvidenceStore(tmp / "demo.sqlite3", enums.ENV_DEMO)
        self.addCleanup(store.close)
        return store

    def _insert(self, store, school, preds, ys, q10s, q90s):
        for i, (pred, y, q10, q90) in enumerate(zip(preds, ys, q10s, q90s)):
            d = (date(2026, 10, 5) + timedelta(days=i)).isoformat()
            store.add_shadow_record(
                school_id=school, meal_type="LUNCH", target_date=d,
                content_key=f"{school}-{i}", action="ADJUST_PREPARED_VOLUME",
                proposal_hash="x", planned_g=pred, baseline_g=pred + 500,
                pred_g=pred, pred_q10=q10, pred_q90=q90, y_g=y,
                actual_prepared_g=y + 400, cost_delta_vs_actual=-12.5,
                payload={"notes": []})

    def test_ok_case_hand_values(self):
        store = self._mk_store()
        ys = [1010, 1000, 990, 1010, 1000, 930]  # остатки [10,0,-10,10,0,-70]
        self._insert(store, "SCH-A", [1000] * 6, ys, [940] * 6, [1060] * 6)
        r = monitor_shadow(store, "SCH-A", "LUNCH")
        self.assertEqual(r["status"], "OK")
        self.assertAlmostEqual(r["mae_recent_g"], 100 / 6, places=6)
        self.assertAlmostEqual(r["interval_coverage"], 5 / 6, places=6)
        self.assertEqual(r["drift"]["status"], enums.DRIFT_OK)
        self.assertAlmostEqual(r["cost_delta_vs_actual_mean"], -12.5, places=6)
        self.assertEqual(r["nominal_coverage"], 0.80)

    def test_warn_coverage_case(self):
        store = self._mk_store()
        ys = [1010, 1000, 990, 1010, 1000, 990]
        self._insert(store, "SCH-B", [1000] * 6, ys, [995] * 6, [1005] * 6)
        r = monitor_shadow(store, "SCH-B", "LUNCH")
        self.assertEqual(r["status"], "WARN_COVERAGE")
        self.assertAlmostEqual(r["interval_coverage"], 1 / 3, places=6)
        self.assertIn("не считаются калиброванными", r["reason"])

    def test_drift_case(self):
        store = self._mk_store()
        preds = [1000] * 12
        ys = [1000] * 6 + [1200] * 6
        self._insert(store, "SCH-C", preds, ys, [900] * 12, [1100] * 12)
        r = monitor_shadow(store, "SCH-C", "LUNCH")
        self.assertEqual(r["status"], enums.DRIFT_DETECTED)
        self.assertAlmostEqual(r["mae_recent_g"], 120.0, places=6)  # 2 нуля + 8×200

    def test_data_not_ready_and_pred_filtering(self):
        store = self._mk_store()
        self._insert(store, "SCH-D", [1000] * 3, [1000] * 3, [900] * 3, [1100] * 3)
        r = monitor_shadow(store, "SCH-D", "LUNCH")
        self.assertEqual(r["status"], enums.DATA_NOT_READY)
        self.assertEqual(r["n_quality_records"], 3)
        # запись без прогноза (NO_CHANGE) не попадает в качество, но в общем счёте
        store.add_shadow_record(
            school_id="SCH-D", meal_type="LUNCH", target_date="2026-10-30",
            content_key="nochange", action="NO_CHANGE", proposal_hash="x",
            planned_g=None, baseline_g=None, pred_g=None, pred_q10=None,
            pred_q90=None, y_g=1000.0, actual_prepared_g=1400.0,
            cost_delta_vs_actual=None, payload={"notes": []})
        r2 = monitor_shadow(store, "SCH-D", "LUNCH")
        self.assertEqual(r2["n_quality_records"], 3)
        self.assertEqual(r2["n_total_records"], 4)


if __name__ == "__main__":
    unittest.main()
