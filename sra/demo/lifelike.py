"""Lifelike-генератор учебного года (максимально приближенные SYNTHETIC-данные).

Зачем (решение 2026-10-22): реальной школы не будет; готовых открытых
датасетов с подтверждённой лицензией и дневной структурой школьной столовой
кроме уже используемых FI (Zenodo 20825233) и LV (Zenodo 18441715) не
найдено. Этот генератор создаёт ПОЛНЫЙ учебный год школы КЗ (1 Sep – 25 May)
с реализмом трёх слоёв:

1. КАЛЕНДАРЬ (KZ): 5-дневная неделя, каникулы и праздники из
   sra/kz_calendar.py (ориентировочные даты, редактируемые).
2. ШУМ И СТРУКТУРА, ИЗМЕРЕННЫЕ ПО РЕАЛЬНЫМ ОТКРЫТЫМ ДАННЫМ: функция
   calibrate_from_fi() оценивает по FI-файлу фактические доли: фактор дня
   недели, остаточную вариацию едоков (AR(1)-автокорреляцию), средний
   отход/едока, разброс между меню цикла — эти параметры подставляются в
   генерацию (в манифесте помечены REAL_OBSERVED-DERIVED с source_ref).
3. РАЦИОН И НОРМЫ: порции по Санправилам РК (NORM_KZ), доли отходов по
   итальянскому исследованию 78 школ (EMPIRICAL_STUDY IT-WASTE) — как в
   calibrated-генераторе.

Честные границы: результат SYNTHETIC во всех записях; калибровка по чужим
открытым данным задаёт ФОРМУ шума, но не измерения школы КЗ; значения FI
не переносятся как «измерения» и не дают заявлений об эффекте. Ни один
параметр не выдаётся за измеренный в КЗ.
"""
from __future__ import annotations

import random
import statistics
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .. import enums
from ..csvio import OBSERVATION_HEADER, SESSION_HEADER
from ..kz_calendar import is_school_day, load_calendar
from .calibrated import (MENU_COMPONENTS, PLATE_WASTE_SHARE, PROD_WASTE_RANGE,
                         UNSERVED_SHARE, _portion)

SCHOOL_ID = "SCH-LIFE-001"
TIMEZONE = "Asia/Almaty"
MEAL_TYPE = "LUNCH"


# --- Калибровка по реальному FI-датасету -------------------------------------
def calibrate_from_fi(xlsx_path) -> dict:
    """Оценивает параметры шума по реальному открытому датасету (FI).

    Возвращает словарь параметров с происхождением; ни одно значение не
    помечается как измерение школы КЗ — это свойства ЧУЖОГО датасета.
    """
    from ..ingest import xlsx_min, zenodo_fi
    wb = xlsx_min.read_workbook(xlsx_path)
    mapped = zenodo_fi.map_workbook(wb)
    sessions = mapped["sessions"]
    observations = mapped["observations"]
    pw_by_sid: dict[str, float] = defaultdict(float)
    for o in observations:
        if o.plate_waste_g is not None:
            pw_by_sid[o.session_id] += o.plate_waste_g

    diners_resid: list[float] = []
    by_wd: dict[int, list[float]] = defaultdict(list)
    pw_per_diner: list[float] = []
    by_menu: dict[str, list[float]] = defaultdict(list)
    n_days = 0
    for s in sessions:
        d_count = s.attendance_count or s.meals_served
        if not d_count or d_count <= 0:
            continue
        n_days += 1
        by_wd[s.service_date.weekday()].append(float(d_count))
        pw = pw_by_sid.get(s.session_id)
        if pw is not None and pw > 0:
            pw_per_diner.append(pw / d_count)
            if s.menu_id:
                by_menu[s.menu_id].append(pw / d_count)

    diners_mean = statistics.fmean(x for wd in by_wd for x in by_wd[wd])
    wd_factor = {wd: (statistics.fmean(by_wd[wd]) / diners_mean
                      if by_wd.get(wd) else 1.0) for wd in range(5)}
    # остатки после фактора дня недели -> CV и AR(1)
    for s in sorted(sessions, key=lambda x: x.service_date):
        d_count = s.attendance_count or s.meals_served
        if not d_count or d_count <= 0:
            continue
        diners_resid.append(d_count / (diners_mean * wd_factor[s.service_date.weekday()]))
    resid_mean = statistics.fmean(diners_resid)
    diners_cv = (statistics.pstdev(diners_resid) / resid_mean
                 if len(diners_resid) > 1 else 0.0)
    rho = 0.0
    if len(diners_resid) > 10:
        xs, ys = diners_resid[:-1], diners_resid[1:]
        mx, my = statistics.fmean(xs), statistics.fmean(ys)
        num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
        den = (sum((x - mx) ** 2 for x in xs) * sum((y - my) ** 2 for y in ys))
        rho = max(0.0, min(0.7, num / den ** 0.5 if den > 0 else 0.0))

    pw_mean = statistics.fmean(pw_per_diner) if pw_per_diner else 0.0
    pw_sd = statistics.pstdev(pw_per_diner) if len(pw_per_diner) > 1 else 0.0
    menu_means = [statistics.fmean(v) for v in by_menu.values()
                  if len(v) >= 3]
    menu_effect_share = 0.0
    if len(menu_means) > 1 and pw_mean > 0:
        menu_effect_share = min(0.35, statistics.pstdev(menu_means) / pw_mean)

    return {
        "source_class": "REAL_OBSERVED_DERIVED",
        "source_ref": str(mapped["report"].get("source_ref")),
        "source_note": ("параметры шума оценены по чужому открытому датасету "
                        "(Финляндия); НЕ измерения школы КЗ"),
        "n_days": n_days,
        "diners_mean": round(diners_mean, 2),
        "diners_weekday_factor": {str(k): round(v, 4)
                                  for k, v in wd_factor.items()},
        "diners_residual_cv": round(diners_cv, 4),
        "diners_ar1_rho": round(rho, 3),
        "pw_per_diner_mean_g": round(pw_mean, 2),
        "pw_per_diner_sd_g": round(pw_sd, 2),
        "pw_cv": (round(pw_sd / pw_mean, 4) if pw_mean > 0 else 0.0),
        "n_menus": len(by_menu),
        "menu_effect_share": round(menu_effect_share, 4),
    }


# --- Генерация учебного года --------------------------------------------------
def _school_year_days(start: date, end: date, cal: dict) -> list[date]:
    """Учебные дни года: Пн–Пт без каникул/праздников/лета (kz_calendar)."""
    days, d = [], start
    while d <= end:
        if is_school_day(d, cal)[0]:
            days.append(d)
        d += timedelta(days=1)
    return days


def generate_lifelike(out_dir, *, fi_xlsx, seed: int = 2026,
                      enrollment: int = 120, start: str = "2025-09-01",
                      end: str = "2026-05-25", p_shortage: float = 0.07,
                      n_gap_days: int = 2, p_missing_mass: float = 0.03,
                      p_missing_attendance: float = 0.015,
                      flu_waves: int = 2) -> dict:
    """Генерирует учебный год SYNTHETIC sessions/observations + манифест.

    fi_xlsx обязателен: генератор честно отказывается работать без
    калибровки по реальным данным (никаких молчаливых выдуманных шумов).
    """
    fi = Path(fi_xlsx)
    if not fi.exists():
        from ..errors import ValidationError
        raise ValidationError(
            f"файл калибровки не найден: {fi}; lifelike-генератор без "
            "реального датасета не работает (честный отказ, не выдумка)",
            code="E_CALIBRATION_FILE_REQUIRED")
    calib = calibrate_from_fi(fi)
    rng = random.Random(seed)
    cal = load_calendar()
    days = _school_year_days(date.fromisoformat(start), date.fromisoformat(end),
                             cal)
    if len(days) < 100:
        from ..errors import ValidationError
        raise ValidationError(
            f"получено слишком мало учебных дней ({len(days)}); "
            "проверьте календарь/границы года", code="E_BAD_YEAR")

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    source_ref = f"synthetic-lifelike:v1:seed={seed}:cal=FI"
    tz = ZoneInfo(TIMEZONE)

    # структура реализма, фиксированная на seed
    menu_cycle = max(5, int(calib.get("n_menus") or 25))
    menu_mult = {str(k): 1.0 + rng.gauss(0.0, calib.get("menu_effect_share", 0.0))
                 for k in range(menu_cycle)}
    menu_diners_mult = {str(k): 1.0 + rng.gauss(0.0, 0.02)
                        for k in range(menu_cycle)}  # ASSUMPTION: меню слегка
    # влияет и на посещаемость (популярное меню)
    wave_starts = set(rng.sample(range(30, len(days) - 40),
                                 min(flu_waves, max(1, len(days) - 70)))) \
        if len(days) > 80 else set()
    wave_days: set[int] = set()
    for w in wave_starts:
        wave_days.update(range(w, w + rng.randint(3, 7)))
    gap_idx = set(rng.sample(range(len(days)), n_gap_days)) if n_gap_days else set()
    shortage_idx = set()
    for i in range(len(days)):
        if i not in gap_idx and rng.random() < p_shortage:
            shortage_idx.add(i)

    cv = calib.get("diners_residual_cv", 0.08)
    rho = calib.get("diners_ar1_rho", 0.3)
    wd_factor = {int(k): v for k, v in
                 calib.get("diners_weekday_factor", {}).items()} or \
        {i: 1.0 for i in range(5)}
    pw_cv = calib.get("pw_cv", 0.35) or 0.35

    session_rows, obs_rows = [], []
    stats = {"school_days": len(days), "gap_days": len(gap_idx),
             "shortage_days": 0, "censored_days": 0, "flu_wave_days": 0,
             "missing_cells": 0}
    eps_prev = 0.0
    d0 = days[0]
    total_pw_per_child = []

    for i, day in enumerate(days):
        if i in gap_idx:
            continue                      # день без данных (журнал не вели)
        doy_frac = (day - d0).days / max(1, (days[-1] - d0).days)
        seasonal_diners = 1.0 + 0.03 * (0.5 - doy_frac)   # выгорание к весне
        eps = rho * eps_prev + (1.0 - rho) ** 0.5 * rng.gauss(0.0, cv)
        eps_prev = eps
        diners_f = (enrollment * 0.85 * wd_factor.get(day.weekday(), 1.0)
                    * seasonal_diners
                    * menu_diners_mult[str(i % menu_cycle)]
                    * (0.85 if i in wave_days else 1.0)
                    * max(0.5, 1.0 + eps))
        demand = max(5, int(round(diners_f)))
        shortage = i in shortage_idx
        if shortage:
            served_meals = int(demand * rng.uniform(0.72, 0.92))
            requested = demand
        else:
            served_meals = demand
            requested = demand if rng.random() < 0.60 else None
        censored = ("true" if (requested is not None and served_meals < requested)
                    else ("false" if requested is not None else "unknown"))
        if censored == "true":
            stats["shortage_days"] += 1
            stats["censored_days"] += 1
        if i in wave_days:
            stats["flu_wave_days"] += 1
        attendance = served_meals + int(rng.uniform(0, max(2, int(0.06 * enrollment))))
        if rng.random() < p_missing_attendance:
            attendance = None
            stats["missing_cells"] += 1
        observed_at = datetime(day.year, day.month, day.day, 13, 30, tzinfo=tz)
        recorded_at = observed_at + timedelta(hours=2)
        menu_id = i % menu_cycle
        sid = f"SES-LIFE-{day.isoformat()}-LUNCH"
        session_rows.append([
            sid, SCHOOL_ID, day.isoformat(), TIMEZONE, MEAL_TYPE,
            "" if attendance is None else attendance,
            "" if requested is None else requested, served_meals, censored,
            enums.SYNTHETIC, enums.PHASE_BASELINE, source_ref,
            observed_at.isoformat(), recorded_at.isoformat(), str(menu_id),
        ])

        # сезонность отходов (зима +8%) + эффект меню + волны болезней
        seasonal_pw = 1.0 + 0.08 * (0.5 - doy_frac) * 2.0
        pw_mult = (menu_mult[str(menu_id)] * seasonal_pw
                   * (1.05 if i in wave_days else 1.0))
        child_total = 0.0
        for comp, _title, lo_hi in MENU_COMPONENTS:
            portion = _portion(lo_hi, rng)
            served_target = served_meals * portion
            prod_waste = served_target * rng.uniform(*PROD_WASTE_RANGE)
            unserved = served_target * UNSERVED_SHARE[comp] * rng.uniform(0.7, 1.4)
            prepared = served_target + unserved + prod_waste
            if shortage and rng.random() < 0.5:
                prepared *= rng.uniform(0.97, 1.03)
            base_share = PLATE_WASTE_SHARE[comp]
            day_noise = 1.0 + rng.gauss(0.0, pw_cv * 0.4)
            plate_waste = served_target * max(0.0, base_share * pw_mult * day_noise)
            serving_waste = max(0.0, prepared - prod_waste - served_target)
            served_mass = prepared - prod_waste - serving_waste
            values = {"prepared_mass_g": prepared, "served_mass_g": served_mass,
                      "production_waste_g": prod_waste,
                      "serving_waste_g": serving_waste,
                      "plate_waste_g": plate_waste}
            out_vals = {}
            for k, v in values.items():
                if rng.random() < p_missing_mass:
                    out_vals[k] = ""
                    stats["missing_cells"] += 1
                else:
                    out_vals[k] = int(round(v))
            if rng.random() < 0.70:
                avoidable = plate_waste * rng.uniform(0.6, 0.85) + serving_waste * 0.4
                avoid_val, avoid_status = int(round(avoidable)), enums.AV_CLASSIFIED
            else:
                avoid_val, avoid_status = "", enums.AV_UNCLASSIFIED
            obs_rows.append([
                f"OBS-{sid}-{comp}", sid, f"{comp}-M{menu_id:02d}",
                out_vals["prepared_mass_g"], out_vals["served_mass_g"],
                out_vals["production_waste_g"], out_vals["serving_waste_g"],
                out_vals["plate_waste_g"], avoid_val, avoid_status,
                enums.SYNTHETIC, source_ref, observed_at.isoformat(),
                recorded_at.isoformat(),
            ])
            if comp != "DRINK":
                child_total += plate_waste
        total_pw_per_child.append(child_total / max(served_meals, 1))

    sessions_header = SESSION_HEADER + ["menu_id"]
    (out / "sessions.csv").write_text(
        "\n".join([",".join(sessions_header)] +
                  [",".join(str(x) for x in r) for r in session_rows]) + "\n",
        encoding="utf-8")
    (out / "observations.csv").write_text(
        "\n".join([",".join(OBSERVATION_HEADER)] +
                  [",".join(str(x) for x in r) for r in obs_rows]) + "\n",
        encoding="utf-8")

    manifest = {
        "data_nature": enums.SYNTHETIC,
        "generator": "lifelike-v1",
        "seed": seed,
        "school_id": SCHOOL_ID,
        "school_year": f"{start}..{end}",
        "source_ref": source_ref,
        "n_sessions": len(session_rows),
        "n_observations": len(obs_rows),
        "menu_cycle_days": menu_cycle,
        "stats": stats,
        "calibration_from_fi": calib,
        "assumptions": [
            {"parameter": "enrollment_share_diners", "value": 0.85,
             "class": "ASSUMPTION", "note": "доля класса, ходящая на обед"},
            {"parameter": "seasonal_diners_amplitude", "value": 0.03,
             "class": "ASSUMPTION", "note": "выгорание посещаемости к весне"},
            {"parameter": "seasonal_pw_amplitude", "value": 0.08,
             "class": "ASSUMPTION", "note": "зимний рост отходов"},
            {"parameter": "menu_diners_effect_sd", "value": 0.02,
             "class": "ASSUMPTION", "note": "влияние меню на посещаемость"},
            {"parameter": "flu_waves", "value": len(wave_starts),
             "class": "ASSUMPTION", "note": "волны болезней -15% едоков"},
            {"parameter": "p_shortage", "value": p_shortage,
             "class": "ASSUMPTION", "note": "как в calibrated v2"},
        ],
        "warnings": [
            "SYNTHETIC: это НЕ измерения школы КЗ; форма шума откалибрована "
            "по чужому открытому датасету (FI), нормы порций — NORM_KZ, доли "
            "отходов — итальянское исследование",
            "ни один вывод на этих данных не является заявлением об эффекте "
            "или точности для реальной школы",
            "данные можно перегенерировать: python -m sra gen-lifelike",
        ],
        "plate_waste_per_child_mean_g": (round(statistics.fmean(total_pw_per_child), 1)
                                         if total_pw_per_child else None),
    }
    (out / "manifest.json").write_text(
        __import__("json").dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8")
    return manifest
