"""Тесты дизайна оценки эффекта: ручные контрольные значения объёма выборки."""

import unittest

from sra import enums
from sra.errors import ValidationError
from sra.evaluation.design import build_evaluation_plan, required_n_per_arm


class TestRequiredN(unittest.TestCase):
    def test_two_arm_hand_value(self):
        # (1.959964 + 0.8416212)² · 2 · (1000/400)² = 98.111 → 99
        self.assertEqual(required_n_per_arm(sigma_g=1000.0, effect_g=400.0), 99)

    def test_paired_hand_value(self):
        self.assertEqual(required_n_per_arm(sigma_g=1000.0, effect_g=400.0,
                                            paired=True), 50)

    def test_alpha_01_more_n(self):
        n05 = required_n_per_arm(sigma_g=1000.0, effect_g=400.0, alpha=0.05)
        n01 = required_n_per_arm(sigma_g=1000.0, effect_g=400.0, alpha=0.01)
        self.assertGreater(n01, n05)

    def test_monotonicity_in_effect(self):
        big = required_n_per_arm(sigma_g=1000.0, effect_g=800.0)
        small = required_n_per_arm(sigma_g=1000.0, effect_g=200.0)
        self.assertLess(big, small)

    def test_invalid_inputs(self):
        with self.assertRaises(ValidationError):
            required_n_per_arm(sigma_g=0.0, effect_g=100.0)
        with self.assertRaises(ValidationError):
            required_n_per_arm(sigma_g=100.0, effect_g=0.0)
        with self.assertRaises(ValidationError):
            required_n_per_arm(sigma_g=100.0, effect_g=100.0, alpha=0.10)
        with self.assertRaises(ValidationError):
            required_n_per_arm(sigma_g=100.0, effect_g=100.0, power=0.95)


class TestEvaluationPlan(unittest.TestCase):
    def test_plan_structure_and_hand_math(self):
        plan = build_evaluation_plan(
            school_id="SCH-1", design="two_arm_parallel", sigma_g=1000.0,
            baseline_waste_g_per_day=2000.0, expected_effect_rel=0.20,
            school_days_per_week=5)
        self.assertEqual(plan["status"], "DRAFT_REQUIRES_APPROVAL")
        self.assertEqual(plan["artifact_nature"], enums.PREDICTED)
        self.assertAlmostEqual(plan["effect_g_per_day"], 400.0, places=6)
        self.assertEqual(plan["n_per_arm"], 99)
        self.assertEqual(plan["sessions_total"], 198)
        self.assertEqual(plan["estimated_weeks_min"], 40)  # ceil(198/5)
        # критерии остановки не выдумываются
        self.assertIsNone(plan["stopping_criteria_template"]["service_level_drop_pct"])
        self.assertIn("не выдумывает пороги",
                      plan["stopping_criteria_template"]["note"])
        # честные предупреждения
        joined = " ".join(plan["warnings"])
        self.assertIn("является разрешением", joined)
        self.assertIn("не экстраполируются", joined)
        self.assertTrue(any("σ" in a for a in plan["assumptions"]))

    def test_before_after_design(self):
        plan = build_evaluation_plan(
            school_id="SCH-1", design="before_after", sigma_g=1000.0,
            baseline_waste_g_per_day=2000.0, expected_effect_rel=0.20)
        self.assertEqual(plan["n_per_arm"], 50)
        self.assertEqual(plan["sessions_total"], 50)
        self.assertIn("парные разности", plan["sigma_interpretation"])

    def test_invalid_params(self):
        with self.assertRaises(ValidationError):
            build_evaluation_plan(school_id="S", design="stepped_wedge",
                                  sigma_g=1.0, baseline_waste_g_per_day=100.0,
                                  expected_effect_rel=0.2)
        with self.assertRaises(ValidationError):
            build_evaluation_plan(school_id="S", design="before_after",
                                  sigma_g=1.0, baseline_waste_g_per_day=100.0,
                                  expected_effect_rel=0.0)
        with self.assertRaises(ValidationError):
            build_evaluation_plan(school_id="S", design="before_after",
                                  sigma_g=1.0, baseline_waste_g_per_day=100.0,
                                  expected_effect_rel=1.5)


if __name__ == "__main__":
    unittest.main()
