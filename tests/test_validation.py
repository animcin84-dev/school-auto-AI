"""Тесты семантической валидации (правила контракта данных)."""

import unittest

from sra import validation
from sra.contracts import parse_observation_row, parse_session_row
from tests.helpers import make_observation, make_session


class TestSessionValidation(unittest.TestCase):
    def _valid(self, **over):
        return parse_session_row(make_session(**over), row_number=2)

    def test_ok(self):
        report = validation.validate_sessions([self._valid()], existing_ids=set())
        self.assertTrue(report.ok)
        self.assertEqual(len(report.accepted), 1)

    def test_raw_evidence_verified_rejected(self):
        report = validation.validate_sessions([self._valid(evidence_status="VERIFIED")],
                                              existing_ids=set())
        self.assertFalse(report.ok)
        self.assertEqual(report.errors[0].code, "E_BAD_EVIDENCE")

    def test_predicted_evidence_rejected(self):
        report = validation.validate_sessions([self._valid(evidence_status="PREDICTED")],
                                              existing_ids=set())
        self.assertEqual(report.errors[0].code, "E_BAD_EVIDENCE")

    def test_time_order_rejected(self):
        report = validation.validate_sessions(
            [self._valid(recorded_at="2026-10-01T12:00:00+05:00")], existing_ids=set())
        self.assertEqual(report.errors[0].code, "E_TIME_ORDER")

    def test_served_gt_requested_warns(self):
        """Выдач больше зарегистрированных запросов: возможно при регистрации на
        месте — предупреждение с флагом, а не отказ (семантику сверяет школа)."""
        report = validation.validate_sessions([self._valid(meals_served="120")],
                                              existing_ids=set())
        self.assertTrue(report.ok)
        self.assertIn("W_SERVED_GT_REQUESTED", report.accepted[0].quality_flags)

    def test_shortfall_without_censoring_warns(self):
        # served < requested при demand_censored=false — не противоречие,
        # но требует подтверждения протокола; предупреждение фиксируется флагом.
        report = validation.validate_sessions([self._valid(meals_served="80")],
                                              existing_ids=set())
        self.assertTrue(report.ok)
        self.assertIn("W_CENSOR_CHECK", report.accepted[0].quality_flags)

    def test_duplicate_in_batch_rejected(self):
        r = self._valid()
        report = validation.validate_sessions([r, self._valid()], existing_ids=set())
        self.assertEqual(len(report.accepted), 1)
        self.assertEqual(report.errors[0].code, "E_DUPLICATE_ID")

    def test_duplicate_against_store_rejected(self):
        report = validation.validate_sessions([self._valid(session_id="S-EXISTING")],
                                              existing_ids={"S-EXISTING"})
        self.assertEqual(report.errors[0].code, "E_DUPLICATE_ID")


class TestObservationValidation(unittest.TestCase):
    def _valid(self, **over):
        return parse_observation_row(make_observation(**over), row_number=2)

    def test_ok(self):
        report = validation.validate_observations([self._valid()],
                                                  known_sessions={"S1"},
                                                  existing_observation_ids=set(),
                                                  existing_pairs=set())
        self.assertTrue(report.ok)

    def test_served_gt_prepared_without_notes_rejected(self):
        report = validation.validate_observations(
            [self._valid(served_mass_g="11000")], known_sessions={"S1"},
            existing_observation_ids=set(), existing_pairs=set())
        self.assertEqual(report.errors[0].code, "E_SERVED_GT_PREPARED")

    def test_served_gt_prepared_with_notes_flagged_not_rejected(self):
        report = validation.validate_observations(
            [self._valid(served_mass_g="11000",
                         notes="добавлена вторая партия по факту; базы согласованы")],
            known_sessions={"S1"}, existing_observation_ids=set(), existing_pairs=set())
        self.assertTrue(report.ok)
        self.assertIn("BASE_MISMATCH_EXPLAINED", report.accepted[0].quality_flags)
        self.assertEqual(len(report.warnings), 1)

    def test_unknown_session_rejected(self):
        report = validation.validate_observations([self._valid()], known_sessions=set(),
                                                  existing_observation_ids=set(),
                                                  existing_pairs=set())
        self.assertEqual(report.errors[0].code, "E_UNKNOWN_SESSION")

    def test_duplicate_pair_rejected(self):
        report = validation.validate_observations(
            [self._valid(), self._valid(observation_id="O2")],
            known_sessions={"S1"}, existing_observation_ids=set(), existing_pairs=set())
        codes = [e.code for e in report.errors]
        self.assertIn("E_DUPLICATE_DISH_SESSION", codes)

    def test_avoidable_exceeding_categories_rejected(self):
        # все три категории = 400+600+900=1900; avoidable 5000 — нарушение границ
        report = validation.validate_observations(
            [self._valid(avoidable_waste_g="5000")], known_sessions={"S1"},
            existing_observation_ids=set(), existing_pairs=set())
        self.assertEqual(report.errors[0].code, "E_AVOIDABLE_GT_CATEGORIES")

    def test_avoidable_with_missing_categories_warns(self):
        report = validation.validate_observations(
            [self._valid(serving_waste_g="")], known_sessions={"S1"},
            existing_observation_ids=set(), existing_pairs=set())
        self.assertTrue(report.ok)
        self.assertIn("W_AVOIDABLE_UNVERIFIABLE", [w.code for w in report.warnings])

    def test_classified_requires_value(self):
        report = validation.validate_observations(
            [self._valid(avoidable_waste_g="")], known_sessions={"S1"},
            existing_observation_ids=set(), existing_pairs=set())
        self.assertEqual(report.errors[0].code, "E_CLASSIFIED_WITHOUT_VALUE")

    def test_unclassified_with_value_rejected(self):
        report = validation.validate_observations(
            [self._valid(avoidability_status="UNCLASSIFIED")], known_sessions={"S1"},
            existing_observation_ids=set(), existing_pairs=set())
        self.assertEqual(report.errors[0].code, "E_AVOIDABLE_UNCLASSIFIED")


if __name__ == "__main__":
    unittest.main()
