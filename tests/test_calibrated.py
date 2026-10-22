"""Тесты откалиброванного генератора v2: детерминизм, калибровка, конвейер.

Ручная контрольная оценка (доли Италии на порции Прил. 7 КЗ, середины):
  обед 6–11 лет ≈ салат 80 + первое 225 + второе ~240 + напиток 200 = ~745 г;
  отходы тарелок на ребёнка ≈ 0.212·80 + 0.218·225 + 0.194·240 + 0.02·200 ≈ 117 г
  (с plate_factor=0.75 на второй половине ряда среднее по ряду ≈ 100–120 г).
  Италия (78 школ): 89.9 г/день/ребёнок с хлебом и фруктом.
"""

import csv
import shutil
import tempfile
import unittest
from datetime import date
from pathlib import Path

from sra import enums
from sra.csvio import parse_file
from sra.demo.calibrated import CALIBRATION, MENU_COMPONENTS, PLATE_WASTE_SHARE, generate
from sra.store import EvidenceStore
from sra.validation import validate_observations, validate_sessions


class TestCalibratedGenerator(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="sra-cal-"))
        cls.manifest = generate(cls.tmp / "a", seed=2026, n_days=90)
        generate(cls.tmp / "b", seed=2026, n_days=90)
        cls.sessions = list(csv.DictReader(open(cls.tmp / "a" / "sessions.csv")))
        cls.obs = list(csv.DictReader(open(cls.tmp / "a" / "observations.csv")))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_menu_id_column_parsed(self):
        ids = {r.menu_id for r in
               (parse_file(self.tmp / "a" / "sessions.csv", "sessions"))}
        self.assertFalse({None} & ids)          # колонка заполнена
        self.assertTrue(all(str(x).isdigit() for x in ids))

    def test_determinism(self):
        for name in ("sessions.csv", "observations.csv", "manifest.json"):
            self.assertEqual((self.tmp / "a" / name).read_bytes(),
                             (self.tmp / "b" / name).read_bytes(), msg=name)

    def test_manifest_calibration_table_complete(self):
        classes = {c["class"] for c in self.manifest["calibration"]}
        self.assertTrue({"NORM_KZ", "EMPIRICAL_STUDY", "ASSUMPTION"} <= classes)
        for c in self.manifest["calibration"]:
            self.assertTrue(c["source"], msg=c["parameter"])
        # нормативные порции Прил. 7 точно записаны в таблице
        norms = {c["parameter"]: c["value"] for c in self.manifest["calibration"]
                 if c["class"] == "NORM_KZ" and c["parameter"].startswith("portion")}
        self.assertEqual(norms["portion_first_g"], [200, 250])
        self.assertEqual(norms["portion_salad_g"], [60, 100])
        self.assertEqual(norms["portion_drink_g"], [200, 200])

    def test_portions_within_kz_norm_ranges(self):
        """Средние компоненты порций внутри диапазонов Прил. 7 КЗ (6–11 лет)."""
        ranges = {comp: lo_hi for comp, _t, lo_hi in MENU_COMPONENTS}
        meals = {r["session_id"]: float(r["meals_served"]) for r in self.sessions}
        sums = {comp: 0.0 for comp in ranges}
        counts = {comp: 0 for comp in ranges}
        for o in self.obs:
            comp = o["dish_id"].split("-")[0]
            if o["served_mass_g"] and o["served_mass_g"] != "":
                sums[comp] += float(o["served_mass_g"]) / meals[o["session_id"]]
                counts[comp] += 1
        for comp in ranges:
            if comp == "MAIN":
                continue  # MAIN = гарнир+мясо: агрегат двух диапазонов
            mean = sums[comp] / counts[comp]
            lo, hi = ranges[comp]
            self.assertGreaterEqual(mean, lo, msg=comp)
            self.assertLessEqual(mean, hi, msg=comp)

    def test_plate_waste_share_matches_italy_study(self):
        """Доли отходов тарелок близки к эмпирическим (Италия), а не произвольны."""
        # FIRST: фактическая доля ≈ 44.3/203.6 с шумом ±20% и plate_factor
        target = PLATE_WASTE_SHARE["FIRST"]
        self.assertAlmostEqual(target, 44.3 / 203.6, places=4)
        self.assertAlmostEqual(PLATE_WASTE_SHARE["SALAD"], 20.2 / 95.2, places=4)

    def test_per_child_plate_waste_sane(self):
        """~97 г/ребёнок/день против итальянских 89.9 г (наш обед без хлеба/фрукта);
        грубая полоса правдоподобия, а не точное утверждение."""
        mean = self.manifest["plate_waste_per_child_mean_g"]
        self.assertGreater(mean, 50)
        self.assertLess(mean, 200)

    def test_anomalies_and_consistency(self):
        stats = self.manifest["stats"]
        self.assertGreater(stats["censored_days"], 0)
        self.assertGreater(stats["gap_days"], 0)
        self.assertGreater(stats["missing_cells"], 0)
        served_by_sid = {r["session_id"]: r for r in self.sessions}
        for o in self.obs:
            if o["prepared_mass_g"] and o["served_mass_g"]:
                self.assertLessEqual(float(o["served_mass_g"]),
                                     float(o["prepared_mass_g"]),
                                     msg=o["observation_id"])
            self.assertIn(o["session_id"], served_by_sid)
        for r in self.sessions:
            self.assertEqual(r["evidence_status"], enums.SYNTHETIC)
            self.assertTrue(r["source_ref"].startswith("synthetic-calibrated:v2"))

    def test_passes_validation_and_imports(self):
        recs = parse_file(self.tmp / "a" / "sessions.csv", "sessions")
        rep = validate_sessions(recs, existing_ids=set())
        self.assertTrue(rep.ok)
        obs_recs = parse_file(self.tmp / "a" / "observations.csv", "observations")
        rep2 = validate_observations(obs_recs, known_sessions=set(rec.session_id for rec in rep.accepted),
                                     existing_observation_ids=set(), existing_pairs=set())
        self.assertTrue(rep2.ok, msg=[i.to_dict() for i in rep2.errors][:3])
        store = EvidenceStore(self.tmp / "db.sqlite3", enums.ENV_DEMO)
        try:
            store.import_sessions(rep.accepted, actor="t")
            store.import_observations(rep2.accepted, actor="t")
            self.assertEqual(len(store.session_ids()), len(self.sessions))
        finally:
            store.close()


if __name__ == "__main__":
    unittest.main()
