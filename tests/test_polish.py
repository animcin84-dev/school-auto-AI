"""Тесты полировки P1: инструменты помощника (ml_status/digest), сверка
«Σ блюд = итог дня» (§C.12), дедупликация алертов (§C.13), экспорт md/CSV
(№19).
"""
from __future__ import annotations

import csv
import io
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from sra import enums
from sra.assistant.tools import call_tool
from sra.contracts import ObservationRecord, SessionRecord
from sra.monitoring import alert_fingerprint, diff_alerts
from sra.quality import data_quality_report
from sra.reportio import digest_norms_csv, digest_to_markdown, digest_window_csv
from sra.store import EvidenceStore

TZ = timezone.utc


def _session(i, served=100):
    d = date(2026, 3, 1) + timedelta(days=i)
    dt = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc) + timedelta(days=i)
    return SessionRecord(
        session_id=f"SES-{i}", school_id="SCH-T", service_date=d,
        timezone="Asia/Almaty", meal_type="LUNCH", attendance_count=served,
        meals_requested=None, meals_served=served,
        demand_censored=enums.CENSOR_FALSE, evidence_status=enums.SYNTHETIC,
        study_phase=None, menu_id=str(i % 5),
        source_ref="t:polish", observed_at=dt, recorded_at=dt)


def _obs(session_id, dish, served_mass=None, plate_waste=None):
    return ObservationRecord(
        observation_id=f"OBS-{session_id}-{dish}", session_id=session_id,
        dish_id=dish, prepared_mass_g=None, served_mass_g=served_mass,
        production_waste_g=None, serving_waste_g=None, plate_waste_g=plate_waste,
        avoidable_waste_g=None, avoidability_status=enums.AV_UNCLASSIFIED,
        evidence_status=enums.SYNTHETIC, source_ref="t:polish",
        observed_at=datetime(2026, 3, 2, 13, 0, tzinfo=timezone.utc),
        recorded_at=datetime(2026, 3, 2, 14, 0, tzinfo=timezone.utc))


class TestAssistantNewTools(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.store = EvidenceStore(self.tmp / "db.sqlite3", enums.ENV_DEMO)
        self.store.import_sessions([_session(i) for i in range(3)], actor="t")

    def tearDown(self):
        self.store.close()

    def test_ml_status_tool(self):
        res = call_tool(self.store, None, "ml_status",
                        {"school_id": "SCH-T"}, actor="test")
        self.assertTrue(res["ok"])
        self.assertTrue(res["facts"]["read_only"])
        self.assertEqual(res["facts"]["sessions"], 3)
        self.assertIn("DATA_NOT_READY", res["facts"]["diners_sufficiency"])

    def test_nutrition_digest_tool(self):
        res = call_tool(self.store, None, "nutrition_digest",
                        {"school_id": "SCH-T", "days": 7}, actor="test")
        self.assertTrue(res["ok"])
        self.assertTrue(res["facts"]["read_only"])
        self.assertIn("window_metric", res["facts"])
        # days ограничен сверху
        res2 = call_tool(self.store, None, "nutrition_digest",
                         {"school_id": "SCH-T", "days": 999}, actor="test")
        self.assertTrue(res2["ok"])

    def test_refusal_for_foreign_school(self):
        res = call_tool(self.store, None, "ml_status",
                        {"school_id": "SCH-OTHER"}, actor="test")
        self.assertFalse(res["ok"])
        self.assertEqual(res["refusal"]["code"], "NO_SUCH_SCHOOL")


class TestDishSumVsTotal(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.store = EvidenceStore(self.tmp / "db.sqlite3", enums.ENV_DEMO)

    def tearDown(self):
        self.store.close()

    def _check(self):
        rep = data_quality_report(self.store, "SCH-T")
        return next(c for c in rep["checks"] if c["check"] == "dish_sum_vs_total")

    def test_mismatch_detected(self):
        self.store.import_sessions([_session(0)], actor="t")
        self.store.import_observations([
            _obs("SES-0", "SALAD", served_mass=8000.0, plate_waste=100.0),
            _obs("SES-0", "FIRST", served_mass=22000.0, plate_waste=200.0),
            _obs("SES-0", "ALL", served_mass=30000.0, plate_waste=500.0),
            # итог дня 500 vs Σ блюд 300 -> расхождение
        ], actor="t")
        chk = self._check()
        self.assertFalse(chk["ok"])
        mm = chk["details"]["mismatches"][0]
        self.assertEqual(mm["session_id"], "SES-0")
        self.assertEqual(mm["sum_of_dishes_g"], 300.0)
        self.assertEqual(mm["total_row_g"], 500.0)

    def test_consistent_totals_pass(self):
        self.store.import_sessions([_session(0)], actor="t")
        self.store.import_observations([
            _obs("SES-0", "SALAD", served_mass=8000.0, plate_waste=100.0),
            _obs("SES-0", "ALL", served_mass=8000.0, plate_waste=100.0),
        ], actor="t")
        self.assertTrue(self._check()["ok"])

    def test_only_dishes_or_only_total_skipped(self):
        self.store.import_sessions([_session(0), _session(1)], actor="t")
        self.store.import_observations([
            _obs("SES-0", "SALAD", served_mass=8000.0, plate_waste=100.0),  # без итога
            _obs("SES-1", "ALL", served_mass=8000.0, plate_waste=100.0),    # без разбивки
        ], actor="t")
        chk = self._check()
        self.assertTrue(chk["ok"])
        self.assertEqual(chk["details"]["n_comparisons"], 0)


class TestAlertsDedup(unittest.TestCase):
    def test_same_value_deduplicated(self):
        a = [{"code": "UNEATEN_INDEX_OK", "severity": "INFO", "status": "OK",
              "value_pct": 15.2, "threshold_pct": 30}]
        r1 = diff_alerts(a, None)
        self.assertEqual(r1["n_new"], 1)
        r2 = diff_alerts(a, a)
        self.assertEqual(r2["n_unchanged"], 1)
        self.assertEqual(r2["to_notify"], [])

    def test_small_drift_deduplicated_threshold_jump_notified(self):
        prev = [{"code": "UNEATEN_INDEX_OK", "value_pct": 15.2,
                 "threshold_pct": 30}]
        drift = [{"code": "UNEATEN_INDEX_OK", "value_pct": 15.5,
                  "threshold_pct": 30}]
        self.assertEqual(diff_alerts(drift, prev)["n_new"], 0)   # < 1 п.п.
        jump = [{"code": "UNEATEN_INDEX_HIGH", "value_pct": 35.0,
                 "threshold_pct": 30}]
        self.assertEqual(diff_alerts(jump, prev)["n_new"], 1)    # код сменился

    def test_unavailable_alerts_dedup_too(self):
        prev = [{"code": "UNEATEN_INDEX_UNAVAILABLE", "severity": "INFO",
                 "status": "UNAVAILABLE", "reason": "нет данных"}]
        curr = [{"code": "UNEATEN_INDEX_UNAVAILABLE", "severity": "INFO",
                 "status": "UNAVAILABLE", "reason": "нет данных"}]
        self.assertEqual(diff_alerts(curr, prev)["n_unchanged"], 1)

    def test_fingerprint_ignores_subpercent(self):
        # усечение: 15.2 и 15.5 в одном корзине, 15.2 и 16.0 — в разных
        a = alert_fingerprint({"code": "X", "value_pct": 15.2,
                               "threshold_pct": 30})
        b = alert_fingerprint({"code": "X", "value_pct": 15.5,
                               "threshold_pct": 30})
        c = alert_fingerprint({"code": "X", "value_pct": 16.0,
                               "threshold_pct": 30})
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)


class TestReportExport(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.store = EvidenceStore(self.tmp / "db.sqlite3", enums.ENV_DEMO)
        self.store.import_sessions([_session(0, served=10)], actor="t")
        self.store.import_observations([
            _obs("SES-0", "GARNISH", served_mass=1800.0, plate_waste=300.0),
        ], actor="t")
        from sra.digest import build_digest
        self.d = build_digest(self.store, "SCH-T", days=7)

    def tearDown(self):
        self.store.close()

    def test_markdown_sections(self):
        md = digest_to_markdown(self.d)
        self.assertIn("# Дайджест питания — SCH-T", md)
        self.assertIn("## Отходы за окно", md)
        self.assertIn("## Качество данных", md)
        self.assertIn("## Порции vs Санправила РК", md)
        self.assertIn("## Алерты", md)
        self.assertIn("## Прогнозы (ML)", md)
        # недоступные секции сохраняют причину, а не исчезают
        self.assertIn("нет измерений", md)

    def test_csv_roundtrip(self):
        win = digest_window_csv(self.d)
        rows = list(csv.reader(io.StringIO(win.lstrip("\ufeff"))))
        self.assertEqual(rows[0][0], "school_id")
        self.assertEqual(rows[1][0], "SCH-T")
        norms = digest_norms_csv(self.d)
        rows2 = list(csv.DictReader(io.StringIO(norms.lstrip("\ufeff"))))
        garnish = next(r for r in rows2 if r["dish"] == "garnish")
        self.assertEqual(garnish["status"], "WARN")
        self.assertEqual(garnish["n_measured"], "1")
        self.assertIn("Σ масса блюда", self.d["norms"]["portion_semantics"])


if __name__ == "__main__":
    unittest.main()
