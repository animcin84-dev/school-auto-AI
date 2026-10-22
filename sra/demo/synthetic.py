"""Воспроизводимый SYNTHETIC-генератор данных столовой (seed-детерминизм).

Назначение — проверка КОДА и поведение системы в сценарии:
пропуски, смена режима, выбросы, дни с нехваткой еды. Качество на синтетике
НЕ является ожидаемой точностью в реальной школе и не публикуется как результат.

Гарантии:
  - детерминизм: только random.Random(seed), никаких текущих времён;
  - все записи evidence_status=SYNTHETIC, source_ref="synthetic:v1:seed=<N>";
  - согласованность масс: served = prepared − production − serving (без отрицательных);
  - цензурирование: в дни нехватки meals_served < meals_requested, demand_censored=true;
  - сцены: недельная сезонность спроса, смена режима (день mode_change_day —
    изменение меню/технологии снижает отходы на тарелках), редкие выбросы
    (закупка/списание), случайные пропуски полей, случайные дни-пропуски.
"""

from __future__ import annotations

import csv
import json
import random
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .. import enums
from ..csvio import OBSERVATION_HEADER, SESSION_HEADER

SCHOOL_ID = "SCH-DEMO-001"
TIMEZONE = "Asia/Almaty"
MEAL_TYPE = "LUNCH"
STUDY_PHASE = enums.PHASE_BASELINE

# (dish_id, доля порции в рационе, типичная plate_waste доля до/после смены режима)
MENU_CYCLE_DAYS = 20  # цикл меню демо (как в calibrated-генераторе)
DISHES = [
    ("DISH-001-SOUP", 0.30, 0.11, 0.07),
    ("DISH-002-MAIN", 0.45, 0.14, 0.09),
    ("DISH-003-DRINK", 0.25, 0.07, 0.05),
]
PORTION_MEAN_G = 1000.0  # суммарная масса рациона на 1 приём пищи (3 блюда), г

WEEKDAY_FACTOR = {0: 0.92, 1: 1.00, 2: 0.98, 3: 1.05, 4: 0.85}  # Mon..Fri


def _school_days(start: date, count: int) -> list[date]:
    days: list[date] = []
    d = start
    while len(days) < count:
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)
    return days


def generate(out_dir: str | Path, *, seed: int = 42, n_days: int = 60,
             start_date: str = "2026-09-01", mode_change_day: int = 30,
             p_missing_mass: float = 0.04, p_missing_attendance: float = 0.15,
             p_outlier: float = 0.02, p_shortage: float = 0.08,
             p_requested_observed: float = 0.60, p_classified: float = 0.70,
             n_gap_days: int = 2) -> dict:
    """Генерирует sessions.csv и observations.csv в out_dir. Возвращает манифест."""
    rng = random.Random(seed)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    source_ref = f"synthetic:v1:seed={seed}"
    days = _school_days(date.fromisoformat(start_date), n_days)
    # Дни-пропуски (каникулы/сбой учёта): детерминированный выбор.
    gap_days = set(rng.sample(range(n_days), n_gap_days)) if n_gap_days else set()

    # Детерминированный «план» аномалий — нужен для манифеста и тестов.
    shortage_day_idx: set[int] = set()
    outlier_day_idx: set[int] = set()
    for i in range(n_days):
        if i in gap_days:
            continue
        if rng.random() < p_shortage:
            shortage_day_idx.add(i)
        if rng.random() < p_outlier:
            outlier_day_idx.add(i)

    session_rows: list[list] = []
    obs_rows: list[list] = []
    stats = {"censored_days": 0, "outlier_days": 0, "gap_days": len(gap_days),
             "missing_cells": 0, "mode_change_day": mode_change_day}

    for i, day in enumerate(days):
        if i in gap_days:
            continue
        wd = day.weekday()
        base = 120.0
        demand = base * WEEKDAY_FACTOR[wd] * rng.uniform(0.95, 1.05)
        if i >= mode_change_day:
            demand *= 1.06  # смена режима: слегка выросший спрос
        requested: int | None
        shortage = i in shortage_day_idx
        if shortage:
            cap = rng.uniform(0.70, 0.92)
            served_meals = int(demand * cap)
            requested = int(round(demand))
        else:
            served_meals = int(round(demand))
            requested = int(round(demand)) if rng.random() < p_requested_observed else None
        censored = ("true" if (requested is not None and served_meals < requested)
                    else ("false" if requested is not None else "unknown"))
        if censored == "true":
            stats["censored_days"] += 1
        if i in outlier_day_idx:
            stats["outlier_days"] += 1

        attendance = served_meals + int(rng.uniform(0, 8))
        if rng.random() < p_missing_attendance:
            attendance = None
            stats["missing_cells"] += 1

        observed_at = datetime(day.year, day.month, day.day, 13, 30,
                               tzinfo=ZoneInfo(TIMEZONE))
        recorded_at = observed_at + timedelta(hours=2)
        sid = f"SES-{day.isoformat()}-{MEAL_TYPE}"

        session_rows.append([
            sid, SCHOOL_ID, day.isoformat(), TIMEZONE, MEAL_TYPE,
            "" if attendance is None else attendance,
            "" if requested is None else requested,
            served_meals, censored,
            enums.SYNTHETIC, STUDY_PHASE, source_ref,
            observed_at.isoformat(), recorded_at.isoformat(),
            str(i % MENU_CYCLE_DAYS),  # menu_id: идентификатор цикла меню
        ])

        for dish_id, share, pw_before, pw_after in DISHES:
            plate_share = pw_after if i >= mode_change_day else pw_before
            portion = PORTION_MEAN_G * share
            served_target = served_meals * portion
            over = max(0.0, rng.gauss(0.10, 0.035))
            prepared = served_target * (1.0 + over)
            if i in outlier_day_idx:
                prepared *= 3.0  # выброс: разовая закупка/списание
            prod_waste = prepared * max(0.0, rng.gauss(0.045, 0.012))
            serving_waste = max(0.0, prepared - prod_waste - served_target)
            served_mass = prepared - prod_waste - serving_waste
            plate_waste = served_mass * max(0.0, rng.gauss(plate_share, 0.02))

            values = {
                "prepared_mass_g": prepared,
                "served_mass_g": served_mass,
                "production_waste_g": prod_waste,
                "serving_waste_g": serving_waste,
                "plate_waste_g": plate_waste,
            }
            out_vals = {}
            for k, v in values.items():
                if rng.random() < p_missing_mass:
                    out_vals[k] = ""
                    stats["missing_cells"] += 1
                else:
                    out_vals[k] = int(round(v))
            if rng.random() < p_classified:
                avoidable = plate_waste * rng.uniform(0.55, 0.80) + serving_waste * rng.uniform(0.30, 0.50)
                avoidable_val = int(round(avoidable))
                avoid_status = enums.AV_CLASSIFIED
            else:
                avoidable_val = ""
                avoid_status = enums.AV_UNCLASSIFIED
            obs_rows.append([
                f"OBS-{sid}-{dish_id}", sid, dish_id,
                out_vals["prepared_mass_g"], out_vals["served_mass_g"],
                out_vals["production_waste_g"], out_vals["serving_waste_g"],
                out_vals["plate_waste_g"], avoidable_val, avoid_status,
                enums.SYNTHETIC, source_ref, observed_at.isoformat(), recorded_at.isoformat(),
            ])

    sessions_path = out / "sessions.csv"
    observations_path = out / "observations.csv"
    with sessions_path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(SESSION_HEADER + ["menu_id"])
        w.writerows(session_rows)
    with observations_path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(OBSERVATION_HEADER)
        w.writerows(obs_rows)

    manifest = {
        "data_nature": enums.SYNTHETIC,
        "seed": seed,
        "n_days_requested": n_days,
        "n_sessions": len(session_rows),
        "n_observations": len(obs_rows),
        "start_date": start_date,
        "mode_change_day": mode_change_day,
        "anomaly_plan": {
            "shortage_days": sorted(shortage_day_idx),
            "outlier_days": sorted(outlier_day_idx),
            "gap_days": sorted(gap_days),
        },
        "stats": stats,
        "source_ref": source_ref,
        "school_id": SCHOOL_ID,
        "warning": "СИНТЕТИЧЕСКИЕ ДАННЫЕ: проверяют код, а не пользу в реальной школе",
    }
    (out / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest
