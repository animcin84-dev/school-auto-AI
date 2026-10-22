"""Тесты этапа 3: целевой ряд, базовые модели, backtest, утечки, дрейф, cold start.

Ручной контрольный ряд (все числа посчитаны вручную):
  3 учебные недели (пн–пт), served по дням недели: пн 100, вт 120, ср 110, чт 130, пт 90.
  Недели: 2026-10-05..09, 2026-10-12..16 (калибровка), 2026-10-19..23 (тест).
  min_train=5, calib_days=5, test_days=5.

  Наивный: предсказание = предыдущий учебный день.
    Ошибки точки на тесте: пн |100−90|=10, вт |120−100|=20, ср |110−120|=10,
    чт |130−110|=20, пт |90−130|=40 → MAE = 100/5 = 20.
    Остатки калибровки: [10,20,−10,20,−40], сортированные [−40,−10,10,20,20];
    q10 = −40 + 0.4·30 = −28; q50 = 10; q90 = 20.
    Квантильные прогнозы КАЛИБРОВАНЫ: pred_qX = pred + qX(остатки)
    (pred_q50 смещён на медиану остатков — коррекция систематического смещения).
    pinball(0.5) по pred_q50=[100,110,130,120,140] против y=[100,120,110,130,90]:
    e=[0,10,−20,10,−50] → 0.5·|e| = [0,5,10,5,25] → mean 9.
    pinball(0.1) по pred_q10=[62,72,92,82,102]:
    e=[38,48,18,48,−12] → [3.8,4.8,1.8,4.8,10.8] → mean 5.2.
    pinball(0.9) по pred_q90=[110,120,140,130,150]:
    e=[−10,0,−30,0,−60] → [1,0,3,0,6] → mean 2.
    Интервал [pred−28, pred+20], ширина 48.
    Покрытие: пн [62,110]✓, вт [72,120]✓(граница), ср [92,140]✓, чт [82,130]✓(граница),
    пт [102,150] против 90 ✗ → 4/5 = 0.8.
  Сезонный наивный (weeks=3): медиана того же дня недели → совпадает с фактом
    → MAE = 0, остатки 0, ширина 0, покрытие 1.0.
  Политика кухни (hindsight): prepared = y + 10 → |e| = 10, переготовка 10 г/день.
"""

import shutil
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from sra import enums
from sra.contracts import parse_observation_row, parse_session_row
from sra.demo.synthetic import generate
from sra.forecasting.baselines import (KitchenPolicyModel, MovingAverageModel, NaiveModel,
                                       SeasonalNaiveModel)
from sra.forecasting.backtest import decision_moment_iso, run_backtest
from sra.forecasting.drift import detect_drift
from sra.forecasting.score import empirical_quantile, pinball_loss
from sra.forecasting.series import (TargetPoint, TargetSeries, build_target_series,
                                    eligible_points, make_snapshot_fn)
from sra.store import EvidenceStore
from tests.helpers import make_observation, make_session

WEEKDAY_BASE = {0: 100.0, 1: 120.0, 2: 110.0, 3: 130.0, 4: 90.0}
TZ = "Asia/Almaty"


def hand_series() -> list[TargetPoint]:
    """15 учебных дней с 2026-10-05 (пн), served по WEEKDAY_BASE, prepared = y+10."""
    pts = []
    d = date(2026, 10, 5)
    while len(pts) < 15:
        if d.weekday() < 5:
            y = WEEKDAY_BASE[d.weekday()]
            pts.append(TargetPoint(
                session_id=f"S-{d.isoformat()}", service_date=d,
                served_mass_g=y, prepared_mass_g=y + 10,
                n_obs=1, n_served_obs=1, censored=False, censored_unknown=False,
                meals_served=None))
        d += timedelta(days=1)
    return pts


def snap_by_date(points):
    """Снапшот-функция: включает точки с датой ≤ даты cutoff (имитация as-of по датам)."""
    def fn(cutoff_iso: str) -> TargetSeries:
        cut = date.fromisoformat(cutoff_iso[:10])
        return TargetSeries(points=[p for p in points if p.service_date <= cut],
                            school_id="SCH-T", meal_type="LUNCH")
    return fn


class TestScoreFunctions(unittest.TestCase):
    def test_empirical_quantile_hand(self):
        self.assertEqual(empirical_quantile([], 0.5), None)
        self.assertEqual(empirical_quantile([5.0], 0.1), 5.0)
        self.assertAlmostEqual(empirical_quantile([1, 2, 3, 4, 5], 0.1), 1.4)
        self.assertAlmostEqual(empirical_quantile([1, 2, 3, 4, 5], 0.5), 3.0)
        self.assertAlmostEqual(empirical_quantile([-40, -10, 10, 20, 20], 0.1), -28.0)
        self.assertAlmostEqual(empirical_quantile([-40, -10, 10, 20, 20], 0.9), 20.0)

    def test_pinball_hand(self):
        self.assertAlmostEqual(pinball_loss(100, 90, 0.9), 9.0)   # занижение при q=0.9 дорого
        self.assertAlmostEqual(pinball_loss(100, 110, 0.9), 1.0)  # завышение дёшево
        self.assertAlmostEqual(pinball_loss(100, 110, 0.5), 5.0)


class TestHandComputedBacktest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.points = hand_series()
        cls.report = run_backtest(
            snapshot_fn=snap_by_date(cls.points),
            test_days=5, calib_days=5, min_train=5, tz_name=TZ,
            environment=enums.ENV_REAL)

    def test_status_and_windows(self):
        self.assertEqual(self.report["status"], "OK")
        self.assertEqual(self.report["artifact_nature"], enums.PREDICTED)
        self.assertEqual(self.report["windows"]["train_points"], 5)
        self.assertEqual(self.report["windows"]["calib_range"], ["2026-10-12", "2026-10-16"])
        self.assertEqual(self.report["windows"]["test_range"], ["2026-10-19", "2026-10-23"])

    def test_naive_hand_values(self):
        m = next(x for x in self.report["models"] if x["name"] == "naive")
        self.assertAlmostEqual(m["mae_g"], 20.0, places=6)
        self.assertAlmostEqual(m["pinball"]["0.5"], 9.0, places=6)
        self.assertAlmostEqual(m["pinball"]["0.1"], 5.2, places=6)
        self.assertAlmostEqual(m["pinball"]["0.9"], 2.0, places=6)
        self.assertAlmostEqual(m["interval"]["mean_width_g"], 48.0, places=6)
        self.assertAlmostEqual(m["interval"]["coverage"], 0.8, places=6)
        self.assertEqual(m["n_cold_start"], 0)
        # прокси: готовить pred_q90 = pred + 20
        self.assertAlmostEqual(m["decision_proxy"]["waste_proxy_mean_g"], 20.0, places=6)
        self.assertAlmostEqual(m["decision_proxy"]["shortfall_rate"], 0.0, places=6)

    def test_seasonal_naive_hand_values(self):
        m = next(x for x in self.report["models"] if x["name"] == "seasonal_naive")
        self.assertAlmostEqual(m["mae_g"], 0.0, places=6)
        self.assertAlmostEqual(m["interval"]["coverage"], 1.0, places=6)
        self.assertAlmostEqual(m["interval"]["mean_width_g"], 0.0, places=6)
        self.assertAlmostEqual(m["decision_proxy"]["waste_proxy_mean_g"], 0.0, places=6)
        self.assertEqual(m["drift"]["status"], enums.DRIFT_OK)

    def test_kitchen_policy_hindsight(self):
        kp = self.report["kitchen_policy"]
        self.assertTrue(kp["hindsight_only"])
        self.assertAlmostEqual(kp["mae_implicit_g"], 10.0, places=6)
        self.assertAlmostEqual(kp["overproduction_proxy_mean_g"], 10.0, places=6)
        self.assertAlmostEqual(kp["shortfall_rate"], 0.0, places=6)

    def test_demo_environment_marks_simulated(self):
        rep = run_backtest(snapshot_fn=snap_by_date(self.points),
                           test_days=5, calib_days=5, min_train=5, tz_name=TZ,
                           environment=enums.ENV_DEMO)
        self.assertEqual(rep["artifact_nature"], enums.SIMULATED)

    def test_unknown_model_rejected(self):
        with self.assertRaises(KeyError):
            run_backtest(snapshot_fn=snap_by_date(self.points),
                         test_days=5, calib_days=5, min_train=5, tz_name=TZ,
                         model_names=["llm_magic"])


class TestDataNotReadyAndColdStart(unittest.TestCase):
    def test_data_not_ready_when_history_insufficient(self):
        pts = hand_series()[:12]
        rep = run_backtest(snapshot_fn=snap_by_date(pts),
                           test_days=10, calib_days=8, min_train=10, tz_name=TZ)
        self.assertEqual(rep["status"], enums.DATA_NOT_READY)
        self.assertEqual(rep["models"], [])
        self.assertTrue(any("INSUFFICIENT_HISTORY" in r for r in rep["readiness"]["reasons"]))
        self.assertIn("данных недостаточно", rep["notes"][0])

    def test_cold_start_when_window_not_filled(self):
        pts = hand_series()[:10]
        rep = run_backtest(snapshot_fn=snap_by_date(pts),
                           test_days=1, calib_days=1, min_train=1, tz_name=TZ,
                           model_names=["moving_average"], moving_avg_window=9)
        m = rep["models"][0]
        # день калибровки (9-я точка) имеет историю 8 < 9 → остатков нет
        self.assertEqual(m["n_calib_residuals"], 0)
        self.assertEqual(m["interval_status"], "INSUFFICIENT_CALIBRATION")
        self.assertEqual(m["decision_proxy"]["status"], "UNAVAILABLE")
        self.assertEqual(m["drift"]["status"], enums.DRIFT_INSUFFICIENT)
        # тестовый день предсказан (история 9 = окно), но без интервала
        self.assertEqual(m["n_cold_start"], 0)
        day = m["days"][0]
        self.assertIn("pred_g", day)
        self.assertIsNone(day["pred_q90"])

    def test_all_censored_flagged(self):
        pts = [TargetPoint(session_id=p.session_id, service_date=p.service_date,
                           served_mass_g=p.served_mass_g, prepared_mass_g=p.prepared_mass_g,
                           n_obs=1, n_served_obs=1, censored=True, censored_unknown=False,
                           meals_served=None) for p in hand_series()[:12]]
        rep = run_backtest(snapshot_fn=snap_by_date(pts),
                           test_days=2, calib_days=2, min_train=2, tz_name=TZ)
        self.assertTrue(any("ALL_CENSORED" in r for r in rep["readiness"]["reasons"]))


class TestCensoringAndEligibility(unittest.TestCase):
    def test_eligible_points_excludes_censored(self):
        pts = hand_series()[:5]
        pts[-1].censored = True
        keep = eligible_points(pts, exclude_censored=True)
        self.assertEqual(len(keep), 4)
        keep_all = eligible_points(pts, exclude_censored=False)
        self.assertEqual(len(keep_all), 5)

    def test_naive_skips_censored_last_day(self):
        pts = hand_series()[:5]
        pts[-1].served_mass_g = 1.0  # цензурированный день с заниженной выдачей
        pts[-1].censored = True
        model = NaiveModel()
        model.fit(eligible_points(pts, exclude_censored=True))
        self.assertEqual(model.predict(date(2026, 10, 20)), pts[-2].served_mass_g)


class TestDrift(unittest.TestCase):
    def test_drift_detected(self):
        res = detect_drift([0, 0, 0, 0, 0], [0, 0, 0, 50, 50], k=2)
        self.assertEqual(res["status"], enums.DRIFT_DETECTED)

    def test_no_drift(self):
        res = detect_drift([0, 0, 0, 0, 0], [0, 0, 0, 0, 0], k=5)
        self.assertEqual(res["status"], enums.DRIFT_OK)

    def test_insufficient(self):
        self.assertEqual(detect_drift([0], [0])["status"], enums.DRIFT_INSUFFICIENT)

    def test_drift_does_not_block_calculations(self):
        self.assertIn("не блокирует", detect_drift([0, 0, 0], [9, 9, 9])["note"])


class TestNoFutureLeakageWithStore(unittest.TestCase):
    """Утечка будущего через поздние исправления: прогноз на день 16 обязан
    использовать значение дня 15, доступное на момент решения (до ревизии)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="sra-fc-"))
        self.store = EvidenceStore(self.tmp / "real.sqlite3", enums.ENV_REAL)
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def tearDown(self):
        self.store.close()

    def test_revision_after_decision_moment_not_visible(self):
        store = self.store
        start = date(2026, 9, 7)
        s_rows, o_rows = [], []
        d = start
        i = 1
        while i <= 16:
            if d.weekday() < 5:
                sid = f"SES-{i:03d}"
                served = 1000.0 * i
                observed = datetime(d.year, d.month, d.day, 13, 30, tzinfo=ZoneInfo(TZ))
                recorded = observed + timedelta(hours=2)
                s_rows.append(parse_session_row(make_session(
                    session_id=sid, school_id="SCH-1", service_date=d.isoformat(),
                    meals_served=str(100 + i), meals_requested=str(100 + i),
                    observed_at=observed.isoformat(), recorded_at=recorded.isoformat()),
                    row_number=i))
                o_rows.append(parse_observation_row(make_observation(
                    observation_id=f"O-{i:03d}", session_id=sid,
                    served_mass_g=str(int(served)), prepared_mass_g=str(int(served + 500)),
                    observed_at=observed.isoformat(), recorded_at=recorded.isoformat()),
                    row_number=i))
                i += 1
            d += timedelta(days=1)
        store.import_sessions(s_rows, actor="t")
        store.import_observations(o_rows, actor="t")
        # Поздняя ревизия дня 15 — появляется ЗАДОЛГО до неё недоступна (2026-12-30).
        store.revise("observation", "O-015",
                     {"served_mass_g": 90000,
                      "notes": "добавлена вторая партия; базы масс согласованы"},
                     reason="перепроверка веса", actor="t",
                     recorded_at="2026-12-30T18:00:00+05:00")

        # Финальные данные видят ревизию (проверка осмысленности теста):
        final = build_target_series(store, "SCH-1", "LUNCH")
        s15 = next(p for p in final.points if p.service_date == date(2026, 9, 25))
        self.assertEqual(s15.served_mass_g, 90000.0)

        # Backtest с моментом решения 2026-09-23 18:00 обязан ревизию НЕ видеть.
        rep = run_backtest(snapshot_fn=make_snapshot_fn(store, "SCH-1", "LUNCH"),
                           test_days=1, calib_days=3, min_train=10, tz_name=TZ,
                           model_names=["naive"])
        m = rep["models"][0]
        self.assertEqual(m["days"][0]["date"], "2026-09-28")
        self.assertEqual(m["days"][0]["pred_g"], 15000.0, 
                         "наивный прогноз должен использовать ДОревизионное значение 15000")
        self.assertAlmostEqual(m["mae_g"], abs(16000 - 15000), places=6)

    def test_decision_moment_is_previous_day(self):
        dm = decision_moment_iso(date(2026, 10, 21), TZ, 18)
        self.assertTrue(dm.startswith("2026-10-20T18:00"))
        self.assertTrue(dm.endswith("+05:00"))


class TestMovingAverageAndKitchenPolicy(unittest.TestCase):
    def test_moving_average_cold_start_model_level(self):
        model = MovingAverageModel(window=4)
        model.fit(hand_series()[:3])
        self.assertIsNone(model.predict(date(2026, 10, 9)))
        model.fit(hand_series()[:4])
        self.assertAlmostEqual(model.predict(date(2026, 10, 9)), (100 + 120 + 110 + 130) / 4)

    def test_kitchen_policy_is_hindsight(self):
        model = KitchenPolicyModel()
        pts = hand_series()[:5]
        model.fit(pts)
        # «предсказывает» фактическую prepared того же дня — потому и hindsight
        self.assertEqual(model.predict(pts[0].service_date), pts[0].prepared_mass_g)


class TestStoreBackedDemoSeries(unittest.TestCase):
    """Интеграция: целевой ряд из демо-хранилища, исключение частичных дней."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="sra-fcdemo-"))
        generate(cls.tmp / "data", seed=42, n_days=40)
        cls.store = EvidenceStore(cls.tmp / "demo.sqlite3", enums.ENV_DEMO)
        from sra.csvio import parse_file
        from sra.validation import validate_observations, validate_sessions
        for kind in ("sessions", "observations"):
            recs = parse_file(cls.tmp / "data" / f"{kind}.csv", kind)
            if kind == "sessions":
                rep = validate_sessions(recs, existing_ids=set())
                cls.store.import_sessions(rep.accepted, actor="t")
            else:
                rep = validate_observations(recs, known_sessions=cls.store.session_ids(),
                                            existing_observation_ids=set(),
                                            existing_pairs=set())
                cls.store.import_observations(rep.accepted, actor="t")

    @classmethod
    def tearDownClass(cls):
        cls.store.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_series_built_with_exclusions(self):
        series = build_target_series(self.store, "SCH-DEMO-001", "LUNCH")
        self.assertEqual(series.school_id, "SCH-DEMO-001")
        self.assertGreater(len(series.points), 10)
        # полный инвариант: у всех точек полное покрытие блюд
        for p in series.points:
            self.assertEqual(p.n_served_obs, p.n_obs)
            self.assertGreater(p.served_mass_g, 0)
        # синтетика содержит пропуски → есть исключённые частичные дни (учтены явно)
        self.assertTrue(all(e["reason"] in ("NO_OBSERVATIONS", "PARTIAL_SERVED_COVERAGE")
                            for e in series.excluded))

    def test_backtest_on_demo_series_runs(self):
        rep = run_backtest(snapshot_fn=make_snapshot_fn(self.store, "SCH-DEMO-001", "LUNCH"),
                           test_days=5, calib_days=5, min_train=5,
                           environment=enums.ENV_DEMO)
        self.assertEqual(rep["status"], "OK")
        self.assertEqual(rep["artifact_nature"], enums.SIMULATED)
        names = {m["name"] for m in rep["models"]}
        self.assertEqual(names, {"naive", "seasonal_naive", "moving_average"})
        for m in rep["models"]:
            self.assertIn(m["drift"]["status"],
                          (enums.DRIFT_OK, enums.DRIFT_DETECTED, enums.DRIFT_INSUFFICIENT))
            self.assertGreaterEqual(m["n_test_days"], 1)


if __name__ == "__main__":
    unittest.main()
