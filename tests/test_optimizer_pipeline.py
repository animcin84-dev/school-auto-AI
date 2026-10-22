"""Интеграционные тесты этапа 4: предложения, пути отказа, согласования, сравнение."""

import json
import shutil
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

from sra import enums
from sra.csvio import parse_file
from sra.demo.synthetic import generate
from sra.errors import GuardError
from sra.optimizer.compare import compare_strategies
from sra.optimizer.propose import (COND_BELOW_RESOLUTION, COND_FORECAST_DRIFT,
                                   COND_FORECAST_UNAVAILABLE, COND_INFEASIBLE,
                                   COND_MODE_BLOCKED_STAGE4, COND_NO_IMPROVEMENT,
                                   COND_POLICY_INVALID, COND_POLICY_NOT_USABLE,
                                   propose_adjustment)
from sra.policies import PolicyRegistry
from sra.store import EvidenceStore, proposal_hash
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


class OptimizerStoreCase(unittest.TestCase):
    """DEMO-хранилище с синтетикой seed=42 (детерминированное)."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="sra-opt-"))
        generate(cls.tmp / "data", seed=42, n_days=60)
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
        # Целевая дата для УСПЕШНОГО пути: первый учебный день после 55-й сессии
        # (стабильный режим сценария; прогноз и базовая линия устойчивы).
        sessions = cls.store.fetch_sessions(school_id="SCH-DEMO-001")
        d = date.fromisoformat(sessions[55].service_date.isoformat()) + timedelta(days=1)
        while d.weekday() >= 5:
            d += timedelta(days=1)
        cls.target_date = d.isoformat()
        # Дата сразу после смены режима (40-я сессия) — отдельный тест INFEASIBLE.
        d2 = date.fromisoformat(sessions[39].service_date.isoformat()) + timedelta(days=1)
        while d2.weekday() >= 5:
            d2 += timedelta(days=1)
        cls.regime_change_date = d2.isoformat()

    @classmethod
    def tearDownClass(cls):
        cls.store.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def propose(self, **over):
        kwargs = dict(school_id="SCH-DEMO-001", meal_type="LUNCH",
                      target_date=self.target_date,
                      policy_id=OPT_POLICY["policy_id"], actor="t")
        kwargs.update(over)
        return propose_adjustment(self.store, self.policies, **kwargs)


class TestProposalSuccess(OptimizerStoreCase):
    def test_full_contract_fields(self):
        p = self.propose().to_dict()
        self.assertEqual(p["action"], "ADJUST_PREPARED_VOLUME")
        self.assertEqual(p["action_status"], "SIMULATED_NOT_EXECUTABLE")
        self.assertEqual(p["mode"], enums.MODE_DEMO)
        self.assertEqual(p["environment"], enums.ENV_DEMO)
        self.assertEqual(p["reasons"], [])
        self.assertEqual(p["missing_conditions"], [])
        self.assertTrue(p["policy_refs"])
        self.assertIn("seasonal_naive", p["forecast_ref"])
        ap = p["action_params"]
        lo = ap["baseline_mass_g"] - ap["delta_max_g"]
        hi = ap["baseline_mass_g"] + ap["delta_max_g"]
        self.assertGreaterEqual(ap["planned_prepared_mass_g"], lo)
        self.assertLessEqual(ap["planned_prepared_mass_g"], hi)
        self.assertLess(ap["planned_prepared_mass_g"], ap["baseline_mass_g"],
                        "на синтетике seed 42 кухня переготавливает — оптимизатор режет объём")
        # ограничения все пройдены
        for key in ("shortage_risk_max", "delta_bound_g", "nonnegative_mass_g",
                    "time_budget_s"):
            self.assertTrue(p["constraint_checks"][key]["pass"], msg=key)
        # интервал эффекта: SIMULATED с разбросом по сценариям
        eff = p["expected_effect_interval"]
        self.assertEqual(eff["nature"], enums.SIMULATED)
        self.assertLessEqual(eff["q50"], 0.0, "выбранный P не должен быть хуже NO_CHANGE")
        # ключевые честные оговорки
        joined = " ".join(p["notes"])
        self.assertIn("порци", joined)
        self.assertIn("цензурированн", joined)
        self.assertIn("СИМУЛЯЦИЯ", joined)

    def test_expected_savings_math(self):
        """При доминировании P и P0 над всеми сценариями экономия = c_w·(P0−P)/1000."""
        p = self.propose().to_dict()
        ap = p["action_params"]
        eff = p["expected_effect_interval"]
        # сценарии занимают узкий диапазон вокруг прогноза; P* и P0 выше их
        # (риск 0 у обоих) → экономия одинакова по сценариям
        if ap["expected_outcome_chosen"]["shortage_risk"] == 0.0 \
                and ap["expected_outcome_baseline"]["shortage_risk"] == 0.0:
            saving = 500.0 * (ap["baseline_mass_g"] - ap["planned_prepared_mass_g"]) / 1000.0
            # эффект — стоимость сценария при P минус при P0: экономия ОТРИЦАТЕЛЬНА
            self.assertAlmostEqual(eff["q50"], -saving, places=6)
            self.assertAlmostEqual(ap["improvement"], saving, places=6)

    def test_determinism_of_payload_hash(self):
        """Структура предложения детерминирована (id и время меняются, параметры нет)."""
        p1 = self.propose().to_dict()
        p2 = self.propose().to_dict()
        for key in ("action", "action_status", "action_params", "constraint_checks"):
            self.assertEqual(p1[key], p2[key], msg=key)


class TestNoChangePaths(OptimizerStoreCase):
    def test_real_env_blocked(self):
        real_tmp = Path(tempfile.mkdtemp(prefix="sra-opt-real-"))
        self.addCleanup(shutil.rmtree, real_tmp, ignore_errors=True)
        store = EvidenceStore(real_tmp / "real.sqlite3", enums.ENV_REAL)
        try:
            p = propose_adjustment(store, PolicyRegistry(store), school_id="SCH-X",
                                   meal_type="LUNCH", target_date=self.target_date,
                                   policy_id=OPT_POLICY["policy_id"], actor="t").to_dict()
            self.assertEqual(p["action"], "NO_CHANGE")
            self.assertIn(COND_MODE_BLOCKED_STAGE4, p["reasons"])
        finally:
            store.close()

    def test_unknown_policy(self):
        p = self.propose(policy_id="NO-SUCH-POLICY").to_dict()
        self.assertEqual(p["action"], "NO_CHANGE")
        self.assertIn(COND_POLICY_INVALID, p["reasons"])

    def test_invalid_policy_params(self):
        self.policies.register(policy_id="POLICY-BROKEN", version=1, jurisdiction="DEMO",
                               source_ref="synthetic:v1:seed=42",
                               valid_from="2000-01-01", valid_until="2099-12-31",
                               payload={**OPT_POLICY["payload"],
                                        "cost_shortage_per_kg": 100.0},  # нет доминирования
                               actor="t", data_nature=enums.SYNTHETIC)
        p = self.propose(policy_id="POLICY-BROKEN").to_dict()
        self.assertIn(COND_POLICY_INVALID, p["reasons"])
        self.assertTrue(any("доминировать" in n for n in p["notes"]))

    def test_real_nature_policy_rejected_in_demo(self):
        self.policies.register(policy_id="POLICY-REAL-NATURE", version=1,
                               jurisdiction="DEMO", source_ref="some-document",
                               valid_from="2000-01-01", valid_until="2099-12-31",
                               payload=OPT_POLICY["payload"], actor="t",
                               data_nature=enums.REAL_OBSERVED)
        p = self.propose(policy_id="POLICY-REAL-NATURE").to_dict()
        self.assertIn(COND_POLICY_NOT_USABLE, p["reasons"])

    def test_cold_start_target_before_history(self):
        p = self.propose(target_date="2020-01-05").to_dict()
        self.assertEqual(p["action"], "NO_CHANGE")
        self.assertIn(COND_FORECAST_UNAVAILABLE, p["reasons"])

    def test_drift_blocks_proposal(self):
        fake = {"status": "OK", "target_date": self.target_date,
                "cutoff_iso": "2026-01-01T18:00:00+05:00",
                "model_name": "seasonal_naive", "model_params": {},
                "pred_g": 100000.0, "residuals_g": [100.0] * 8,
                "n_calib_used": 8, "n_calib_skipped": 0, "n_history": 30,
                "drift": {"status": enums.DRIFT_DETECTED, "details": {}}}
        with mock.patch("sra.optimizer.propose.decision_forecast", return_value=fake):
            p = self.propose().to_dict()
        self.assertEqual(p["action"], "NO_CHANGE")
        self.assertIn(COND_FORECAST_DRIFT, p["reasons"])

    def test_infeasible_after_regime_change_notifies_unsafe_baseline(self):
        """После смены режима калибровочные остатки велики: даже +Δ_max не даёт
        допустимого риска нехватки → NO_CHANGE + ВНИМАНИЕ ответственному."""
        p = self.propose(target_date=self.regime_change_date).to_dict()
        self.assertEqual(p["action"], "NO_CHANGE")
        self.assertIn(COND_INFEASIBLE, p["reasons"])
        self.assertTrue(any("ВНИМАНИЕ" in n and "не расширяет" in n for n in p["notes"]),
                        msg=p["notes"])

    def test_min_improvement_threshold_blocks(self):
        # завышенный порог значимости: любое улучшение признаётся незначимым
        self.policies.register(policy_id="POLICY-STRICT", version=1, jurisdiction="DEMO",
                               source_ref="synthetic:v1:seed=42",
                               valid_from="2000-01-01", valid_until="2099-12-31",
                               payload={**OPT_POLICY["payload"], "min_improvement_rel": 10.0},
                               actor="t", data_nature=enums.SYNTHETIC)
        p = self.propose(policy_id="POLICY-STRICT").to_dict()
        self.assertEqual(p["action"], "NO_CHANGE")
        self.assertIn(COND_NO_IMPROVEMENT, p["reasons"])


class TestApprovalsAndExecution(OptimizerStoreCase):
    def test_approval_version_binding_and_double_execution(self):
        proposal = self.propose().to_dict()
        # NO_CHANGE согласовать нечего
        with self.assertRaises(GuardError) as cm:
            self.store.approve_proposal({**proposal, "action": "NO_CHANGE",
                                         "action_status": "NOT_EXECUTABLE"},
                                        approver="demo-approver")
        self.assertEqual(cm.exception.code, "E_NOTHING_TO_APPROVE")
        # согласование конкретной версии
        approval = self.store.approve_proposal(proposal, approver="demo-approver",
                                               role="заведующая", notes="демо-согласование")
        h = proposal_hash(proposal)
        self.assertEqual(approval["proposal_hash"], h)
        # то же лицо повторно — отклоняется
        with self.assertRaises(GuardError) as cm:
            self.store.approve_proposal(proposal, approver="demo-approver")
        self.assertEqual(cm.exception.code, "E_ALREADY_APPROVED")
        # исполнение согласованной версии
        execution = self.store.record_action_execution(proposal,
                                                       approval_id=approval["approval_id"],
                                                       executed_by="demo-kitchen")
        self.assertEqual(execution["proposal_id"], proposal["proposal_id"])
        # повторное исполнение запрещено
        with self.assertRaises(GuardError) as cm:
            self.store.record_action_execution(proposal,
                                               approval_id=approval["approval_id"],
                                               executed_by="demo-kitchen")
        self.assertEqual(cm.exception.code, "E_DOUBLE_EXECUTION")
        # изменённая версия не исполняется со старым согласованием
        modified = {**proposal, "action_params": {**proposal["action_params"],
                                                  "planned_prepared_mass_g": 999.0}}
        with self.assertRaises(GuardError) as cm:
            self.store.record_action_execution(modified,
                                               approval_id=approval["approval_id"],
                                               executed_by="demo-kitchen")
        self.assertEqual(cm.exception.code, "E_APPROVAL_MISMATCH")
        # новое согласование другого согласующего допустимо (та же версия)
        a2 = self.store.approve_proposal(proposal, approver="second-approver")
        self.assertEqual(a2["proposal_hash"], h)
        self.assertEqual(len(self.store.list_approvals(proposal["proposal_id"])), 2)

    def test_real_env_approvals_and_execution_blocked(self):
        real_tmp = Path(tempfile.mkdtemp(prefix="sra-appr-real-"))
        self.addCleanup(shutil.rmtree, real_tmp, ignore_errors=True)
        store = EvidenceStore(real_tmp / "real.sqlite3", enums.ENV_REAL)
        try:
            proposal = {"proposal_id": "P", "action": "ADJUST_PREPARED_VOLUME",
                        "environment": "REAL"}
            with self.assertRaises(GuardError) as cm:
                store.approve_proposal(proposal, approver="x")
            self.assertEqual(cm.exception.code, "E_APPROVALS_NOT_AVAILABLE_STAGE4")
            with self.assertRaises(GuardError) as cm:
                store.record_action_execution(proposal, approval_id=1, executed_by="x")
            self.assertEqual(cm.exception.code, "E_EXECUTION_BLOCKED_STAGE4")
        finally:
            store.close()

    def test_env_mismatch_rejected(self):
        proposal = self.propose().to_dict()  # environment=DEMO
        real_tmp = Path(tempfile.mkdtemp(prefix="sra-apr-mix-"))
        self.addCleanup(shutil.rmtree, real_tmp, ignore_errors=True)
        store = EvidenceStore(real_tmp / "real.sqlite3", enums.ENV_REAL)
        try:
            with self.assertRaises(GuardError) as cm:
                store.approve_proposal(proposal, approver="x")
            self.assertEqual(cm.exception.code, "E_ENV_MISMATCH")
        finally:
            store.close()


class TestCompareStrategies(OptimizerStoreCase):
    def _run(self):
        return compare_strategies(
            self.store, school_id="SCH-DEMO-001", meal_type="LUNCH",
            test_days=7, calib_days=8, baseline_days=8,
            model_name="seasonal_naive", seasonal_weeks=3,
            cost_waste_per_kg=500.0, cost_shortage_per_kg=5000.0,
            shortage_risk_max=0.10, delta_max_pct=0.15, grid_step_g=250.0)

    def test_structure_and_determinism(self):
        r1 = self._run()
        r2 = self._run()
        self.assertEqual(r1["status"], "OK")
        self.assertEqual(r1["artifact_nature"], enums.SIMULATED)
        self.assertEqual(set(r1["strategies"]),
                         {"no_change", "fixed_quantile_rule", "optimizer"})
        self.assertTrue(r1["kitchen_actual_reference"]["hindsight_only"])
        for name, s in r1["strategies"].items():
            self.assertGreater(s["n_days"], 0, msg=name)
            self.assertGreaterEqual(s["shortfall_day_rate"], 0.0)
        # детерминированность (кроме отсутствующих временных меток — их тут нет)
        self.assertEqual(json.dumps(r1, sort_keys=True), json.dumps(r2, sort_keys=True))
        # честные оговорки
        self.assertTrue(any("SIMULATED" in n for n in r1["notes"]))
        self.assertTrue(any("цензурированн" in n or "censored" in n.lower()
                            for n in r1["notes"]))

    def test_optimizer_respects_hard_shortage_constraint_better(self):
        """Ключевой содержательный результат на этом сценарии: оптимизатор,
        уважая жёсткую границу риска, даёт меньше дней нехватки, чем простое
        правило q90, и заметно дешевле NO_CHANGE."""
        r = self._run()
        opt = r["strategies"]["optimizer"]
        fixed = r["strategies"]["fixed_quantile_rule"]
        no_change = r["strategies"]["no_change"]
        self.assertLessEqual(opt["shortfall_day_rate"], fixed["shortfall_day_rate"])
        self.assertLess(opt["total_cost"], no_change["total_cost"])

    def test_data_not_ready_on_tiny_store(self):
        tmp = Path(tempfile.mkdtemp(prefix="sra-cmp-tiny-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        store = EvidenceStore(tmp / "demo.sqlite3", enums.ENV_DEMO)
        try:
            r = compare_strategies(store, school_id="SCH-DEMO-001", meal_type="LUNCH",
                                   test_days=10, cost_waste_per_kg=500.0,
                                   cost_shortage_per_kg=5000.0)
            self.assertEqual(r["status"], enums.DATA_NOT_READY)
            self.assertEqual(r["strategies"], {})
        finally:
            store.close()


if __name__ == "__main__":
    unittest.main()
