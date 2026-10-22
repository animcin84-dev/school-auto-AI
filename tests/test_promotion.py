"""Тесты promotion_gate (docs/AUDIT.md §E, P0-2).

Проверяются: формальные критерии (достаточность данных, улучшение,
стабильность по окнам, покрытие), честный REJECTED на реальном FI-сценарии
(ML не превосходит базовое правило — gate обязан это отклонить) и запись
модели-карточки только при PROMOTED.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from sra.errors import ValidationError
from sra.promotion import (GatePolicy, VERDICT_PROMOTED, VERDICT_REJECTED,
                           evaluate_promotion, gate_from_report)


def _model(name, mae, per_origin, coverage=None):
    m = {"name": name, "status": "OK", "mae_mean": mae,
         "mae_per_origin": per_origin}
    if coverage is not None:
        m["intervals"] = {"coverage": coverage}
    return m


EVAL_OK = {"n_origins": 3, "test_days": 60}


class TestGateCriteria(unittest.TestCase):
    def test_promoted_when_all_criteria_met(self):
        cand = _model("gbdt", 30.0, [31.0, 29.0, 30.0], coverage=0.78)
        inc = _model("menu_history", 34.0, [34.5, 33.5, 34.0], coverage=0.77)
        res = evaluate_promotion(cand, inc, evaluation=EVAL_OK)
        self.assertEqual(res["verdict"], VERDICT_PROMOTED)
        self.assertEqual(res["reasons"], [])
        # улучшение: (34-30)/34*100 = 11.76%
        self.assertAlmostEqual(res["numbers"]["improvement_pct"], 11.7647, places=3)

    def test_rejected_when_improvement_below_threshold(self):
        cand = _model("gbdt", 33.5, [34.0, 33.0, 33.5])
        inc = _model("menu_history", 34.0, [34.5, 33.5, 34.0])
        res = evaluate_promotion(cand, inc, evaluation=EVAL_OK)
        self.assertEqual(res["verdict"], VERDICT_REJECTED)
        crit = {c["name"]: c for c in res["criteria"]}
        self.assertFalse(crit["improvement"]["passed"])   # 1.47% < 5%
        self.assertTrue(crit["stability"]["passed"])      # окна выиграны, но мало

    def test_rejected_when_unstable_across_origins(self):
        # Кандидат лучше только в 1 окне из 3, но по среднему «выглядит» хорошо:
        # [1, 40, 40] → MAE 27.0 (лучше 34.0 на 20.6%), а выиграно 1/3 окон.
        cand = _model("ridge", 27.0, [1.0, 40.0, 40.0])
        inc = _model("menu_history", 34.0, [34.0, 34.0, 34.0])
        res = evaluate_promotion(cand, inc, evaluation=EVAL_OK,
                                 gate=GatePolicy(min_improvement_pct=20.0))
        self.assertEqual(res["verdict"], VERDICT_REJECTED)
        crit = {c["name"]: c for c in res["criteria"]}
        self.assertFalse(crit["stability"]["passed"])     # 2/3 не достигнуто
        self.assertTrue(any("нестабильно" in r for r in res["reasons"]))

    def test_rejected_when_insufficient_data(self):
        cand = _model("gbdt", 30.0, [30.0, 30.0, 30.0])
        inc = _model("menu_history", 34.0, [34.0, 34.0, 34.0])
        res = evaluate_promotion(cand, inc,
                                 evaluation={"n_origins": 2, "test_days": 15})
        self.assertEqual(res["verdict"], VERDICT_REJECTED)
        crit = {c["name"]: c for c in res["criteria"]}
        self.assertFalse(crit["data_sufficiency"]["passed"])
        self.assertTrue(any("недостаточно данных" in r for r in res["reasons"]))

    def test_rejected_when_coverage_out_of_band(self):
        cand = _model("gbdt", 30.0, [30.0, 30.0, 30.0], coverage=0.98)
        inc = _model("menu_history", 34.0, [34.0, 34.0, 34.0], coverage=0.77)
        res = evaluate_promotion(cand, inc, evaluation=EVAL_OK)
        self.assertEqual(res["verdict"], VERDICT_REJECTED)
        crit = {c["name"]: c for c in res["criteria"]}
        self.assertFalse(crit["coverage"]["passed"])

    def test_missing_origin_lists_rejects_stability(self):
        cand = _model("gbdt", 30.0, [30.0])
        inc = _model("menu_history", 34.0, [34.0, 34.0, 34.0])
        res = evaluate_promotion(cand, inc, evaluation=EVAL_OK,
                                 gate=GatePolicy(min_improvement_pct=1.0))
        self.assertEqual(res["verdict"], VERDICT_REJECTED)
        self.assertTrue(any("пооконные MAE недоступны" in r for r in res["reasons"]))


class TestGateFromReport(unittest.TestCase):
    def _report(self):
        return {
            "status": "OK",
            "evaluation": {"n_origins": 3, "test_days": 90},
            "first_test_date": "2025-01-01", "last_test_date": "2025-03-31",
            "models": [_model("ma5", 40.0, [40.0, 40.0, 40.0]),
                       _model("weekday3", 41.0, [41.0, 41.0, 41.0]),
                       _model("menu_history", 34.0, [34.0, 34.0, 34.0]),
                       _model("gbdt", 31.0, [31.0, 31.0, 31.0], coverage=0.8)]}

    def test_candidate_excludes_baseline_rules(self):
        res = gate_from_report(self._report(), incumbent_name="menu_history",
                               candidate_skip={"ma5", "weekday3", "menu_history"})
        self.assertEqual(res["numbers"]["candidate"], "gbdt")
        self.assertEqual(res["numbers"]["incumbent"], "menu_history")
        self.assertEqual(res["verdict"], VERDICT_PROMOTED)

    def test_rejected_when_no_candidate_model(self):
        rep = self._report()
        rep["models"] = [m for m in rep["models"] if m["name"] != "gbdt"]
        res = gate_from_report(rep, incumbent_name="menu_history",
                               candidate_skip={"ma5", "weekday3", "menu_history"})
        self.assertEqual(res["verdict"], VERDICT_REJECTED)
        self.assertTrue(any("не содержит сопоставимых моделей" in r
                            for r in res["reasons"]))


class TestGateOnRealFiScenario(unittest.TestCase):
    """Честный сценарий: на FI-данных ML не превосходит menu_history —
    gate обязан вернуть REJECTED, карточку не писать."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        from sra.demo.synthetic import generate
        from sra.csvio import parse_file
        from sra.forecasting.diners import backtest_diners
        from sra.store import EvidenceStore
        from sra import enums
        generate(self.tmp, seed=42, n_days=90)
        self.store = EvidenceStore(self.tmp / "db.sqlite3", enums.ENV_DEMO)
        sessions = parse_file(self.tmp / "sessions.csv", "sessions")
        self.store.import_sessions(sessions, actor="test",
                                   import_meta={"source": "synthetic_generator"})
        fetched = [s for s in self.store.fetch_sessions(school_id="SCH-DEMO-001")
                   if s.meal_type == "LUNCH"]
        self.report = backtest_diners(fetched, test_days=30, calib_days=30)

    def tearDown(self):
        self.store.close()

    def test_gate_on_synthetic_90d(self):
        res = gate_from_report(self.report, incumbent_name="menu_history",
                               candidate_skip={"menu_history", "ma5", "weekday3"})
        self.assertIn(res["verdict"], (VERDICT_PROMOTED, VERDICT_REJECTED))
        # при любом вердикте критерии заполнены и числа присутствуют
        self.assertTrue(res["criteria"] or res["reasons"])
        self.assertIn("improvement_pct", res.get("numbers", {}))

    def test_fi_real_gate_rejects_without_card(self):
        # FI-числа из ранее измеренного: menu_history 34.34 лучшая → кандидаты
        # (gbdt/ridge) не дают +5% → REJECTED, карточка не пишется.
        rep = {"status": "OK",
               "evaluation": {"n_origins": 3, "test_days": 90},
               "models": [_model("menu_history", 34.343, [33.9, 34.8, 34.3]),
                          _model("ma5", 34.661, [33.5, 35.9, 34.6]),
                          _model("gbdt", 37.38, [36.0, 39.0, 37.1])]}
        res = gate_from_report(rep, incumbent_name="menu_history",
                               candidate_skip={"menu_history", "ma5"})
        self.assertEqual(res["verdict"], VERDICT_REJECTED)
        self.assertEqual(len(self.store.get_model_cards("sra.promotion.diners")), 0)


class TestGateCardWritten(unittest.TestCase):
    def test_promoted_writes_card(self):
        from sra.store import EvidenceStore
        from sra import enums
        tmp = Path(tempfile.mkdtemp())
        store = EvidenceStore(tmp / "db.sqlite3", enums.ENV_DEMO)
        try:
            cand = _model("gbdt", 30.0, [30.0, 30.0, 30.0])
            inc = _model("menu_history", 34.0, [34.0, 34.0, 34.0])
            res = evaluate_promotion(cand, inc, evaluation=EVAL_OK)
            self.assertEqual(res["verdict"], VERDICT_PROMOTED)
            store.save_model_card(
                model_id="sra.promotion.diners", model_name=cand["name"],
                params={"gate": res["gate"], "criteria": res["criteria"]},
                data_meta={"target": "diners"}, metrics=res["numbers"],
                actor="test")
            cards = store.get_model_cards("sra.promotion.diners")
            self.assertEqual(len(cards), 1)
            self.assertEqual(cards[0]["model_name"], "gbdt")
            self.assertEqual(cards[0]["version"], 1)
        finally:
            store.close()

    def test_gate_policy_frozen_in_dict(self):
        g = GatePolicy(min_improvement_pct=7.5)
        d = g.to_dict()
        self.assertEqual(d["min_improvement_pct"], 7.5)
        self.assertEqual(d["coverage_min"], 0.60)
        self.assertEqual(d["coverage_max"], 0.95)


if __name__ == "__main__":
    unittest.main()
