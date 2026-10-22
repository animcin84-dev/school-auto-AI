"""Ограниченные read-only инструменты для НЕОБЯЗАТЕЛЬНОГО языкового помощника.

Контракт безопасности (раздел 10 задания):
  - ни один вызов внешних сервисов или LLM здесь не выполняется: модуль только
    собирает разрешённые факты из хранилища;
  - инструменты только читают; каждое обращение пишется в аудит (ASSISTANT_READ);
  - нет произвольного SQL, путей файловой системы, исполнения действий и
    согласований: аргументы строго валидируются по именованной схеме;
  - изоляция школы: существование и принадлежность сущности сверяются с
    хранилищем; чужая школа → отказ E_SCOPE;
  - при отсутствии доказательств инструмент возвращает структурированный
    отказ — помощник обязан сказать «я этого не знаю».

Ядро (метрики, прогнозы, решения) работает без этого модуля: отказ
необязательного помощника не блокирует расчёты.
"""

from __future__ import annotations

import json

from .. import enums
from ..explain import quote_untrusted
from ..forecasting.series import build_target_series
from ..monitoring import monitor_shadow
from ..phases import INTERVENTION_CONDITIONS, PHASE_ORDER
from ..errors import SraError


class ToolRefusal(Exception):
    """Структурированный отказ инструмента (не исключение ошибки данных)."""

    def __init__(self, code: str, message: str, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "details": self.details}


# --- Схемы аргументов: {имя: (тип, обязательность, значение_по_умолчанию)} ----
ARG_SPEC = {
    "school_overview": {"school_id": (str, True, None), "meal_type": (str, False, "LUNCH")},
    "metrics_report": {"school_id": (str, True, None), "date_from": (str, False, None),
                       "date_to": (str, False, None)},
    "phase_status": {"school_id": (str, True, None)},
    "monitoring_status": {"school_id": (str, True, None), "meal_type": (str, False, "LUNCH")},
    "recent_shadow": {"school_id": (str, True, None), "meal_type": (str, False, "LUNCH"),
                      "limit": (int, False, 5)},
    "record_details": {"school_id": (str, True, None), "entity_type": (str, True, None),
                       "entity_id": (str, True, None)},
    "ml_status": {"school_id": (str, True, None), "meal_type": (str, False, "LUNCH")},
    "nutrition_digest": {"school_id": (str, True, None), "meal_type": (str, False, "LUNCH"),
                         "days": (int, False, 7)},
}

TOOL_DESCRIPTIONS = {
    "school_overview": "Сводка школы: счётчики, происхождение, фаза, авторитетные метрики, мониторинг.",
    "metrics_report": "Авторитетный отчёт метрик по школе и периоду (расчёт сервера).",
    "phase_status": "Текущая фаза исследования и требования следующего перехода.",
    "monitoring_status": "Мониторинг качества прогнозов по shadow-записям.",
    "recent_shadow": "Последние shadow-дни (без payload; ядро полей стабильно).",
    "record_details": "Содержимое сессии/наблюдения: payload, статусы, флаги (notes — недоверенный текст).",
    "ml_status": "Read-only ML-сводка: достаточность данных для backtest'ов, карточки моделей, решение gate.",
    "nutrition_digest": "Дайджест питания за окно: метрики окна, качество, порции vs Санправила, алерты.",
}

MAX_SHADOW_LIMIT = 20


def _validate_args(tool: str, args: dict) -> dict:
    spec = ARG_SPEC.get(tool)
    if spec is None:
        raise ToolRefusal("UNKNOWN_TOOL",
                          f"инструмент {tool!r} не входит в разрешённый список",
                          {"allowed": sorted(ARG_SPEC)})
    problems = []
    for key, value in (args or {}).items():
        if key not in spec:
            problems.append(f"неизвестный аргумент {key!r}")
    clean: dict = {}
    for key, (vtype, required, default) in spec.items():
        if key not in args or args[key] is None:
            if required:
                problems.append(f"отсутствует обязательный аргумент {key!r}")
            else:
                clean[key] = default
            continue
        value = args[key]
        if vtype is int:
            # bool — подкласс int; явно отклоняем
            if isinstance(value, bool) or not isinstance(value, int):
                problems.append(f"аргумент {key!r} должен быть целым числом")
                continue
        elif not isinstance(value, vtype):
            problems.append(f"аргумент {key!r} должен быть {vtype.__name__}")
            continue
        clean[key] = value
    if problems:
        raise ToolRefusal("E_BAD_TOOL_ARGS", "; ".join(problems), {"tool": tool})
    return clean


def _require_school(store, school_id: str) -> None:
    row = store._conn.execute("SELECT school_id FROM schools WHERE school_id = ?",
                              (school_id,)).fetchone()
    if row is None:
        raise ToolRefusal(
            "NO_SUCH_SCHOOL",
            f"школа {school_id!r} отсутствует в разрешённых данных этого хранилища")


def _tool_school_overview(store, policies, args: dict) -> dict:
    school_id = args["school_id"]
    meal_type = args["meal_type"]
    sessions = store.fetch_sessions(school_id=school_id)
    observations = store.fetch_observations(school_id=school_id)
    from ..metrics import build_metric_report
    metrics_rep = build_metric_report(sessions, observations)
    phase = store.get_school_phase(school_id)
    mon = monitor_shadow(store, school_id, meal_type)
    shadow = store.list_shadow_records(school_id, meal_type)
    last = None
    if shadow:
        r = shadow[-1]
        last = {k: r[k] for k in ("target_date", "action", "planned_g", "baseline_g",
                                  "pred_g", "y_g", "actual_prepared_g")}
    return {
        "school_id": school_id, "meal_type": meal_type,
        "environment": store.environment,
        "dataset_version": store.get_dataset_version(),
        "counts": {"sessions": len(sessions), "observations": len(observations),
                   "evidence_status_counts": store.stats()["evidence_status_counts"]},
        "phase": phase,
        "metrics": metrics_rep,
        "monitoring": mon,
        "last_shadow": last,
        "target_series": {
            "n_points": len(build_target_series(store, school_id, meal_type).points),
            "meal_type": meal_type,
        },
    }


def _tool_metrics_report(store, policies, args: dict) -> dict:
    from ..metrics import build_metric_report
    school_id = args["school_id"]
    sessions = store.fetch_sessions(school_id=school_id, date_from=args["date_from"],
                                    date_to=args["date_to"])
    observations = store.fetch_observations(school_id=school_id,
                                            date_from=args["date_from"],
                                            date_to=args["date_to"])
    if not sessions and not observations:
        raise ToolRefusal(
            "NO_DATA",
            f"в разрешённых данных нет записей школы {school_id!r} за указанный период",
            {"date_from": args["date_from"], "date_to": args["date_to"]})
    rep = build_metric_report(sessions, observations)
    rep["school_id"] = school_id
    return rep


def _tool_phase_status(store, policies, args: dict) -> dict:
    school_id = args["school_id"]
    phase = store.get_school_phase(school_id)
    if phase is None:
        nxt = PHASE_ORDER[0]
        return {"school_id": school_id, "phase": None, "next_phase": nxt,
                "note": "фаз нет; первый шаг — " + nxt}
    idx = PHASE_ORDER.index(phase["phase"])
    nxt = PHASE_ORDER[idx + 1] if idx + 1 < len(PHASE_ORDER) else None
    out = {"school_id": school_id, "phase": phase["phase"],
           "decided_at": phase["decided_at"], "protocol_ref": phase["protocol_ref"],
           "next_phase": nxt}
    if nxt in (enums.PHASE_INTERVENTION, enums.PHASE_FOLLOWUP):
        out["blocking_conditions"] = INTERVENTION_CONDITIONS
        out["note"] = "следующая фаза заблокирована до выполнения всех условий"
    return out


def _tool_monitoring_status(store, policies, args: dict) -> dict:
    return monitor_shadow(store, args["school_id"], args["meal_type"])


def _tool_recent_shadow(store, policies, args: dict) -> dict:
    school_id = args["school_id"]
    limit = min(int(args["limit"]), MAX_SHADOW_LIMIT)
    records = store.list_shadow_records(school_id, args["meal_type"])[-limit:]
    out = []
    for r in records:
        out.append({k: r[k] for k in ("target_date", "action", "planned_g", "baseline_g",
                                      "pred_g", "pred_q10", "pred_q90", "y_g",
                                      "actual_prepared_g", "cost_delta_vs_actual")})
    if not out:
        raise ToolRefusal("NO_DATA", "shadow-записей нет; их создаёт shadow-run")
    return {"school_id": school_id, "meal_type": args["meal_type"], "records": out,
            "note": "SIMULATED-записи; cost_delta — контрфакт фактического дня, "
                    "не измеренная экономия"}


def _tool_ml_status(store, policies, args: dict) -> dict:
    from ..status import ml_status
    st = ml_status(store, args["school_id"], meal_type=args["meal_type"])
    st["note"] = ("read-only: сводка не считает backtest'ы заново и ничего "
                  "не пишет; вердиктов о будущей точности не даёт")
    return st


def _tool_nutrition_digest(store, policies, args: dict) -> dict:
    days = min(int(args["days"]), 31)
    from ..digest import build_digest
    d = build_digest(store, args["school_id"], meal_type=args["meal_type"],
                     days=days)
    # компактная версия для помощника: без полного ml_status и quality-деталей
    return {
        "read_only": True, "school_id": d["school_id"],
        "window": d["window"], "window_metric": d["window_metric"],
        "quality": d["quality"], "norms_status": d["norms"].get("status"),
        "norms_dishes": [x for x in d["norms"].get("dishes", [])
                         if x.get("status") == "WARN"],
        "alerts": d["alerts"],
        "ml_brief": {"diners_sufficiency": d["ml_status"]["diners_sufficiency"],
                     "composite_sufficiency":
                         d["ml_status"]["composite_sufficiency"],
                     "warnings": d["ml_status"]["warnings"]},
        "disclaimer": d["disclaimer"],
    }


def _tool_record_details(store, policies, args: dict) -> dict:
    school_id = args["school_id"]
    entity_type = args["entity_type"]
    entity_id = args["entity_id"]
    if entity_type not in ("session", "observation"):
        raise ToolRefusal("E_BAD_TOOL_ARGS",
                          "entity_type должен быть 'session' или 'observation'")
    try:
        current = store.get_current(entity_type, entity_id)
    except SraError:
        raise ToolRefusal("NO_SUCH_RECORD",
                          f"{entity_type} {entity_id!r} не найден в разрешённых данных")
    payload = current["payload"]
    if entity_type == "session":
        owner = payload.get("school_id")
    else:
        session = store._conn.execute(
            "SELECT school_id FROM sessions_current WHERE entity_id = ?",
            (payload.get("session_id"),)).fetchone()
        owner = session["school_id"] if session else None
    if owner != school_id:
        raise ToolRefusal(
            "E_SCOPE",
            f"запись {entity_id!r} принадлежит другой школе; доступ запрещён")
    payload = dict(payload)
    if payload.get("notes"):
        payload["notes"] = quote_untrusted(str(payload["notes"]))
    return {
        "entity_type": entity_type, "entity_id": entity_id, "school_id": owner,
        "evidence_status": current["evidence_status"],
        "revision_id": current["revision_id"],
        "pending_verification": current["pending_verification"],
        "quality_flags": current["quality_flags"],
        "payload": payload,
        "note": "поле notes (если было) обёрнуто маркером недоверенного текста",
    }


_TOOL_FNS = {
    "school_overview": _tool_school_overview,
    "metrics_report": _tool_metrics_report,
    "phase_status": _tool_phase_status,
    "monitoring_status": _tool_monitoring_status,
    "recent_shadow": _tool_recent_shadow,
    "record_details": _tool_record_details,
    "ml_status": _tool_ml_status,
    "nutrition_digest": _tool_nutrition_digest,
}


def call_tool(store, policies, name: str, args: dict | None, *, actor: str = "assistant") -> dict:
    """Единая точка вызова: валидация → функция → аудит (включая отказы).

    Возвращает {"ok": True, "tool": ..., "facts": ...} либо
    {"ok": False, "tool": ..., "refusal": {...}}.
    """
    result: dict
    try:
        clean = _validate_args(name, args or {})
        if "school_id" in clean:
            _require_school(store, clean["school_id"])
        facts = _TOOL_FNS[name](store, policies, clean)
        result = {"ok": True, "tool": name, "facts": facts}
    except ToolRefusal as e:
        result = {"ok": False, "tool": name, "refusal": e.to_dict()}
    except SraError as e:
        result = {"ok": False, "tool": name,
                  "refusal": {"code": "TOOL_ERROR", "message": str(e), "details": {}}}
    with store._conn:
        store._conn.execute(
            "INSERT INTO audit_events(ts, actor, action, entity_type, entity_id, revision_id, "
            "details) VALUES (?,?,?,?,?,?,?)",
            (store._clock(), actor, "ASSISTANT_READ", "tool", name, None,
             json.dumps({"ok": result["ok"],
                         "school": (args or {}).get("school_id"),
                         "refusal": result.get("refusal", {}).get("code")},
                        ensure_ascii=False)),
        )
    return result
