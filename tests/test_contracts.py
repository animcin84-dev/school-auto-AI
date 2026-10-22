"""Тесты структурного парсинга (contracts) и CSV-ввода (csvio)."""

import unittest
from datetime import date, datetime

from sra import contracts, csvio
from sra.errors import ValidationError


def session_row(**overrides) -> dict:
    row = {
        "session_id": "S1", "school_id": "SCH-1", "service_date": "2026-10-01",
        "timezone": "Asia/Almaty", "meal_type": "LUNCH",
        "attendance_count": "100", "meals_requested": "95", "meals_served": "90",
        "demand_censored": "false", "evidence_status": "REAL_OBSERVED",
        "study_phase": "BASELINE", "source_ref": "SRC-1",
        "observed_at": "2026-10-01T13:30:00+05:00",
        "recorded_at": "2026-10-01T15:00:00+05:00",
    }
    row.update(overrides)
    return row


def observation_row(**overrides) -> dict:
    row = {
        "observation_id": "O1", "session_id": "S1", "dish_id": "D1",
        "prepared_mass_g": "10000", "served_mass_g": "9000",
        "production_waste_g": "400", "serving_waste_g": "600",
        "plate_waste_g": "900", "avoidable_waste_g": "500",
        "avoidability_status": "CLASSIFIED", "evidence_status": "REAL_OBSERVED",
        "source_ref": "SRC-1",
        "observed_at": "2026-10-01T13:30:00+05:00",
        "recorded_at": "2026-10-01T15:00:00+05:00",
    }
    row.update(overrides)
    return row


class TestSessionParsing(unittest.TestCase):
    def test_full_parse(self):
        rec = contracts.parse_session_row(session_row(), row_number=2)
        self.assertEqual(rec.session_id, "S1")
        self.assertIsInstance(rec.service_date, date)
        self.assertEqual(rec.attendance_count, 100)
        self.assertEqual(rec.demand_censored, "false")
        self.assertEqual(rec.observed_at.utcoffset().total_seconds(), 5 * 3600)

    def test_missing_is_not_zero(self):
        rec = contracts.parse_session_row(session_row(attendance_count="", meals_served=""), row_number=2)
        self.assertIsNone(rec.attendance_count)
        self.assertIsNone(rec.meals_served)

    def test_empty_censored_becomes_unknown(self):
        rec = contracts.parse_session_row(session_row(demand_censored=""), row_number=2)
        self.assertEqual(rec.demand_censored, "unknown")

    def test_fractional_counter_rejected(self):
        with self.assertRaises(ValidationError) as cm:
            contracts.parse_session_row(session_row(meals_served="12.5"), row_number=2)
        self.assertEqual(cm.exception.code, "E_BAD_INT")

    def test_naive_time_rejected(self):
        with self.assertRaises(ValidationError) as cm:
            contracts.parse_session_row(session_row(observed_at="2026-10-01T13:30:00"), row_number=2)
        self.assertEqual(cm.exception.code, "E_NAIVE_TIME")

    def test_bad_timezone_rejected(self):
        with self.assertRaises(ValidationError) as cm:
            contracts.parse_session_row(session_row(timezone="Mars/Phobos"), row_number=2)
        self.assertEqual(cm.exception.code, "E_BAD_TZ")

    def test_bad_date_rejected(self):
        with self.assertRaises(ValidationError) as cm:
            contracts.parse_session_row(session_row(service_date="01.10.2026"), row_number=2)
        self.assertEqual(cm.exception.code, "E_BAD_DATE")

    def test_unknown_meal_type_rejected(self):
        with self.assertRaises(ValidationError) as cm:
            contracts.parse_session_row(session_row(meal_type="BRUNCH"), row_number=2)
        self.assertEqual(cm.exception.code, "E_BAD_ENUM")

    def test_negative_counter_rejected(self):
        with self.assertRaises(ValidationError) as cm:
            contracts.parse_session_row(session_row(meals_served="-5"), row_number=2)
        self.assertEqual(cm.exception.code, "E_NEGATIVE_COUNT")


class TestObservationParsing(unittest.TestCase):
    def test_zero_mass_is_valid_measurement(self):
        rec = contracts.parse_observation_row(observation_row(plate_waste_g="0"), row_number=2)
        self.assertEqual(rec.plate_waste_g, 0.0)  # ноль — измерение, не пропуск

    def test_negative_mass_rejected(self):
        with self.assertRaises(ValidationError) as cm:
            contracts.parse_observation_row(observation_row(plate_waste_g="-3"), row_number=2)
        self.assertEqual(cm.exception.code, "E_NEGATIVE_MASS")

    def test_nonfinite_rejected(self):
        with self.assertRaises(ValidationError) as cm:
            contracts.parse_observation_row(observation_row(prepared_mass_g="inf"), row_number=2)
        self.assertEqual(cm.exception.code, "E_NONFINITE")

    def test_missing_avoidability_defaults_unclassified(self):
        rec = contracts.parse_observation_row(observation_row(avoidability_status=""), row_number=2)
        self.assertEqual(rec.avoidability_status, "UNCLASSIFIED")


class TestCsvIO(unittest.TestCase):
    def test_header_check_rejects_extra_columns(self):
        with self.assertRaises(ValidationError) as cm:
            csvio.check_header(csvio.SESSION_HEADER + ["pupil_name"], "sessions")
        self.assertEqual(cm.exception.code, "E_BAD_HEADER")

    def test_header_check_accepts_notes_extension(self):
        csvio.check_header(csvio.OBSERVATION_HEADER + ["notes"], "observations")

    def test_kg_conversion(self):
        rec = contracts.parse_observation_row(observation_row(prepared_mass_g="10.5"), row_number=2)
        csvio.convert_mass_unit(rec, from_unit="kg")
        self.assertEqual(rec.prepared_mass_g, 10500.0)
        self.assertIn("UNIT_CONVERTED_KG_TO_G", rec.quality_flags)

    def test_roundtrip_payload(self):
        rec = contracts.parse_observation_row(observation_row(), row_number=2)
        rec2 = contracts.ObservationRecord.from_payload(rec.to_payload())
        self.assertEqual(rec.to_payload(), rec2.to_payload())


if __name__ == "__main__":
    unittest.main()
