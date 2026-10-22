"""Сравнение фаз исследования (baseline/intervention) на данных хранилища.

НАЗНАЧЕНИЕ И ЧЕСТНЫЕ ГРАНИЦЫ:
  - это разведочный (post-hoc) анализ: сравнение фаз ЧУЖОГО вмешательства
    или разметки фаз, сделанной НЕ нами, не доказывает причинность — нет
    рандомизации, контролируемого внедрения и предварительной регистрации;
  - сезонные и меню-конфаундинги не устраняются этим модулем (частичная
    проверка меню-конфаундинга — выравнивание по идентификаторам меню);
  - агрегация — по числителям/знаменателям: доля фазы = Σ отходов / Σ едоков,
    проценты выводятся только из агрегатов, не усредняются;
  - результат используется как репетиция протокола оценки эффекта (этап 5b/6)
    и для планирования n через design.required_n_per_arm.
"""

from __future__ import annotations

import re

from .. import enums
from ..errors import ValidationError
from .design import required_n_per_arm
from . import stats

_MENU_RE = re.compile(r"MENU=(\d+)")
_ANALYSIS_PHASES = (enums.PHASE_BASELINE, enums.PHASE_INTERVENTION)

CAVEATS = [
    "РАЗВЕДОЧНЫЙ анализ: фазы размечены источником данных (правило импорта), "
    "вмешательство выполняла чужая школа — причинный эффект НЕ утверждается",
    "сезонный конфаундинг не устранён (фазы идут подряд по календарю); "
    "частичная проверка меню-конфаундинга — выравнивание по MENU-идентификаторам",
    "это чужие измерения: результаты не переносятся на другие школы и не "
    "доказывают эффект нашей системы",
    "нормальное приближение Манна–Уитни и t-критерий Уэлча — инженерные "
    "методы; при малых n (интевенция ~20 дней) p-значения ориентировочные",
]


def compare_phases(store, school_id: str, *, alpha: float = 0.05,
                   power: float = 0.80, min_days_per_phase: int = 5) -> dict:
    """Сравнивает daily plate-waste-per-diner между фазами.

    Возвращает отчёт со статусом: "OK" либо "DATA_NOT_READY" с reason
    (недоступная метрика — причина, а не число).
    """
    sessions = [s for s in store.fetch_sessions(school_id=school_id)
                if s.meal_type == "LUNCH"]
    obs_by_session: dict[str, float] = {}
    for o in store.fetch_observations(school_id=school_id):
        if o.plate_waste_g is not None:
            obs_by_session[o.session_id] = obs_by_session.get(o.session_id, 0.0) + o.plate_waste_g

    phases_points: dict[str, list[float]] = {p: [] for p in _ANALYSIS_PHASES}
    phase_sums: dict[str, dict] = {p: {"pw_g": 0.0, "diners": 0, "days": 0}
                                   for p in _ANALYSIS_PHASES}
    menu_values: dict[str, dict] = {}  # (phase, menu_id) -> [per_diner, ...]
    other_phases: dict[str, int] = {}
    for s in sessions:
        pw = obs_by_session.get(s.session_id)
        diners = s.attendance_count or s.meals_served  # как в diners.py
        if pw is None or not diners or diners <= 0:
            continue
        per_diner = pw / diners
        menu_id = getattr(s, "menu_id", None)
        if not menu_id and s.notes:
            m = _MENU_RE.search(s.notes)
            if m:
                menu_id = m.group(1)
        if s.study_phase in phases_points:
            phases_points[s.study_phase].append(per_diner)
            phase_sums[s.study_phase]["pw_g"] += pw
            phase_sums[s.study_phase]["diners"] += diners
            phase_sums[s.study_phase]["days"] += 1
            if menu_id:
                menu_values.setdefault((s.study_phase, menu_id), []).append(per_diner)
        else:
            other_phases[s.study_phase or "None"] = other_phases.get(s.study_phase or "None", 0) + 1

    base, interv = phases_points[enums.PHASE_BASELINE], phases_points[enums.PHASE_INTERVENTION]
    if len(base) < min_days_per_phase or len(interv) < min_days_per_phase:
        return {
            "status": "DATA_NOT_READY",
            "reason": (f"нужно >= {min_days_per_phase} дней с отходами и едоками в каждой "
                       f"фазе; baseline={len(base)}, intervention={len(interv)}; "
                       f"другие фазы: {other_phases or 'нет'}"),
            "school_id": school_id,
        }
    if any(v["diners"] == 0 for v in phase_sums.values()):
        return {"status": "DATA_NOT_READY",
                "reason": "в одной из фаз нет едоков (знаменатель 0)",
                "school_id": school_id}

    agg_base = phase_sums[enums.PHASE_BASELINE]["pw_g"] / phase_sums[enums.PHASE_BASELINE]["diners"]
    agg_int = phase_sums[enums.PHASE_INTERVENTION]["pw_g"] / phase_sums[enums.PHASE_INTERVENTION]["diners"]
    welch = stats.welch_ttest(base, interv)
    mw = stats.mann_whitney_u(base, interv)

    # меню-выравнивание: идентификаторы меню, встречающиеся в обеих фазах
    menu_ids_base = {mid for (ph, mid) in menu_values if ph == enums.PHASE_BASELINE}
    menu_ids_int = {mid for (ph, mid) in menu_values if ph == enums.PHASE_INTERVENTION}
    common = sorted(menu_ids_base & menu_ids_int)
    menu_pairs = []
    for mid in common:
        b = stats.mean(menu_values[(enums.PHASE_BASELINE, mid)])
        i = stats.mean(menu_values[(enums.PHASE_INTERVENTION, mid)])
        menu_pairs.append({"menu_id": mid, "baseline_per_diner_g": b,
                           "intervention_per_diner_g": i, "diff_g": i - b})
    menu_aligned_diff = (stats.mean([p["diff_g"] for p in menu_pairs])
                         if menu_pairs else None)

    sd_base = stats.sample_sd(base)
    effect = abs(agg_int - agg_base)
    design_note = "эффект нулевой — расчёт n на этом эффекте не имеет смысла"
    required_n = None
    if effect > 1e-9 and sd_base > 1e-9:
        try:
            required_n = required_n_per_arm(sigma_g=sd_base, effect_g=effect,
                                            alpha=alpha, power=power)
            design_note = (f"чтобы детектировать наблюдаемый эффект {round(effect, 3)} г/едока "
                           f"при σ(base)={round(sd_base, 3)} г, α={alpha}, power={power}, "
                           f"нужно ~{required_n} дней на группу (k=2 группы)")
        except ValidationError:
            design_note = "параметры alpha/power не поддержаны design.required_n_per_arm"

    return {
        "status": "OK",
        "school_id": school_id,
        "metric": "plate_waste_per_diner_g",
        "phases": {
            enums.PHASE_BASELINE: {
                **phase_sums[enums.PHASE_BASELINE],
                "aggregate_per_diner_g": agg_base,
                "mean_daily_per_diner_g": welch["mean1"],
                "sd_daily_per_diner_g": sd_base,
            },
            enums.PHASE_INTERVENTION: {
                **phase_sums[enums.PHASE_INTERVENTION],
                "aggregate_per_diner_g": agg_int,
                "mean_daily_per_diner_g": welch["mean2"],
                "sd_daily_per_diner_g": stats.sample_sd(interv),
            },
        },
        "diff_g": agg_int - agg_base,
        "diff_pct_of_baseline": ((agg_int - agg_base) / agg_base * 100.0
                                 if agg_base > 0 else None),
        "welch": welch,
        "mann_whitney": mw,
        "menu_aligned": {"n_common_menu_ids": len(common),
                         "mean_paired_diff_g": menu_aligned_diff,
                         "pairs": menu_pairs},
        "design_check": {"sigma_baseline_g": sd_base, "effect_abs_g": effect,
                         "required_days_per_arm": required_n, "alpha": alpha,
                         "power": power, "note": design_note},
        "other_phases_skipped": other_phases,
        "caveats": CAVEATS,
    }
