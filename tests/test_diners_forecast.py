"""Тесты прогноза едоков (diners): причинность признаков, ridge, backtest.

Модуль обязан работать и без scikit-learn (кандидат sklearn_gbdt тогда
UNAVAILABLE с причиной — это проверяется), и с ним (статус OK).
"""

import json
import math
import shutil
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from sra import enums
from sra.contracts import SessionRecord
from sra.forecasting.diners import (DinersPoint, _RidgeModel, _causal_features,
                                    backtest_diners, build_diners_series,
                                    default_menu_fn)


def _mk_session(i: int, diners: float, *, menu: int | None = None) -> SessionRecord:
    d = date(2024, 9, 2) + timedelta(days=i)
    observed = datetime(d.year, d.month, d.day, 12, 0, tzinfo=timezone.utc)
    return SessionRecord(
        session_id=f"SES-T-{d.isoformat()}-LUNCH", school_id="T",
        service_date=d, timezone="UTC", meal_type="LUNCH",
        attendance_count=diners, meals_requested=None, meals_served=diners,
        demand_censored=enums.CENSOR_UNKNOWN, evidence_status=enums.REAL_OBSERVED,
        study_phase=None, source_ref="t", observed_at=observed,
        recorded_at=observed,
        notes=(f"MENU={menu}" if menu is not None else ""))


class TestFeatures(unittest.TestCase):
    def test_menu_extractor(self):
        s = _mk_session(0, 100, menu=7)
        self.assertEqual(default_menu_fn(s), "7")
        s2 = _mk_session(1, 100)
        self.assertIsNone(default_menu_fn(s2))

    def test_features_are_causal(self):
        pts = [DinersPoint("s1", date(2024, 1, 1), 0, "A", 10.0),
               DinersPoint("s2", date(2024, 1, 2), 1, "A", 20.0),
               DinersPoint("s3", date(2024, 1, 3), 2, "A", 30.0)]
        f = _causal_features(pts)
        self.assertIsNone(f[0]["f_menu"])           # истории ещё нет
        self.assertEqual(f[1]["f_menu"], 10.0)      # только прошлое
        self.assertEqual(f[2]["f_menu"], 15.0)      # (10+20)/2, текущая НЕ включена
        self.assertEqual(f[1]["f_lag"], 10.0)
        self.assertEqual(f[2]["f_ma5"], 15.0)

    def test_build_series_skips_empty_and_sorts(self):
        sessions = [_mk_session(2, 100), _mk_session(0, 50),
                    _mk_session(1, 0)]              # ноль едоков → пропуск
        pts = build_diners_series(sessions)
        self.assertEqual([p.y for p in pts], [50.0, 100.0])


class TestRidgeModel(unittest.TestCase):
    def test_recovers_linear_relation(self):
        rows = []
        for i in range(60):
            f_menu = 40.0 + (i % 7) * 3.0
            f_wd = 100.0 + (i % 5) * 5.0
            rows.append({"f_menu": f_menu, "f_wd": f_wd, "f_lag": 100.0,
                         "f_ma5": 100.0, "trend": float(i),
                         "y": 2.0 * f_menu + 0.5 * f_wd + 10.0})
        m = _RidgeModel(lam=0.1).fit(rows)
        for r in rows:
            self.assertAlmostEqual(m.predict(r), r["y"], delta=0.2)

    def test_params_reported(self):
        m = _RidgeModel(lam=3.0).fit(
            [{"f_menu": 1.0, "f_wd": 1.0, "f_lag": 1.0, "f_ma5": 1.0,
              "trend": 0.0, "y": 1.0} for _ in range(15)])
        self.assertIn("lam", m.params())


class TestBacktest(unittest.TestCase):
    def _synthetic_sessions(self, n_days=90):
        out = []
        for i in range(n_days):
            d = date(2024, 9, 2) + timedelta(days=i)
            menu = (i % 5) + 1
            wd = d.weekday()
            y = 100 + (8 if wd == 4 else 0) + menu * 3 + (i % 3)
            out.append(_mk_session(i, y, menu=menu))
        return out

    def test_backtest_ok_and_honest_fields(self):
        rep = backtest_diners(self._synthetic_sessions(), test_days=15,
                              calib_days=10)
        self.assertEqual(rep["status"], "OK", msg=rep.get("reason"))
        by_name = {m["name"]: m for m in rep["models"]}
        for base in ("moving_average5", "weekday3", "menu_history", "ridge",
                     "ridge_notrend"):
            self.assertEqual(by_name[base]["status"], "OK", msg=base)
            self.assertGreaterEqual(by_name[base]["mae_mean"], 0.0)
            self.assertTrue(math.isfinite(by_name[base]["mae_mean"]))
            self.assertGreaterEqual(by_name[base]["n_windows"], 1)
            # три стратегии интервалов с покрытием в [0,1]
            for strat in ("static_q80", "rolling_q80", "aci"):
                cov = by_name[base]["intervals"][strat]["coverage"]
                self.assertGreaterEqual(cov, 0.0, msg=(base, strat))
                self.assertLessEqual(cov, 1.0, msg=(base, strat))
        gb = by_name["sklearn_gbdt"]
        self.assertIn(gb["status"], ("OK", "UNAVAILABLE"))
        if gb["status"] == "UNAVAILABLE":
            self.assertTrue(gb["reason"])   # причина, не молчание
        else:
            self.assertTrue(math.isfinite(gb["mae_mean"]))
        self.assertIn(rep["best_by_mae"],
                      {"moving_average5", "weekday3", "menu_history",
                       "ridge", "ridge_notrend", "sklearn_gbdt"})
        # детерминизм (включая GBDT с фиксированным random_state)
        rep2 = backtest_diners(self._synthetic_sessions(), test_days=15,
                               calib_days=10)
        self.assertEqual(json.dumps(rep, sort_keys=True),
                         json.dumps(rep2, sort_keys=True))

    def test_data_not_ready_small_series(self):
        rep = backtest_diners(self._synthetic_sessions(20))
        self.assertEqual(rep["status"], "DATA_NOT_READY")
        self.assertIn("точек", rep["reason"])



class TestRealFI(unittest.TestCase):
    def test_integration_on_real_finland_data(self):
        fi = Path("Food waste data school Finland 2024-2025.xlsx")
        if not fi.exists():
            self.skipTest("реальный FI-файл отсутствует в рабочей копии")
        from sra.ingest import xlsx_min, zenodo_fi
        tmp = Path(tempfile.mkdtemp(prefix="sra-diners-"))
        store = None
        try:
            m = zenodo_fi.map_workbook(xlsx_min.read_workbook(fi),
                                       recorded_at=datetime(2026, 10, 22,
                                                            tzinfo=timezone.utc))
            store = __import__("sra.store", fromlist=["EvidenceStore"]).EvidenceStore(
                tmp / "open.sqlite3", enums.ENV_OPEN)
            store.import_sessions(m["sessions"], actor="t")
            sessions = store.fetch_sessions(school_id="FI-ZEN-20825233")
            rep = backtest_diners(sessions, test_days=30, calib_days=20)
            self.assertEqual(rep["status"], "OK", msg=rep.get("reason"))
            by_name = {x["name"]: x for x in rep["models"]}
            self.assertEqual(by_name["ridge"]["status"], "OK")
            self.assertEqual(by_name["moving_average5"]["status"], "OK")
            # честный rolling-origin результат (см. ROADMAP): на FI лучшие —
            # простые правила; ML-кандидаты не обязаны выигрывать, их MAE конечны
            self.assertTrue(all(math.isfinite(x["mae_mean"])
                                for x in rep["models"] if x["status"] == "OK"))
            self.assertIsNotNone(rep["best_by_mae"])
        finally:
            if store is not None:
                store.close()
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
