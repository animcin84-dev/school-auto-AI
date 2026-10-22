"""Тесты качества данных, KZ-календаря и алертов питания."""

import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from sra import enums
from sra.contracts import ObservationRecord, SessionRecord
from sra.kz_calendar import (load_calendar, is_school_day, non_school_reason,
                             school_days_between)
from sra.monitoring import nutrition_alerts
from sra.quality import crosscheck_zenodo_fi, data_quality_report
from sra.store import EvidenceStore


def _sess(store, i: int, diners: int, *, school="T", waste=None, day=None,
          served=None):
    d = day or (date(2024, 9, 2) + timedelta(days=i))
    ts = datetime(d.year, d.month, d.day, 12, 0, tzinfo=timezone.utc)
    store.import_sessions([SessionRecord(
        session_id=f"SES-{school}-{d.isoformat()}-LUNCH", school_id=school,
        service_date=d, timezone="UTC", meal_type="LUNCH",
        attendance_count=diners, meals_requested=None, meals_served=diners,
        demand_censored=enums.CENSOR_UNKNOWN, evidence_status=enums.REAL_OBSERVED,
        study_phase=None, source_ref="t", observed_at=ts, recorded_at=ts)], actor="t")
    if waste is not None:
        store.import_observations([ObservationRecord(
            observation_id=f"OBS-{school}-{d.isoformat()}-ALL",
            session_id=f"SES-{school}-{d.isoformat()}-LUNCH", dish_id="ALL",
            prepared_mass_g=None, served_mass_g=served, production_waste_g=None,
            serving_waste_g=None, plate_waste_g=waste,
            avoidable_waste_g=None, avoidability_status=enums.AV_UNCLASSIFIED,
            evidence_status=enums.REAL_OBSERVED, source_ref="t",
            observed_at=ts, recorded_at=ts)], actor="t")


class TestCalendar(unittest.TestCase):
    def test_known_dates(self):
        cal = load_calendar()
        # фиксированный праздник
        ok, reason = is_school_day(date(2027, 3, 8), cal)
        self.assertFalse(ok)
        self.assertIn("праздник", reason)
        # выходной
        d = date(2026, 10, 24)  # суббота
        self.assertEqual(non_school_reason(d, cal), "выходной (сб/вс)")
        # лето
        self.assertIn("летние", non_school_reason(date(2026, 7, 10), cal))
        # обычный учебный день
        ok, reason = is_school_day(date(2026, 9, 15), cal)  # вторник
        self.assertTrue(ok, msg=reason)
        self.assertIsNone(reason)

    def test_school_days_count_and_windows(self):
        cal = load_calendar()
        # 5 будних учебных дней без праздников: 15–19 сентября 2026 (вт–пт+пн)
        n = school_days_between(date(2026, 9, 14), date(2026, 9, 20), cal)
        self.assertEqual(n, 5)
        # окно через Новый год ловит и декабрь, и январь
        self.assertTrue(non_school_reason(date(2026, 12, 27), cal))
        self.assertTrue(non_school_reason(date(2027, 1, 2), cal))


class TestQuality(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = Path(tempfile.mkdtemp(prefix="sra-q-"))
        self.store = EvidenceStore(self.tmp / "open.sqlite3", enums.ENV_OPEN)

    def tearDown(self):
        self.store.close()
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_clean_data_all_ok(self):
        for i in range(12):  # будни подряд, стабильные значения
            d = date(2024, 9, 2) + timedelta(days=i)
            if d.weekday() >= 5:
                continue
            _sess(self.store, i, 100 + i % 3, waste=2000 + i * 10, day=d)
        rep = data_quality_report(self.store, "T")
        self.assertEqual(rep["status"], "OK", msg=rep["checks"])
        checks = {c["check"]: c for c in rep["checks"]}
        self.assertTrue(checks["completeness"]["ok"])
        self.assertTrue(checks["calendar_gaps"]["ok"])

    def test_gap_and_outlier_flagged(self):
        # 10 дней с одним выбросом (×10) и один пропущенный будний день
        d = date(2024, 9, 2)
        added = 0
        while added < 10:
            if d.weekday() < 5:
                if added == 5:
                    pass  # пропуск учебного дня
                else:
                    waste = 20000 if added == 3 else 2000 + added * 50
                    _sess(self.store, added, 100, waste=waste, day=d)
                added += 1
            d += timedelta(days=1)
        rep = data_quality_report(self.store, "T")
        checks = {c["check"]: c for c in rep["checks"]}
        self.assertFalse(checks["calendar_gaps"]["ok"])
        self.assertFalse(checks["per_diner_outliers"]["ok"])
        self.assertEqual(rep["status"], "ATTENTION")
        # выброс указан с session_id и modified z
        outl = checks["per_diner_outliers"]["details"]["outliers_gt_3_5mad"]
        self.assertTrue(outl and "modified_z" in outl[0])

    def test_empty_school_honest(self):
        rep = data_quality_report(self.store, "NOPE")
        self.assertEqual(rep["status"], "DATA_NOT_READY")
        self.assertTrue(rep["reason"])

    def test_fi_crosscheck_finds_bad_cell(self):
        fi = Path("Food waste data school Finland 2024-2025.xlsx")
        if not fi.exists():
            self.skipTest("реальный FI-файл отсутствует")
        from sra.ingest import xlsx_min, zenodo_fi
        m = zenodo_fi.map_workbook(xlsx_min.read_workbook(fi))
        for r in m["sessions"]:
            self.store.import_sessions([r], actor="t")
        for o in m["observations"]:
            self.store.import_observations([o], actor="t")
        cc = crosscheck_zenodo_fi(fi, self.store, "FI-ZEN-20825233")
        self.assertEqual(cc["status"], "OK")
        self.assertEqual(cc["rows_checked"], 155)
        dates = [x["date"] for x in cc["mismatches"]]
        self.assertIn("2025-03-17", dates)  # ячейка авторов 8.25 vs 34.21
        bad = next(x for x in cc["mismatches"] if x["date"] == "2025-03-17")
        self.assertAlmostEqual(bad["source_pw_g_per_diner"], 8.25, places=1)
        self.assertAlmostEqual(bad["computed_from_pw_and_diners"], 34.21, places=1)


class TestNutritionAlerts(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = Path(tempfile.mkdtemp(prefix="sra-a-"))
        self.store = EvidenceStore(self.tmp / "open.sqlite3", enums.ENV_OPEN)

    def tearDown(self):
        self.store.close()
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_no_data_reason_not_number(self):
        alerts = nutrition_alerts(self.store, "EMPTY")
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["code"], "UNEATEN_INDEX_UNAVAILABLE")
        self.assertTrue(alerts[0]["reason"])

    def test_below_and_above_threshold(self):
        # ~19% несъеденного: ниже порога
        for i in range(8):
            _sess(self.store, i, 100, waste=1900, served=8100,
                  day=date(2024, 9, 2) + timedelta(days=i))
        low = nutrition_alerts(self.store, "T", uneaten_threshold_pct=30.0)
        self.assertEqual(low[-1]["code"], "UNEATEN_INDEX_OK")
        self.assertLess(low[-1]["value_pct"], 30.0)
        # ~50%: выше порога
        store2 = EvidenceStore(self.tmp / "open2.sqlite3", enums.ENV_OPEN)
        try:
            for i in range(8):
                _sess(store2, i, 100, waste=5000, served=5000,
                      day=date(2024, 9, 2) + timedelta(days=i))
            high = nutrition_alerts(store2, "T", uneaten_threshold_pct=30.0)
            self.assertEqual(high[-1]["code"], "UNEATEN_INDEX_HIGH")
            self.assertEqual(high[-1]["severity"], "WARN")
            self.assertIn("бракеражной", high[-1]["note"])
        finally:
            store2.close()


if __name__ == "__main__":
    unittest.main()
