"""Тесты ядра оптимизатора с ручными контрольными значениями.

Сценарии спроса [100, 110, 130] г; стоимости за кг: c_w=1000, c_s=10000
(в пересчёте на грамм: 1 и 10). При P=110:
  waste_mean  = (10+0+0)/3  = 3.3333…
  short_mean  = (0+0+20)/3  = 6.6667…
  risk        = 1/3
  cost        = 3.3333·1 + 6.6667·10 = 70 (в единицах «валюта×1000»/…: точно 70)

Ручное решение (P0=120, Δ_max=30, шаг 10, α=0.10): риск 1/3 > 0.1 при любом
P ≤ 130−ε, поэтому допустимы только P ≥ 130 (кандидаты 130, 140, 150);
стоимости: 16.667 / 26.667 / 36.667 → P* = 130, Δ = +10.
cost(P0): waste (20+10+0)/3 = 10, short (0+0+10)/3 = 3.3333 → 10 + 33.333 = 43.333.
improvement = 43.333 − 16.667 = 26.667; rel = 26.667/43.333 ≈ 0.6154.
"""

import unittest

from sra.optimizer.core import (DemandScenarios, expected_outcome, habitual_baseline,
                                scenario_cost_deltas, solve_prepared_volume)
from sra.forecasting.series import TargetPoint

CW, CS = 1000.0, 10000.0
SCEN = DemandScenarios.build(110.0, [-10.0, 0.0, 20.0])  # values [100,110,130]


class TestExpectedOutcome(unittest.TestCase):
    def test_hand_values(self):
        o = expected_outcome(110.0, SCEN)
        self.assertAlmostEqual(o["waste_g_mean"], 10 / 3, places=6)
        self.assertAlmostEqual(o["shortage_g_mean"], 20 / 3, places=6)
        self.assertAlmostEqual(o["shortage_risk"], 1 / 3, places=6)
        self.assertEqual(o["n_scenarios"], 3)

    def test_cost_hand_values(self):
        c = solve_prepared_volume and 0  # noqa: F841 (заглушка для читаемости)
        from sra.optimizer.core import total_cost
        self.assertAlmostEqual(total_cost(110.0, SCEN, cost_waste_per_kg=CW,
                                          cost_shortage_per_kg=CS), 70.0, places=6)

    def test_scenario_cost_deltas_constant_when_both_dominating(self):
        # P=140 и P0=135 выше всех сценариев (max=130): дельта одинакова
        # во всех сценариях и равна c_w·(P0−P)/1000
        d = scenario_cost_deltas(140.0, 135.0, SCEN, cost_waste_per_kg=CW,
                                 cost_shortage_per_kg=CS)
        self.assertEqual(len(d), 3)
        for x in d:
            self.assertAlmostEqual(x, CW * 5 / 1000, places=6)  # 5 г × 1/г

    def test_scenario_cost_deltas_differ_when_baseline_violated(self):
        # P0=120 НИЖЕ сценария 130: в том сценарии нехватка дороже — дельты разные.
        # Функция возвращает отсортированный список: [-90, 20, 20].
        d = scenario_cost_deltas(140.0, 120.0, SCEN, cost_waste_per_kg=CW,
                                 cost_shortage_per_kg=CS)
        self.assertAlmostEqual(d[0], -90.0, places=6)   # v=130: cost(P)=10, cost(P0)=100
        self.assertAlmostEqual(d[1], 20.0, places=6)    # v=100: (40−20)
        self.assertAlmostEqual(d[2], 20.0, places=6)    # v=110: (30−10)


class TestSolveHandCase(unittest.TestCase):
    def test_manual_solution(self):
        r = solve_prepared_volume(
            baseline_g=120.0, scenarios=SCEN, cost_waste_per_kg=CW,
            cost_shortage_per_kg=CS, shortage_risk_max=0.10, delta_max_g=30.0,
            grid_step_g=10.0)
        self.assertEqual(r["status"], "OK")
        self.assertAlmostEqual(r["chosen_g"], 130.0, places=6)
        self.assertAlmostEqual(r["delta_g"], 10.0, places=6)
        self.assertAlmostEqual(r["cost_baseline"], 43.3333333, places=4)
        self.assertAlmostEqual(r["cost_chosen"], 50 / 3, places=6)   # 16.6667
        self.assertAlmostEqual(r["improvement"], 26.6666667, places=4)
        self.assertAlmostEqual(r["improvement_rel"], 26.6666667 / 43.3333333, places=6)
        ch = r["checks"]
        self.assertTrue(ch["shortage_risk_max"]["pass"])
        self.assertFalse(ch["shortage_risk_max"]["baseline_pass"],
                         "базовая линия с риском 1/3 > α обязана быть помечена")
        self.assertTrue(ch["delta_bound_g"]["pass"])
        self.assertTrue(ch["nonnegative_mass_g"]["pass"])
        self.assertTrue(ch["time_budget_s"]["pass"])
        self.assertEqual(ch["grid"]["n_candidates"], 7)

    def test_alpha_zero_forces_full_coverage(self):
        # α → почти 0: допустим только P строго выше максимума сценариев (130)
        r = solve_prepared_volume(
            baseline_g=120.0, scenarios=SCEN, cost_waste_per_kg=CW,
            cost_shortage_per_kg=CS, shortage_risk_max=1e-9, delta_max_g=30.0,
            grid_step_g=10.0)
        self.assertEqual(r["status"], "OK")
        self.assertAlmostEqual(r["chosen_g"], 130.0, places=6)
        self.assertEqual(r["chosen_metrics"]["shortage_risk"], 0.0)

    def test_infeasible_within_bounds(self):
        r = solve_prepared_volume(
            baseline_g=120.0, scenarios=SCEN, cost_waste_per_kg=CW,
            cost_shortage_per_kg=CS, shortage_risk_max=0.10, delta_max_g=5.0,
            grid_step_g=1.0)
        self.assertEqual(r["status"], "INFEASIBLE")
        self.assertIn("расширять границы молча запрещено", r["reason"])
        self.assertFalse(r["checks"]["shortage_risk_max"]["baseline_pass"])

    def test_timeout_path(self):
        r = solve_prepared_volume(
            baseline_g=120.0, scenarios=SCEN, cost_waste_per_kg=CW,
            cost_shortage_per_kg=CS, shortage_risk_max=0.10, delta_max_g=30.0,
            grid_step_g=10.0, time_budget_s=0.0)
        self.assertEqual(r["status"], "TIMEOUT")
        self.assertFalse(r["checks"]["time_budget_s"]["pass"])

    def test_no_change_preferred_below_resolution(self):
        # единичные сценарии, крошечное Δ: лучший кандидат на границе ±5,
        # шаг сетки 25 → изменение ниже разрешения рекомендации
        scen = DemandScenarios.build(120.0, [0.0])
        r = solve_prepared_volume(
            baseline_g=120.0, scenarios=scen, cost_waste_per_kg=CW,
            cost_shortage_per_kg=CS, shortage_risk_max=0.3, delta_max_g=5.0,
            grid_step_g=25.0)
        self.assertEqual(r["status"], "NO_CHANGE_PREFERRED")
        self.assertIn("шага сетки", r["reason"])

    def test_bad_grid_step(self):
        r = solve_prepared_volume(baseline_g=120.0, scenarios=SCEN, cost_waste_per_kg=CW,
                                  cost_shortage_per_kg=CS, shortage_risk_max=0.1,
                                  delta_max_g=30.0, grid_step_g=0.0)
        self.assertEqual(r["status"], "BAD_PARAMS")

    def test_candidate_grid_capped(self):
        r = solve_prepared_volume(
            baseline_g=100000.0, scenarios=SCEN, cost_waste_per_kg=CW,
            cost_shortage_per_kg=CS, shortage_risk_max=0.10, delta_max_g=15000.0,
            grid_step_g=1.0, max_candidates=50)
        self.assertEqual(r["checks"]["grid"]["n_candidates"], 50)
        self.assertIn("note", r["checks"]["grid"])


class TestHabitualBaseline(unittest.TestCase):
    def _pt(self, d, prepared):
        return TargetPoint(session_id=f"S{d}", service_date=d, served_mass_g=None,
                           prepared_mass_g=prepared, n_obs=1, n_served_obs=0,
                           censored=False, censored_unknown=False, meals_served=None)

    def test_median_of_window(self):
        from datetime import date, timedelta
        base = date(2026, 10, 5)
        pts = [self._pt(base + timedelta(days=i), 100.0 + i) for i in range(5)]
        # окно 3 дня = последние точки (102, 103, 104) → медиана 103
        self.assertAlmostEqual(habitual_baseline(pts, days=3), 103.0)
        self.assertAlmostEqual(habitual_baseline(pts, days=5), 102.0)

    def test_none_without_prepared(self):
        from datetime import date
        pts = [self._pt(date(2026, 10, 5), None)]
        self.assertIsNone(habitual_baseline(pts, days=3))


class TestPolicyValidation(unittest.TestCase):
    def test_valid_demo_policy(self):
        from sra.optimizer.policies import validate_decision_policy
        values = {
            "cost_waste_per_kg": 500.0, "cost_shortage_per_kg": 5000.0,
            "currency": "KZT_DEMO", "shortage_risk_max": 0.10,
            "delta_max_pct_of_baseline": 0.15, "baseline_days": 8,
            "forecast_model": "seasonal_naive", "calib_days": 8,
            "grid_step_g": 250.0, "min_improvement_rel": 0.02,
        }
        self.assertEqual(validate_decision_policy(values), [])

    def test_dominance_required(self):
        from sra.optimizer.policies import validate_decision_policy
        values = {"cost_waste_per_kg": 500.0, "cost_shortage_per_kg": 500.0,
                  "currency": "X", "shortage_risk_max": 0.1,
                  "delta_max_pct_of_baseline": 0.1, "baseline_days": 8,
                  "forecast_model": "naive", "calib_days": 8, "grid_step_g": 1.0,
                  "min_improvement_rel": 0.0}
        problems = validate_decision_policy(values)
        self.assertTrue(any("доминировать" in p for p in problems))

    def test_missing_params(self):
        from sra.optimizer.policies import validate_decision_policy
        problems = validate_decision_policy({})
        self.assertTrue(any("отсутствуют параметры" in p for p in problems))

    def test_real_policy_requires_verified_and_real_values(self):
        from sra.optimizer.policies import check_policy_usable
        row = {"payload_json": {"data_nature": "SYNTHETIC"},
               "verification_status": "UNVERIFIED"}
        problems = check_policy_usable(row, environment="REAL")
        self.assertEqual(len(problems), 2)
        self.assertEqual(check_policy_usable(row, environment="DEMO"), [])


if __name__ == "__main__":
    unittest.main()
