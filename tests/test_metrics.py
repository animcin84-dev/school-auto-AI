"""Тесты авторитетных метрик с ручными контрольными значениями.

Контрольный набор (все числа подобраны и посчитаны вручную):

  Сессии:
    S1: requested=120, served=100, censored=false
    S2: requested=100, served=50,  censored=true
    S3: requested=пусто, served=пусто, censored=unknown
    S4: requested=40,  served=0,   censored=true   (день с нехваткой)

  Наблюдения (блюда):
    O1 (S1,D1): prepared=10000 served=9000 prod=400 serv=600 plate=900  avoidable=500 CLASSIFIED
    O2 (S1,D2): prepared=11000 served=10000 prod=500 serv=500 plate=1000 avoidable=пусто UNCLASSIFIED
    O3 (S2,D1): prepared=5200  served=5000  prod=пусто serv=800 plate=400 avoidable=200 CLASSIFIED

  Ожидания (ручной счёт):
    UneatenIndex      = (900+1000+400) / (9000+10000+5000) × 100 = 2300/24000×100 = 9.583333…
    Overproduction    = ((10000-9000)+(11000-10000)+(5200-5000)) / (10000+11000+5200) × 100
                        = 2200/26200×100 = 8.396947…
    TotalMeasuredWaste = (400+600+900) + (500+500+1000) = 1900+2000 = 3900 г (O3 исключён: prod пусто)
    WastePerMeal      = (500+200) / (100+50+0) = 700/150 = 4.666667 г/приём
    ServiceLevel      = (100+50+0) / (120+100+40) × 100 = 150/260×100 = 57.692308…
    CostWastePerMeal  = 0.7 кг × 500/кг / 150 = 350/150 = 2.333333 вал/приём
"""

import unittest

from sra import enums, metrics
from sra.contracts import parse_observation_row, parse_session_row
from tests.helpers import make_observation, make_session

TOL = 1e-4


def sessions_fixture():
    return [
        parse_session_row(make_session(session_id="S1", meals_requested="120",
                                       meals_served="100", demand_censored="false"), row_number=1),
        parse_session_row(make_session(session_id="S2", meals_requested="100",
                                       meals_served="50", demand_censored="true"), row_number=2),
        parse_session_row(make_session(session_id="S3", meals_requested="", meals_served="",
                                       demand_censored="unknown"), row_number=3),
        parse_session_row(make_session(session_id="S4", meals_requested="40",
                                       meals_served="0", demand_censored="true"), row_number=4),
    ]


def observations_fixture(evidence=enums.REAL_OBSERVED):
    over = {"evidence_status": evidence}
    return [
        parse_observation_row(make_observation(observation_id="O1", session_id="S1", dish_id="D1",
                                               **over), row_number=1),
        parse_observation_row(make_observation(observation_id="O2", session_id="S1", dish_id="D2",
                                               prepared_mass_g="11000", served_mass_g="10000",
                                               production_waste_g="500", serving_waste_g="500",
                                               plate_waste_g="1000", avoidable_waste_g="",
                                               avoidability_status="UNCLASSIFIED", **over),
                              row_number=2),
        parse_observation_row(make_observation(observation_id="O3", session_id="S2", dish_id="D1",
                                               prepared_mass_g="5200", served_mass_g="5000",
                                               production_waste_g="", serving_waste_g="800",
                                               plate_waste_g="400", avoidable_waste_g="200",
                                               **over), row_number=3),
    ]


class TestHandComputed(unittest.TestCase):
    def setUp(self):
        self.sessions = sessions_fixture()
        self.obs = observations_fixture()

    def test_uneaten_index(self):
        r = metrics.uneaten_index(self.obs)
        self.assertEqual(r.status, "OK")
        self.assertAlmostEqual(r.value, 2300 / 24000 * 100, places=4)
        self.assertEqual(r.numerator, 2300)
        self.assertEqual(r.denominator, 24000)
        self.assertEqual(r.n_inputs, 3)

    def test_overproduction_rate(self):
        r = metrics.overproduction_rate(self.obs)
        self.assertEqual(r.status, "OK")
        self.assertAlmostEqual(r.value, 2200 / 26200 * 100, places=4)
        self.assertEqual(r.numerator, 2200)
        self.assertEqual(r.denominator, 26200)

    def test_total_measured_waste_excludes_incomplete_rows(self):
        r = metrics.total_measured_waste(self.obs)
        self.assertEqual(r.status, "OK")
        self.assertEqual(r.value, 3900)      # 1900 (O1) + 2000 (O2); O3 без prod исключён
        self.assertEqual(r.n_inputs, 2)

    def test_waste_per_meal_counts_meals_once(self):
        r = metrics.waste_per_meal(self.obs, self.sessions)
        self.assertEqual(r.status, "OK")
        self.assertAlmostEqual(r.value, 700 / 150, places=4)
        # Знаменатель: одна сессия один раз, несмотря на 2 блюда в S1 и 4 наблюдения всего.
        self.assertEqual(r.denominator, 150)
        self.assertEqual(r.numerator, 700)

    def test_waste_per_meal_coverage_threshold(self):
        r = metrics.waste_per_meal(self.obs, self.sessions, min_classified_coverage=0.9)
        self.assertEqual(r.status, "UNAVAILABLE")
        self.assertTrue(r.reason.startswith("INSUFFICIENT_CLASSIFICATION_COVERAGE"))

    def test_service_level_with_censoring(self):
        r = metrics.service_level(self.sessions)
        self.assertEqual(r.status, "OK")
        self.assertAlmostEqual(r.value, 150 / 260 * 100, places=4)
        self.assertEqual(r.censoring, {"true": 2, "false": 1, "unknown": 0})
        self.assertTrue(any("цензур" in n for n in r.notes))

    def test_aggregation_not_average_of_percentages(self):
        """Процент школы ≠ среднее процентов блюд (защита от неверной агрегации)."""
        r = metrics.uneaten_index(self.obs)
        dish_pcts = [900 / 9000, 1000 / 10000, 400 / 5000]
        avg_pct = sum(dish_pcts) / 3 * 100
        self.assertNotAlmostEqual(r.value, avg_pct, places=2)
        self.assertAlmostEqual(r.value, 2300 / 24000 * 100, places=6)


class TestUnavailable(unittest.TestCase):
    def test_missing_fields(self):
        r = metrics.service_level(sessions_fixture()[:2])  # есть данные
        # отдельный случай: вообще нет наблюдаемого requested
        s = [parse_session_row(make_session(session_id="X", meals_requested="", meals_served="10",
                                            demand_censored="unknown"), row_number=1)]
        r2 = metrics.service_level(s)
        self.assertEqual(r2.status, "UNAVAILABLE")
        self.assertTrue(r2.reason.startswith("MISSING_FIELDS"))

    def test_zero_denominator_service_level(self):
        s = [parse_session_row(make_session(session_id="X", meals_requested="0", meals_served="0",
                                            demand_censored="true"), row_number=1)]
        r = metrics.service_level(s)
        self.assertEqual(r.status, "UNAVAILABLE")
        self.assertTrue(r.reason.startswith("ZERO_DENOMINATOR"))

    def test_zero_denominator_uneaten(self):
        o = [parse_observation_row(make_observation(observation_id="Z", plate_waste_g="100",
                                                    served_mass_g="0"), row_number=1)]
        r = metrics.uneaten_index(o)
        self.assertEqual(r.status, "UNAVAILABLE")
        self.assertTrue(r.reason.startswith("ZERO_DENOMINATOR"))

    def test_total_waste_unavailable_without_complete_categories(self):
        o = [parse_observation_row(make_observation(observation_id="Z", production_waste_g=""),
                                   row_number=1)]
        r = metrics.total_measured_waste(o)
        self.assertEqual(r.status, "UNAVAILABLE")
        self.assertTrue(r.reason.startswith("MISSING_FIELDS"))

    def test_cost_without_source(self):
        r = metrics.cost_waste_per_meal(observations_fixture(), sessions_fixture(), None)
        self.assertEqual(r.status, "UNAVAILABLE")
        self.assertTrue(r.reason.startswith("COST_SOURCE_ABSENT"))


class TestProvenanceAndCost(unittest.TestCase):
    def test_synthetic_inputs_make_result_simulated(self):
        r = metrics.uneaten_index(observations_fixture(evidence=enums.SYNTHETIC))
        self.assertEqual(r.derived_status, enums.DERIVED_SIMULATED)
        self.assertGreater(r.evidence_counts.get(enums.SYNTHETIC, 0), 0)

    def test_real_inputs_computed(self):
        r = metrics.uneaten_index(observations_fixture(evidence=enums.REAL_OBSERVED))
        self.assertEqual(r.derived_status, enums.DERIVED_COMPUTED)

    def test_cost_with_simulated_source(self):
        cs = metrics.CostSource(policy_id="P-DEMO", cost_per_kg=500.0, currency="KZT_DEMO",
                                evidence_status=enums.SYNTHETIC)
        r = metrics.cost_waste_per_meal(observations_fixture(), sessions_fixture(), cs)
        self.assertEqual(r.status, "OK")
        self.assertAlmostEqual(r.value, 350 / 150, places=4)  # 0.7 кг × 500 / 150 приёмов
        self.assertEqual(r.derived_status, enums.DERIVED_SIMULATED)
        self.assertTrue(any("денежной экономией" in n for n in r.notes))

    def test_cost_with_verified_source_computed(self):
        cs = metrics.CostSource(policy_id="P-REAL", cost_per_kg=500.0, currency="KZT",
                                evidence_status=enums.VERIFIED)
        r = metrics.cost_waste_per_meal(observations_fixture(), sessions_fixture(), cs)
        self.assertEqual(r.derived_status, enums.DERIVED_COMPUTED)


class TestReportBuilder(unittest.TestCase):
    def test_report_nature(self):
        rep = metrics.build_metric_report(sessions_fixture(), observations_fixture())
        self.assertTrue(rep["nature"].startswith("COMPUTED"))
        self.assertEqual(len(rep["metrics"]), 6)
        names = {m["name"] for m in rep["metrics"]}
        self.assertEqual(names, {"UneatenIndex", "OverproductionRate", "TotalMeasuredWaste",
                                 "WastePerMeal", "ServiceLevel", "CostWastePerMeal"})


if __name__ == "__main__":
    unittest.main()
