"""Проверка наблюдаемых порций по нормам КЗ Санитарных правил (AUDIT §D.17).

Источник диапазонов: Приложение 7 Санитарных правил РК (приказы МЗ
№ ҚР ДСМ-98/2020 и МОН № 235) — те же значения, что использованы в
калибровке sra/demo/calibrated.py (класс NORM_KZ, source KZ-PORTIONS):
салат 60–100 г, первое 200–250 г, гарнир 100–150 г, мясо/рыба 80–150 г,
напиток 200 г — для возрастной группы 6–11 лет.

ПРОВЕРЕННЫХ значений для 11–18 лет в репозитории НЕТ — запрос другой группы
даёт UNAVAILABLE с причиной, значения не выдумываются.

Правило: порция ВНЕ диапазона → ПРЕДУПРЕЖДЕНИЕ качества (WARN), не ошибка:
данные остаются данными, решение о пересмотре — за бракеражной комиссией.
Проверяется served_mass_g (масса выданной порции, граммы). Порции с пустой
массой — «не измеряли» (пусто≠0), в отклонения не попадают, но считаются.

Инварианты: граммы; происхождение норм указано в отчёте; вывод на экран не
является действием и никуда не исполняется.
"""
from __future__ import annotations

from statistics import median

from . import enums

# Диапазоны масс порций (граммы) по Прил. 7 Санправил РК, класс NORM_KZ.
PORTION_NORMS: dict[str, dict[str, list[int]]] = {
    "6-11": {
        "salad": [60, 100],          # салат
        "first": [200, 250],         # первое блюдо
        "garnish": [100, 150],       # гарнир
        "meat": [80, 150],           # мясо/рыба
        "main_combined": [180, 300],  # второе: гарнир + мясо (сумма)
        "drink": [200, 200],         # напиток (третье блюдо)
    },
    # "11-18": НЕ добавляем — проверенных значений нет; см. докстринг модуля.
}

# dish_id в хранилище → ключ нормы. Покрывает калиброванный генератор
# (SALAD/FIRST/MAIN/DRINK) и типовые названия; остальное — «без нормы».
DEFAULT_DISH_MAP = {
    "SALAD": "salad",
    "FIRST": "first",
    "GARNISH": "garnish",
    "MEAT": "meat",
    "FISH": "meat",
    "MAIN": "main_combined",
    "DRINK": "drink",
}

NORM_SOURCE = {
    "norm_class": "NORM_KZ",
    "source_ref": "KZ-PORTIONS",
    "note": ("Приложение 7 Санитарных правил РК (ҚР ДСМ-98/2020, № 235); "
             "значения совпадают с калибровкой sra/demo/calibrated.py"),
}


def available_age_groups() -> list[str]:
    return sorted(PORTION_NORMS)


def check_portions(observations, *, age_group: str = "6-11",
                   dish_map: dict | None = None, sessions=None,
                   per: str = "portion") -> dict:
    """Сверяет наблюдаемые порции с нормами → отчёт-предупреждение.

    observations — записи ObservationRecord (served_mass_g, dish_id).
    per="portion" — served_mass_g уже масса ОДНОЙ порции (г);
    per="total" — масса на школу за день; тогда ОБЯЗАТЕЛЬНЫ sessions:
    порция выводится честным делением агрегатов: Σ масса блюда /
    Σ выданных порций сессии (пусто≠0; сессии без числа порций пропускаются
    с подсчётом). Смешение «сумма школы» с нормой одной порции — та самая
    семантическая ошибка, которую ловит этот режим.

    Возвращает отчёт со статусами по блюдам: OK | WARN | UNAVAILABLE.
    Предупреждение (WARN) НЕ является ошибкой данных. Сравнение с допуском
    0.5% от верхней границы (погрешность вывода порции из агрегатов).
    """
    if per not in ("portion", "total"):
        return {"status": "UNAVAILABLE",
                "reason": f"per: portion|total, получено {per!r}"}
    servings_of: dict[str, float] = {}
    if per == "total":
        if sessions is None:
            return {"status": "UNAVAILABLE",
                    "reason": ("per=total требует sessions для нормализации "
                               "на порцию (Σ масса / Σ выдач)")}
        for s in sessions:
            n = s.meals_served if s.meals_served is not None \
                else s.attendance_count
            if n and n > 0:
                servings_of[s.session_id] = float(n)
        if not servings_of:
            return {"status": "UNAVAILABLE",
                    "reason": "ни в одной сессии нет числа выдач (meals_served/"
                              "attendance_count) — порцию вывести нельзя"}
    if age_group not in PORTION_NORMS:
        return {
            "status": "UNAVAILABLE",
            "reason": (f"для возрастной группы {age_group!r} нет проверенных "
                       f"норм в репозитории; доступны группы: "
                       f"{available_age_groups()}"),
            "norm_source": NORM_SOURCE,
            "dishes": [],
        }
    norms = PORTION_NORMS[age_group]
    mapping = dict(DEFAULT_DISH_MAP)
    if dish_map:
        mapping.update(dish_map)

    def _norm_key(dish_id: str) -> str | None:
        """Точное имя, затем первый токен до «-» (например SALAD-M00 → SALAD)."""
        d = (dish_id or "").upper()
        if d in mapping:
            return mapping[d]
        token = d.split("-")[0]
        return mapping.get(token)

    by_dish: dict[str, list[float]] = {}
    n_missing_mass = 0
    n_skipped_no_servings = 0
    unknown_dishes: set[str] = set()
    n_total = 0
    for o in observations:
        n_total += 1
        key = _norm_key(o.dish_id)
        if key is None:
            unknown_dishes.add(o.dish_id or "")
            continue
        if o.served_mass_g is None:
            n_missing_mass += 1          # пусто = «не измеряли», не 0
            continue
        mass = float(o.served_mass_g)
        if per == "total":
            n_srv = servings_of.get(o.session_id)
            if not n_srv:
                n_skipped_no_servings += 1
                continue
            mass = mass / n_srv          # порция: Σ масса / Σ выдач
        by_dish.setdefault(key, []).append(mass)

    dishes_report = []
    n_warn = 0
    for key in sorted(norms):
        lo, hi = norms[key]
        values = by_dish.get(key, [])
        entry = {
            "dish": key, "norm_g": [lo, hi], "n_measured": len(values),
            "n_missing_mass": n_missing_mass if not values else None,
        }
        if not values:
            entry.update({"status": "UNAVAILABLE",
                          "reason": "нет измеренных порций этого блюда"})
            dishes_report.append(entry)
            continue
        eps = 0.005 * hi + 0.5   # допуск: погрешность деления агрегатов, г
        below = [v for v in values if v < lo - eps]
        above = [v for v in values if v > hi + eps]
        n_out = len(below) + len(above)
        examples = ([f"{v:.0f} г < нормы" for v in sorted(below)[:3]] +
                    [f"{v:.0f} г > нормы" for v in sorted(above)[-3:]])
        entry.update({
            "status": "WARN" if n_out else "OK",
            "min_g": round(min(values), 1), "median_g": round(median(values), 1),
            "max_g": round(max(values), 1),
            "n_below": len(below), "n_above": len(above),
            "share_out_pct": (round(n_out / len(values) * 100.0, 1)
                              if values else 0.0),
            "examples": examples,
        })
        if n_out:
            n_warn += 1
        dishes_report.append(entry)

    known_n = sum(len(by_dish.get(k, [])) for k in norms)
    if known_n == 0:
        status = "UNAVAILABLE"
        if unknown_dishes and n_missing_mass == 0:
            reason = ("все наблюдения с dish_id вне карты норм: "
                      f"{sorted(unknown_dishes)[:8]}; задайте dish_map")
        else:
            reason = ("нет измеренных порций (served_mass_g пуст во всех "
                      "наблюдениях); пустое поле = «не измеряли», не ноль")
    elif n_warn:
        status = "WARN"
        reason = None
    else:
        status = "OK"
        reason = None
    return {
        "status": status,
        "reason": reason,
        "age_group": age_group,
        "portion_semantics": ("масса одной порции" if per == "portion" else
                              "порция = Σ масса блюда / Σ выдач сессии "
                              "(наблюдения были суммами школы за день)"),
        "norm_source": NORM_SOURCE,
        "observations_total": n_total,
        "observations_without_mass": n_missing_mass,
        "observations_without_servings": n_skipped_no_servings,
        "dishes_without_norm": sorted(unknown_dishes),
        "dishes": dishes_report,
        "note": ("порция вне диапазона — ПРЕДУПРЕЖДЕНИЕ качества, не ошибка; "
                 "решение о пересмотре меню — за бракеражной комиссией школы"),
    }
