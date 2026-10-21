"""Изолированное демо: генерация SYNTHETIC → импорт в DEMO-БД → метрики → NO_CHANGE.

Изоляция:
  - демо живёт только в указанном каталоге (--dir, по умолчанию data/demo);
  - БД демо создаётся с маркером среды DEMO; REAL-данные в неё не проходят
    (guard в EvidenceStore), SYNTHETIC не проходят в REAL-хранилище;
  - reset удаляет ТОЛЬКО файлы демо с фиксированными именами в каталоге демо;
    реальные данные (любые другие пути/файлы) не затрагиваются.
"""

from __future__ import annotations

import json
from pathlib import Path

from .. import enums, metrics, validation
from ..decide import propose_action
from ..policies import PolicyRegistry
from ..store import EvidenceStore
from .synthetic import generate

DEMO_FILES = ("demo.sqlite3", "sessions.csv", "observations.csv", "manifest.json", "demo_report.json")

# Синтетическая политика стоимости для демонстрации ветки CostWastePerMeal.
# Значения ИСКУССТВЕННЫЕ; политика остаётся UNVERIFIED и не даёт реальных прав.
DEMO_COST_POLICY = {
    "policy_id": "POLICY-DEMO-COST-001",
    "version": 1,
    "jurisdiction": "DEMO",
    "payload": {"cost_per_kg": 500.0, "currency": "KZT_DEMO"},
}


def run_demo(out_dir: str | Path = "data/demo", *, seed: int = 42, n_days: int = 60,
             reset: bool = False) -> dict:
    out = Path(out_dir)
    db_path = out / "demo.sqlite3"
    if db_path.exists():
        if not reset:
            raise RuntimeError(
                f"демо уже создано в {out}; используйте --reset (удалит только файлы демо: "
                f"{', '.join(DEMO_FILES)})")
        _reset_demo(out)

    # 1. Генерация синтетики (детерминированная).
    manifest = generate(out, seed=seed, n_days=n_days)

    # 2. DEMO-хранилище (маркер среды DEMO; реальные данные сюда не проходят).
    store = EvidenceStore(db_path, enums.ENV_DEMO)

    # 3. Импорт + валидация (валидация идёт ДО записи; батч атомарен).
    session_recs = _validated_import(store, out / "sessions.csv", "sessions", actor="demo")
    obs_recs = _validated_import(store, out / "observations.csv", "observations", actor="demo")

    # 4. Явно обозначенная синтетическая политика стоимости (остаётся UNVERIFIED).
    policies = PolicyRegistry(store)
    policies.register(
        policy_id=DEMO_COST_POLICY["policy_id"], version=DEMO_COST_POLICY["version"],
        jurisdiction=DEMO_COST_POLICY["jurisdiction"], source_ref=manifest["source_ref"],
        valid_from="2000-01-01", valid_until="2099-12-31",
        payload=DEMO_COST_POLICY["payload"], actor="demo", data_nature=enums.SYNTHETIC)
    cost_source = metrics.CostSource(
        policy_id=DEMO_COST_POLICY["policy_id"],
        cost_per_kg=DEMO_COST_POLICY["payload"]["cost_per_kg"],
        currency=DEMO_COST_POLICY["payload"]["currency"],
        evidence_status=enums.SYNTHETIC)

    # 5. Метрики по всей области демо (SIMULATED, т.к. входы SYNTHETIC).
    sessions = store.fetch_sessions(school_id=manifest["school_id"])
    observations = store.fetch_observations(school_id=manifest["school_id"])
    metric_report = metrics.build_metric_report(sessions, observations, cost_source=cost_source)

    # 6. Решение: защитный чек-лист -> NO_CHANGE с причинами.
    proposal = propose_action(store, policies, school_id=manifest["school_id"],
                              target_date=sessions[-1].service_date.isoformat()).to_dict()

    # 7. Самопроверка инвариантов хранилища.
    selfcheck = store.selfcheck()

    report = {
        "data_nature": enums.SYNTHETIC,
        "environment": enums.ENV_DEMO,
        "mode": enums.MODE_DEMO,
        "warnings": [
            "ДЕМО: все данные SYNTHETIC, метрики SIMULATED; это проверка кода, "
            "а не польза в реальной школе",
            "результат решения NO_CHANGE: в этапе 1 нет модели, проверенных политик "
            "и одобрений; предложение действия не формируется",
            "качество на синтетике не переносится на реальные школы",
        ],
        "manifest": manifest,
        "validation": {"sessions": session_recs, "observations": obs_recs},
        "metric_report": metric_report,
        "decision": proposal,
        "selfcheck": selfcheck,
    }
    (out / "demo_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    store.close()
    return report


def _validated_import(store: EvidenceStore, path: Path, kind: str, *, actor: str) -> dict:
    from ..csvio import parse_file
    records = parse_file(path, kind)
    if kind == "sessions":
        report = validation.validate_sessions(records, existing_ids=store.session_ids())
        if not report.ok:
            raise RuntimeError(f"валидация {kind} не прошла в демо: "
                               f"{[i.to_dict() for i in report.errors][:3]}")
        return store.import_sessions(report.accepted, actor=actor,
                                     import_meta={"source": "synthetic_generator"})
    report = validation.validate_observations(
        records, known_sessions=store.session_ids(),
        existing_observation_ids=store.observation_ids(),
        existing_pairs=store.observation_pairs())
    if not report.ok:
        raise RuntimeError(f"валидация {kind} не прошла в демо: "
                           f"{[i.to_dict() for i in report.errors][:3]}")
    return store.import_observations(report.accepted, actor=actor,
                                     import_meta={"source": "synthetic_generator"})


def reset_demo(out_dir: str | Path = "data/demo") -> list[str]:
    """Удаляет ТОЛЬКО файлы демо с фиксированными именами. Реальные данные не затрагивает."""
    out = Path(out_dir)
    removed = []
    for name in DEMO_FILES:
        p = out / name
        if p.exists():
            p.unlink()
            removed.append(name)
    return removed


def _reset_demo(out: Path) -> None:
    reset_demo(out)
