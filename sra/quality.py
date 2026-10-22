"""Контроль качества данных хранилища + кросс-чек с первоисточником.

Мотивация: кросс-чек финского датасета выявил ошибочную ячейку в
производной колонке авторов (PW/g/diner=8.25 при вычисляемых 34.21).
Такие проверки должны быть ШТАТНЫМИ, а не случайной находкой.

Честность: отчёт только ФИКСИРУЕТ проблемы с причинами и ссылками на
записи; никакие данные не правятся молча. Исправление — через ревизии.
"""

from __future__ import annotations

from datetime import date, timedelta

from .ingest import xlsx_min
from .kz_calendar import is_school_day, load_calendar


def _median(xs: list[float]) -> float:
    xs = sorted(xs)
    n = len(xs)
    if n == 0:
        return float("nan")
    return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2


def data_quality_report(store, school_id: str, *, calendar: dict | None = None) -> dict:
    """Отчёт качества данных школы: полнота, выбросы, календарь, счётчики."""
    sessions = store.fetch_sessions(school_id=school_id)
    observations = store.fetch_observations(school_id=school_id)
    cal = calendar or load_calendar()
    out: dict = {"school_id": school_id, "checks": [], "status": "OK"}

    def add(name: str, ok: bool, details: dict):
        out["checks"].append({"check": name, "ok": bool(ok), "details": details})
        if not ok:
            out["status"] = "ATTENTION"

    if not sessions:
        return {"school_id": school_id, "status": "DATA_NOT_READY",
                "reason": "нет сессий школы в хранилище", "checks": []}

    # 1. Полнота полей (доля записей с измеренными значениями, пусто≠0).
    n = len(sessions)
    n_att = sum(1 for s in sessions if s.attendance_count is not None)
    n_served = sum(1 for s in sessions if s.meals_served is not None)
    n_obs = len(observations)
    n_pw = sum(1 for o in observations if o.plate_waste_g is not None)
    n_prep = sum(1 for o in observations if o.prepared_mass_g is not None)
    add("completeness", n_obs > 0 and n_pw > 0, {
        "sessions": n, "sessions_with_attendance": n_att,
        "sessions_with_meals_served": n_served,
        "observations": n_obs, "obs_with_plate_waste": n_pw,
        "obs_with_prepared_mass": n_prep,
        "note": "пустое поле = «не измеряли»; полнота определяет, какие метрики "
                "останутся UNAVAILABLE",
    })

    # 2. Выбросы отходов на едока (modified z-score по MAD; Iglewicz–Hoaglin).
    pw_by_session: dict[str, float] = {}
    for o in observations:
        if o.plate_waste_g is not None:
            pw_by_session[o.session_id] = (pw_by_session.get(o.session_id, 0.0)
                                           + o.plate_waste_g)
    diners_by_sid = {s.session_id: s.attendance_count or s.meals_served
                     for s in sessions}
    per_diner = [(sid, pw / diners_by_sid[sid])
                 for sid, pw in pw_by_session.items()
                 if diners_by_sid.get(sid)]
    outliers = []
    if len(per_diner) >= 8:
        med = _median([v for _, v in per_diner])
        mad = _median([abs(v - med) for _, v in per_diner])
        if mad > 0:
            for sid, v in per_diner:
                mz = 0.6745 * (v - med) / mad
                if abs(mz) > 3.5:
                    outliers.append({"session_id": sid, "per_diner_g": round(v, 2),
                                     "modified_z": round(mz, 2)})
    add("per_diner_outliers", len(outliers) == 0, {
        "n_points": len(per_diner), "outliers_gt_3_5mad": outliers[:20],
        "note": "выброс — НЕ ошибка сам по себе: проверить руками "
                "(праздник, авария, запись); исправление — через ревизию",
    })

    # 3. Календарь: учебные дни без сессии между первой и последней датой.
    dates = {s.service_date for s in sessions}
    d0, d1 = min(dates), max(dates)
    missing = []
    d = d0
    while d <= d1 and len(missing) <= 30:
        if d.weekday() < 5 and d not in dates:
            school_day, reason = is_school_day(d, cal)
            if school_day:
                missing.append({"date": d.isoformat()})
        d += timedelta(days=1)
    add("calendar_gaps", len(missing) == 0, {
        "range": [d0.isoformat(), d1.isoformat()],
        "weekday_school_days_without_session": len(missing),
        "examples": missing[:10],
        "note": "учебный день без сессии = потенциальный пропуск измерения "
                "(не праздник/каникулы — календарь ориентировочный)",
    })

    # 4. Счётчики: meals_served > attendance_count — подозрительно.
    weird = [{"session_id": s.session_id, "meals_served": s.meals_served,
              "attendance": s.attendance_count}
             for s in sessions
             if s.meals_served is not None and s.attendance_count is not None
             and s.meals_served > s.attendance_count]
    add("served_gt_attendance", len(weird) == 0, {
        "cases": weird[:20], "note": "возможно корректно (гости/доп. выдачи), "
                                     "но требует объяснения в notes",
    })

    # 5. Сессии в неучебные дни (по ориентировочному календарю).
    off_days = [{"session_id": s.session_id, "date": s.service_date.isoformat(),
                 "reason": chk[1]}
                for s in sorted(sessions, key=lambda x: x.service_date)
                if not (chk := is_school_day(s.service_date, cal))[0]]
    add("sessions_on_non_school_days", len(off_days) == 0, {
        "cases": off_days[:20], "note": "календарь ориентировочный; если день "
                                        "был учебным — обновите конфиг календаря",
    })
    return out


def crosscheck_zenodo_fi(xlsx_path, store, school_id: str) -> dict:
    """Кросс-чек с первоисточником FI: колонка PW/g/diner против вычисленной.

    Ловит ошибки производных колонок источника (пример: строка 137 файла,
    PW/g/diner=8.25 при PW=11633 г и DINERS=340 → 34.21).
    """
    wb = xlsx_min.read_workbook(xlsx_path)
    sheet = wb["sheets"][0]
    rows = sheet["rows"]
    hdr = [str(c).strip().lower() if c is not None else "" for c in rows[0]]
    need = ("date", "pw/g/diner", "diners")
    missing = [k for k in need if k not in hdr]
    if missing:
        return {"status": "UNAVAILABLE",
                "reason": f"в источнике нет колонок {missing} — кросс-чек невозможен"}
    i_date, i_ppd, i_din = hdr.index("date"), hdr.index("pw/g/diner"), hdr.index("diners")
    i_pw = hdr.index("pw g") if "pw g" in hdr else None

    from_store: dict = {}
    for s in store.fetch_sessions(school_id=school_id):
        d = s.service_date
        din = s.attendance_count
        if din:
            from_store[d.isoformat()] = din
    mismatches = []
    checked = 0
    serial0 = None
    for r in rows[1:]:
        raw_d = r[i_date] if i_date < len(r) else None
        raw_din = r[i_din] if i_din < len(r) else None
        raw_ppd = r[i_ppd] if i_ppd < len(r) else None
        if raw_d in (None, "") or raw_din in (None, "") or raw_ppd in (None, ""):
            continue
        if serial0 is None:
            serial0 = int(raw_d)
            base = date(1899, 12, 30)
        from datetime import timedelta as _td
        d_iso = (base + _td(days=int(raw_d))).isoformat()
        try:
            din_src, ppd_src = int(raw_din), float(raw_ppd)
        except (TypeError, ValueError):
            continue
        pw_src = None
        if i_pw is not None and i_pw < len(r) and r[i_pw] not in (None, ""):
            try:
                pw_src = float(r[i_pw])
            except (TypeError, ValueError):
                pass
        checked += 1
        if pw_src is not None and din_src > 0:
            computed = pw_src / din_src
            if abs(computed - ppd_src) > 0.05:
                mismatches.append({
                    "date": d_iso, "source_pw_g_per_diner": ppd_src,
                    "computed_from_pw_and_diners": round(computed, 2),
                    "note": "расхождение производной колонки источника",
                })
        din_store = from_store.get(d_iso)
        if din_store is not None and din_store != din_src:
            mismatches.append({"date": d_iso, "diners_in_store": din_store,
                               "diners_in_source": din_src,
                               "note": "DINERS в хранилище не совпадает с источником"})
    return {"status": "OK", "rows_checked": checked, "mismatches": mismatches,
            "note": ("найденные расхождения — претензии к производным колонкам "
                     "ИСТОЧНИКА; наши импортированные первичные значения (PW, "
                     "DINERS) не затронуты") if mismatches else
                    "расхождений не найдено"}
