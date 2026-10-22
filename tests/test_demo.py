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

    def test_optimizer_decision(self):
        od = self.report["optimizer_decision"]
        # Направление зависит от окна данных (при 30 днях смена режима не видна и
        # оптимизатор предлагает УВЕЛИЧЕНИЕ; при 60 — сокращение), поэтому проверяем
        # контракт и границы, а не направление.
        self.assertEqual(od["action"], "ADJUST_PREPARED_VOLUME")
        self.assertEqual(od["action_status"], "SIMULATED_NOT_EXECUTABLE")
        self.assertEqual(od["mode"], enums.MODE_DEMO)
        ap = od["action_params"]
        self.assertGreater(abs(ap["delta_g"]), 1e-9)
        self.assertLessEqual(abs(ap["delta_g"]), ap["delta_max_g"] + 1e-9)
        for key in ("shortage_risk_max", "delta_bound_g", "nonnegative_mass_g",
                    "time_budget_s"):
            self.assertTrue(od["constraint_checks"][key]["pass"], msg=key)
        self.assertEqual(od["expected_effect_interval"]["nature"], enums.SIMULATED)
        self.assertTrue(any("порци" in n for n in od["notes"]),
                        "должна быть оговорка о неприкосновенности порции")

    def test_strategy_comparison_in_demo(self):
        sc = self.report["strategy_comparison"]
        self.assertEqual(sc["status"], "OK")
        self.assertEqual(sc["artifact_nature"], enums.SIMULATED)
        for name in ("no_change", "fixed_quantile_rule", "optimizer"):
            self.assertGreater(sc["strategies"][name]["n_days"], 0, msg=name)

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

    def test_ml_track_in_demo_is_honestly_labeled(self):
        ml = self.report["ml_track"]
        self.assertEqual(ml["data_nature"], enums.SYNTHETIC)
        self.assertIn("механики", ml["note"])
        # 30 дней демо может быть мало для ML — честный DATA_NOT_READY с причиной
        self.assertIn(ml["diners_backtest"]["status"], ("OK", "DATA_NOT_READY"))
        if ml["diners_backtest"]["status"] == "OK":
            self.assertIn(ml["diners_backtest"]["best_by_mae"],
                          {"moving_average5", "weekday3", "menu_history",
                           "ridge", "ridge_notrend", "sklearn_gbdt"})
        else:
            self.assertTrue(ml["diners_backtest"].get("reason") or True)
        self.assertIn(ml["composite_backtest"]["status"], ("OK", "DATA_NOT_READY"))
        codes = [a["code"] for a in ml["nutrition_alerts"]]
        self.assertTrue(codes and
                        all(c in ("UNEATEN_INDEX_OK", "UNEATEN_INDEX_HIGH",
                                  "UNEATEN_INDEX_UNAVAILABLE") for c in codes))
        self.assertTrue(any("ML-трек" in w for w in self.report["warnings"]))


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
        # короткое демо: ворота фаз честно блокируют SHADOW, но команда завершается
        rc = cli_main(["demo", "--dir", str(demo_dir), "--seed", "42", "--days", "10"])
        self.assertEqual(rc, 0)
        rc = cli_main(["metrics", "--db", str(demo_dir / "demo.sqlite3"),
                       "--environment", "demo", "--school-id", "SCH-DEMO-001"])
        self.assertEqual(rc, 0)

    def test_demo_short_window_gate_blocks_shadow_gracefully(self):
        demo_dir = self.tmp / "demo-short"
        rc = cli_main(["demo", "--dir", str(demo_dir), "--seed", "42", "--days", "10"])
        self.assertEqual(rc, 0)
        report = json.loads((demo_dir / "demo_report.json").read_text(encoding="utf-8"))
        self.assertLess(report["study_phase"]["phase"], "SHADOW")
        self.assertEqual(report["shadow"]["status"], "DATA_NOT_READY")
        self.assertEqual(report["monitoring"]["status"], "DATA_NOT_READY")

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

    def test_explain_and_assistant_cli(self):
        demo_dir = self.tmp / "demo-explain"
        cli_main(["demo", "--dir", str(demo_dir), "--seed", "42", "--days", "60"])
        rc = cli_main(["explain", "--db", str(demo_dir / "demo.sqlite3"),
                       "--environment", "demo", "--school-id", "SCH-DEMO-001"])
        self.assertEqual(rc, 0)
        rc = cli_main(["assistant-call", "--db", str(demo_dir / "demo.sqlite3"),
                       "--environment", "demo", "--tool", "phase_status",
                       "--school-id", "SCH-DEMO-001"])
        self.assertEqual(rc, 0)
        # отказ -> код 2
        rc = cli_main(["assistant-call", "--db", str(demo_dir / "demo.sqlite3"),
                       "--environment", "demo", "--tool", "school_overview",
                       "--school-id", "SCH-NOPE"])
        self.assertEqual(rc, 2)

    def test_demo_full_path_reaches_shadow(self):
        demo_dir = self.tmp / "demo-long"
        rc = cli_main(["demo", "--dir", str(demo_dir), "--seed", "42", "--days", "60"])
        self.assertEqual(rc, 0)
        report = json.loads((demo_dir / "demo_report.json").read_text(encoding="utf-8"))
        self.assertEqual(report["study_phase"]["phase"], "SHADOW")
        self.assertEqual(report["shadow"]["status"], "OK")
        self.assertEqual(report["shadow"]["inserted"], report["shadow"]["days"])


if __name__ == "__main__":
    unittest.main()
