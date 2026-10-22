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


if __name__ == "__main__":
    unittest.main()
