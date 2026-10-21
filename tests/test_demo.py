"""Тесты демо-пайплайна и изоляции сред (демо не трогает реальные данные)."""

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from sra import enums
from sra.cli import main as cli_main
from sra.contracts import parse_session_row
from sra.demo.run import reset_demo, run_demo
from sra.store import EvidenceStore
from tests.helpers import make_session

CLOCK = lambda: "2026-10-21T12:00:00+00:00"  # noqa: E731


class TestDemoPipeline(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="sra-demo-"))
        cls.report = run_demo(cls.tmp / "demo", seed=42, n_days=30)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_report_flags_synthetic(self):
        self.assertEqual(self.report["data_nature"], enums.SYNTHETIC)
        self.assertEqual(self.report["environment"], enums.ENV_DEMO)
        self.assertEqual(self.report["metric_report"]["nature"],
                         "SIMULATED: в расчёт вошли SYNTHETIC-входы")
        self.assertTrue(self.report["warnings"])

    def test_validation_passed_and_data_loaded(self):
        self.assertEqual(self.report["validation"]["sessions"]["imported"],
                         self.report["manifest"]["n_sessions"])
        self.assertEqual(self.report["validation"]["observations"]["imported"],
                         self.report["manifest"]["n_observations"])

    def test_decision_is_no_change(self):
        d = self.report["decision"]
        self.assertEqual(d["action"], "NO_CHANGE")
        self.assertEqual(d["action_status"], "NOT_EXECUTABLE")
        self.assertEqual(d["mode"], enums.MODE_DEMO)
        self.assertTrue(d["missing_conditions"])

    def test_forecast_section(self):
        f = self.report.get("forecast")
        self.assertIsNotNone(f)
        self.assertEqual(f["status"], "OK")  # 30 дней синтетики достаточно
        self.assertEqual(f["artifact_nature"], enums.SIMULATED)
        names = {m["name"] for m in f["models"]}
        self.assertEqual(names, {"naive", "seasonal_naive", "moving_average"})
        self.assertTrue(f["model_cards"], "карты моделей должны быть сохранены в демо-БД")

    def test_model_cards_in_demo_db(self):
        store = EvidenceStore(self.tmp / "demo" / "demo.sqlite3", enums.ENV_DEMO,
                              clock=CLOCK)
        try:
            cards = store.get_model_cards()
            self.assertTrue(cards)
            self.assertTrue(all(c["model_id"].startswith("sra.forecast.") for c in cards))
        finally:
            store.close()

    def test_selfcheck_ok(self):
        self.assertTrue(all(r["ok"] for r in self.report["selfcheck"]),
                        msg=json.dumps(self.report["selfcheck"], ensure_ascii=False))

    def test_metrics_report_structure(self):
        names = {m["name"] for m in self.report["metric_report"]["metrics"]}
        self.assertEqual(len(names), 6)
        uneaten = next(m for m in self.report["metric_report"]["metrics"]
                       if m["name"] == "UneatenIndex")
        self.assertEqual(uneaten["status"], "OK")
        self.assertEqual(uneaten["derived_status"], enums.DERIVED_SIMULATED)


class TestDemoIsolation(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="sra-iso-"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_real_db_untouched_by_demo_reset(self):
        # 1. "Реальное" хранилище в отдельном каталоге с реальной записью.
        real_dir = self.tmp / "real-data"
        real_db = real_dir / "real.sqlite3"
        store = EvidenceStore(real_db, enums.ENV_REAL, clock=CLOCK)
        store.import_sessions([parse_session_row(make_session(), row_number=0)], actor="admin")
        store.close()

        # 2. Демо рядом, затем чистый сброс демо (без пересоздания).
        demo_dir = self.tmp / "real-data" / "demo"
        run_demo(demo_dir, seed=42, n_days=10)
        self.assertTrue((demo_dir / "demo.sqlite3").exists())
        removed = reset_demo(demo_dir)
        self.assertIn("demo.sqlite3", removed)
        self.assertFalse((demo_dir / "demo.sqlite3").exists())

        # 3. Реальные данные не пострадали.
        store = EvidenceStore(real_db, enums.ENV_REAL, clock=CLOCK)
        sessions = store.fetch_sessions(school_id="SCH-1")
        store.close()
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0].evidence_status, enums.REAL_OBSERVED)

    def test_demo_refuses_real_rows_via_store_guard(self):
        demo_dir = self.tmp / "d2"
        run_demo(demo_dir, seed=1, n_days=5)
        store = EvidenceStore(demo_dir / "demo.sqlite3", enums.ENV_DEMO, clock=CLOCK)
        try:
            from sra.errors import GuardError
            with self.assertRaises(GuardError):
                store.import_sessions([parse_session_row(make_session(), row_number=0)],
                                      actor="x")
        finally:
            store.close()

    def test_demo_db_refuses_real_environment_handle(self):
        demo_dir = self.tmp / "d3"
        run_demo(demo_dir, seed=1, n_days=5)
        from sra.errors import GuardError
        with self.assertRaises(GuardError):
            EvidenceStore(demo_dir / "demo.sqlite3", enums.ENV_REAL, clock=CLOCK)


class TestCliSmoke(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="sra-cli-"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_demo_command_and_metrics_command(self):
        demo_dir = self.tmp / "demo"
        rc = cli_main(["demo", "--dir", str(demo_dir), "--seed", "42", "--days", "10"])
        self.assertEqual(rc, 0)
        rc = cli_main(["metrics", "--db", str(demo_dir / "demo.sqlite3"),
                       "--environment", "demo", "--school-id", "SCH-DEMO-001"])
        self.assertEqual(rc, 0)

    def test_import_into_real_db(self):
        real_db = self.tmp / "real.sqlite3"
        rc = cli_main(["init-db", "--db", str(real_db), "--environment", "real"])
        self.assertEqual(rc, 0)
        # шаблон без строк данных — чистый отказ
        rc = cli_main(["import", "--db", str(real_db), "--environment", "real",
                       "--kind", "sessions", "--file", "templates/sessions.csv",
                       "--actor", "tester"])
        self.assertEqual(rc, 2)
        # валидная строка
        csv_file = self.tmp / "one_session.csv"
        csv_file.write_text(
            "session_id,school_id,service_date,timezone,meal_type,attendance_count,"
            "meals_requested,meals_served,demand_censored,evidence_status,study_phase,"
            "source_ref,observed_at,recorded_at\n"
            "S1,SCH-1,2026-10-01,Asia/Almaty,LUNCH,100,95,90,false,REAL_OBSERVED,BASELINE,"
            "SRC-1,2026-10-01T13:30:00+05:00,2026-10-01T15:00:00+05:00\n",
            encoding="utf-8")
        rc = cli_main(["import", "--db", str(real_db), "--environment", "real",
                       "--kind", "sessions", "--file", str(csv_file), "--actor", "tester"])
        self.assertEqual(rc, 0)

    def test_selfcheck_command(self):
        demo_dir = self.tmp / "demo2"
        cli_main(["demo", "--dir", str(demo_dir), "--seed", "3", "--days", "5"])
        rc = cli_main(["selfcheck", "--db", str(demo_dir / "demo.sqlite3"),
                       "--environment", "demo"])
        self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
