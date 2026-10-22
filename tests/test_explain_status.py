"""Тесты explain_* для новых отчётов и read-only ml_status (AUDIT §E, P0-4).

Проверяются: честные формулировки (DATA_NOT_READY с причиной, а не число),
человекочитаемые тексты diners/composite/quality/alerts/promotion и то, что
ml_status НИЧЕГО не пишет (read-only) и честно считает достаточность данных.
"""
from __future__ import annotations

import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

from sra import enums
from sra.contracts import SessionRecord
from sra.explain import (explain_alerts, explain_composite_backtest,
                         explain_diners_backtest, explain_ml_status,
                         explain_promotion, explain_quality)
from sra.status import ml_status
from sra.store import EvidenceStore


def _diner_report(status="OK"):
    if status != "OK":
        return {"status": "DATA_NOT_READY",
                "reason": "нужно >= 70 точек; есть 25"}
    return {"status": "OK",
            "evaluation": {"scheme": "rolling-origin", "n_origins": 3,
                           "test_days": 30},
            "first_test_date": "2025-01-01", "last_test_date": "2025-03-31",
            "best_model": "menu_history",
            "models": [
                {"name": "menu_history", "status": "OK", "mae_mean": 34.34,
                 "mae_spread": 4.9,
                 "intervals": {"aci": {"coverage": 0.77, "mean_width": 55.1}}},
                {"name": "ridge", "status": "UNAVAILABLE", "reason": "нет"}]}


class TestExplainBacktests(unittest.TestCase):
    def test_diners_not_ready_honest(self):
        text = explain_diners_backtest(_diner_report("NR"))
        self.assertIn("DATA_NOT_READY", text)
        self.assertIn("есть 25", text)

    def test_diners_ok_lists_models_and_best(self):
        text = explain_diners_backtest(_diner_report())
        self.assertIn("menu_history", text)
        self.assertIn("34.3", text)
        self.assertIn("Лучшая по MAE: menu_history", text)
        self.assertIn("не гарантия", text)

    def test_composite_verdict_explained(self):
        rep = {"status": "OK", "field": "plate_waste_g", "n_origins": 3,
               "test_days": 20, "first_test_date": "2025-01-01",
               "last_test_date": "2025-03-31",
               "decision_params": {"tau_newsvendor": 0.909, "c_w": 500,
                                   "c_s": 5000},
               "models": [{"name": "direct_ma5", "status": "OK",
                           "mae_mean": 2020.3, "dec_cost_kzt_mean": 2410.5,
                           "coverage_mean": 0.83}],
               "verdict": {"composite_best": "composite_ma5",
                           "direct_best": "direct_ma5",
                           "composite_vs_direct_mae_pct": 0.63,
                           "note": "прямой прогноз лучше"}}
        text = explain_composite_backtest(rep)
        self.assertIn("прямой прогноз лучше", text)
        self.assertIn("decision-focused", text)
        self.assertIn("не переносятся", text)

    def test_composite_not_ready(self):
        text = explain_composite_backtest(
            {"status": "DATA_NOT_READY", "reason": "мало дней"})
        self.assertIn("DATA_NOT_READY", text)
        self.assertIn("мало дней", text)


class TestExplainQualityAlertsPromotion(unittest.TestCase):
    def test_quality_checks_and_crosscheck(self):
        rep = {"status": "OK", "checks": [
            {"name": "completeness", "ok": True,
             "details": {"note": "полнота полей"}},
            {"name": "outliers", "ok": False, "message": "2 выброса"}],
            "source_crosscheck": {"status": "OK", "rows_checked": 155,
                                  "mismatches": [{"row": 1}]}}
        text = explain_quality(rep)
        self.assertIn("completeness: OK", text)
        self.assertIn("outliers: ПРОБЛЕМА", text)
        self.assertIn("расхождений 1", text)

    def test_alerts_ok_and_threshold_note(self):
        text = explain_alerts([{"code": "UNEATEN_INDEX_OK", "severity": "INFO",
                                "status": "OK", "value_pct": 9.3,
                                "threshold_pct": 30}])
        self.assertIn("9.3%", text)
        self.assertIn("бракеражной комиссией", text)
        self.assertIn("Алертов нет", explain_alerts([]))

    def test_promotion_rejected_lists_reasons(self):
        res = {"verdict": "REJECTED", "target": "diners",
               "numbers": {"candidate": "gbdt", "incumbent": "menu_history"},
               "reasons": ["улучшение MAE -8.85% < порога 5.00%"]}
        text = explain_promotion(res)
        self.assertIn("REJECTED", text)
        self.assertIn("улучшение MAE", text)
        self.assertIn("не разрешение", text)

    def test_promotion_promoted_mentions_card(self):
        res = {"verdict": "PROMOTED", "target": "diners",
               "numbers": {"candidate": "gbdt", "incumbent": "menu_history",
                           "improvement_pct": 11.76}}
        text = explain_promotion(res)
        self.assertIn("PROMOTED", text)
        self.assertIn("карточке", text)


class TestMlStatus(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.store = EvidenceStore(self.tmp / "db.sqlite3", enums.ENV_DEMO)

    def tearDown(self):
        self.store.close()

    def _mk_session(self, i, *, menu_id=None, attendance=100):
        d = date(2026, 1, 1 + i // 1) if False else date(2026, 1, 1 + i)
        dt = datetime(2026, 1, 1 + i, 12, 0, tzinfo=timezone.utc)
        return SessionRecord(
            session_id=f"SES-{i}", school_id="SCH-T", service_date=d,
            timezone="Asia/Almaty", meal_type="LUNCH",
            attendance_count=attendance, meals_requested=None,
            meals_served=attendance, demand_censored=enums.CENSOR_FALSE,
            evidence_status=enums.SYNTHETIC, study_phase=None,
            menu_id=menu_id,
            source_ref="test:ml-status", observed_at=dt, recorded_at=dt)

    def test_read_only_and_honest_sufficiency(self):
        # мало данных: ни diners, ни composite не наберут минимум
        self.store.import_sessions([self._mk_session(0, menu_id="1")],
                                   actor="test")
        before_cards = self.store.get_model_cards()
        st = ml_status(self.store, "SCH-T")
        self.assertTrue(st["read_only"])
        self.assertEqual(st["sessions"], 1)
        self.assertEqual(st["sessions_with_menu_id"], 1)
        self.assertIn("DATA_NOT_READY", st["diners_sufficiency"])
        self.assertIn("DATA_NOT_READY", st["composite_sufficiency"])
        self.assertIsNone(st["latest_promotion"])
        self.assertEqual(self.store.get_model_cards(), before_cards)  # не пишет

    def test_warning_when_menu_id_missing(self):
        self.store.import_sessions([self._mk_session(0, menu_id=None)],
                                   actor="test")
        st = ml_status(self.store, "SCH-T")
        self.assertTrue(any("menu_id не заполнен" in w for w in st["warnings"]))
        text = explain_ml_status(st)
        self.assertIn("предупреждение", text)
        self.assertIn("карточек нет", text)

    def test_latest_card_and_promotion_surfaces(self):
        self.store.import_sessions([self._mk_session(i, menu_id=str(i % 20))
                                    for i in range(3)], actor="test")
        self.store.save_model_card(
            model_id="sra.diners.menu_history", model_name="menu_history",
            params={}, data_meta={},
            metrics={"mae_diners_mean": 34.34}, actor="test")
        self.store.save_model_card(
            model_id="sra.promotion.diners", model_name="gbdt",
            params={"gate": {}}, data_meta={"target": "diners"},
            metrics={"improvement_pct": 11.76}, actor="test")
        st = ml_status(self.store, "SCH-T")
        ids = [c["model_id"] for c in st["latest_cards"]]
        self.assertIn("sra.diners.menu_history", ids)
        self.assertNotIn("sra.promotion.diners", ids)  # gate — отдельный блок
        self.assertEqual(st["latest_promotion"]["metrics"]["improvement_pct"],
                         11.76)
        text = explain_ml_status(st)
        self.assertIn("sra.diners.menu_history v1", text)
        self.assertIn("Последнее решение gate", text)


if __name__ == "__main__":
    unittest.main()
