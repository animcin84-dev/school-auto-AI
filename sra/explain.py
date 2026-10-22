"""Детерминированные русскоязычные объяснения результатов (Explanation Assistant).

Границы (docs/ARCHITECTURE_AND_GATES.md, раздел 10 задания):
  - текст строится ТОЛЬКО из переданных структур — результатов серверных
    расчётов; новые числа не вычисляются и не «уточняются»;
  - недоступная метрика объясняется причиной, а не заменяется числом;
  - записи пользователя (notes) — недоверенный контент, включаются только
    через quote_untrusted();
  - это НЕ LLM: тексты детерминированы, воспроизводимы и покрыты тестами.
    Необязательный языковой помощник в будущем читает готовые факты через
    sra.assistant.tools и не считает метрики заново.
"""

from __future__ import annotations

UNTRUSTED_MARKER = "НЕДОВЕРЕННЫЙ ТЕКСТ"


def quote_untrusted(text: str) -> str:
    """Оборачивает недоверенный текст (записи пользователя, документы) маркером.

    Будущий языковой помощник обязан трактовать содержимое внутри маркера как
    данные, а не инструкции (защита от prompt injection).
    """
    return f'[{UNTRUSTED_MARKER}: "{text}"]'


def _fmt_g(x: float | None) -> str:
    if x is None:
        return "нет"
    return f"{x:,.0f}".replace(",", " ")


def _fmt_num(x: float | None, digits: int = 1) -> str:
    if x is None:
        return "нет"
    return f"{x:.{digits}f}".rstrip("0").rstrip(".") if digits else f"{x:.0f}"


def _counts_line(m: dict) -> str:
    counts = m.get("evidence_counts") or {}
    parts = [f"{k}: {v}" for k, v in sorted(counts.items())] or ["нет входов"]
    return (f"входов: {m.get('n_inputs', 0)} ({'; '.join(parts)}); "
            f"производная природа: {m.get('derived_status', '?')}")


UNIT_RU = {
    "percent": "%",
    "g": "г",
    "g/meal": "г/приём",
    "currency/meal": "валюта/приём",
}


def _unit_ru(unit: str | None) -> str:
    return UNIT_RU.get(unit or "", unit or "")


def explain_metric_result(m: dict) -> list[str]:
    name = m.get("name", "?")
    lines = []
    if m.get("status") == "OK":
        base = ""
        if m.get("numerator") is not None and m.get("denominator") is not None:
            base = (f" (числитель {_fmt_g(m['numerator'])} / знаменатель "
                    f"{_fmt_g(m['denominator'])})")
        lines.append(f"{name} = {_fmt_num(m.get('value'), 3)} {_unit_ru(m.get('unit'))}{base}")
    else:
        lines.append(f"{name} — НЕДОСТУПЕН. Причина: {m.get('reason', 'не указана')}. "
                     "Значение не заполняется догадкой.")
    lines.append(f"   [{_counts_line(m)}]")
    for note in m.get("notes") or []:
        lines.append(f"   примечание: {note}")
    if m.get("censoring"):
        c = m["censoring"]
        lines.append(f"   цензурирование дней: true={c.get('true', 0)}, "
                     f"false={c.get('false', 0)}, unknown={c.get('unknown', 0)}")
    return lines


def explain_metrics_report(rep: dict) -> str:
    lines = ["=== Авторитетные метрики (расчёт сервера) ==="]
    scope = rep.get("scope") or {}
    lines.append(f"Область: сессий {scope.get('sessions', 0)}, наблюдений "
                 f"{scope.get('observations', 0)}. {rep.get('nature', '')}")
    for m in rep.get("metrics", []):
        lines.extend(explain_metric_result(m))
    lines.append("Значения рассчитаны сервером из хранилища; этот текст "
                 "сгенерирован детерминированно и не является мнением модели.")
    return "\n".join(lines)


def explain_proposal(p: dict) -> str:
    lines = [f"=== Предложение решения {str(p.get('proposal_id', ''))[:8]}… ==="]
    lines.append(f"Среда: {p.get('environment')}, режим: {p.get('mode')}. "
                 f"Школа: {p.get('school_id')}, план на дату {p.get('target_date')}. "
                 f"Момент решения: {p.get('decision_at')}")
    lines.append(f"Действие: {p.get('action')} [{p.get('action_status')}]")
    if p.get("action") == "NO_CHANGE":
        lines.append("Новое вмешательство не предлагается. Причины:")
        descriptions = p.get("missing_conditions") or []
        for i, code in enumerate(p.get("reasons", [])):
            d = descriptions[i] if i < len(descriptions) else ""
            lines.append(f"   — {code}: {d}")
        lines.append("Важно: NO_CHANGE — отказ от НОВОГО вмешательства; он НЕ "
                     "подтверждает безопасность текущей практики. Обнаруженная "
                     "опасность передаётся ответственному человеку.")
        lines.append("Для расчётов (метрики/прогнозы) это не сбой: они продолжают "
                     "работать независимо от отказа в действии.")
    else:
        ap = p.get("action_params") or {}
        lines.append(f"План: готовить {_fmt_g(ap.get('planned_prepared_mass_g'))} г "
                     f"вместо привычных {_fmt_g(ap.get('baseline_mass_g'))} г "
                     f"(Δ = {_fmt_g(ap.get('delta_g'))} г при границе "
                     f"±{_fmt_g(ap.get('delta_max_g'))} г).")
        ch = ap.get("expected_outcome_chosen") or {}
        cb = ap.get("expected_outcome_baseline") or {}
        lines.append(f"Ожидания по сценариям: при плане отходы ~{_fmt_g(ch.get('waste_g_mean'))} г, "
                     f"риск нехватки {ch.get('shortage_risk')} (у базовой линии: "
                     f"отходы ~{_fmt_g(cb.get('waste_g_mean'))} г, риск {cb.get('shortage_risk')}).")
        eff = p.get("expected_effect_interval")
        if eff:
            lines.append(f"Эффект против NO_CHANGE ({eff.get('nature')}): "
                         f"q50 = {_fmt_num(eff.get('q50'), 2)}, "
                         f"q10..q90 = [{_fmt_num(eff.get('q10'), 2)} … "
                         f"{_fmt_num(eff.get('q90'), 2)}] {eff.get('unit', '')}. "
                         f"{eff.get('caveat', '')}")
        failed = [k for k, v in (p.get("constraint_checks") or {}).items()
                  if isinstance(v, dict) and v.get("pass") is False]
        if failed:
            lines.append(f"ВНИМАНИЕ: нарушения ограничений: {failed}. Такое предложение "
                         "не должно было быть сформировано — сообщите администратору.")
        else:
            lines.append("Проверка ограничений: все пройдены (жёсткие ограничения "
                         "не расширялись).")
        if p.get("approval"):
            lines.append(f"Согласование: есть ({p['approval']}).")
        else:
            lines.append("Согласование КОНКРЕТНОЙ версии отсутствует — исполнение "
                         "невозможно; решение за уполномоченным человеком.")
        lines.append(f"Ссылки: прогноз {p.get('forecast_ref')}; политики "
                     f"{', '.join(p.get('policy_refs') or []) or 'нет'}.")
    for note in p.get("notes") or []:
        lines.append(f"примечание: {note}")
    return "\n".join(lines)


def explain_forecast_report(f: dict) -> str:
    lines = ["=== Прогнозирование (backtest этапа 3) ==="]
    lines.append(f"Статус: {f.get('status')}. Природа артефакта: {f.get('artifact_nature')}.")
    tgt = f.get("target") or {}
    if tgt:
        lines.append(f"Цель: {tgt.get('definition')} | горизонт: {tgt.get('horizon')} | "
                     f"решение: {tgt.get('decision_moment')}")
    rd = f.get("readiness") or {}
    if f.get("status") == "DATA_NOT_READY":
        for r in rd.get("reasons", []):
            lines.append(f"   — {r}")
        lines.append("Метрик нет: фиктивная модель не строится.")
        return "\n".join(lines)
    w = f.get("windows") or {}
    lines.append(f"Окна: обучение {w.get('train_points')} точек; калибровка "
                 f"{w.get('calib_range')}; тест {w.get('test_range')}.")
    d = f.get("data") or {}
    lines.append(f"Данные: {d.get('n_points')} точек ({d.get('date_from')} … "
                 f"{d.get('date_to')}); цензурированных дней {d.get('censored_days')}; "
                 f"частичных дней исключено {len(d.get('excluded_partial_days') or [])}.")
    for m in f.get("models", []):
        iv = m.get("interval") or {}
        dp = m.get("decision_proxy") or {}
        lines.append(
            f"— {m.get('name')}: MAE = {_fmt_num(m.get('mae_g'))} г; "
            f"pinball 0.1/0.5/0.9 = {_fmt_num((m.get('pinball') or {}).get('0.1'), 2)} / "
            f"{_fmt_num((m.get('pinball') or {}).get('0.5'), 2)} / "
            f"{_fmt_num((m.get('pinball') or {}).get('0.9'), 2)}; "
            f"покрытие = {_fmt_num(iv.get('coverage'), 2)} (номинал 0.8; гарантии нет), "
            f"ширина = {_fmt_num(iv.get('mean_width_g'))} г; "
            f"дрейф: {m.get('drift', {}).get('status')}; "
            f"cold_start: {m.get('n_cold_start')} дн.")
        if dp.get("waste_proxy_mean_g") is not None:
            lines.append(f"   прокси решения ({dp.get('rule')}): отходы "
                         f"{_fmt_num(dp.get('waste_proxy_mean_g'))} г/день, доля дней "
                         f"нехватки {_fmt_num(dp.get('shortfall_rate'), 2)}.")
    kp = f.get("kitchen_policy")
    if kp:
        lines.append(f"Базовая линия кухни (hindsight, НЕ прогноз): неявная ошибка "
                     f"{_fmt_num(kp.get('mae_implicit_g'))} г, переготовка "
                     f"{_fmt_num(kp.get('overproduction_proxy_mean_g'))} г/день.")
    for note in f.get("notes", [])[:2]:
        lines.append(f"примечание: {note}")
    return "\n".join(lines)


def explain_monitoring(m: dict) -> str:
    if m.get("status") == "DATA_NOT_READY":
        return (f"=== Мониторинг ===\nСтатус: DATA_NOT_READY. {m.get('reason', '')}. "
                "Помощник не знает того, чего нет в разрешённых данных.")
    lines = ["=== Мониторинг качества (shadow) ===",
             f"Статус: {m.get('status')}" + (f". {m['reason']}" if m.get("reason") else ""),
             f"MAE последних дней = {_fmt_num(m.get('mae_recent_g'))} г "
             f"(k={m.get('recent_k')}); покрытие интервала = "
             f"{_fmt_num(m.get('interval_coverage'), 3)} при номинале "
             f"{m.get('nominal_coverage')} (гарантий нет); дрейф: "
             f"{(m.get('drift') or {}).get('status')}; "
             f"качественных дней: {m.get('n_quality_days')}."]
    if m.get("cost_delta_vs_actual_mean") is not None:
        lines.append(f"Средний контрфакт фактического дня = "
                     f"{_fmt_num(m['cost_delta_vs_actual_mean'], 2)} (SIMULATED, "
                     "не измеренная экономия).")
    lines.append("DRIFT_DETECTED автоматически отключает предложения — это "
                 "свойство ядра, а не мнение помощника.")
    return "\n".join(lines)


def explain_shadow(s: dict) -> str:
    if s.get("status") != "OK":
        return f"=== SHADOW ===\nСтатус: {s.get('status')}. {s.get('reason', '')}"
    a = s.get("actions") or {}
    return ("\n".join([
        "=== SHADOW-контур ===",
        f"Окно {s.get('window')}: дней {s.get('days')}, записей всего "
        f"{s.get('records_total')} (в этот прогон вставлено {s.get('inserted')}).",
        f"Действий предложено: ADJUST_PREPARED_VOLUME = {a.get('ADJUST_PREPARED_VOLUME', 0)}, "
        f"NO_CHANGE = {a.get('NO_CHANGE', 0)}.",
        "Исполнений нет: кухня не изменялась; записи SIMULATED.",
    ]))


def explain_plan(plan: dict) -> str:
    if plan.get("status") != "DRAFT_REQUIRES_APPROVAL":
        return (f"=== План оценки ===\nСтатус: {plan.get('status')}. "
                f"{plan.get('reason', '')}")
    return "\n".join([
        "=== Проект плана оценки эффекта ===",
        f"Дизайн: {plan.get('design')}; эффект {plan.get('effect_g_per_day')} г/день; "
        f"n на группу: {plan.get('n_per_arm')} (всего сессий {plan.get('sessions_total')}, "
        f"≈{plan.get('estimated_weeks_min')} недель).",
        f"σ интерпретация: {plan.get('sigma_interpretation')}.",
        "Статус DRAFT_REQUIRES_APPROVAL: план НЕ является разрешением на "
        "вмешательство; критерии остановки задаёт проверенная политика школы.",
    ])


def explain_school_overview(facts: dict) -> str:
    """Рендер пакета фактов sra.assistant.tools.school_overview."""
    lines = [f"=== Статус школы {facts.get('school_id')} "
             f"(среда {facts.get('environment')}, версия данных "
             f"{facts.get('dataset_version')}) ==="]
    c = facts.get("counts") or {}
    ev = c.get("evidence_status_counts") or {}
    lines.append(f"Данные: сессий {c.get('sessions', 0)}, наблюдений "
                 f"{c.get('observations', 0)}; статусы происхождения: "
                 f"{'; '.join(f'{k}={v}' for k, v in sorted(ev.items())) or 'нет'}.")
    phase = facts.get("phase")
    lines.append("Фаза исследования: " + (f"{phase['phase']} (с {phase['decided_at']})"
                                          if phase else "не начата"))
    if facts.get("metrics"):
        lines.append(explain_metrics_report(facts["metrics"]))
    if facts.get("monitoring"):
        lines.append(explain_monitoring(facts["monitoring"]))
    ls = facts.get("last_shadow")
    if ls:
        lines.append(f"Последний shadow-день: {ls.get('target_date')}, действие "
                     f"{ls.get('action')} (запись SIMULATED, исполнений нет).")
    lines.append("Помощник не утверждает действия и не пересчитывает метрики; "
                 "все значения выданы сервером из хранилища.")
    return "\n".join(lines)
