"""Тесты синтетического генератора: детерминизм, аномалии, согласованность."""

import csv
import shutil
import tempfile
import unittest
from pathlib import Path

from sra import enums
from sra.csvio import OBSERVATION_HEADER, SESSION_HEADER
from sra.demo.synthetic import generate

REPO_TEMPLATES = Path(__file__).resolve().parent.parent / "templates"


def read_csv(path: Path):
    with path.open(encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


class TestSyntheticGenerator(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="sra-synth-"))
        cls.manifest = generate(cls.tmp / "a", seed=42, n_days=60)
        generate(cls.tmp / "b", seed=42, n_days=60)
        cls.sessions = read_csv(cls.tmp / "a" / "sessions.csv")
        cls.observations = read_csv(cls.tmp / "a" / "observations.csv")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_determinism_same_seed(self):
        for name in ("sessions.csv", "observations.csv", "manifest.json"):
            self.assertEqual((self.tmp / "a" / name).read_bytes(),
                             (self.tmp / "b" / name).read_bytes(),
                             msg=f"файл {name} не совпал между запусками с одним seed")

    def test_headers_match_repo_templates(self):
        tpl_sessions = (REPO_TEMPLATES / "sessions.csv").read_text(encoding="utf-8").strip()
        tpl_obs = (REPO_TEMPLATES / "observations.csv").read_text(encoding="utf-8").strip()
        self.assertEqual(",".join(SESSION_HEADER), tpl_sessions)
        self.assertEqual(",".join(OBSERVATION_HEADER), tpl_obs)

    def test_all_records_synthetic(self):
        for row in self.sessions + self.observations:
            self.assertEqual(row["evidence_status"], enums.SYNTHETIC)
            self.assertIn("seed=42", row["source_ref"])

    def test_anomalies_present(self):
        stats = self.manifest["stats"]
        self.assertGreaterEqual(stats["censored_days"], 1, "нет дней с нехваткой еды")
        self.assertGreaterEqual(stats["missing_cells"], 1, "нет пропусков")
        self.assertGreaterEqual(stats["outlier_days"], 1, "нет дней-выбросов")
        self.assertEqual(stats["gap_days"], 2, "нет дней-пропусков")
        # сменa режима внутри ряда
        self.assertLess(self.manifest["mode_change_day"], self.manifest["n_days_requested"])

    def test_mass_consistency_no_served_gt_prepared(self):
        """В согласованные дни served = prepared − production − serving ≥ 0."""
        for row in self.observations:
            prepared = row["prepared_mass_g"]
            served = row["served_mass_g"]
            if prepared and served:
                self.assertLessEqual(float(served), float(prepared),
                                     msg=f"served > prepared в {row['observation_id']}")

    def test_requested_ge_served(self):
        for row in self.sessions:
            req, srv = row["meals_requested"], row["meals_served"]
            if req and srv:
                self.assertGreaterEqual(int(req), int(srv))

    def test_censoring_consistent(self):
        for row in self.sessions:
            req, srv, cens = row["meals_requested"], row["meals_served"], row["demand_censored"]
            if req and srv:
                if int(srv) < int(req):
                    self.assertEqual(cens, "true")
                else:
                    self.assertEqual(cens, "false")

    def test_timezone_and_weekdays(self):
        from datetime import date
        seen = set()
        for row in self.sessions:
            self.assertEqual(row["timezone"], "Asia/Almaty")
            self.assertTrue(row["observed_at"].endswith("+05:00"))
            d = date.fromisoformat(row["service_date"])
            self.assertLess(d.weekday(), 5, "учебные дни: пн–пт")
            seen.add(row["session_id"])
        self.assertEqual(len(seen), len(self.sessions), "session_id уникальны")

    def test_observation_references_and_uniqueness(self):
        session_ids = {r["session_id"] for r in self.sessions}
        pairs = set()
        for row in self.observations:
            self.assertIn(row["session_id"], session_ids)
            pair = (row["session_id"], row["dish_id"])
            self.assertNotIn(pair, pairs)
            pairs.add(pair)

    def test_different_seed_changes_data(self):
        m2 = generate(self.tmp / "c", seed=7, n_days=60)
        a = (self.tmp / "a" / "sessions.csv").read_bytes()
        c = (self.tmp / "c" / "sessions.csv").read_bytes()
        self.assertNotEqual(a, c)
        self.assertEqual(m2["seed"], 7)


if __name__ == "__main__":
    unittest.main()
