"""Тесты универсального CSV-импорта по карте (внешние выгрузки, Соцкошелёк)."""

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from sra import enums
from sra.errors import ValidationError
from sra.ingest.csv_map import load_map, parse_csv
from sra.store import EvidenceStore

CSV_RU = """Дата;Код школы;Номер порции;Выдано
2026-10-20;SCH-KZ-001;SW-000001;1
2026-10-20;SCH-KZ-001;SW-000002;1
2026-10-21;SCH-KZ-002;SW-000003;1
"""

MAP = {
    "kind": "sessions",
    "options": {"delimiter": ";"},
    "columns": {"service_date": "Дата", "school_id": "Код школы",
                "session_id": "Номер порции"},
    "constants": {"timezone": "Asia/Almaty", "meal_type": "LUNCH",
                  "evidence_status": "REAL_OBSERVED",
                  "source_ref": "social-wallet:export:test",
                  "demand_censored": "false", "meals_served": "1",
                  "attendance_count": "1", "menu_id": "M-07"},
    "derive": {"observed_at": {"date_field": "service_date", "time": "13:30"},
               "recorded_at": {"at_import": True}},
}


class TestCsvMap(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = Path(tempfile.mkdtemp(prefix="sra-map-"))
        self.csv_path = self.tmp / "export.csv"
        self.csv_path.write_text(CSV_RU, encoding="utf-8")
        self.map_path = self.tmp / "map.json"
        self.map_path.write_text(json.dumps(MAP, ensure_ascii=False), encoding="utf-8")
        self.store = EvidenceStore(self.tmp / "open.sqlite3", enums.ENV_OPEN)

    def tearDown(self):
        self.store.close()
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _import(self):
        m = load_map(self.map_path)
        records, rep = parse_csv(self.csv_path, m)
        if rep["errors_total"]:
            return 0, rep
        from sra.validation import validate_sessions
        v = validate_sessions(records, existing_ids=self.store.session_ids())
        if not v.ok:
            raise AssertionError(f"валидация: {[i.to_dict() for i in v.errors][:2]}")
        res = self.store.import_sessions(v.accepted, actor="t")
        return res["imported"], rep

    def test_russian_headers_import(self):
        imported, rep = self._import()
        self.assertEqual(imported, 3)
        sessions = self.store.fetch_sessions()
        self.assertEqual(len(sessions), 3)
        by_ref = {s.source_ref for s in sessions}
        self.assertEqual(by_ref, {"social-wallet:export:test"})
        s = next(s for s in sessions if s.school_id == "SCH-KZ-002")
        self.assertEqual(s.service_date.isoformat(), "2026-10-21")
        self.assertEqual(s.timezone, "Asia/Almaty")
        self.assertEqual(s.attendance_count, 1)
        self.assertEqual(s.demand_censored, enums.CENSOR_FALSE)
        self.assertEqual(s.menu_id, "M-07")     # меню константой карты
        # observed_at выведен из даты + 13:30 по зоне школы
        self.assertEqual(s.observed_at.hour, 13)
        self.assertEqual(s.observed_at.minute, 30)

    def test_missing_column_reported(self):
        self.csv_path.write_text(CSV_RU.replace("Дата;Код школы", "Day;School"),
                                 encoding="utf-8")
        with self.assertRaises(ValidationError) as cm:
            m = load_map(self.map_path)
            parse_csv(self.csv_path, m)
        self.assertEqual(cm.exception.code, "E_COLUMN_NOT_FOUND")
        self.assertIn("Дата", str(cm.exception))

    def test_incomplete_map(self):
        bad = dict(MAP)
        bad["constants"] = {k: v for k, v in MAP["constants"].items()
                            if k != "source_ref"}
        self.map_path.write_text(json.dumps(bad, ensure_ascii=False), encoding="utf-8")
        with self.assertRaises(ValidationError) as cm:
            load_map(self.map_path)
        self.assertEqual(cm.exception.code, "E_MAP_INCOMPLETE")
        self.assertIn("source_ref", str(cm.exception))

    def test_unknown_map_field(self):
        bad = dict(MAP)
        bad["columns"] = dict(MAP["columns"], паста_с_майонезом="X")
        self.map_path.write_text(json.dumps(bad, ensure_ascii=False), encoding="utf-8")
        with self.assertRaises(ValidationError) as cm:
            load_map(self.map_path)
        self.assertEqual(cm.exception.code, "E_BAD_MAP_FIELD")

    def test_bad_row_is_atomic(self):
        self.csv_path.write_text(CSV_RU.replace("2026-10-21", "не дата"),
                                 encoding="utf-8")
        imported, rep = self._import()
        self.assertEqual(imported, 0)
        self.assertEqual(rep["errors_total"], 1)
        self.assertEqual(rep["errors"][0]["row"], 4)
        self.assertEqual(len(self.store.session_ids()), 0)  # атомарность

    def test_derive_requires_date_field_in_columns(self):
        bad = json.loads(json.dumps(MAP, ensure_ascii=False))
        bad["derive"]["observed_at"]["date_field"] = "НетТакой"
        self.map_path.write_text(json.dumps(bad, ensure_ascii=False), encoding="utf-8")
        with self.assertRaises(ValidationError) as cm:
            load_map(self.map_path)
        self.assertEqual(cm.exception.code, "E_BAD_MAP_FIELD")


class TestAggregate(unittest.TestCase):
    """Aggregate-режим: строка = порция «Соцкошелька» → сессия дня (AUDIT №7)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.columns = {"school_id": "Школа", "service_date": "Дата",
                        "meal_type": "Прием", "menu_id": "Меню"}
        self.constants = {"timezone": "Asia/Almaty",
                          "evidence_status": "REAL_OBSERVED",
                          "demand_censored": "false",
                          "source_ref": "social-wallet:export:test-agg"}
        self.derive = {"observed_at": {"date_field": "service_date",
                                       "time": "13:30"},
                       "recorded_at": {"at_import": True}}

    def _map(self, *, pupil="Ученик", extra_options=None):
        options = {"delimiter": ";",
                   "aggregate": ({"pupil_column": pupil} if pupil else {})}
        if extra_options:
            options.update(extra_options)
        return {"kind": "sessions", "columns": dict(self.columns),
                "constants": dict(self.constants), "derive": dict(self.derive),
                "options": options}

    def _write(self, rows, header="Дата;Школа;Прием;Ученик;Меню", name="p.csv"):
        path = self.tmp / name
        lines = [header] + rows
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    def test_portions_fold_into_day_sessions(self):
        path = self._write([
            "2026-10-20;SCH-KZ-001;Обед;P1001;M-3",
            "2026-10-20;SCH-KZ-001;Обед;P1002;M-3",
            "2026-10-20;SCH-KZ-001;Обед;P1001;M-3",
            "2026-10-20;SCH-KZ-001;Завтрак;P1001;M-3"])
        records, rep = parse_csv(path, load_map(self._map_file(self._map())))
        self.assertEqual(rep["kind"], "sessions-aggregate")
        self.assertEqual(rep["sessions_built"], 2)
        by_id = {r.session_id: r for r in records}
        lunch = [r for k, r in by_id.items() if "ОБЕД" in k][0]
        self.assertEqual(lunch.meals_served, 3)          # 3 порции
        self.assertEqual(lunch.attendance_count, 2)      # 2 уникальных ученика
        self.assertEqual(lunch.menu_id, "M-3")
        self.assertEqual(lunch.demand_censored, enums.CENSOR_FALSE)

    def test_no_pupil_column_attendance_none(self):
        path = self._write([
            "2026-10-20;SCH-KZ-001;Обед;;M-3",
            "2026-10-20;SCH-KZ-001;Обед;;M-3"])
        records, rep = parse_csv(path, load_map(self._map_file(self._map(pupil=None))))
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].meals_served, 2)
        self.assertIsNone(records[0].attendance_count)   # посещаемость не выдумываем

    def test_conflicting_group_value_is_atomic_error(self):
        path = self._write([
            "2026-10-20;SCH-KZ-001;Обед;P1001;M-3",
            "2026-10-20;SCH-KZ-001;Обед;P1002;M-9"])     # другое меню в группе
        records, rep = parse_csv(path, load_map(self._map_file(self._map())))
        self.assertEqual(records, [])
        self.assertEqual(rep["errors"][0]["code"], "E_AGG_CONFLICT_VALUE")
        self.assertIn("M-9", rep["errors"][0]["message"])

    def test_observations_with_aggregate_rejected(self):
        bad = {"kind": "observations", "columns": dict(self.columns),
               "constants": dict(self.constants), "derive": dict(self.derive),
               "options": {"aggregate": {"pupil_column": "Ученик"}}}
        with self.assertRaises(ValidationError) as cm:
            load_map(self._map_file(bad))
        self.assertEqual(cm.exception.code, "E_AGGREGATE_CONFLICT")

    def test_bad_aggregate_spec_rejected(self):
        bad = self._map()
        bad["options"]["aggregate"] = {"unknown_key": 1}
        with self.assertRaises(ValidationError) as cm:
            load_map(self._map_file(bad))
        self.assertEqual(cm.exception.code, "E_BAD_MAP_FIELD")

    def test_bad_date_in_one_row_blocks_batch(self):
        path = self._write([
            "2026-10-20;SCH-KZ-001;Обед;P1001;M-3",
            "не-дата;SCH-KZ-001;Обед;P1002;M-3"])
        records, rep = parse_csv(path, load_map(self._map_file(self._map())))
        self.assertEqual(records, [])                    # атомарность
        self.assertEqual(rep["errors"][0]["row"], 3)

    def _map_file(self, m):
        fp = self.tmp / "map.json"
        fp.write_text(json.dumps(m, ensure_ascii=False), encoding="utf-8")
        return fp



if __name__ == "__main__":
    unittest.main()
