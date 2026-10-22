"""Тесты статистики и сравнения фаз (репетиция оценки эффекта).

Контрольные значения p для t-распределения взяты из таблиц/эталонных
вычислений (не из этого же кода); формула df Уэлча в тесте реализована
независимо от sra.evaluation.stats.
"""

import math
import shutil
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from sra import enums
from sra.contracts import ObservationRecord, SessionRecord
from sra.evaluation import stats
from sra.evaluation.compare_phases import compare_phases
from sra.errors import ValidationError
from sra.store import EvidenceStore
from tests.test_ingest_open import write_xlsx  # noqa: F401 (используется ниже)


class TestStats(unittest.TestCase):
    def test_betainc_known_values(self):
        self.assertAlmostEqual(stats.betainc(0.5, 0.5, 0.5), 0.5, places=10)
        self.assertAlmostEqual(stats.betainc(2, 2, 0.5), 0.5, places=10)
        # I_x(a,1) = x^a
        self.assertAlmostEqual(stats.betainc(2, 1, 0.25), 0.25 ** 2, places=10)
        self.assertAlmostEqual(stats.betainc(3, 1, 0.4), 0.4 ** 3, places=10)

    def test_t_p_known_reference_values(self):
        # эталон: t=2.228139, df=10 → p=0.05; t=2.0, df=10 → p≈0.07337
        self.assertAlmostEqual(stats.t_two_sided_p(2.228139, 10), 0.05, places=4)
        self.assertAlmostEqual(stats.t_two_sided_p(2.0, 10), 0.0734, places=3)
        self.assertAlmostEqual(stats.t_two_sided_p(0.0, 7), 1.0, places=12)

    def test_welch_df_formula_independent(self):
        # независимая реализация формулы Уэлча–Саттертуэйта
        xs = [10.0, 12.0, 9.0, 11.0, 13.0, 10.0, 12.0]
        ys = [15.0, 14.0, 16.0, 15.5, 14.5, 15.0, 16.5, 14.0]
        w = stats.welch_ttest(xs, ys)
        v1 = stats.sample_sd(xs) ** 2
        v2 = stats.sample_sd(ys) ** 2
        se2 = v1 / len(xs) + v2 / len(ys)
        df_expected = se2 ** 2 / (v1 ** 2 / (len(xs) ** 2 * (len(xs) - 1))
                                  + v2 ** 2 / (len(ys) ** 2 * (len(ys) - 1)))
        self.assertAlmostEqual(w["df"], df_expected, places=10)
        t_expected = (stats.mean(xs) - stats.mean(ys)) / math.sqrt(se2)
        self.assertAlmostEqual(w["t"], t_expected, places=10)

    def test_welch_detects_shift_and_no_shift(self):
        xs = [10.0, 11.0, 12.0, 10.5, 11.5, 10.8, 11.2, 10.2, 11.8, 10.9]
        ys = [14.0, 15.0, 16.0, 14.5, 15.5, 14.8, 15.2, 14.2, 15.8, 14.9]
        w = stats.welch_ttest(xs, ys)
        self.assertLess(w["p"], 0.001)
        w0 = stats.welch_ttest(xs, [x + 0.01 for x in xs])
        self.assertGreater(w0["p"], 0.05)
        # симметричный случай: зеркальные выборки → diff с противоположным знаком
        wr = stats.welch_ttest(ys, xs)
        self.assertAlmostEqual(wr["diff"], -w["diff"], places=12)

    def test_mann_whitney_shift_ties_and_zero_variance(self):
        xs = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0]
        ys = [6.0, 7.0, 8.0, 9.0, 10.0, 11.0, 12.0, 13.0]
        r = stats.mann_whitney_u(xs, ys)
        # связки (6,6),(7,7),(8,8): R1 = 15 + 6.5+8.5+10.5 = 40.5; U1 = 40.5−36 = 4.5
        self.assertEqual(r["u"], 4.5)
        self.assertLess(r["p"], 0.01)
        same = stats.mann_whitney_u([5.0] * 5, [5.0] * 5)
        self.assertAlmostEqual(same["p"], 1.0, msg="все связки → p=1")

    def test_small_sample_rejected_with_code(self):
        with self.assertRaises(ValidationError) as cm:
            stats.welch_ttest([1.0], [2.0, 3.0])
        self.assertEqual(cm.exception.code, "E_SMALL_SAMPLE")


class ComparePhasesTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="sra-cmp-"))
        self.store = EvidenceStore(self.tmp / "open.sqlite3", enums.ENV_OPEN)

    def tearDown(self):
        self.store.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _add_day(self, d: date, phase: str | None, diners: int, pw_g: float,
                 menu: int | None = None, school="FI-TEST"):
        iso = d.isoformat()
        observed = datetime(d.year, d.month, d.day, 12, 0, tzinfo=timezone.utc)
        notes = f"чужое измерение; MENU={menu}" if menu else "чужое измерение"
        self.store.import_sessions([SessionRecord(
            session_id=f"SES-{school}-{iso}-LUNCH", school_id=school,
            service_date=d, timezone="UTC", meal_type="LUNCH",
            attendance_count=diners, meals_requested=None, meals_served=diners,
            demand_censored=enums.CENSOR_UNKNOWN, evidence_status=enums.REAL_OBSERVED,
            study_phase=phase, source_ref="test:compare", observed_at=observed,
            recorded_at=observed, notes=notes)], actor="t")
        self.store.import_observations([ObservationRecord(
            observation_id=f"OBS-{school}-{iso}-ALL", session_id=f"SES-{school}-{iso}-LUNCH",
            dish_id="ALL", prepared_mass_g=None, served_mass_g=None,
            production_waste_g=None, serving_waste_g=None, plate_waste_g=pw_g,
            avoidable_waste_g=None, avoidability_status=enums.AV_UNCLASSIFIED,
            evidence_status=enums.REAL_OBSERVED, source_ref="test:compare",
            observed_at=observed, recorded_at=observed)], actor="t")

    def _fill(self, base_vals: list[float], int_vals: list[float],
              menus: int = 5, school="FI-TEST"):
        d0 = date(2024, 9, 2)
        for i, v in enumerate(base_vals):
            self._add_day(d0 + timedelta(days=i), enums.PHASE_BASELINE,
                          100 + i, v * (100 + i), menu=(i % menus) + 1, school=school)
        for i, v in enumerate(int_vals):
            self._add_day(d0 + timedelta(days=200 + i), enums.PHASE_INTERVENTION,
                          100 + i, v * (100 + i), menu=(i % menus) + 1, school=school)


class TestComparePhases(ComparePhasesTestBase):
    def test_effect_shift_detected_and_aggregation_correct(self):
        base = [20.0] * 10 + [22.0, 18.0]          # средняя 20, агрегат 20
        interv = [16.0] * 8                        # явное снижение
        self._fill(base, interv)
        r = compare_phases(self.store, "FI-TEST")
        self.assertEqual(r["status"], "OK")
        b = r["phases"]["BASELINE"]
        i = r["phases"]["INTERVENTION"]
        # агрегат = Σpw/Σdiners; при per_diner=v и diners=100+i агрегат ≠ среднее v
        self.assertAlmostEqual(i["aggregate_per_diner_g"], 16.0, places=6)
        self.assertLess(r["diff_g"], 0)
        self.assertLess(r["welch"]["p"], 0.001)
        self.assertEqual(len(r["caveats"]), 4)
        self.assertTrue(any("причин" in c or "НЕ утверждается" in c for c in r["caveats"]))
        # меню-выравнивание: 5 общих меню, парная разница отрицательная
        ma = r["menu_aligned"]
        self.assertEqual(ma["n_common_menu_ids"], 5)
        self.assertLess(ma["mean_paired_diff_g"], 0)
        # design_check: n на группу — целое > 0
        dc = r["design_check"]
        self.assertIsInstance(dc["required_days_per_arm"], int)
        self.assertGreater(dc["required_days_per_arm"], 0)
        self.assertGreater(dc["sigma_baseline_g"], 0)

    def test_no_shift_nonsignificant(self):
        vals = [20.0, 21.0, 19.0, 20.5, 19.5, 20.2, 19.8, 20.1]
        self._fill(vals, vals)
        r = compare_phases(self.store, "FI-TEST")
        self.assertEqual(r["status"], "OK")
        self.assertGreater(r["welch"]["p"], 0.05)
        self.assertLess(abs(r["diff_g"]), 1.0)

    def test_data_not_ready_without_phases(self):
        self._fill([20.0] * 10, [16.0] * 8)
        # затираем фазы: добавляем школу с одной фазой — используем другую школу
        r = compare_phases(self.store, "NO-PHASES-SCHOOL")
        self.assertEqual(r["status"], "DATA_NOT_READY")
        self.assertTrue(r["reason"])
        self.assertIn("baseline=0", r["reason"])

    def test_other_phases_listed_and_skipped(self):
        base = [20.0] * 10
        interv = [16.0] * 8
        self._fill(base, interv)
        d0 = date(2024, 9, 2)
        self._add_day(d0 - timedelta(days=1), enums.PHASE_CALIBRATION, 100, 2000.0)
        r = compare_phases(self.store, "FI-TEST")
        self.assertEqual(r["other_phases_skipped"], {enums.PHASE_CALIBRATION: 1})
        self.assertEqual(r["phases"]["BASELINE"]["days"], 10)


if __name__ == "__main__":
    unittest.main()
