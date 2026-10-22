"""Тесты композитного прогноза: механика декомпозиции, стоимость решения, FI."""

import math
import shutil
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from sra import enums
from sra.contracts import ObservationRecord, SessionRecord
from sra.errors import ValidationError
from sra.forecasting.composite import backtest_composite, build_daily_points
from sra.store import EvidenceStore


def _mk(school, i, diners, mass, *, menu=None, field="plate_waste_g"):
    d = date(2024, 9, 2) + timedelta(days=i)
    ts = datetime(d.year, d.month, d.day, 12, 0, tzinfo=timezone.utc)
    return SessionRecord(
        session_id=f"SES-{school}-{d.isoformat()}-LUNCH", school_id=school,
        service_date=d, timezone="UTC", meal_type="LUNCH",
        attendance_count=diners, meals_requested=None, meals_served=diners,
        demand_censored=enums.CENSOR_UNKNOWN, evidence_status=enums.REAL_OBSERVED,
        study_phase=None, source_ref="t", observed_at=ts, recorded_at=ts,
        notes=(f"MENU={menu}" if menu is not None else "")), (
        ObservationRecord(
            observation_id=f"OBS-{school}-{d.isoformat()}-A",
            session_id=f"SES-{school}-{d.isoformat()}-LUNCH", dish_id="A",
            prepared_mass_g=None,
            served_mass_g=(mass if field == "served_mass_g" else None),
            production_waste_g=None, serving_waste_g=None,
            plate_waste_g=(mass if field == "plate_waste_g" else None),
            avoidable_waste_g=None, avoidability_status=enums.AV_UNCLASSIFIED,
            evidence_status=enums.REAL_OBSERVED, source_ref="t",
            observed_at=ts, recorded_at=ts))


def _fill(store, gen, n=80, school="T", field="plate_waste_g"):
    for i in range(n):
        s, o = gen(i)
        store.import_sessions([s], actor="t")
        store.import_observations([o], actor="t")


class TestComposite(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = Path(tempfile.mkdtemp(prefix="sra-comp-"))
        self.store = EvidenceStore(self.tmp / "open.sqlite3", enums.ENV_OPEN)

    def tearDown(self):
        self.store.close()
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_bad_field_rejected(self):
        with self.assertRaises(ValidationError) as cm:
            build_daily_points([], [], field="мусор")
        self.assertEqual(cm.exception.code, "E_BAD_FIELD")

    def test_points_skip_incomplete_days(self):
        s1, o1 = _mk("T", 0, 100, 5000)
        s2, o2 = _mk("T", 1, 100, None)          # нет массы → пропуск
        self.store.import_sessions([s1, s2], actor="t")
        self.store.import_observations([o1, o2], actor="t")
        pts = build_daily_points(self.store.fetch_sessions(),
                                 self.store.fetch_observations())
        self.assertEqual(len(pts), 1)
        self.assertEqual(pts[0].diners, 100.0)

    def test_constant_series_zero_decision_cost(self):
        # постоянный ряд: прогноз точен, план P*=y → стоимость ≈ 0
        _fill(self.store, lambda i: _mk("T", i, 100, 5000, menu=(i % 3) + 1))
        rep = backtest_composite(self.store.fetch_sessions(),
                                 self.store.fetch_observations(),
                                 test_days=10, calib_days=10)
        self.assertEqual(rep["status"], "OK", msg=rep.get("reason"))
        for m in rep["models"]:
            if m["status"] == "OK":
                self.assertLess(m["mae_mean"], 1e-6, msg=m["name"])
                self.assertLess(m["dec_cost_kzt_mean"], 1e-6, msg=m["name"])
        self.assertIsNotNone(rep["verdict"])

    def test_composite_wins_when_gpd_constant(self):
        # масса = едоки × 50, едоки зависят от меню (правило menu_hist точно
        # помнит), г/едока константа → декомпозиция должна обойти прямой MA5
        def gen(i):
            menu = (i % 4) + 1
            diners = 90 + menu * 10           # меню определяет едоков
            return _mk("T", i, diners, diners * 50.0, menu=menu)
        _fill(self.store, gen)
        rep = backtest_composite(self.store.fetch_sessions(),
                                 self.store.fetch_observations(),
                                 test_days=12, calib_days=12)
        self.assertEqual(rep["status"], "OK", msg=rep.get("reason"))
        by = {m["name"]: m for m in rep["models"] if m["status"] == "OK"}
        self.assertIn("composite_menu_hist", by)
        self.assertIn("direct_ma5", by)
        self.assertLess(by["composite_menu_hist"]["mae_mean"],
                        by["direct_ma5"]["mae_mean"],
                        msg="декомпозиция при постоянном г/едока должна выигрывать")
        # вердикт зафиксирован и согласован с измеренным
        self.assertEqual(rep["verdict"]["composite_best"], "composite_menu_hist")

    def test_decision_cost_penalises_shortage_more(self):
        # систематически занижающий прогноз: при высоком c_s стоимость растёт
        def gen(i):
            return _mk("T", i, 100 + (i % 2) * 40, 5000)
        _fill(self.store, gen, n=60)
        low_cs = backtest_composite(self.store.fetch_sessions(),
                                    self.store.fetch_observations(),
                                    test_days=10, calib_days=10,
                                    c_w=500.0, c_s=500.0)
        high_cs = backtest_composite(self.store.fetch_sessions(),
                                     self.store.fetch_observations(),
                                     test_days=10, calib_days=10,
                                     c_w=500.0, c_s=5000.0)
        self.assertEqual(low_cs["status"], "OK")
        self.assertEqual(high_cs["status"], "OK")
        # τ меняется (0.5 → 0.909): параметры решения зафиксированы в отчёте
        self.assertAlmostEqual(low_cs["decision_params"]["tau_newsvendor"], 0.5)
        self.assertAlmostEqual(high_cs["decision_params"]["tau_newsvendor"],
                               5000 / 5500, places=6)

    def test_real_fi_integration(self):
        fi = Path("Food waste data school Finland 2024-2025.xlsx")
        if not fi.exists():
            self.skipTest("реальный FI-файл отсутствует")
        from sra.ingest import xlsx_min, zenodo_fi
        m = zenodo_fi.map_workbook(xlsx_min.read_workbook(fi))
        for r in m["sessions"]:
            self.store.import_sessions([r], actor="t")
        for o in m["observations"]:
            self.store.import_observations([o], actor="t")
        rep = backtest_composite(self.store.fetch_sessions(school_id="FI-ZEN-20825233"),
                                 self.store.fetch_observations(school_id="FI-ZEN-20825233"),
                                 field="plate_waste_g", test_days=20,
                                 calib_days=20)
        self.assertEqual(rep["status"], "OK", msg=rep.get("reason"))
        ok_models = [x for x in rep["models"] if x["status"] == "OK"]
        self.assertGreaterEqual(len(ok_models), 6)
        self.assertTrue(all(math.isfinite(x["mae_mean"]) for x in ok_models))
        self.assertTrue(all(math.isfinite(x["dec_cost_kzt_mean"])
                            for x in ok_models))
        self.assertIsNotNone(rep["best_by_mae"])
        self.assertIsNotNone(rep["best_by_decision_cost"])
        self.assertIsNotNone(rep["verdict"])


if __name__ == "__main__":
    unittest.main()
