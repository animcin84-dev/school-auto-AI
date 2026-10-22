"""Тесты lifelike-генератора (учебный год, калибровка по реальному FI).

Проверяются: честный отказ без файла калибровки; параметры калибровки из
реального FI (при наличии файла в корне); детерминизм; совместимость CSV с
контрактом; соответствие календарю КЗ (Пн–Пт, без каникул/праздников);
SYNTHETIC-маркировка и menu_id во всех сессиях.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from sra.csvio import parse_file
from sra.errors import ValidationError
from sra.kz_calendar import is_school_day
from sra.demo.lifelike import calibrate_from_fi, generate_lifelike

FI_XLSX = Path("Food waste data school Finland 2024-2025.xlsx")
HAS_FI = FI_XLSX.exists()


@unittest.skipUnless(HAS_FI, "FI-датасет недоступен в корне репозитория")
class TestCalibrateFromFi(unittest.TestCase):
    def test_measured_parameters(self):
        cal = calibrate_from_fi(FI_XLSX)
        self.assertEqual(cal["n_days"], 155)
        self.assertEqual(cal["n_menus"], 25)
        self.assertEqual(cal["source_class"], "REAL_OBSERVED_DERIVED")
        self.assertTrue(0 < cal["diners_residual_cv"] < 1)
        self.assertTrue(0 <= cal["diners_ar1_rho"] <= 0.7)
        self.assertGreater(cal["menu_effect_share"], 0)
        # фактор понедельника ниже среднего (известный провал)
        self.assertLess(cal["diners_weekday_factor"]["0"],
                        cal["diners_weekday_factor"]["3"])


@unittest.skipUnless(HAS_FI, "FI-датасет недоступен в корне репозитория")
class TestGenerateLifelike(unittest.TestCase):
    def test_refuses_without_calibration_file(self):
        with self.assertRaises(ValidationError) as cm:
            generate_lifelike(tempfile.mkdtemp(),
                              fi_xlsx="нет-такого-файла.xlsx")
        self.assertEqual(cm.exception.code, "E_CALIBRATION_FILE_REQUIRED")

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.manifest = generate_lifelike(self.tmp / "life",
                                          fi_xlsx=FI_XLSX, seed=2026)

    def test_year_structure_and_contract(self):
        sessions = parse_file(self.tmp / "life" / "sessions.csv", "sessions")
        observations = parse_file(self.tmp / "life" / "observations.csv",
                                  "observations")
        self.assertGreater(len(sessions), 120)          # полный год
        cal = __import__("sra.kz_calendar", fromlist=["load_calendar"]).load_calendar()
        for s in sessions:
            self.assertTrue(is_school_day(s.service_date, cal)[0],
                            f"{s.service_date} не учебный день")
            self.assertEqual(s.evidence_status, "SYNTHETIC")
            self.assertIsNotNone(s.menu_id)
        self.assertGreater(len(observations), 400)

    def test_determinism(self):
        other = Path(tempfile.mkdtemp())
        generate_lifelike(other / "life", fi_xlsx=FI_XLSX, seed=2026)
        self.assertEqual((self.tmp / "life" / "sessions.csv").read_bytes(),
                         (other / "life" / "sessions.csv").read_bytes())

    def test_manifest_honesty(self):
        self.assertEqual(self.manifest["data_nature"], "SYNTHETIC")
        self.assertEqual(self.manifest["calibration_from_fi"]["source_ref"],
                         "zenodo:20825233:v1")
        self.assertTrue(any("SYNTHETIC" in w for w in self.manifest["warnings"]))
        self.assertTrue(any(a["class"] == "ASSUMPTION"
                            for a in self.manifest["assumptions"]))

    def test_menu_signal_present(self):
        # между-меню разброс множителей зафиксирован на seed и ненулевой
        cal = self.manifest["calibration_from_fi"]
        self.assertGreater(cal["menu_effect_share"], 0.0)
        self.assertEqual(self.manifest["menu_cycle_days"], 25)


if __name__ == "__main__":
    unittest.main()
