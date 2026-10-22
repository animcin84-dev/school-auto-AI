"""Тесты дайджеста директора (sra/digest.py, AUDIT §D.16).

Проверяются: read-only (ничего не пишет в хранилище), честность секций
(нет данных — причина, не число), агрегаты окна (числители/знаменатели),
подключение норм/алертов/ml-статуса и русский текст.
"""
from __future__ import annotations

import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from sra import enums
from sra.contracts import ObservationRecord, SessionRecord
from sra.digest import build_digest
from sra.explain import explain_digest
from sra.store import EvidenceStore

TZ = timezone.utc


def _session(i, served, *, evidence=enums.SYNTHETIC):
    d = date(2026, 3, 1) + timedelta(days=i)
    dt = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc) + timedelta(days=i)
    return SessionRecord(
        session_id=f"SES-{i}", school_id="SCH-T", service_date=d,
        timezone="Asia/Almaty", meal_type="LUNCH", attendance_count=served,
        meals_requested=None, meals_served=served,
        demand_censored=enums.CENSOR_FALSE, evidence_status=evidence,
        study_phase=None, source_ref="t:digest", observed_at=dt, recorded_at=dt)


def _obs(session_id, dish, served_mass, plate_waste):
    return ObservationRecord(
        observation_id=f"OBS-{session_id}-{dish}", session_id=session_id,
        dish_id=dish, prepared_mass_g=None, served_mass_g=served_mass,
        production_waste_g=None, serving_waste_g=None, plate_waste_g=plate_waste,
        avoidable_waste_g=None, avoidability_status=enums.AV_UNCLASSIFIED,
        evidence_status=enums.SYNTHETIC, source_ref="t:digest",
        observed_at=datetime(2026, 3, 2, 13, 0, tzinfo=timezone.utc),
        recorded_at=datetime(2026, 3, 2, 14, 0, tzinfo=timezone.utc))


class TestDigest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.store = EvidenceStore(self.tmp / "db.sqlite3", enums.ENV_DEMO)

    def tearDown(self):
        self.store.close()

    def test_empty_store_honest(self):
        d = build_digest(self.store, "SCH-NONE")
        self.assertEqual(d["window_metric"]["status"], "UNAVAILABLE")
        self.assertIn("нет сессий", d["window_metric"]["reason"])
        text = explain_digest(d)
        self.assertIn("данных нет", text)
        self.assertIn("недоступна", text)

    def test_window_metric_aggregates(self):
        self.store.import_sessions(
            [_session(i, 100 + i) for i in range(10)], actor="t")
        self.store.import_observations(
            [_obs(f"SES-{i}", "SALAD", 8000.0, 1000.0 + 10 * i)
             for i in range(10)], actor="t")
        d = build_digest(self.store, "SCH-T", days=7)
        wm = d["window_metric"]
        self.assertEqual(wm["status"], "OK")
        # окно: последние 7 сессий SES-3..9; Σ pw = Σ(1000+10i, i=3..9)=7420
        self.assertAlmostEqual(wm["plate_waste_g_total"], 7420.0, places=1)
        self.assertEqual(wm["diners_total"], sum(100 + i for i in range(3, 10)))
        self.assertAlmostEqual(wm["plate_waste_per_diner_g"],
                               7420.0 / sum(100 + i for i in range(3, 10)),
                               places=1)
        # нормы: 8000 г / ~100 порций = 80 г салата — в норме
        self.assertEqual(d["norms"]["status"], "OK")
        text = explain_digest(d)
        self.assertIn("г на едока", text)
        self.assertIn("в пределах порога", text)
        self.assertIn("не исполняет", text)

    def test_read_only_no_writes(self):
        self.store.import_sessions([_session(i, 100) for i in range(3)],
                                   actor="t")
        self.store.import_observations(
            [_obs(f"SES-{i}", "SALAD", 8000.0, 900.0) for i in range(3)],
            actor="t")
        rev_before = self.store.get_dataset_version()
        cards_before = self.store.get_model_cards()
        d = build_digest(self.store, "SCH-T")
        explain_digest(d)
        self.assertTrue(d["read_only"])
        self.assertEqual(self.store.get_dataset_version(), rev_before)
        self.assertEqual(self.store.get_model_cards(), cards_before)

    def test_norms_warn_surfaces_in_text(self):
        self.store.import_sessions([_session(0, 10)], actor="t")
        # 10 порций × 180 г гарнира = 1800 г — норма 100–150 → WARN
        self.store.import_observations(
            [_obs("SES-0", "GARNISH", 1800.0, 200.0)], actor="t")
        d = build_digest(self.store, "SCH-T")
        self.assertEqual(d["norms"]["status"], "WARN")
        text = explain_digest(d)
        self.assertIn("вне норм", text)
        self.assertIn("г", text)

    def test_alerts_high_threshold(self):
        # все порции нетронуты → индекс несъедаемости 100% → HIGH
        self.store.import_sessions([_session(0, 10)], actor="t")
        self.store.import_observations(
            [_obs("SES-0", "MAIN", 3000.0, 3000.0)], actor="t")
        d = build_digest(self.store, "SCH-T")
        codes = [a["code"] for a in d["alerts"]]
        self.assertIn("UNEATEN_INDEX_HIGH", codes)
        self.assertIn("АЛЕРТ", explain_digest(d))


if __name__ == "__main__":
    unittest.main()
