"""Тесты проверки порций по Санправилам РК (sra/norms.py, AUDIT §D.17).

Проверяются: режимы portion/total (нормализация Σ масса / Σ выдач), честный
UNAVAILABLE (группа 11–18 отсутствует, нет sessions при per=total, нет чисел
выдач), WARN как предупреждение (не ошибка), допуск на границе диапазона.
"""
from __future__ import annotations

import unittest
from datetime import date, datetime, timezone

from sra import enums
from sra.contracts import ObservationRecord, SessionRecord
from sra.norms import available_age_groups, check_portions


def _session(i, served=100):
    d = date(2026, 3, 1 + i)
    dt = datetime(2026, 3, 1 + i, 12, 0, tzinfo=timezone.utc)
    return SessionRecord(
        session_id=f"SES-{i}", school_id="SCH-T", service_date=d,
        timezone="Asia/Almaty", meal_type="LUNCH", attendance_count=served,
        meals_requested=None, meals_served=served,
        demand_censored=enums.CENSOR_FALSE, evidence_status=enums.SYNTHETIC,
        study_phase=None, source_ref="t:norms", observed_at=dt, recorded_at=dt)


def _obs(session, dish, served_mass_g):
    return ObservationRecord(
        observation_id=f"OBS-{session}-{dish}", session_id=session,
        dish_id=dish, prepared_mass_g=None, served_mass_g=served_mass_g,
        production_waste_g=None, serving_waste_g=None, plate_waste_g=None,
        avoidable_waste_g=None, avoidability_status=enums.AV_UNCLASSIFIED,
        evidence_status=enums.SYNTHETIC, source_ref="t:norms",
        observed_at=datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc),
        recorded_at=datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc))


class TestPortionMode(unittest.TestCase):
    def test_in_range_ok_and_out_of_range_warn(self):
        obs = [_obs("S1", "SALAD", 80.0),      # в норме 60–100
               _obs("S1", "GARNISH", 180.0)]   # вне нормы 100–150
        rep = check_portions(obs, age_group="6-11")
        self.assertEqual(rep["status"], "WARN")
        by = {d["dish"]: d for d in rep["dishes"]}
        self.assertEqual(by["salad"]["status"], "OK")
        self.assertEqual(by["garnish"]["status"], "WARN")
        self.assertEqual(by["garnish"]["n_above"], 1)
        self.assertIn("180 г > нормы", by["garnish"]["examples"][0])

    def test_boundary_within_tolerance(self):
        # drink: норма ровно 200; допуск 0.005*200+0.5 = 1.5 г
        obs = [_obs("S1", "DRINK", 201.0)]
        rep = check_portions(obs, age_group="6-11")
        by = {d["dish"]: d for d in rep["dishes"]}
        self.assertEqual(by["drink"]["status"], "OK")     # в допуске
        obs2 = [_obs("S1", "DRINK", 210.0)]
        rep2 = check_portions(obs2, age_group="6-11")
        by2 = {d["dish"]: d for d in rep2["dishes"]}
        self.assertEqual(by2["drink"]["status"], "WARN")  # за допуском

    def test_suffix_dish_ids_mapped(self):
        obs = [_obs("S1", "SALAD-M00", 80.0), _obs("S1", "FIRST-M03", 220.0)]
        rep = check_portions(obs, age_group="6-11")
        by = {d["dish"]: d for d in rep["dishes"]}
        self.assertEqual(by["salad"]["n_measured"], 1)
        self.assertEqual(by["first"]["n_measured"], 1)

    def test_unknown_age_group_unavailable(self):
        rep = check_portions([], age_group="11-18")
        self.assertEqual(rep["status"], "UNAVAILABLE")
        self.assertIn("11-18", rep["reason"])
        self.assertNotIn("11-18", available_age_groups())

    def test_missing_mass_is_not_zero(self):
        obs = [_obs("S1", "SALAD", None)]
        rep = check_portions(obs, age_group="6-11")
        self.assertEqual(rep["status"], "UNAVAILABLE")
        self.assertEqual(rep["observations_without_mass"], 1)


class TestTotalMode(unittest.TestCase):
    def test_normalized_by_servings(self):
        ses = [_session(1, served=100)]
        # школа: 100 порций × 80 г = 8000 г салата за день
        obs = [_obs("SES-1", "SALAD", 8000.0)]
        rep = check_portions(obs, age_group="6-11", sessions=ses, per="total")
        by = {d["dish"]: d for d in rep["dishes"]}
        self.assertEqual(by["salad"]["status"], "OK")
        self.assertEqual(by["salad"]["median_g"], 80.0)
        self.assertIn("Σ масса блюда", rep["portion_semantics"])

    def test_total_without_sessions_unavailable(self):
        obs = [_obs("SES-1", "SALAD", 8000.0)]
        rep = check_portions(obs, age_group="6-11", per="total")
        self.assertEqual(rep["status"], "UNAVAILABLE")
        self.assertIn("требует sessions", rep["reason"])

    def test_total_school_sum_vs_norm_is_semantic_error_caught(self):
        # Анти-тест AUDIT №7: сумму школы сравнить с нормой порции напрямую —
        # получим ложный WARN на все 100%; режим per=total это исключает.
        ses = [_session(1, served=100)]
        obs = [_obs("SES-1", "SALAD", 8000.0)]
        raw = check_portions(obs, age_group="6-11", per="portion")
        by = {d["dish"]: d for d in raw["dishes"]}
        self.assertEqual(by["salad"]["status"], "WARN")   # 8000 г «порция»
        good = check_portions(obs, age_group="6-11", sessions=ses,
                              per="total")
        by2 = {d["dish"]: d for d in good["dishes"]}
        self.assertEqual(by2["salad"]["status"], "OK")    # 80 г после деления

    def test_sessions_without_servings_counted(self):
        s = _session(1, served=100)
        s.meals_served = None
        s.attendance_count = None
        obs = [_obs("SES-1", "SALAD", 8000.0)]
        rep = check_portions([obs[0]], age_group="6-11", sessions=[s],
                             per="total")
        self.assertEqual(rep["status"], "UNAVAILABLE")
        self.assertIn("ни в одной сессии", rep["reason"])


if __name__ == "__main__":
    unittest.main()
