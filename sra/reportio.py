"""Экспорт отчётов в чистый markdown/CSV для печати (AUDIT §D.19).

Без зависимостей: на входе — уже построенные dict-отчёты (digest, norms,
quality), на выходе — текст markdown и CSV-строки. Никаких новых расчётов:
экспорт перефразирует готовые факты; недоступные секции попадают в текст
своей причиной (не пропадают и не заменяются нулём). CSV — с запятой и
кавычками по RFC 4180 (стандартный csv-модуль), кодировка utf-8-sig для
Excel (кириллица открывается без настроек).

Каждая функция — чистая: ничего не читает из хранилища и не пишет на диск
(запись файла делает CLI по явному пути пользователя).
"""
from __future__ import annotations

import csv
import io


def _md_escape(s) -> str:
    return str(s).replace("|", "\\|").replace("\n", " ")


def _fmt(x) -> str:
    if x is None:
        return "—"
    if isinstance(x, float):
        return f"{x:.1f}".rstrip("0").rstrip(".") if x == x else "—"
    return str(x)


def digest_to_markdown(d: dict) -> str:
    """Дайджест → markdown-документ для печати/архива."""
    lines: list[str] = []
    w = d.get("window") or {}
    lines.append(f"# Дайджест питания — {d.get('school_id')}")
    lines.append("")
    lines.append(f"- Приём пищи: {d.get('meal_type')}; среда: {d.get('environment')}; "
                 f"происхождение данных: {d.get('data_nature') or '—'}")
    if w.get("date_from"):
        lines.append(f"- Окно: {w['date_from']} … {w['date_to']} "
                     f"(запрошено дней: {w.get('requested_days')})")
    lines.append("")
    wm = d.get("window_metric") or {}
    lines.append("## Отходы за окно")
    lines.append("")
    if wm.get("status") == "OK":
        lines.append(f"**{wm['plate_waste_per_diner_g']} г на едока** "
                     f"(Σ {wm['plate_waste_g_total']} г / Σ едоков "
                     f"{wm['diners_total']}; дней с сессиями: "
                     f"{wm['days_with_sessions']})")
    else:
        lines.append(f"Недоступно: {wm.get('reason')}")
    lines.append("")
    lines.append("## Качество данных")
    lines.append("")
    q = d.get("quality") or {}
    if q.get("status") == "DATA_NOT_READY":
        lines.append(f"Недоступно: {q.get('reason')}")
    else:
        lines.append("| Проверка | Статус | Примечание |")
        lines.append("|---|---|---|")
        for c in q.get("checks") or []:
            mark = "OK" if c.get("ok") else "ПРОБЛЕМА"
            lines.append(f"| {_md_escape(c.get('name'))} | {mark} | "
                         f"{_md_escape(c.get('message') or '')} |")
    lines.append("")
    lines.append("## Порции vs Санправила РК")
    lines.append("")
    n = d.get("norms") or {}
    if n.get("status") == "UNAVAILABLE":
        lines.append(f"Недоступно: {n.get('reason')}")
    else:
        lines.append(f"Семантика порции: {n.get('portion_semantics')}")
        lines.append("")
        lines.append("| Блюдо | Норма, г | Измерено | Медиана, г | Вне нормы | Статус |")
        lines.append("|---|---|---|---|---|---|")
        for dish in n.get("dishes") or []:
            if dish.get("status") == "UNAVAILABLE":
                lines.append(f"| {_md_escape(dish['dish'])} | "
                             f"{dish['norm_g'][0]}–{dish['norm_g'][1]} | 0 | — | — | "
                             "нет измерений |")
                continue
            lo, hi = dish["norm_g"]
            lines.append(f"| {_md_escape(dish['dish'])} | {lo}–{hi} | "
                         f"{dish['n_measured']} | {_fmt(dish['median_g'])} | "
                         f"{_fmt(dish.get('share_out_pct'))}% | "
                         f"{dish['status']} |")
    lines.append("")
    lines.append("## Алерты")
    lines.append("")
    alerts = d.get("alerts") or []
    if not alerts:
        lines.append("Алертов нет.")
    for a in alerts:
        v = (f": {_fmt(a.get('value_pct'))}% при пороге "
             f"{a.get('threshold_pct')}%" if a.get("value_pct") is not None else "")
        lines.append(f"- **[{a.get('severity')}] {a.get('code')}**{v}")
        if a.get("reason"):
            lines.append(f"  - причина: {a['reason']}")
        if a.get("note"):
            lines.append(f"  - примечание: {a['note']}")
    lines.append("")
    ml = d.get("ml_status") or {}
    lines.append("## Прогнозы (ML)")
    lines.append("")
    lines.append(f"- diners: {ml.get('diners_sufficiency')}")
    lines.append(f"- composite: {ml.get('composite_sufficiency')}")
    for warn in ml.get("warnings") or []:
        lines.append(f"- предупреждение: {warn}")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append(f"*{d.get('disclaimer')}*")
    lines.append("")
    return "\n".join(lines)


def digest_norms_csv(d: dict) -> str:
    """Таблица норм порций из дайджеста → CSV (utf-8-sig, RFC 4180)."""
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["dish", "norm_lo_g", "norm_hi_g", "n_measured",
                     "median_g", "min_g", "max_g", "share_out_pct", "status"])
    n = d.get("norms") or {}
    for dish in n.get("dishes") or []:
        lo, hi = dish.get("norm_g", ["", ""])
        writer.writerow([
            dish.get("dish"), lo, hi,
            dish.get("n_measured", 0),
            dish.get("median_g", ""), dish.get("min_g", ""),
            dish.get("max_g", ""), dish.get("share_out_pct", ""),
            dish.get("status", "UNAVAILABLE"),
        ])
    return "\ufeff" + buf.getvalue()


def digest_window_csv(d: dict) -> str:
    """Метрика окна одной строкой → CSV (для годового журнала)."""
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["school_id", "meal_type", "date_from", "date_to",
                     "days_with_sessions", "plate_waste_g_total",
                     "diners_total", "plate_waste_per_diner_g", "status"])
    w = d.get("window") or {}
    wm = d.get("window_metric") or {}
    writer.writerow([
        d.get("school_id"), d.get("meal_type"),
        w.get("date_from", ""), w.get("date_to", ""),
        wm.get("days_with_sessions", 0),
        wm.get("plate_waste_g_total", ""),
        wm.get("diners_total", ""),
        wm.get("plate_waste_per_diner_g", ""),
        wm.get("status", ""),
    ])
    return "\ufeff" + buf.getvalue()
