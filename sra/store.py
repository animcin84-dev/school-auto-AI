"""Evidence Store: SQLite-хранилище наблюдений с ревизиями, аудитом и guards.

Дизайн:
  - каноническая история — таблица `revisions` (append-only); текущее состояние —
    материализованные *_current указатели на последнюю ревизию;
  - каждая запись несёт recorded_at (когда данные появились), dataset_version,
    created_by, quality_flags; чтение "as of" не видит будущих исправлений
    (защита от утечки будущего в исторические признаки для будущих бэктестов);
  - VERIFIED — только через verify() (verifier, method, source); SYNTHETIC не
    подлежит проверке; правка VERIFIED-записи сбрасывает её в REAL_OBSERVED
    с pending_verification и флагом REVERIFICATION_REQUIRED;
  - изоляция сред: REAL-хранилище не принимает SYNTHETIC, DEMO-хранилище не
    принимает REAL_OBSERVED; среда фиксируется маркером при создании файла БД;
  - все значимые действия пишут события в audit_events (append-only).

SQLite — выбор этапа 1 (stdlib, ноль зависимостей, файловая изоляция демо).
Интерфейс магазина узкий и не раскрывает SQL наружу; перенос на PostgreSQL
в этапе эксплуатации — ограниченное изменение этого модуля + миграции.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from . import enums
from .contracts import ObservationRecord, SessionRecord
from .errors import GuardError, NotFoundError, ValidationError
from .validation import revalidate_record

MIGRATIONS: list[tuple[int, str]] = [
    # Примечание: таблица schema_migrations создаётся в _migrate() до применения скриптов.
    (1, """
CREATE TABLE meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE schools (
    school_id TEXT PRIMARY KEY,
    timezone TEXT,
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL
);
CREATE TABLE dishes (
    dish_id TEXT PRIMARY KEY,
    first_seen_at TEXT NOT NULL
);
CREATE TABLE sources (
    source_ref TEXT PRIMARY KEY,
    first_seen_at TEXT NOT NULL
);
CREATE TABLE revisions (
    revision_id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT NOT NULL CHECK (entity_type IN ('session','observation')),
    entity_id TEXT NOT NULL,
    supersedes_revision_id INTEGER,
    superseded_by INTEGER,
    payload TEXT NOT NULL,
    evidence_status TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    quality_flags TEXT NOT NULL DEFAULT '[]',
    pending_verification INTEGER NOT NULL DEFAULT 0,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    dataset_version INTEGER NOT NULL
);
CREATE INDEX idx_revisions_entity ON revisions(entity_type, entity_id);
CREATE INDEX idx_revisions_recorded ON revisions(entity_type, recorded_at);
CREATE TABLE sessions_current (
    entity_id TEXT PRIMARY KEY,
    revision_id INTEGER NOT NULL REFERENCES revisions(revision_id),
    school_id TEXT NOT NULL,
    service_date TEXT NOT NULL,
    meal_type TEXT NOT NULL,
    evidence_status TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    pending_verification INTEGER NOT NULL DEFAULT 0,
    dataset_version INTEGER NOT NULL
);
CREATE TABLE observations_current (
    entity_id TEXT PRIMARY KEY,
    revision_id INTEGER NOT NULL REFERENCES revisions(revision_id),
    session_id TEXT NOT NULL,
    dish_id TEXT NOT NULL,
    evidence_status TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    pending_verification INTEGER NOT NULL DEFAULT 0,
    dataset_version INTEGER NOT NULL
);
CREATE INDEX idx_obs_session ON observations_current(session_id);
CREATE TABLE audit_events (
    audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    entity_type TEXT,
    entity_id TEXT,
    revision_id INTEGER,
    details TEXT
);
CREATE TABLE verifications (
    verification_id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    revision_id INTEGER NOT NULL,
    verifier TEXT NOT NULL,
    method TEXT NOT NULL,
    source_ref TEXT,
    verified_at TEXT NOT NULL,
    notes TEXT
);
CREATE TABLE policies (
    policy_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    jurisdiction TEXT,
    source_ref TEXT NOT NULL,
    valid_from TEXT,
    valid_until TEXT,
    verification_status TEXT NOT NULL,
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL,
    PRIMARY KEY (policy_id, version)
);
"""),
    # Миграция 2 (этап 3): реестр версий моделей прогноза (model cards).
    (2, """
CREATE TABLE model_cards (
    model_card_id INTEGER PRIMARY KEY AUTOINCREMENT,
    model_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    model_name TEXT NOT NULL,
    params TEXT NOT NULL,
    data_meta TEXT NOT NULL,
    metrics TEXT,
    environment TEXT NOT NULL,
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL,
    UNIQUE (model_id, version)
);
"""),
]

_ENTITY_TABLE = {"session": "sessions_current", "observation": "observations_current"}


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class EvidenceStore:
    """Фасад хранилища. Наружу не отдаёт SQL-соединение (кроме selfcheck-тестов)."""

    def __init__(self, path: str | Path, environment: str,
                 *, clock: Callable[[], str] = _utcnow):
        if environment not in enums.ENVIRONMENTS:
            raise GuardError(f"недопустимая среда: {environment!r}", code="E_BAD_ENV")
        self.path = Path(path)
        self.environment = environment
        self._clock = clock
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path))
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._migrate()
        self._init_marker()

    # --- Инициализация ------------------------------------------------------
    def _migrate(self) -> None:
        cur = self._conn
        cur.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        applied = {r["version"] for r in cur.execute("SELECT version FROM schema_migrations")}
        for version, sql in MIGRATIONS:
            if version in applied:
                continue
            with self._conn:
                cur.executescript(sql)
                cur.execute("INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                            (version, self._clock()))

    def _init_marker(self) -> None:
        row = self._conn.execute("SELECT value FROM meta WHERE key='environment'").fetchone()
        if row is None:
            with self._conn:
                self._conn.execute("INSERT INTO meta(key, value) VALUES ('environment', ?)",
                                   (self.environment,))
                self._conn.execute("INSERT INTO meta(key, value) VALUES ('created_at', ?)",
                                   (self._clock(),))
                self._conn.execute("INSERT INTO meta(key, value) VALUES ('dataset_version', '0')")
        elif row["value"] != self.environment:
            raise GuardError(
                f"среда хранилища {self.path} — {row['value']}, запросана {self.environment}; "
                "смешение REAL и DEMO данных запрещено",
                code="E_ENV_MISMATCH",
                details={"db_environment": row["value"], "requested": self.environment},
            )

    # --- Служебное ------------------------------------------------------------
    def close(self) -> None:
        self._conn.close()

    def _next_dataset_version(self) -> int:
        with self._conn:
            self._conn.execute(
                "UPDATE meta SET value = CAST(CAST(value AS INTEGER) + 1 AS TEXT) WHERE key='dataset_version'"
            )
        row = self._conn.execute("SELECT value FROM meta WHERE key='dataset_version'").fetchone()
        return int(row["value"])

    def _audit(self, actor: str, action: str, *, entity_type: str | None = None,
               entity_id: str | None = None, revision_id: int | None = None,
               details: dict | None = None) -> None:
        self._conn.execute(
            "INSERT INTO audit_events(ts, actor, action, entity_type, entity_id, revision_id, details) "
            "VALUES (?,?,?,?,?,?,?)",
            (self._clock(), actor, action, entity_type, entity_id, revision_id,
             json.dumps(details or {}, ensure_ascii=False)),
        )

    # --- Импорт ----------------------------------------------------------------
    def import_sessions(self, records: list[SessionRecord], *, actor: str,
                        import_meta: dict | None = None) -> dict:
        self._guard_environment([r.evidence_status for r in records], kind="sessions")
        ids = self.session_ids()
        for r in records:
            if r.session_id in ids:
                raise ValidationError(f"дубликат session_id {r.session_id!r} уже в хранилище",
                                      code="E_DUPLICATE_ID", details={"entity_id": r.session_id})
        ds_version = self._next_dataset_version()
        now = self._clock()
        with self._conn:
            for r in records:
                cur = self._conn.execute(
                    "INSERT INTO revisions(entity_type, entity_id, supersedes_revision_id, payload, "
                    "evidence_status, recorded_at, quality_flags, pending_verification, created_by, "
                    "created_at, dataset_version) "
                    "VALUES ('session', ?, NULL, ?, ?, ?, ?, 0, ?, ?, ?)",
                    (r.session_id, json.dumps(r.to_payload(), ensure_ascii=False),
                     r.evidence_status, r.recorded_at.isoformat(),
                     json.dumps(r.quality_flags, ensure_ascii=False), actor, now, ds_version),
                )
                rev_id = cur.lastrowid
                self._conn.execute(
                    "INSERT INTO sessions_current(entity_id, revision_id, school_id, service_date, "
                    "meal_type, evidence_status, recorded_at, pending_verification, dataset_version) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    (r.session_id, rev_id, r.school_id, r.service_date.isoformat(),
                     r.meal_type, r.evidence_status, r.recorded_at.isoformat(),
                     0, ds_version),
                )
                self._conn.execute(
                    "INSERT INTO schools(school_id, timezone, created_at, created_by) VALUES (?,?,?,?) "
                    "ON CONFLICT(school_id) DO NOTHING",
                    (r.school_id, r.timezone, now, actor),
                )
                self._conn.execute(
                    "INSERT INTO sources(source_ref, first_seen_at) VALUES (?,?) ON CONFLICT(source_ref) DO NOTHING",
                    (r.source_ref, now),
                )
                self._audit(actor, "RECORD_CREATED", entity_type="session",
                            entity_id=r.session_id, revision_id=rev_id,
                            details={"evidence_status": r.evidence_status,
                                     "quality_flags": r.quality_flags})
            self._audit(actor, "IMPORT_APPLIED", entity_type="session",
                        details={"rows": len(records), "dataset_version": ds_version,
                                 **(import_meta or {})})
        return {"imported": len(records), "dataset_version": ds_version}

    def import_observations(self, records: list[ObservationRecord], *, actor: str,
                            import_meta: dict | None = None) -> dict:
        self._guard_environment([r.evidence_status for r in records], kind="observations")
        known_sessions = self.session_ids()
        existing_ids = self.observation_ids()
        existing_pairs = self.observation_pairs()
        for r in records:
            if r.session_id not in known_sessions:
                raise ValidationError(
                    f"наблюдение {r.observation_id!r} ссылается на несуществующую сессию "
                    f"{r.session_id!r}; импортируйте sessions.csv первым",
                    code="E_UNKNOWN_SESSION", details={"observation_id": r.observation_id})
            if r.observation_id in existing_ids:
                raise ValidationError(f"дубликат observation_id {r.observation_id!r}",
                                      code="E_DUPLICATE_ID", details={"entity_id": r.observation_id})
            if (r.session_id, r.dish_id) in existing_pairs:
                raise ValidationError(
                    f"повторное измерение блюда {r.dish_id!r} в сессии {r.session_id!r}; "
                    "исправление — через store.revise()",
                    code="E_DUPLICATE_DISH_SESSION", details={"entity_id": r.observation_id})
        ds_version = self._next_dataset_version()
        now = self._clock()
        with self._conn:
            for r in records:
                cur = self._conn.execute(
                    "INSERT INTO revisions(entity_type, entity_id, supersedes_revision_id, payload, "
                    "evidence_status, recorded_at, quality_flags, pending_verification, created_by, "
                    "created_at, dataset_version) "
                    "VALUES ('observation', ?, NULL, ?, ?, ?, ?, 0, ?, ?, ?)",
                    (r.observation_id, json.dumps(r.to_payload(), ensure_ascii=False),
                     r.evidence_status, r.recorded_at.isoformat(),
                     json.dumps(r.quality_flags, ensure_ascii=False), actor, now, ds_version),
                )
                rev_id = cur.lastrowid
                self._conn.execute(
                    "INSERT INTO observations_current(entity_id, revision_id, session_id, dish_id, "
                    "evidence_status, recorded_at, pending_verification, dataset_version) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (r.observation_id, rev_id, r.session_id, r.dish_id,
                     r.evidence_status, r.recorded_at.isoformat(), 0, ds_version),
                )
                self._conn.execute(
                    "INSERT INTO dishes(dish_id, first_seen_at) VALUES (?,?) ON CONFLICT(dish_id) DO NOTHING",
                    (r.dish_id, now),
                )
                self._conn.execute(
                    "INSERT INTO sources(source_ref, first_seen_at) VALUES (?,?) ON CONFLICT(source_ref) DO NOTHING",
                    (r.source_ref, now),
                )
                self._audit(actor, "RECORD_CREATED", entity_type="observation",
                            entity_id=r.observation_id, revision_id=rev_id,
                            details={"evidence_status": r.evidence_status,
                                     "quality_flags": r.quality_flags})
            self._audit(actor, "IMPORT_APPLIED", entity_type="observation",
                        details={"rows": len(records), "dataset_version": ds_version,
                                 **(import_meta or {})})
        return {"imported": len(records), "dataset_version": ds_version}

    def _guard_environment(self, statuses: list[str], *, kind: str) -> None:
        invalid = {s for s in statuses if s not in enums.RAW_ALLOWED_EVIDENCE}
        if invalid:
            raise ValidationError(
                f"сырые записи (kind={kind}) не могут иметь статус {sorted(invalid)}; допустимо "
                f"{sorted(enums.RAW_ALLOWED_EVIDENCE)}; VERIFIED создаётся процедурой store.verify()",
                code="E_BAD_RAW_EVIDENCE")
        if self.environment == enums.ENV_REAL:
            bad = {s for s in statuses if s != enums.REAL_OBSERVED}
            if bad:
                raise GuardError(
                    f"REAL-хранилище принимает только {enums.REAL_OBSERVED}; получено {sorted(bad)} "
                    f"(kind={kind}); синтетика и симуляция живут только в DEMO-среде",
                    code="E_SYNTHETIC_IN_REAL_ENV")
        else:  # DEMO
            bad = {s for s in statuses if s != enums.SYNTHETIC}
            if bad:
                raise GuardError(
                    f"DEMO-хранилище принимает только {enums.SYNTHETIC}; получено {sorted(bad)} "
                    f"(kind={kind}); реальные данные в демо-среду запрещены",
                    code="E_REAL_IN_DEMO_ENV")

    # --- Чтение ------------------------------------------------------------------
    def session_ids(self) -> set[str]:
        return {r["entity_id"] for r in self._conn.execute("SELECT entity_id FROM sessions_current")}

    def observation_ids(self) -> set[str]:
        return {r["entity_id"] for r in self._conn.execute("SELECT entity_id FROM observations_current")}

    def observation_pairs(self) -> set[tuple[str, str]]:
        return {(r["session_id"], r["dish_id"])
                for r in self._conn.execute("SELECT session_id, dish_id FROM observations_current")}

    def fetch_sessions(self, *, school_id: str | None = None,
                       date_from: str | None = None, date_to: str | None = None) -> list[SessionRecord]:
        q = ("SELECT r.payload, r.quality_flags FROM sessions_current c "
             "JOIN revisions r ON r.revision_id = c.revision_id WHERE 1=1")
        args: list = []
        if school_id:
            q += " AND c.school_id = ?"
            args.append(school_id)
        if date_from:
            q += " AND c.service_date >= ?"
            args.append(date_from)
        if date_to:
            q += " AND c.service_date <= ?"
            args.append(date_to)
        q += " ORDER BY c.service_date, c.entity_id"
        return [SessionRecord.from_payload(json.loads(r["payload"]),
                                           quality_flags=json.loads(r["quality_flags"]))
                for r in self._conn.execute(q, args)]

    def fetch_observations(self, *, session_ids: list[str] | None = None,
                           school_id: str | None = None,
                           date_from: str | None = None, date_to: str | None = None) -> list[ObservationRecord]:
        q = ("SELECT r.payload, r.quality_flags FROM observations_current o "
             "JOIN revisions r ON r.revision_id = o.revision_id WHERE 1=1")
        args: list = []
        if session_ids is not None:
            if not session_ids:
                return []
            q += f" AND o.session_id IN ({','.join('?' * len(session_ids))})"
            args.extend(session_ids)
        if school_id or date_from or date_to:
            sess_q = "SELECT DISTINCT entity_id FROM sessions_current WHERE 1=1"
            sess_args: list = []
            if school_id:
                sess_q += " AND school_id = ?"
                sess_args.append(school_id)
            if date_from:
                sess_q += " AND service_date >= ?"
                sess_args.append(date_from)
            if date_to:
                sess_q += " AND service_date <= ?"
                sess_args.append(date_to)
            ids = [r["entity_id"] for r in self._conn.execute(sess_q, sess_args)]
            if not ids:
                return []
            q += f" AND o.session_id IN ({','.join('?' * len(ids))})"
            args.extend(ids)
        q += " ORDER BY o.session_id, o.entity_id"
        return [ObservationRecord.from_payload(json.loads(r["payload"]),
                                               quality_flags=json.loads(r["quality_flags"]))
                for r in self._conn.execute(q, args)]

    def get_current(self, entity_type: str, entity_id: str) -> dict:
        """Текущая ревизия сущности: payload + метаданные ревизии."""
        table = _ENTITY_TABLE.get(entity_type)
        if table is None:
            raise ValidationError(f"неизвестный тип сущности {entity_type!r}", code="E_BAD_ENTITY")
        row = self._conn.execute(
            f"SELECT c.revision_id, r.payload, r.evidence_status, r.recorded_at, r.quality_flags, "
            f"r.pending_verification, r.created_by, r.created_at, r.dataset_version, "
            f"r.supersedes_revision_id "
            f"FROM {table} c JOIN revisions r ON r.revision_id = c.revision_id "
            f"WHERE c.entity_id = ?", (entity_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"{entity_type} {entity_id!r} не найден")
        return {
            "entity_type": entity_type, "entity_id": entity_id,
            "revision_id": row["revision_id"],
            "payload": json.loads(row["payload"]),
            "evidence_status": row["evidence_status"],
            "recorded_at": row["recorded_at"],
            "quality_flags": json.loads(row["quality_flags"]),
            "pending_verification": bool(row["pending_verification"]),
            "created_by": row["created_by"], "created_at": row["created_at"],
            "dataset_version": row["dataset_version"],
            "supersedes_revision_id": row["supersedes_revision_id"],
        }

    def asof(self, entity_type: str, cutoff_iso: str) -> list[dict]:
        """Состояние знаний на момент cutoff: последняя (по revision_id) ревизия
        каждой сущности с recorded_at <= cutoff.

        Защита от утечки будущего: поздние исправления не видны в историческом
        срезе, если на момент решения они ещё не появились. Сравнение времени —
        в UTC (aware-датETIME), поэтому разные offset в recorded_at корректны;
        строковое сравнение здесь было бы небезопасно.
        """
        table = _ENTITY_TABLE.get(entity_type)
        if table is None:
            raise ValidationError(f"неизвестный тип сущности {entity_type!r}", code="E_BAD_ENTITY")
        try:
            cutoff = datetime.fromisoformat(cutoff_iso)
            if cutoff.tzinfo is None or cutoff.utcoffset() is None:
                raise ValueError
        except ValueError:
            raise ValidationError(
                f"cutoff должен быть ISO-8601 с явным offset: {cutoff_iso!r}",
                code="E_BAD_CUTOFF")
        cutoff_utc = cutoff.astimezone(timezone.utc)
        rows = self._conn.execute(
            "SELECT entity_id, revision_id, payload, recorded_at FROM revisions "
            "WHERE entity_type = ? ORDER BY revision_id",
            (entity_type,)).fetchall()
        latest: dict[str, sqlite3.Row] = {}
        for r in rows:
            try:
                ra = datetime.fromisoformat(r["recorded_at"])
            except ValueError:
                continue
            if ra.tzinfo is None or ra.utcoffset() is None:
                continue
            if ra.astimezone(timezone.utc) <= cutoff_utc:
                latest[r["entity_id"]] = r
        return [{"entity_id": r["entity_id"], "revision_id": r["revision_id"],
                 "payload": json.loads(r["payload"]), "recorded_at": r["recorded_at"]}
                for r in latest.values()]

    def get_dataset_version(self) -> int:
        row = self._conn.execute("SELECT value FROM meta WHERE key='dataset_version'").fetchone()
        return int(row["value"])

    def save_model_card(self, *, model_id: str, model_name: str, params: dict,
                        data_meta: dict, metrics: dict | None = None,
                        actor: str = "system", environment: str | None = None) -> dict:
        """Регистрирует новую версию модели (model card). Версия назначается автоматически."""
        if not model_id or not model_name:
            raise ValidationError("model card требует model_id и model_name",
                                  code="E_MODEL_CARD_PARAMS")
        row = self._conn.execute(
            "SELECT COALESCE(MAX(version), 0) v FROM model_cards WHERE model_id = ?",
            (model_id,)).fetchone()
        version = int(row["v"]) + 1
        now = self._clock()
        with self._conn:
            self._conn.execute(
                "INSERT INTO model_cards(model_id, version, model_name, params, data_meta, "
                "metrics, environment, created_at, created_by) VALUES (?,?,?,?,?,?,?,?,?)",
                (model_id, version, model_name,
                 json.dumps(params, ensure_ascii=False),
                 json.dumps(data_meta, ensure_ascii=False),
                 json.dumps(metrics, ensure_ascii=False) if metrics else None,
                 environment or self.environment, now, actor))
            self._audit(actor, "MODEL_CARD_SAVED", entity_type="model_card",
                        entity_id=f"{model_id}@v{version}",
                        details={"model_name": model_name})
        return {"model_id": model_id, "version": version}

    def get_model_cards(self, model_id: str | None = None) -> list[dict]:
        q = ("SELECT model_id, version, model_name, params, data_meta, metrics, environment, "
             "created_at, created_by FROM model_cards")
        args: list = []
        if model_id is not None:
            q += " WHERE model_id = ?"
            args.append(model_id)
        q += " ORDER BY model_id, version"
        return [{"model_id": r["model_id"], "version": r["version"], "model_name": r["model_name"],
                 "params": json.loads(r["params"]), "data_meta": json.loads(r["data_meta"]),
                 "metrics": json.loads(r["metrics"]) if r["metrics"] else None,
                 "environment": r["environment"], "created_at": r["created_at"],
                 "created_by": r["created_by"]}
                for r in self._conn.execute(q, args)]

    # --- Ревизии и проверка --------------------------------------------------------
    def revise(self, entity_type: str, entity_id: str, changes: dict, *,
               reason: str, actor: str, recorded_at: str) -> dict:
        """Создаёт новую ревизию сущности. Прежняя помечается superseded_by.

        Если текущая запись VERIFIED — новая ревизия получает REAL_OBSERVED,
        pending_verification=1 и флаг REVERIFICATION_REQUIRED (исправленная
        VERIFIED-запись требует повторной проверки).
        Изменять entity_id и session_id наблюдения запрещено (разрыв ссылок).
        """
        if not reason or not reason.strip():
            raise ValidationError("ревизия требует причину (reason)", code="E_REASON_REQUIRED")
        table = _ENTITY_TABLE.get(entity_type)
        if table is None:
            raise ValidationError(f"неизвестный тип сущности {entity_type!r}", code="E_BAD_ENTITY")
        current = self.get_current(entity_type, entity_id)
        payload = dict(current["payload"])
        forbidden = {"session_id"} if entity_type == "observation" else set()
        id_field = "observation_id" if entity_type == "observation" else "session_id"
        for key, value in changes.items():
            if key in forbidden or key == id_field:
                raise ValidationError(
                    f"поле {key!r} изменять ревизией запрещено", code="E_IMMUTABLE_FIELD")
            payload[key] = value
        # Пересобрать запись через контракт и повторно проверить контекстные-независимые правила.
        try:
            if entity_type == "session":
                from .contracts import parse_session_row  # локальный импорт против цикла
                row_dict = {k: ("" if payload.get(k) is None else str(payload.get(k))) for k in
                            ("session_id", "school_id", "service_date", "timezone", "meal_type",
                             "attendance_count", "meals_requested", "meals_served", "demand_censored",
                             "evidence_status", "study_phase", "source_ref", "observed_at", "recorded_at")}
                row_dict["notes"] = payload.get("notes") or ""
                rec = parse_session_row(row_dict, row_number=0)
                rec.quality_flags = list(current["quality_flags"])
            else:
                from .contracts import parse_observation_row
                keys = ("observation_id", "session_id", "dish_id", "prepared_mass_g", "served_mass_g",
                        "production_waste_g", "serving_waste_g", "plate_waste_g", "avoidable_waste_g",
                        "avoidability_status", "evidence_status", "source_ref", "observed_at", "recorded_at")
                row_dict = {k: ("" if payload.get(k) is None else str(payload.get(k))) for k in keys}
                row_dict["notes"] = payload.get("notes") or ""
                rec = parse_observation_row(row_dict, row_number=0)
                rec.quality_flags = list(current["quality_flags"])
        except ValidationError as e:
            raise ValidationError(
                f"ревизия отклонена: изменённые значения нарушают контракт ({e})",
                code="E_REVISION_INVALID",
                details={"parse_error": e.to_dict()}) from e
        issues = revalidate_record(rec)
        errors = [i for i in issues if i.severity == "error"]
        if errors:
            raise ValidationError(
                "ревизия отклонена валидацией: " + "; ".join(f"{i.code}: {i.message}" for i in errors),
                code="E_REVISION_INVALID",
                details={"issues": [i.to_dict() for i in errors]})
        warnings = [i.code for i in issues if i.severity == "warning"]

        was_verified = current["evidence_status"] == enums.VERIFIED
        new_status = enums.REAL_OBSERVED if was_verified else current["evidence_status"]
        new_flags = list(rec.quality_flags)
        if was_verified:
            new_flags.append("REVERIFICATION_REQUIRED")
        pending = 1 if was_verified else current["pending_verification"]
        now = self._clock()
        with self._conn:
            cur = self._conn.execute(
                "INSERT INTO revisions(entity_type, entity_id, supersedes_revision_id, payload, "
                "evidence_status, recorded_at, quality_flags, pending_verification, created_by, "
                "created_at, dataset_version) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (entity_type, entity_id, current["revision_id"],
                 json.dumps(rec.to_payload(), ensure_ascii=False), new_status,
                 recorded_at, json.dumps(new_flags, ensure_ascii=False), pending, actor, now,
                 current["dataset_version"]),
            )
            new_rev = cur.lastrowid
            self._conn.execute("UPDATE revisions SET superseded_by = ? WHERE revision_id = ?",
                               (new_rev, current["revision_id"]))
            if entity_type == "session":
                self._conn.execute(
                    "UPDATE sessions_current SET revision_id=?, evidence_status=?, recorded_at=?, "
                    "pending_verification=?, dataset_version=? WHERE entity_id=?",
                    (new_rev, new_status, recorded_at, pending, current["dataset_version"], entity_id))
            else:
                self._conn.execute(
                    "UPDATE observations_current SET revision_id=?, evidence_status=?, recorded_at=?, "
                    "pending_verification=?, dataset_version=? WHERE entity_id=?",
                    (new_rev, new_status, recorded_at, pending, current["dataset_version"], entity_id))
            self._audit(actor, "REVISION_CREATED", entity_type=entity_type, entity_id=entity_id,
                        revision_id=new_rev,
                        details={"reason": reason, "changes": changes,
                                 "warnings": warnings, "recorded_at": recorded_at})
            if was_verified:
                self._audit(actor, "VERIFICATION_INVALIDATED", entity_type=entity_type,
                            entity_id=entity_id, revision_id=current["revision_id"],
                            details={"new_revision_id": new_rev,
                                     "note": "исправленная VERIFIED-запись требует повторной проверки"})
        return {"revision_id": new_rev, "supersedes": current["revision_id"],
                "evidence_status": new_status, "pending_verification": bool(pending),
                "quality_flags": new_flags, "warnings": warnings}

    def verify(self, entity_type: str, entity_id: str, *, verifier: str, method: str,
               source_ref: str, actor: str, notes: str | None = None) -> dict:
        """Процедура VERIFIED: отдельная проверка реальной записи человеком/методом.

        SYNTHETIC проверке не подлежит (защита от легализации искусственных данных).
        Повторная проверка разрешена для pending_verification-ревизий.
        """
        current = self.get_current(entity_type, entity_id)
        if current["evidence_status"] == enums.SYNTHETIC:
            raise GuardError(
                f"{entity_type} {entity_id!r} имеет статус SYNTHETIC и не может стать VERIFIED; "
                "синтетика не превращается в реальное доказательство",
                code="E_SYNTHETIC_CANNOT_BE_VERIFIED")
        if current["evidence_status"] == enums.VERIFIED and not current["pending_verification"]:
            raise GuardError(f"{entity_type} {entity_id!r} уже VERIFIED",
                             code="E_ALREADY_VERIFIED")
        if current["evidence_status"] not in (enums.REAL_OBSERVED, enums.VERIFIED):
            raise GuardError(
                f"VERIFIED применим только к REAL_OBSERVED-записям; текущий статус "
                f"{current['evidence_status']}", code="E_VERIFY_WRONG_STATUS")
        if not verifier.strip() or not method.strip():
            raise ValidationError("verify требует verifier и method", code="E_VERIFY_PARAMS_REQUIRED")
        now = self._clock()
        with self._conn:
            self._conn.execute(
                "INSERT INTO verifications(entity_type, entity_id, revision_id, verifier, method, "
                "source_ref, verified_at, notes) VALUES (?,?,?,?,?,?,?,?)",
                (entity_type, entity_id, current["revision_id"], verifier, method, source_ref, now, notes))
            self._conn.execute(
                "UPDATE revisions SET evidence_status = ?, pending_verification = 0 WHERE revision_id = ?",
                (enums.VERIFIED, current["revision_id"]))
            table = _ENTITY_TABLE[entity_type]
            self._conn.execute(
                f"UPDATE {table} SET evidence_status = ?, pending_verification = 0 "
                "WHERE entity_id = ? AND revision_id = ?",
                (enums.VERIFIED, entity_id, current["revision_id"]))
            self._audit(actor, "VERIFY_APPLIED", entity_type=entity_type, entity_id=entity_id,
                        revision_id=current["revision_id"],
                        details={"verifier": verifier, "method": method, "source_ref": source_ref})
        return {"entity_id": entity_id, "revision_id": current["revision_id"],
                "evidence_status": enums.VERIFIED}

    # --- Аудит и самопроверка ------------------------------------------------------
    def audit_tail(self, n: int = 50) -> list[dict]:
        rows = self._conn.execute(
            "SELECT audit_id, ts, actor, action, entity_type, entity_id, revision_id, details "
            "FROM audit_events ORDER BY audit_id DESC LIMIT ?", (n,)).fetchall()
        return [{"audit_id": r["audit_id"], "ts": r["ts"], "actor": r["actor"], "action": r["action"],
                 "entity_type": r["entity_type"], "entity_id": r["entity_id"],
                 "revision_id": r["revision_id"], "details": json.loads(r["details"])}
                for r in rows]

    def stats(self) -> dict:
        s = self._conn.execute("SELECT COUNT(*) n FROM sessions_current").fetchone()["n"]
        o = self._conn.execute("SELECT COUNT(*) n FROM observations_current").fetchone()["n"]
        ev = {r["evidence_status"]: r["n"] for r in self._conn.execute(
            "SELECT evidence_status, COUNT(*) n FROM revisions GROUP BY evidence_status")}
        return {"sessions": s, "observations": o, "evidence_status_counts": ev,
                "environment": self.environment, "path": str(self.path)}

    def selfcheck(self) -> list[dict]:
        """Проверка инвариантов хранилища. Возвращает список {check, ok, details}."""
        out: list[dict] = []

        def add(name: str, ok: bool, details=None):
            out.append({"check": name, "ok": bool(ok), "details": details or {}})

        # 1. Маркер среды.
        row = self._conn.execute("SELECT value FROM meta WHERE key='environment'").fetchone()
        add("environment_marker", row is not None and row["value"] in enums.ENVIRONMENTS,
            {"environment": row["value"] if row else None})
        # 2. Указатели current ссылаются на существующие ревизии той же сущности.
        for etype, table in _ENTITY_TABLE.items():
            bad = self._conn.execute(
                f"SELECT c.entity_id FROM {table} c LEFT JOIN revisions r "
                "ON r.revision_id = c.revision_id AND r.entity_type = ? AND r.entity_id = c.entity_id "
                "WHERE r.revision_id IS NULL", (etype,)).fetchall()
            add(f"current_points_to_revision[{etype}]", len(bad) == 0,
                {"broken": [b["entity_id"] for b in bad][:20]})
        # 3. VERIFIED-ревизии имеют строку проверки.
        missing = self._conn.execute(
            "SELECT r.revision_id FROM revisions r LEFT JOIN verifications v "
            "ON v.revision_id = r.revision_id "
            "WHERE r.evidence_status = 'VERIFIED' AND v.verification_id IS NULL").fetchall()
        add("verified_has_verification_row", len(missing) == 0,
            {"missing": [m["revision_id"] for m in missing][:20]})
        # 4. Каждая ревизия имеет событие аудита о создании.
        orphans = self._conn.execute(
            "SELECT r.revision_id FROM revisions r LEFT JOIN audit_events a "
            "ON a.revision_id = r.revision_id AND a.action IN ('RECORD_CREATED','REVISION_CREATED') "
            "WHERE a.audit_id IS NULL").fetchall()
        add("revision_has_audit", len(orphans) == 0,
            {"missing": [o["revision_id"] for o in orphans][:20]})
        # 5. Отрицательных масс нет (защита в глубину).
        bad_mass = []
        for r in self._conn.execute("SELECT revision_id, payload FROM revisions WHERE entity_type='observation'"):
            p = json.loads(r["payload"])
            for f in ("prepared_mass_g", "served_mass_g", "production_waste_g",
                      "serving_waste_g", "plate_waste_g", "avoidable_waste_g"):
                v = p.get(f)
                if v is not None and v < 0:
                    bad_mass.append({"revision_id": r["revision_id"], "field": f, "value": v})
        add("no_negative_mass", len(bad_mass) == 0, {"violations": bad_mass[:20]})
        # 6. Нет смешения сред: REAL не содержит SYNTHETIC, DEMO не содержит REAL.
        if self.environment == enums.ENV_REAL:
            n = self._conn.execute(
                "SELECT COUNT(*) n FROM revisions WHERE evidence_status = 'SYNTHETIC'").fetchone()["n"]
        else:
            n = self._conn.execute(
                "SELECT COUNT(*) n FROM revisions WHERE evidence_status IN ('REAL_OBSERVED','VERIFIED') "
                "AND pending_verification = 0").fetchone()["n"]
        add("no_env_contamination", n == 0, {"violating_revisions": n})
        # 7. Счётчик dataset_version не меньше максимума по ревизиям.
        maxv = self._conn.execute("SELECT COALESCE(MAX(dataset_version),0) m FROM revisions").fetchone()["m"]
        cnt = int(self._conn.execute("SELECT value FROM meta WHERE key='dataset_version'").fetchone()["value"])
        add("dataset_version_counter", cnt >= maxv, {"counter": cnt, "max_used": maxv})
        return out
