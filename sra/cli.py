"""CLI School Resource Autopilot (python -m sra).

Команды этапа 1:
  init-db     — создать хранилище (обязательно указать --environment real|demo)
  import      — импорт CSV (sessions|observations), атомарный батч, валидация до записи
  metrics     — серверный отчёт метрик по школе и периоду
  verify      — процедура VERIFIED для реальной записи (verifier, method, source)
  revise      — ревизия записи (причина + новая recorded_at; VERIFIED -> повторная проверка)
  policies    — регистрация/проверка/поиск политик
  decide      — предложение действия (этап 1: всегда NO_CHANGE с причинами)
  forecast    — хронологический backtest базовых прогнозов (этап 3; DATA_NOT_READY при нехватке данных)
  selfcheck   — проверка инвариантов хранилища
  audit       — хвост журнала аудита
  demo        — изолированное синтетическое демо (DEMO-среда)

Отсутствие команды эксплуатации (реальное вмешательство) — намеренное:
условия перехода в OPERATIONAL не выполнены (см. docs/ARCHITECTURE_AND_GATES.md).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import enums, metrics
from .csvio import parse_file
from .decide import propose_action
from .errors import SraError
from .policies import PolicyRegistry
from .store import EvidenceStore
from .validation import (validate_observations, validate_sessions)


def _print(data) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2, default=str))


def _open_db(args) -> EvidenceStore:
    return EvidenceStore(args.db, args.environment)


def cmd_init_db(args) -> int:
    store = EvidenceStore(args.db, args.environment)
    stats = store.stats()
    store.close()
    _print({"created": str(Path(args.db)), "environment": args.environment, "stats": stats})
    return 0


def cmd_import(args) -> int:
    store = EvidenceStore(args.db, args.environment)
    try:
        records = parse_file(args.file, args.kind, unit=args.unit)
        if not records:
            raise SraError("файл не содержит строк данных (только заголовок)",
                           code="E_NO_DATA_ROWS")
        if args.kind == "sessions":
            report = validate_sessions(records, existing_ids=store.session_ids())
        else:
            report = validate_observations(
                records, known_sessions=store.session_ids(),
                existing_observation_ids=store.observation_ids(),
                existing_pairs=store.observation_pairs())
        if not report.ok:
            _print({
                "imported": 0, "dataset_version": None,
                "rejected_reasons": [i.to_dict() for i in report.errors],
                "warnings": [i.to_dict() for i in report.warnings],
                "note": "импорт атомарен: батч с ошибками не записан",
            })
            return 2
        if args.kind == "sessions":
            result = store.import_sessions(report.accepted, actor=args.actor,
                                           import_meta={"file": str(args.file), "unit": args.unit})
        else:
            result = store.import_observations(report.accepted, actor=args.actor,
                                               import_meta={"file": str(args.file), "unit": args.unit})
        _print({**result,
                "warnings": [i.to_dict() for i in report.warnings]})
        return 0
    finally:
        store.close()


def cmd_metrics(args) -> int:
    store = EvidenceStore(args.db, args.environment)
    try:
        sessions = store.fetch_sessions(school_id=args.school_id,
                                        date_from=args.date_from, date_to=args.date_to)
        observations = store.fetch_observations(school_id=args.school_id,
                                                date_from=args.date_from, date_to=args.date_to)
        cost_source = None
        if args.cost_policy:
            reg = PolicyRegistry(store)
            rows = reg.find_applicable(jurisdiction=args.jurisdiction,
                                       on_date=args.date_to or "9999-12-31",
                                       require_verified=False)
            row = next((r for r in rows if r["policy_id"] == args.cost_policy), None)
            if row is None:
                raise SraError(f"политика {args.cost_policy} не найдена/не действует",
                               code="E_POLICY_NOT_FOUND")
            payload = json.loads(row["payload"])
            nature = payload.get("data_nature")
            cost_source = metrics.CostSource(
                policy_id=row["policy_id"], cost_per_kg=payload["values"]["cost_per_kg"],
                currency=payload["values"].get("currency", "?"),
                evidence_status=(enums.VERIFIED
                                 if row["verification_status"] == enums.POLICY_VERIFIED and nature == "REAL_OBSERVED"
                                 else enums.SYNTHETIC))
        report = metrics.build_metric_report(sessions, observations, cost_source=cost_source)
        report["environment"] = store.environment
        report["provenance"] = {"db": str(args.db), "school_id": args.school_id,
                                "date_from": args.date_from, "date_to": args.date_to,
                                "stats": store.stats()}
        _print(report)
        return 0
    finally:
        store.close()


def cmd_verify(args) -> int:
    store = EvidenceStore(args.db, args.environment)
    try:
        result = store.verify(args.entity, args.id, verifier=args.verifier, method=args.method,
                              source_ref=args.source_ref, actor=args.actor, notes=args.notes)
        _print(result)
        return 0
    finally:
        store.close()


def cmd_revise(args) -> int:
    changes = {}
    for pair in args.set or []:
        if "=" not in pair:
            raise SraError(f"--set ожидает FIELD=VALUE, получено {pair!r}", code="E_CLI_ARGS")
        k, v = pair.split("=", 1)
        try:
            changes[k] = json.loads(v)
        except json.JSONDecodeError:
            changes[k] = v
    store = EvidenceStore(args.db, args.environment)
    try:
        result = store.revise(args.entity, args.id, changes, reason=args.reason,
                              actor=args.actor, recorded_at=args.recorded_at)
        _print(result)
        return 0
    finally:
        store.close()


def cmd_policies(args) -> int:
    store = EvidenceStore(args.db, args.environment)
    try:
        reg = PolicyRegistry(store)
        if args.action == "register":
            payload = json.loads(args.payload) if args.payload else {}
            _print(reg.register(policy_id=args.policy_id, version=args.version,
                                jurisdiction=args.jurisdiction, source_ref=args.source_ref,
                                valid_from=args.valid_from, valid_until=args.valid_until,
                                payload=payload, actor=args.actor,
                                data_nature=args.data_nature))
        elif args.action == "verify":
            _print(reg.verify_policy(args.policy_id, args.version, verifier=args.verifier,
                                     method=args.method, actor=args.actor))
        elif args.action == "find":
            _print(reg.find_applicable(jurisdiction=args.jurisdiction, on_date=args.on_date,
                                       require_verified=not args.include_unverified))
        return 0
    finally:
        store.close()


def cmd_decide(args) -> int:
    store = EvidenceStore(args.db, args.environment)
    try:
        policies = PolicyRegistry(store)
        proposal = propose_action(store, policies, school_id=args.school_id,
                                  target_date=args.target_date,
                                  jurisdiction=args.jurisdiction,
                                  requested_mode=args.mode).to_dict()
        _print(proposal)
        return 0
    finally:
        store.close()


def cmd_forecast(args) -> int:
    from sra.forecasting.backtest import run_backtest
    from sra.forecasting.series import make_snapshot_fn
    store = EvidenceStore(args.db, args.environment)
    try:
        # Часовой пояс школы — из её сессий (не пояс сервера); иначе --timezone.
        sessions = store.fetch_sessions(school_id=args.school_id)
        tz_name = args.timezone or (sessions[-1].timezone if sessions else "Asia/Almaty")
        report = run_backtest(
            snapshot_fn=make_snapshot_fn(store, args.school_id, args.meal_type),
            test_days=args.test_days, calib_days=args.calib_days,
            min_train=args.min_train, decision_hour=args.decision_hour,
            tz_name=tz_name, seasonal_weeks=args.seasonal_weeks,
            moving_avg_window=args.window,
            exclude_censored=not args.include_censored_in_train,
            environment=store.environment)
        cards = []
        if not args.no_save_cards and report["status"] == enums.METRIC_OK:
            for m in report.get("models", []):
                if m.get("status") != enums.FORECAST_OK:
                    continue
                card = store.save_model_card(
                    model_id=f"sra.forecast.{m['name']}", model_name=m["name"],
                    params=m["params"],
                    data_meta={"school_id": args.school_id, "meal_type": args.meal_type,
                               "windows": report["windows"],
                               "dataset_version": store.get_dataset_version()},
                    metrics={"mae_g": m.get("mae_g"), "pinball": m.get("pinball"),
                             "interval": m.get("interval"),
                             "decision_proxy": m.get("decision_proxy"),
                             "drift": m.get("drift")},
                    actor=args.actor)
                cards.append(card)
        report["model_cards"] = cards
        report["school_timezone"] = tz_name
        _print(report)
        return 0
    finally:
        store.close()


def cmd_selfcheck(args) -> int:
    store = EvidenceStore(args.db, args.environment)
    try:
        results = store.selfcheck()
        ok = all(r["ok"] for r in results)
        _print({"ok": ok, "checks": results})
        return 0 if ok else 3
    finally:
        store.close()


def cmd_audit(args) -> int:
    store = EvidenceStore(args.db, args.environment)
    try:
        _print(store.audit_tail(args.tail))
        return 0
    finally:
        store.close()


def cmd_demo(args) -> int:
    from .demo.run import run_demo
    report = run_demo(args.dir, seed=args.seed, n_days=args.days, reset=args.reset)
    _print({
        "data_nature": report["data_nature"],
        "environment": report["environment"],
        "warnings": report["warnings"],
        "manifest": report["manifest"],
        "validation": report["validation"],
        "metric_report": report["metric_report"],
        "decision": report["decision"],
        "forecast_status": report["forecast"]["status"],
        "forecast_models": [m["name"] for m in report["forecast"].get("models", [])],
        "selfcheck_ok": all(r["ok"] for r in report["selfcheck"]),
        "report_file": str(Path(args.dir) / "demo_report.json"),
    })
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="sra", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    def db_args(sp, env_required=True):
        sp.add_argument("--db", required=True, help="путь к SQLite-файлу хранилища")
        sp.add_argument("--environment", required=env_required, type=lambda s: s.upper(),
                        choices=sorted(enums.ENVIRONMENTS),
                        help="среда: real | demo (регистр не важен; должна совпадать с маркером БД)")

    sp = sub.add_parser("init-db", help="создать хранилище")
    db_args(sp)
    sp.set_defaults(func=cmd_init_db)

    sp = sub.add_parser("import", help="импортировать CSV (sessions|observations)")
    db_args(sp)
    sp.add_argument("--kind", required=True, choices=["sessions", "observations"])
    sp.add_argument("--file", required=True)
    sp.add_argument("--actor", required=True, help="кто выполняет импорт (аудит)")
    sp.add_argument("--unit", default="g", choices=["g", "kg"],
                    help="единица масс в файле; kg конвертируется с пометкой в quality_flags")
    sp.set_defaults(func=cmd_import)

    sp = sub.add_parser("metrics", help="авторитетные метрики по школе/периоду")
    db_args(sp)
    sp.add_argument("--school-id", required=True)
    sp.add_argument("--date-from", default=None)
    sp.add_argument("--date-to", default=None)
    sp.add_argument("--cost-policy", default=None,
                    help="policy_id источника стоимости (иначе CostWastePerMeal недоступен)")
    sp.add_argument("--jurisdiction", default="KZ")
    sp.set_defaults(func=cmd_metrics)

    sp = sub.add_parser("verify", help="процедура VERIFIED для записи")
    db_args(sp)
    sp.add_argument("--entity", required=True, choices=["session", "observation"])
    sp.add_argument("--id", required=True)
    sp.add_argument("--verifier", required=True)
    sp.add_argument("--method", required=True)
    sp.add_argument("--source-ref", required=True)
    sp.add_argument("--notes", default=None)
    sp.add_argument("--actor", required=True)
    sp.set_defaults(func=cmd_verify)

    sp = sub.add_parser("revise", help="создать ревизию записи")
    db_args(sp)
    sp.add_argument("--entity", required=True, choices=["session", "observation"])
    sp.add_argument("--id", required=True)
    sp.add_argument("--set", action="append", help="FIELD=VALUE, повторяемо")
    sp.add_argument("--reason", required=True)
    sp.add_argument("--recorded-at", required=True,
                    help="момент появления исправленных данных (ISO с offset)")
    sp.add_argument("--actor", required=True)
    sp.set_defaults(func=cmd_revise)

    sp = sub.add_parser("policies", help="реестр политик")
    db_args(sp)
    sp.add_argument("--action", required=True, choices=["register", "verify", "find"])
    sp.add_argument("--policy-id")
    sp.add_argument("--version", type=int, default=1)
    sp.add_argument("--jurisdiction", default="KZ")
    sp.add_argument("--source-ref")
    sp.add_argument("--valid-from", default="2000-01-01")
    sp.add_argument("--valid-until", default="2099-12-31")
    sp.add_argument("--payload", help="JSON значений политики")
    sp.add_argument("--data-nature", choices=["REAL_OBSERVED", "SYNTHETIC"],
                    default="REAL_OBSERVED")
    sp.add_argument("--verifier")
    sp.add_argument("--method")
    sp.add_argument("--on-date")
    sp.add_argument("--include-unverified", action="store_true")
    sp.add_argument("--actor")
    sp.set_defaults(func=cmd_policies)

    sp = sub.add_parser("decide", help="предложение действия (этап 1: NO_CHANGE)")
    db_args(sp)
    sp.add_argument("--school-id", required=True)
    sp.add_argument("--target-date", required=True)
    sp.add_argument("--jurisdiction", default="KZ")
    sp.add_argument("--mode", choices=sorted(enums.DECISION_MODES), default=None)
    sp.set_defaults(func=cmd_decide)

    sp = sub.add_parser("forecast", help="backtest базовых прогнозов (этап 3)")
    db_args(sp)
    sp.add_argument("--school-id", required=True)
    sp.add_argument("--meal-type", default="LUNCH")
    sp.add_argument("--test-days", type=int, default=10)
    sp.add_argument("--calib-days", type=int, default=8)
    sp.add_argument("--min-train", type=int, default=10)
    sp.add_argument("--decision-hour", type=int, default=18,
                    help="час решения D-1 по школьному времени")
    sp.add_argument("--seasonal-weeks", type=int, default=3)
    sp.add_argument("--window", type=int, default=4, help="окно скользящего среднего")
    sp.add_argument("--include-censored-in-train", action="store_true",
                    help="по умолчанию цензурированные дни исключены из обучения")
    sp.add_argument("--timezone", default=None, help="иначе берётся из сессий школы")
    sp.add_argument("--no-save-cards", action="store_true")
    sp.add_argument("--actor", default="forecaster")
    sp.set_defaults(func=cmd_forecast)

    sp = sub.add_parser("selfcheck", help="проверка инвариантов хранилища")
    db_args(sp)
    sp.set_defaults(func=cmd_selfcheck)

    sp = sub.add_parser("audit", help="хвост журнала аудита")
    db_args(sp)
    sp.add_argument("--tail", type=int, default=50)
    sp.set_defaults(func=cmd_audit)

    sp = sub.add_parser("demo", help="изолированное синтетическое демо")
    sp.add_argument("--dir", default="data/demo")
    sp.add_argument("--seed", type=int, default=42)
    sp.add_argument("--days", type=int, default=60)
    sp.add_argument("--reset", action="store_true",
                    help="удалить файлы демо в каталоге и пересоздать (реальные данные не затрагивает)")
    sp.set_defaults(func=cmd_demo)
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except SraError as e:
        _print({"error": e.to_dict()})
        return 2
    except RuntimeError as e:
        _print({"error": {"code": "RUNTIME", "message": str(e)}})
        return 2


if __name__ == "__main__":
    sys.exit(main())
