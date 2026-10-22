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
  optimize    — симуляционное предложение изменения объёма (этап 4; REAL => NO_CHANGE)
  compare     — сравнение стратегий управления объёмом (SIMULATED)
  phase       — фазы исследования школы (ворота переходов; INTERVENTION заблокирован)
  shadow-run  — SHADOW-контур: симуляционные предложения по окну дней без исполнения
  monitor     — мониторинг качества прогнозов по shadow-записям
  design      — проект плана оценки эффекта (требуемый объём выборки)
  explain     — детерминированное русскоязычное объяснение статуса школы
  assistant-tools  — список разрешённых read-only инструментов помощника
  assistant-call   — вызов инструмента помощника (валидация, изоляция, аудит)
  selfcheck   — проверка инвариантов хранилища
  audit       — хвост журнала аудита
  demo        — изолированное синтетическое демо (DEMO-среда)
  gen-calibrated — откалиброванный SYNTHETIC-генератор v2 (параметры из источников)
  eval-compare — сравнение фаз BASELINE/INTERVENTION (разведочный анализ)
  pilot-package — документы пилота для реальной школы (этап 5b)
  forecast-diners — backtest прогноза едоков (базовые vs ML-кандидаты)
  quality-check — контроль качества данных (+ кросс-чек с источником FI)
  forecast-composite — композит (едоки × г/едока) vs прямой прогноз
  import-map — импорт CSV по карте колонок (внешние выгрузки, вкл. Соцкошелёк)
  alerts — алерты питания (индекс несъедаемости vs порог нацстандарта РК)

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
from .store import proposal_hash
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


def cmd_import_open(args) -> int:
    """Импорт ЧУЖИХ открытых данных (Zenodo) в OPEN-среду.

    Статус строк — REAL_OBSERVED с source_ref записи Zenodo; это чужие
    измерения (Финляндия/Латвия), не данные школы КЗ. Хранилище должно быть
    создано с --environment open. Рядом с БД пишутся CSV-копии контрактного
    формата (аудиторский след преобразования) и отчёт-JSON.
    """
    import csv as _csv
    from sra import contracts
    from sra.ingest import xlsx_min, zenodo_fi, zenodo_lv
    from sra.validation import validate_observations, validate_sessions

    store = EvidenceStore(args.db, enums.ENV_OPEN)
    try:
        if store.environment != enums.ENV_OPEN:
            raise SraError("import-open требует хранилище среды OPEN "
                           "(создайте: init-db --environment open)", code="E_BAD_ENV")
        wb = xlsx_min.read_workbook(args.file)
        if args.dataset == "finland":
            mapped = zenodo_fi.map_workbook(
                wb, **({"school_id": args.school_id} if args.school_id else {}))
        else:
            mapped = zenodo_lv.map_workbook(
                wb, **({"school_prefix": args.school_id} if args.school_id else {}))
        report = dict(mapped["report"])
        s_rep = validate_sessions(mapped["sessions"], existing_ids=store.session_ids())
        o_rep = validate_observations(
            mapped["observations"], known_sessions={r.session_id for r in s_rep.accepted},
            existing_observation_ids=store.observation_ids(),
            existing_pairs=store.observation_pairs())
        if not s_rep.ok or not o_rep.ok:
            report["imported"] = 0
            report["rejected_reasons"] = ([i.to_dict() for i in s_rep.errors]
                                          + [i.to_dict() for i in o_rep.errors])
            report["note"] = "импорт атомарен: батч с ошибками не записан"
            _print(report)
            return 2
        # CSV-копии (аудиторский след преобразования xlsx → контракт)
        out_dir = Path(args.out_dir) if args.out_dir else Path(args.db).parent
        out_dir.mkdir(parents=True, exist_ok=True)
        tag = "fi" if args.dataset == "finland" else "lv"
        for kind, recs, header in (
                ("sessions", s_rep.accepted, contracts.SESSION_FIELDS),
                ("observations", o_rep.accepted, contracts.OBSERVATION_FIELDS)):
            # .audit.csv — копия для аудита человеком (не для повторного импорта:
            # заголовок шире контрактного на notes/quality_flags сознательно)
            path = out_dir / f"{tag}-{kind}.audit.csv"
            with open(path, "w", newline="", encoding="utf-8") as fh:
                w = _csv.DictWriter(fh, fieldnames=list(header) + ["notes", "quality_flags"])
                w.writeheader()
                for r in recs:
                    payload = {k: r.to_payload().get(k, "") for k in header}
                    payload["notes"] = (r.notes or "")
                    payload["quality_flags"] = ";".join(r.quality_flags)
                    w.writerow(payload)
            report[f"csv_copy_{kind}"] = str(path)
        res_s = store.import_sessions(s_rep.accepted, actor=args.actor,
                                      import_meta={"source_file": str(args.file),
                                                   "dataset": report["dataset"]})
        res_o = store.import_observations(o_rep.accepted, actor=args.actor,
                                          import_meta={"source_file": str(args.file),
                                                       "dataset": report["dataset"]})
        report["imported_sessions"] = res_s["imported"]
        report["imported_observations"] = res_o["imported"]
        report["dataset_version"] = res_s["dataset_version"]
        report["warnings"] = [i.to_dict() for i in s_rep.warnings] + \
                             [i.to_dict() for i in o_rep.warnings]
        _print(report)
        return 0
    finally:
        store.close()


def cmd_eval_compare(args) -> int:
    """Сравнение фаз (BASELINE vs INTERVENTION) — репетиция оценки эффекта.

    Разведочный анализ (см. оговорки в выводе): причинность не утверждается.
    """
    from sra.evaluation.compare_phases import compare_phases
    store = EvidenceStore(args.db, args.environment)
    try:
        report = compare_phases(store, args.school_id, alpha=args.alpha,
                                power=args.power)
        _print(report)
        return 0 if report["status"] == "OK" else 2
    finally:
        store.close()


def cmd_pilot_package(args) -> int:
    """Генерирует пакет документов пилота (этап 5b) для реальной школы.

    Документы создаются из кода: условия ворот всегда соответствуют
    sra.phases.INTERVENTION_CONDITIONS. Требуют юридической сверки.
    """
    from sra.pilot.package import build_package
    _print(build_package(args.out))
    return 0


def cmd_forecast_diners(args) -> int:
    """Backtest прогноза числа едоков: базовые правила vs ML-кандидаты.

    Едоки — отдельная цель прогноза (спрос = едоки × г/едока); в КЗ наблюдаются
    автоматически через «Социальный кошелёк». Выбор модели — по измеренному MAE.
    """
    from sra.forecasting.diners import backtest_diners
    store = EvidenceStore(args.db, args.environment)
    try:
        sessions = [s for s in store.fetch_sessions(school_id=args.school_id)
                    if s.meal_type == args.meal_type]
        report = backtest_diners(sessions, test_days=args.test_days,
                                 calib_days=args.calib_days)
        if report["status"] == "OK" and not args.no_save_cards:
            for m in report["models"]:
                if m.get("status") != "OK":
                    continue
                store.save_model_card(
                    model_id=f"sra.diners.{m['name']}", model_name=m["name"],
                    params=m.get("params", {}),
                    data_meta={"school_id": args.school_id,
                               "meal_type": args.meal_type,
                               "evaluation": report["evaluation"],
                               "first_test_date": report["first_test_date"],
                               "last_test_date": report["last_test_date"],
                               "dataset_version": store.get_dataset_version()},
                    metrics={"mae_diners_mean": m.get("mae_mean"),
                             "mae_per_origin": m.get("mae_per_origin"),
                             "intervals": m.get("intervals")})
            report["model_cards_saved"] = True
        _print(report)
        return 0 if report["status"] == "OK" else 2
    finally:
        store.close()


def cmd_quality_check(args) -> int:
    """Отчёт качества данных школы + опциональный кросс-чек с источником FI."""
    from sra.quality import crosscheck_zenodo_fi, data_quality_report
    store = EvidenceStore(args.db, args.environment)
    try:
        report = data_quality_report(store, args.school_id)
        if args.file:
            if args.dataset != "finland":
                raise SraError("кросс-чек с источником реализован для finland",
                               code="E_BAD_DATASET")
            report["source_crosscheck"] = crosscheck_zenodo_fi(
                args.file, store, args.school_id)
        _print(report)
        return 0
    finally:
        store.close()


def cmd_alerts(args) -> int:
    """Алерты питания (индекс несъедаемости vs порог нацстандарта РК)."""
    from sra.monitoring import nutrition_alerts
    store = EvidenceStore(args.db, args.environment)
    try:
        _print({"school_id": args.school_id,
                "alerts": nutrition_alerts(store, args.school_id,
                                           uneaten_threshold_pct=args.threshold)})
        return 0
    finally:
        store.close()


def cmd_forecast_composite(args) -> int:
    """Композитный прогноз (едоки × г/едока) vs прямой: MAE + стоимость решения."""
    from sra.forecasting.composite import backtest_composite
    store = EvidenceStore(args.db, args.environment)
    try:
        sessions = [s for s in store.fetch_sessions(school_id=args.school_id)
                    if s.meal_type == args.meal_type]
        observations = store.fetch_observations(school_id=args.school_id)
        report = backtest_composite(sessions, observations, field=args.field,
                                    test_days=args.test_days,
                                    calib_days=args.calib_days,
                                    c_w=args.c_w, c_s=args.c_s)
        _print(report)
        return 0 if report["status"] == "OK" else 2
    finally:
        store.close()


def cmd_import_map(args) -> int:
    """Импорт CSV внешнего источника по карте колонок (JSON).

    Универсальный вход для выгрузок («Социальный кошелёк», школьные журналы):
    карта соответствий пишется по фактическому образцу, догадок нет.
    """
    import csv as _csv
    from sra.ingest.csv_map import load_map, parse_csv
    from sra.validation import validate_observations, validate_sessions

    mapping = load_map(args.map)
    records, parse_report = parse_csv(args.file, mapping)
    if parse_report["errors_total"]:
        _print({"imported": 0, "parse": parse_report,
                "note": "импорт не выполнен: есть ошибки разбора"})
        return 2
    if not records:
        raise SraError("файл не содержит строк данных", code="E_NO_DATA_ROWS")
    store = EvidenceStore(args.db, args.environment)
    try:
        if mapping["kind"] == "sessions":
            v_rep = validate_sessions(records, existing_ids=store.session_ids())
        else:
            v_rep = validate_observations(
                records, known_sessions=store.session_ids(),
                existing_observation_ids=store.observation_ids(),
                existing_pairs=store.observation_pairs())
        if not v_rep.ok:
            _print({"imported": 0, "rejected_reasons": [i.to_dict() for i in v_rep.errors],
                    "warnings": [i.to_dict() for i in v_rep.warnings],
                    "note": "импорт атомарен: батч с ошибками не записан"})
            return 2
        if mapping["kind"] == "sessions":
            res = store.import_sessions(v_rep.accepted, actor=args.actor,
                                        import_meta={"source_file": str(args.file),
                                                     "map": str(args.map)})
        else:
            res = store.import_observations(v_rep.accepted, actor=args.actor,
                                            import_meta={"source_file": str(args.file),
                                                         "map": str(args.map)})
        _print({"imported": res["imported"], "dataset_version": res["dataset_version"],
                "warnings": [i.to_dict() for i in v_rep.warnings],
                "parse": {"rows_total": parse_report["rows_total"]}})
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


def _load_decision_policy(store, policy_id: str) -> tuple[dict, list[str]]:
    """Возвращает (values, problems) параметров политики решения."""
    from sra.optimizer.policies import validate_decision_policy
    reg = PolicyRegistry(store)
    try:
        row = reg.get(policy_id)
    except SraError as e:
        raise SystemExit(f"policy error: {e.to_dict()}")
    values = row["payload_json"].get("values", {})
    problems = validate_decision_policy(values)
    return values, problems


def cmd_optimize(args) -> int:
    from sra.optimizer.propose import propose_adjustment
    store = EvidenceStore(args.db, args.environment)
    try:
        policies = PolicyRegistry(store)
        proposal = propose_adjustment(
            store, policies, school_id=args.school_id, meal_type=args.meal_type,
            target_date=args.target_date, policy_id=args.policy_id,
            actor=args.actor, decision_hour=args.decision_hour).to_dict()
        proposal["payload_hash"] = proposal_hash(proposal)
        _print(proposal)
        return 0
    finally:
        store.close()


def cmd_compare(args) -> int:
    from sra.optimizer.compare import compare_strategies
    store = EvidenceStore(args.db, args.environment)
    try:
        values, problems = _load_decision_policy(store, args.policy_id)
        if problems:
            _print({"status": "POLICY_INVALID", "problems": problems})
            return 2
        report = compare_strategies(
            store, school_id=args.school_id, meal_type=args.meal_type,
            test_days=args.test_days, calib_days=int(values["calib_days"]),
            baseline_days=int(values["baseline_days"]), decision_hour=args.decision_hour,
            model_name=values["forecast_model"],
            seasonal_weeks=int(values.get("seasonal_weeks", 3)),
            cost_waste_per_kg=values["cost_waste_per_kg"],
            cost_shortage_per_kg=values["cost_shortage_per_kg"],
            shortage_risk_max=values["shortage_risk_max"],
            delta_max_pct=values["delta_max_pct_of_baseline"],
            grid_step_g=values["grid_step_g"])
        report["policy"] = f"{args.policy_id} (SIMULATED-параметры)"
        _print(report)
        return 0
    finally:
        store.close()


def cmd_phase(args) -> int:
    from sra.phases import PhaseManager
    store = EvidenceStore(args.db, args.environment)
    try:
        policies = PolicyRegistry(store)
        pm = PhaseManager(store, policies)
        if args.action == "get":
            _print(pm.current(args.school_id))
            return 0
        result = pm.transition(args.school_id, args.phase, actor=args.actor,
                               protocol_ref=args.protocol_ref,
                               policy_id=args.policy_id, meal_type=args.meal_type)
        _print(result)
        return 0
    finally:
        store.close()


def cmd_shadow_run(args) -> int:
    from sra.shadow import run_shadow
    store = EvidenceStore(args.db, args.environment)
    try:
        policies = PolicyRegistry(store)
        report = run_shadow(store, policies, school_id=args.school_id,
                            meal_type=args.meal_type, policy_id=args.policy_id,
                            days=args.days, actor=args.actor,
                            decision_hour=args.decision_hour)
        _print(report)
        return 0
    finally:
        store.close()


def cmd_monitor(args) -> int:
    from sra.monitoring import monitor_shadow
    store = EvidenceStore(args.db, args.environment)
    try:
        _print(monitor_shadow(store, args.school_id, args.meal_type,
                              recent_k=args.recent_k))
        return 0
    finally:
        store.close()


def cmd_design(args) -> int:
    from statistics import mean as _mean, stdev as _stdev
    from sra.evaluation.design import build_evaluation_plan
    store = EvidenceStore(args.db, args.environment)
    try:
        recs = [r for r in store.list_shadow_records(args.school_id, args.meal_type)
                if r["pred_g"] is not None and r["y_g"] is not None]
        if len(recs) < args.min_records:
            _print({"status": enums.DATA_NOT_READY,
                    "reason": f"для оценки σ нужно ≥{args.min_records} shadow-дней с "
                              "прогнозом и фактом; запустите shadow-run",
                    "n_records": len(recs)})
            return 2
        residuals = [r["y_g"] - r["pred_g"] for r in recs]
        sigma = _stdev(residuals)
        baseline_waste = _mean([max(r["actual_prepared_g"] - r["y_g"], 0.0)
                                for r in recs if r["actual_prepared_g"] is not None])
        plan = build_evaluation_plan(
            school_id=args.school_id, design=args.design, sigma_g=sigma,
            baseline_waste_g_per_day=baseline_waste,
            expected_effect_rel=args.effect_rel, alpha=args.alpha,
            power=args.power, school_days_per_week=args.days_per_week)
        plan["sigma_estimated_from"] = {"n_days": len(recs), "stdev_residuals_g": sigma,
                                        "source": "shadow-записи (замена, не дизайн-σ)"}
        _print(plan)
        return 0
    finally:
        store.close()


def cmd_explain(args) -> int:
    from sra.assistant.tools import call_tool
    from sra.explain import explain_school_overview
    store = EvidenceStore(args.db, args.environment)
    try:
        policies = PolicyRegistry(store)
        result = call_tool(store, policies, "school_overview",
                           {"school_id": args.school_id, "meal_type": args.meal_type},
                           actor=args.actor)
        if not result["ok"]:
            _print(result["refusal"])
            return 2
        print(explain_school_overview(result["facts"]))
        return 0
    finally:
        store.close()


def cmd_assistant_tools(args) -> int:
    from sra.assistant.tools import ARG_SPEC, TOOL_DESCRIPTIONS
    _print({name: {"description": TOOL_DESCRIPTIONS.get(name, ""),
                   "args": {k: {"type": v[0].__name__, "required": v[1],
                                "default": v[2]} for k, v in spec.items()}}
            for name, spec in ARG_SPEC.items()})
    return 0


def cmd_assistant_call(args) -> int:
    from sra.assistant.context import build_context
    from sra.assistant.tools import call_tool
    try:
        tool_args = json.loads(args.args) if args.args else {}
    except json.JSONDecodeError as e:
        _print({"error": {"code": "E_CLI_ARGS", "message": f"args не JSON: {e}"}})
        return 2
    store = EvidenceStore(args.db, args.environment)
    try:
        policies = PolicyRegistry(store)
        result = call_tool(store, policies, args.tool,
                           {"school_id": args.school_id, **tool_args},
                           actor=args.actor)
        if result["ok"] and args.with_context:
            _print(build_context(result["facts"], purpose=args.with_context))
        else:
            _print(result)
        return 0 if result["ok"] else 2
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


def cmd_gen_calibrated(args) -> int:
    from sra.demo.calibrated import generate
    manifest = generate(args.dir, seed=args.seed, n_days=args.days,
                        start_date=args.start_date, enrollment=args.enrollment,
                        mode_change_day=args.mode_change_day)
    _print({
        "data_nature": manifest["data_nature"],
        "generator": manifest["generator"],
        "school_id": manifest["school_id"],
        "n_sessions": manifest["n_sessions"],
        "n_observations": manifest["n_observations"],
        "plate_waste_per_child_mean_g": manifest["plate_waste_per_child_mean_g"],
        "calibration_classes": sorted({c["class"] for c in manifest["calibration"]}),
        "files": [str(Path(args.dir) / f) for f in
                  ("sessions.csv", "observations.csv", "manifest.json")],
        "warning": manifest["warnings"][0],
        "import_hint": ("python3 -m sra init-db --db <db> --environment demo && "
                        "python3 -m sra import --db <db> --environment demo "
                        "--kind sessions --file " + str(Path(args.dir) / "sessions.csv") +
                        " --actor <оператор>"),
    })
    return 0


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
        "optimizer_decision": {k: report["optimizer_decision"][k]
                               for k in ("action", "action_status", "mode", "reasons")
                               if k in report["optimizer_decision"]},
        "study_phase": report.get("study_phase"),
        "shadow": {k: report["shadow"][k] for k in ("status", "days", "inserted",
                                                    "records_total", "actions")
                   if k in report["shadow"]},
        "monitoring_status": report.get("monitoring", {}).get("status"),
        "evaluation_plan_status": report.get("evaluation_plan", {}).get("status"),
        "explanation_first_line": (report.get("explanation", "").splitlines() or [""])[0],
        "ml_track": {k: report["ml_track"][k]
                     for k in ("data_nature", "note", "diners_backtest",
                               "composite_backtest", "nutrition_alerts")
                     if k in report.get("ml_track", {})},
        "explanation_lines": len(report.get("explanation", "").splitlines()),
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

    sp = sub.add_parser("import-open",
                        help="импорт чужих открытых данных (Zenodo xlsx) в OPEN-среду")
    sp.add_argument("--db", required=True)
    sp.add_argument("--file", required=True, help="xlsx-файл датасета")
    sp.add_argument("--dataset", required=True, choices=["finland", "latvia"])
    sp.add_argument("--school-id", default=None,
                    help="идентификатор школы (по умолчанию FI-ZEN-20825233 / "
                         "LV-ZEN-18441715-<School_ID>)")
    sp.add_argument("--actor", default="open-data-import")
    sp.add_argument("--out-dir", default=None,
                    help="куда писать CSV-копии (по умолчанию рядом с БД)")
    sp.set_defaults(func=cmd_import_open)

    sp = sub.add_parser("forecast-diners",
                        help="backtest прогноза едоков: базовые vs ML (ridge, sklearn GBDT)")
    sp.add_argument("--db", required=True)
    sp.add_argument("--environment", choices=sorted(enums.ENVIRONMENTS),
                    default=enums.ENV_OPEN)
    sp.add_argument("--school-id", required=True)
    sp.add_argument("--meal-type", default="LUNCH")
    sp.add_argument("--test-days", type=int, default=30)
    sp.add_argument("--calib-days", type=int, default=20)
    sp.add_argument("--no-save-cards", action="store_true")
    sp.set_defaults(func=cmd_forecast_diners)

    sp = sub.add_parser("forecast-composite",
                        help="композит (едоки × г/едока) vs прямой прогноз: "
                             "MAE + стоимость решения")
    sp.add_argument("--db", required=True)
    sp.add_argument("--environment", choices=sorted(enums.ENVIRONMENTS),
                    default=enums.ENV_OPEN)
    sp.add_argument("--school-id", required=True)
    sp.add_argument("--meal-type", default="LUNCH")
    sp.add_argument("--field", choices=["plate_waste_g", "served_mass_g"],
                    default="plate_waste_g")
    sp.add_argument("--test-days", type=int, default=20)
    sp.add_argument("--calib-days", type=int, default=20)
    sp.add_argument("--c-w", type=float, default=500.0)
    sp.add_argument("--c-s", type=float, default=5000.0)
    sp.set_defaults(func=cmd_forecast_composite)

    sp = sub.add_parser("import-map",
                        help="импорт CSV по карте колонок (JSON) — универсальный "
                             "вход для внешних выгрузок")
    sp.add_argument("--db", required=True)
    sp.add_argument("--environment", choices=sorted(enums.ENVIRONMENTS),
                    default=enums.ENV_OPEN)
    sp.add_argument("--file", required=True, help="CSV внешнего источника")
    sp.add_argument("--map", required=True, help="JSON-карта колонок")
    sp.add_argument("--actor", default="csv-map-import")
    sp.set_defaults(func=cmd_import_map)

    sp = sub.add_parser("quality-check",
                        help="качество данных школы (полнота, выбросы, календарь)")
    sp.add_argument("--db", required=True)
    sp.add_argument("--environment", choices=sorted(enums.ENVIRONMENTS),
                    default=enums.ENV_OPEN)
    sp.add_argument("--school-id", required=True)
    sp.add_argument("--file", default=None,
                    help="xlsx первоисточника для кросс-чека (finland)")
    sp.add_argument("--dataset", choices=["finland", "latvia"], default="finland")
    sp.set_defaults(func=cmd_quality_check)

    sp = sub.add_parser("alerts",
                        help="алерты питания (несъедаемость vs порог РК 30%)")
    sp.add_argument("--db", required=True)
    sp.add_argument("--environment", choices=sorted(enums.ENVIRONMENTS),
                    default=enums.ENV_OPEN)
    sp.add_argument("--school-id", required=True)
    sp.add_argument("--threshold", type=float, default=30.0)
    sp.set_defaults(func=cmd_alerts)

    sp = sub.add_parser("eval-compare",
                        help="сравнение фаз BASELINE/INTERVENTION (разведочный анализ)")
    sp.add_argument("--db", required=True)
    sp.add_argument("--environment", choices=sorted(enums.ENVIRONMENTS),
                    default=enums.ENV_OPEN)
    sp.add_argument("--school-id", required=True)
    sp.add_argument("--alpha", type=float, default=0.05)
    sp.add_argument("--power", type=float, default=0.80)
    sp.set_defaults(func=cmd_eval_compare)

    sp = sub.add_parser("pilot-package",
                        help="документы пилота для школы (согласие, протокол, "
                             "чек-лист ворот, план эффекта, памятка NO_CHANGE)")
    sp.add_argument("--out", default="docs/pilot-package")
    sp.set_defaults(func=cmd_pilot_package)

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

    sp = sub.add_parser("optimize", help="симуляционное предложение объёма (этап 4)")
    db_args(sp)
    sp.add_argument("--school-id", required=True)
    sp.add_argument("--meal-type", default="LUNCH")
    sp.add_argument("--target-date", required=True)
    sp.add_argument("--policy-id", required=True, help="policy_id параметров решения")
    sp.add_argument("--decision-hour", type=int, default=18)
    sp.add_argument("--actor", default="planner")
    sp.set_defaults(func=cmd_optimize)

    sp = sub.add_parser("compare", help="сравнение стратегий объёма (SIMULATED)")
    db_args(sp)
    sp.add_argument("--school-id", required=True)
    sp.add_argument("--meal-type", default="LUNCH")
    sp.add_argument("--policy-id", required=True)
    sp.add_argument("--test-days", type=int, default=10)
    sp.add_argument("--decision-hour", type=int, default=18)
    sp.set_defaults(func=cmd_compare)

    sp = sub.add_parser("phase", help="фазы исследования школы")
    db_args(sp)
    sp.add_argument("--action", required=True, choices=["get", "set"])
    sp.add_argument("--school-id", required=True)
    sp.add_argument("--phase", choices=sorted(enums.STUDY_PHASES))
    sp.add_argument("--protocol-ref", default=None)
    sp.add_argument("--policy-id", default=None)
    sp.add_argument("--meal-type", default="LUNCH")
    sp.add_argument("--actor", default="researcher")
    sp.set_defaults(func=cmd_phase)

    sp = sub.add_parser("shadow-run", help="SHADOW-контур по окну дней (без исполнения)")
    db_args(sp)
    sp.add_argument("--school-id", required=True)
    sp.add_argument("--meal-type", default="LUNCH")
    sp.add_argument("--policy-id", required=True)
    sp.add_argument("--days", type=int, default=7)
    sp.add_argument("--decision-hour", type=int, default=18)
    sp.add_argument("--actor", default="shadow")
    sp.set_defaults(func=cmd_shadow_run)

    sp = sub.add_parser("monitor", help="мониторинг качества прогнозов (shadow)")
    db_args(sp)
    sp.add_argument("--school-id", required=True)
    sp.add_argument("--meal-type", default="LUNCH")
    sp.add_argument("--recent-k", type=int, default=10)
    sp.set_defaults(func=cmd_monitor)

    sp = sub.add_parser("design", help="проект плана оценки эффекта")
    db_args(sp)
    sp.add_argument("--school-id", required=True)
    sp.add_argument("--meal-type", default="LUNCH")
    sp.add_argument("--effect-rel", type=float, required=True,
                    help="ожидаемое относительное снижение отходов (0,1)")
    sp.add_argument("--design", choices=["two_arm_parallel", "before_after"],
                    default="two_arm_parallel")
    sp.add_argument("--alpha", type=float, default=0.05)
    sp.add_argument("--power", type=float, default=0.80)
    sp.add_argument("--days-per-week", type=int, default=5)
    sp.add_argument("--min-records", type=int, default=8)
    sp.set_defaults(func=cmd_design)

    sp = sub.add_parser("explain", help="объяснение статуса школы (детерминированный текст)")
    db_args(sp)
    sp.add_argument("--school-id", required=True)
    sp.add_argument("--meal-type", default="LUNCH")
    sp.add_argument("--actor", default="assistant")
    sp.set_defaults(func=cmd_explain)

    sp = sub.add_parser("assistant-tools", help="список разрешённых инструментов помощника")
    sp.set_defaults(func=cmd_assistant_tools)

    sp = sub.add_parser("assistant-call", help="вызов инструмента помощника")
    db_args(sp)
    sp.add_argument("--tool", required=True)
    sp.add_argument("--school-id", required=True)
    sp.add_argument("--args", default=None, help="JSON остальных аргументов")
    sp.add_argument("--actor", default="assistant")
    sp.add_argument("--with-context", metavar="PURPOSE", default=None,
                    help="дополнительно собрать безопасный контекст для будущего LLM")
    sp.set_defaults(func=cmd_assistant_call)

    sp = sub.add_parser("selfcheck", help="проверка инвариантов хранилища")
    db_args(sp)
    sp.set_defaults(func=cmd_selfcheck)

    sp = sub.add_parser("audit", help="хвост журнала аудита")
    db_args(sp)
    sp.add_argument("--tail", type=int, default=50)
    sp.set_defaults(func=cmd_audit)

    sp = sub.add_parser("gen-calibrated",
                        help="откалиброванный SYNTHETIC-генератор (источники в манифесте)")
    sp.add_argument("--dir", default="data/demo-calibrated")
    sp.add_argument("--seed", type=int, default=2026)
    sp.add_argument("--days", type=int, default=90)
    sp.add_argument("--start-date", default="2026-09-01")
    sp.add_argument("--enrollment", type=int, default=120,
                    help="контингент школы (допущение, см. манифест)")
    sp.add_argument("--mode-change-day", type=int, default=45)
    sp.set_defaults(func=cmd_gen_calibrated)

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
