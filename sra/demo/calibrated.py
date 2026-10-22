"""Откалиброванный SYNTHETIC-генератор v2: параметры из опубликованных источников.

Гарантии честности:
  - статус данных остаётся SYNTHETIC: калибровка повышает реалистичность
    сценария, но НЕ превращает синтетику в реальное наблюдение и не даёт
    оснований заявлять ожидаемую точность/эффект в реальной школе;
  - КАЖДЫЙ параметр калибровки в манифесте имеет источник и класс:
      NORM_KZ          — норматив/официальные рекомендации РК (документ);
      EMPIRICAL_STUDY  — опубликованные прямые измерения (другая страна);
      ASSUMPTION       — явное допущение (значение НЕ подтверждено);
  - это не данные реальной школы и не замена этапа 5b (реальный сбор).

Источники калибровки (проверены в октябре 2026 через веб-поиск):
  [KZ-PORTIONS] Приложение 7 к Санитарным правилам «Санитарно-эпидемиологические
      требования к объектам образования» (приказ МЗ РК от 28.08.2020 № ҚР ДСМ-98/2020):
      рекомендуемая масса порций в граммах (первые блюда 200–250 г для 6–11 лет,
      гарнир 100–150, мясо/рыба 80–150, салат 60–100, третьи блюда 200).
      Опубликовано, в т.ч. на kargoo.kz (текст правил).
  [KZ-MEAL] Правила организации питания обучающихся (приказ МОН РК № 235 от 08.06.2020):
      обед = салат + первое + второе (гарнир+мясо/рыба) + третье (напиток).
  [IT-WASTE] Sustainability 2025, 17(17):7836 — прямые взвешивания в 78 школах Италии
      (методология EC 2019/1597), таблица на блюдо: подготовлено/отходы тарелок/
      невыдано, г/день/чел: первое 203.6/44.3/8.9; второе 82.6/14.6/2.4;
      гарнир 95.2/20.2/4.5; хлеб 51.1/5.6/16.8; фрукт 101.8/5.3/37.0.
      Доли от приготовленного: plate 17.7–21.8% (горячие), невыдано 2.9–4.7%.
  [FI-DATA] Zenodo record 20825233 — дневные взвешивания школы (Финляндия,
      2024–2025): поля PW, KSW, DINERS, MENU; 155 дней; структура «день = строка»
      и меню-цикл учтены в дизайне генератора (не значения).
  [LV-RANGE] Sustainability 2022, 14(7):4046 (Резекне, Латвия): отходы на тарелках
      142–268 г/день/школьник — диапазон для sanity-check.

Допущения (ASSUMPTION) помечены явно и не должны использоваться как нормативы.
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

SCHOOL_ID = "SCH-DEMO-CAL"
TIMEZONE = "Asia/Almaty"
MEAL_TYPE = "LUNCH"

# --- Калибровочная таблица (попадает в manifest.json) -------------------------
CALIBRATION = [
    # (параметр, значение, класс, источник, комментарий)
    ("portion_salad_g", [60, 100], "NORM_KZ", "KZ-PORTIONS", "салат, 6–11 лет"),
    ("portion_first_g", [200, 250], "NORM_KZ", "KZ-PORTIONS", "первое блюдо, 6–11 лет"),
    ("portion_garnish_g", [100, 150], "NORM_KZ", "KZ-PORTIONS", "гарнир, 6–11 лет"),
    ("portion_meat_g", [80, 150], "NORM_KZ", "KZ-PORTIONS", "мясо/рыба, 6–11 лет"),
    ("portion_drink_g", [200, 200], "NORM_KZ", "KZ-PORTIONS", "третье блюдо"),
    ("meal_composition", "салат+первое+второе(гарнир+мясо)+напиток", "NORM_KZ", "KZ-MEAL",
     "состав обеда"),
    ("plate_waste_share_first", 44.3 / 203.6, "EMPIRICAL_STUDY", "IT-WASTE",
     "44.3/203.6 г/день (Италия, 78 школ, прямые взвешивания)"),
    ("plate_waste_share_main", 14.6 / 82.6, "EMPIRICAL_STUDY", "IT-WASTE",
     "второе блюдо: 14.6/82.6"),
    ("plate_waste_share_side_in_main", 20.2 / 95.2, "EMPIRICAL_STUDY", "IT-WASTE",
     "гарнир как компонент второго: 20.2/95.2"),
    ("plate_waste_share_drink", None, "ASSUMPTION", "нет данных о напитках в IT-WASTE",
     "взято малым (1–3%); напитки почти возвращаются"),
    ("unserved_share_first", 8.9 / 203.6, "EMPIRICAL_STUDY", "IT-WASTE",
     "невыданное первое"),
    ("unserved_share_main", 2.4 / 82.6, "EMPIRICAL_STUDY", "IT-WASTE", "невыданное второе"),
    ("unserved_share_salad", 4.5 / 95.2, "EMPIRICAL_STUDY", "IT-WASTE", "как гарнир"),
    ("production_waste_share", [0.03, 0.06], "ASSUMPTION",
     "прямых опубликованных значений не найдено",
     "производственные отходы до раздачи; проверить на этапе 5b"),
    ("menu_cycle_days", 20, "DESIGN", "FI-DATA (25-дневный цикл)",
     "циклическое меню для сезонного наивного прогноза"),
    ("plate_waste_sanity_range_g_day_child", [142, 268], "EMPIRICAL_STUDY", "LV-RANGE",
     "контроль правдоподобия, не параметр генерации"),
    ("school_enrollment", 120, "ASSUMPTION", "не задано пользователем",
     "сельская школа; изменить флагом --enrollment"),
    ("shortage_day_prob", 0.07, "ASSUMPTION", "не подтверждено",
     "частота дней нехватки"),
]

# Меню-цикл: 4 компонента обеда (NORM_KZ: состав обеда), dish_id стабильны.
MENU_COMPONENTS = [
    ("SALAD", "Салат", [60, 100]),
    ("FIRST", "Первое блюдо", [200, 250]),
    ("MAIN", "Второе (гарнир+мясо)", [180, 300]),   # 100–150 гарнир + 80–150 мясо
    ("DRINK", "Напиток", [200, 200]),
]
# Доли отходов на тарелках по компонентам (EMPIRICAL_STUDY IT-WASTE / ASSUMPTION).
PLATE_WASTE_SHARE = {
    "SALAD": 20.2 / 95.2,
    "FIRST": 44.3 / 203.6,
    "MAIN": (14.6 / 82.6 + 20.2 / 95.2) / 2,  # смесь второго и гарнира, ДОЛЯ
    "DRINK": 0.02,          # ASSUMPTION
}
# Невыданное (остаётся в мармитах после раздачи) — EMPIRICAL_STUDY IT-WASTE.
UNSERVED_SHARE = {"SALAD": 4.5 / 95.2, "FIRST": 8.9 / 203.6,
                  "MAIN": (2.4 / 82.6 + 4.5 / 95.2) / 2, "DRINK": 0.02}
PROD_WASTE_RANGE = (0.03, 0.06)  # ASSUMPTION

WEEKDAY_FACTOR = {0: 0.94, 1: 1.00, 2: 0.99, 3: 1.03, 4: 0.87}  # Допущение формы недели


def _school_days(start: date, count: int) -> list[date]:
    days, d = [], start
    while len(days) < count:
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)
    return days


def _portion(lo_hi: list[int], rng: random.Random, scale: float = 1.0) -> float:
    lo, hi = lo_hi
    return (lo + (hi - lo) * rng.random()) * scale


def generate(out_dir: str | Path, *, seed: int = 2026, n_days: int = 90,
             start_date: str = "2026-09-01", enrollment: int = 120,
             menu_cycle_days: int = 20, mode_change_day: int = 45,
             p_missing_mass: float = 0.03, p_missing_attendance: float = 0.15,
             p_shortage: float = 0.07, p_requested_observed: float = 0.60,
             n_gap_days: int = 2) -> dict:
    """Генерирует откалиброванные SYNTHETIC sessions/observations + манифест.

    Отличие от v1: порции и доли отходов из источников; меню-цикл; состав обеда
    по КЗ-нормам. Все записи SYNTHETIC, source_ref ссылается на seed и v2.
    """
    rng = random.Random(seed)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    source_ref = f"synthetic-calibrated:v2:seed={seed}"
    days = _school_days(date.fromisoformat(start_date), n_days)
    gap_idx = set(rng.sample(range(n_days), n_gap_days)) if n_gap_days else set()
    shortage_idx = set()
    for i in range(n_days):
        if i not in gap_idx and rng.random() < p_shortage:
            shortage_idx.add(i)

    session_rows, obs_rows = [], []
    stats = {"censored_days": 0, "missing_cells": 0, "gap_days": len(gap_idx),
             "shortage_days": len(shortage_idx)}
    total_plate_per_child = []  # sanity-check против LV-RANGE

    for i, day in enumerate(days):
        if i in gap_idx:
            continue
        menu_id = i % menu_cycle_days
        diners_base = enrollment * WEEKDAY_FACTOR[day.weekday()] * rng.uniform(0.94, 1.04)
        demand = int(round(diners_base))
        shortage = i in shortage_idx
        if shortage:
            served_meals = int(demand * rng.uniform(0.72, 0.92))
            requested = demand
        else:
            served_meals = demand
            requested = demand if rng.random() < p_requested_observed else None
        censored = ("true" if (requested is not None and served_meals < requested)
                    else ("false" if requested is not None else "unknown"))
        if censored == "true":
            stats["censored_days"] += 1
        attendance = served_meals + int(rng.uniform(0, max(2, int(0.06 * enrollment))))
        if rng.random() < p_missing_attendance:
            attendance = None
            stats["missing_cells"] += 1
        observed_at = datetime(day.year, day.month, day.day, 13, 40,
                               tzinfo=ZoneInfo(TIMEZONE))
        recorded_at = observed_at + timedelta(hours=2)
        sid = f"SES-CAL-{day.isoformat()}-LUNCH"
        session_rows.append([
            sid, SCHOOL_ID, day.isoformat(), TIMEZONE, MEAL_TYPE,
            "" if attendance is None else attendance,
            "" if requested is None else requested, served_meals, censored,
            enums.SYNTHETIC, enums.PHASE_BASELINE, source_ref,
            observed_at.isoformat(), recorded_at.isoformat(), str(menu_id),
        ])
        # Эффект режима после mode_change_day: сниженные отходы тарелок (демо-сценарий).
        plate_factor = 0.75 if i >= mode_change_day else 1.0
        child_plate_total = 0.0
        for comp, _title, lo_hi in MENU_COMPONENTS:
            portion = _portion(lo_hi, rng)
            served_target = served_meals * portion
            prod_waste = served_target * rng.uniform(*PROD_WASTE_RANGE)
            unserved = served_target * UNSERVED_SHARE[comp] * rng.uniform(0.7, 1.4)
            prepared = served_target + unserved + prod_waste
            if i in shortage_idx and rng.random() < 0.5:
                prepared *= rng.uniform(0.97, 1.03)  # экономия в дефицитный день
            plate_share = PLATE_WASTE_SHARE[comp] * plate_factor
            plate_waste = served_target * max(0.0, plate_share * rng.uniform(0.8, 1.2))
            serving_waste = max(0.0, prepared - prod_waste - served_target)
            served_mass = prepared - prod_waste - serving_waste
            values = {"prepared_mass_g": prepared, "served_mass_g": served_mass,
                      "production_waste_g": prod_waste, "serving_waste_g": serving_waste,
                      "plate_waste_g": plate_waste}
            out_vals = {}
            for k, v in values.items():
                if rng.random() < p_missing_mass:
                    out_vals[k] = ""
                    stats["missing_cells"] += 1
                else:
                    out_vals[k] = int(round(v))
            # Классификация предотвратимых отходов ~70% дней (как в v1).
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
                child_plate_total += plate_waste
        total_plate_per_child.append(child_plate_total / max(served_meals, 1))

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
        "generator": "calibrated-v2",
        "seed": seed,
        "start_date": start_date,
        "n_sessions": len(session_rows),
        "n_observations": len(obs_rows),
        "mode_change_day": mode_change_day,
        "menu_cycle_days": menu_cycle_days,
        "school_id": SCHOOL_ID,
        "source_ref": source_ref,
        "stats": stats,
        "plate_waste_per_child_mean_g": (sum(total_plate_per_child) / len(total_plate_per_child)
                                         if total_plate_per_child else None),
        "plate_waste_sanity_range_g_day_child": [142, 268],
        "calibration": [
            {"parameter": p, "value": v, "class": cls, "source": src, "note": note}
            for (p, v, cls, src, note) in CALIBRATION
        ],
        "warnings": [
            "СИНТЕТИЧЕСКИЕ данные, откалиброванные по опубликованным источникам; "
            "это проверка кода и реалистичность сценария, НЕ измерения реальной школы",
            "EMPIRICAL_STUDY-доли получены в Италии/Латвии/Финляндии и не переносимы "
            "на школу КЗ без локальных измерений",
            "ASSUMPTION-параметры — явные допущения; их источник не найден",
        ],
    }
    (out / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest
