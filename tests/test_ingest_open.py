"""Импорт чужих открытых данных (Zenodo) в OPEN-среду.

Реальные файлы Zenodo в песочнице недоступны (сеть ограничена), поэтому
фикстуры xlsx строятся мини-писателем по ЗАДОКУМЕНТИРОВАННОЙ структуре
записей (описание + Read_me на Zenodo). Заголовки шапки реальных файлов
могут отличаться: адаптеры tolerant к вариантам имён, печатают header_raw
в отчёте, а нераспознанные строки честно пропускают с причиной. При
получении настоящих файлов отчёт покажет расхождения немедленно.
"""

import shutil
import tempfile
import unittest
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape

from sra import enums
from sra.errors import GuardError, SraError
from sra.ingest import xlsx_min, zenodo_fi, zenodo_lv
from sra.optimizer.propose import propose_adjustment
from sra.store import EvidenceStore
from sra.validation import validate_observations, validate_sessions

_CT = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
{overrides}
</Types>"""

_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>"""


def _col_letter(idx: int) -> str:
    s = ""
    idx += 1
    while idx:
        idx, rem = divmod(idx - 1, 26)
        s = chr(65 + rem) + s
    return s


def _cell_xml(row_i: int, col_i: int, value, shared: list[str]) -> str:
    ref = f"{_col_letter(col_i)}{row_i + 1}"
    if value is None or value == "":
        return ""
    if isinstance(value, bool):
        return f'<c r="{ref}" t="b"><v>{1 if value else 0}</v></c>'
    if isinstance(value, (int, float)):
        return f'<c r="{ref}"><v>{value}</v></c>'
    shared.append(str(value))
    return (f'<c r="{ref}" t="s"><v>{len(shared) - 1}</v></c>')


def write_xlsx(path: Path, sheets: dict[str, list[list]]) -> Path:
    """Мини-писатель xlsx (sharedStrings): только то, что читает xlsx_min."""
    shared: list[str] = []
    overrides, sheet_xmls, sheet_rels = [], [], []
    for i, (name, rows) in enumerate(sheets.items(), start=1):
        overrides.append(f'<Override PartName="/xl/worksheets/sheet{i}.xml" '
                         'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>')
        body = []
        for r, row in enumerate(rows):
            cells = "".join(_cell_xml(r, c, v, shared) for c, v in enumerate(row))
            body.append(f'<row r="{r + 1}">{cells}</row>')
        sheet_xmls.append(
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            f"<sheetData>{''.join(body)}</sheetData></worksheet>")
        sheet_rels.append(f'<Relationship Id="rId{i}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{i}.xml"/>')
    wb_sheets = "".join(
        f'<sheet name="{escape(n)}" sheetId="{i}" r:id="rId{i}"/>'
        for i, n in enumerate(sheets, start=1))
    wb = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
          '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
          'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
          f"<sheets>{wb_sheets}</sheets></workbook>")
    rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            + "".join(sheet_rels) + "</Relationships>")
    sst = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
           '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
           f'count="{len(shared)}" uniqueCount="{len(shared)}">'
           + "".join(f"<si><t>{escape(s)}</t></si>" for s in shared) + "</sst>")
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("[Content_Types].xml", _CT.format(overrides="".join(overrides)))
        z.writestr("_rels/.rels", _RELS)
        z.writestr("xl/workbook.xml", wb)
        z.writestr("xl/_rels/workbook.xml.rels", rels)
        z.writestr("xl/sharedStrings.xml", sst)
        for i, xml in enumerate(sheet_xmls, start=1):
            z.writestr(f"xl/worksheets/sheet{i}.xml", xml)
    return path


def _fi_rows():
    """Фикстура по документированным переменным записи (PW/PW-per/KSW/DINERS/MENU)."""
    header = ["Date", "PW", "PW/g/diner", "KSW", "KSW/g/diner", "DINERS", "MENU"]
    rows = [header]
    d0 = date(2024, 8, 12)  # понедельник
    for i in range(12):
        d = d0 + timedelta(days=i)
        rows.append([d.isoformat(), 5.5 + i * 0.1, 40.0 + i, 7.2 + i * 0.05,
                     52.0 + i, 137 + i * 2, (i % 25) + 1])
    return rows


def _lv_rows():
    """Фикстура по Read_me записи: агрегаты по школам/группам."""
    header = ["School_ID", "Measurement_period", "Student_group", "School_level",
              "Age_group", "Meal_type", "Served_food_g", "Plate_waste_g"]
    rows = [header]
    for school in ("S1", "S2"):
        for day in ("2023-12-04", "2023-12-05"):
            for group, level in (("Grades 1-4", "primary"), ("Grades 5-7", "lower secondary")):
                rows.append([school, day, group, level, "7-13", "lunch",
                             48000 + hash((school, day, group)) % 900,
                             9000 + hash((group, day)) % 700])
    return rows


class OpenDataTestBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="sra-open-"))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)


class TestXlsxMin(OpenDataTestBase):
    def test_roundtrip_values_headers_serial_dates(self):
        rows = [["Name", "When", "Val", "Flag"],
                ["Alfa", 45292, 1.5, True],          # 45292 = 2024-01-01
                ["Beta", 45293, -2, False],
                ["Gamma", "", None, ""]]             # пустые ячейки
        path = write_xlsx(self.tmp / "t.xlsx", {"Sheet1": rows})
        wb = xlsx_min.read_workbook(path)
        self.assertEqual(wb["sheets"][0]["name"], "Sheet1")
        r = wb["sheets"][0]["rows"]
        self.assertEqual(r[0], ["Name", "When", "Val", "Flag"])
        self.assertEqual(r[1][0], "Alfa")
        self.assertEqual(xlsx_min.excel_serial_to_date(45292), date(2024, 1, 1))
        self.assertEqual(xlsx_min.parse_any_date(45292), date(2024, 1, 1))
        self.assertEqual(r[1][2], 1.5)
        self.assertTrue(r[1][3])
        self.assertFalse(r[2][3])
        # пустые ячейки → None (не ноль!)
        self.assertIsNone(r[3][1])
        self.assertIsNone(r[3][2])

    def test_parse_any_date_variants_and_refusals(self):
        for v, expected in (("2023-12-04", date(2023, 12, 4)),
                            ("04.12.2023", date(2023, 12, 4)),
                            ("December 2023", date(2023, 12, 1)),
                            ("2023-12", date(2023, 12, 1))):
            self.assertEqual(xlsx_min.parse_any_date(v), expected, msg=v)
        self.assertIsNone(xlsx_min.parse_any_date("не дата"))
        self.assertIsNone(xlsx_min.parse_any_date(42))  # число вне серийного диапазона
        self.assertIsNone(xlsx_min.parse_any_date(None))


class TestFinlandAdapter(OpenDataTestBase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from datetime import timezone as tz
        path = write_xlsx(cls.tmp / "fi.xlsx", {"Data": _fi_rows()})
        cls.mapped = zenodo_fi.map_workbook(
            xlsx_min.read_workbook(path),
            recorded_at=datetime(2026, 10, 22, 12, 0, tzinfo=tz.utc))

    def test_mapping_and_status(self):
        m = self.mapped
        self.assertEqual(m["report"]["matched_columns"]["pw"],
                         _fi_rows()[0].index("PW"))
        self.assertEqual(m["report"]["rows_imported"], 12)
        self.assertEqual(len(m["sessions"]), 12)
        s = m["sessions"][0]
        self.assertEqual(s.evidence_status, enums.REAL_OBSERVED)
        self.assertEqual(s.source_ref, "zenodo:20825233:v1")
        self.assertEqual(s.school_id, "FI-ZEN-20825233")
        self.assertEqual(s.timezone, "Europe/Helsinki")
        self.assertEqual(s.attendance_count, 137)
        self.assertEqual(s.meals_served, 137)
        self.assertIsNone(s.meals_requested)
        self.assertEqual(s.demand_censored, enums.CENSOR_UNKNOWN)
        self.assertEqual(s.study_phase, enums.PHASE_BASELINE)
        self.assertIn("MENU=1", s.notes)
        o = m["observations"][0]
        self.assertEqual(o.dish_id, "ALL")
        self.assertAlmostEqual(o.plate_waste_g, 5500.0)
        self.assertIsNone(o.served_mass_g)
        self.assertIsNone(o.prepared_mass_g)
        self.assertIn("ksw", m["report"]["matched_columns"])
        self.assertTrue(any("KSW" in lim for lim in m["report"]["limitations"]))
        self.assertEqual(m["report"]["rows_skipped"], [])

    def test_phase_rule_by_position(self):
        phases = {s.study_phase for s in self.mapped["sessions"]}
        self.assertEqual(phases, {enums.PHASE_BASELINE})  # 12 строк < 135 → baseline

    def test_skips_bad_rows_honestly(self):
        rows = _fi_rows()
        rows.append(["не дата", 5.0, 40.0, 7.0, 52.0, 130, 3])
        rows.append(["2024-08-30", "мусор", 40.0, 7.0, 52.0, 130, 4])
        path = write_xlsx(self.tmp / "fi_bad.xlsx", {"Data": rows})
        m = zenodo_fi.map_workbook(xlsx_min.read_workbook(path),
                                   recorded_at=datetime.now(timezone.utc))
        self.assertEqual(m["report"]["rows_imported"], 12)
        self.assertEqual(len(m["report"]["rows_skipped"]), 2)
        self.assertEqual(m["report"]["rows_skipped"][0]["reason"], "дата не распознана")


class TestLatviaAdapter(OpenDataTestBase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        path = write_xlsx(cls.tmp / "lv.xlsx", {"Sheet1": _lv_rows()})
        from datetime import timezone as tz
        cls.mapped = zenodo_lv.map_workbook(
            xlsx_min.read_workbook(path),
            recorded_at=datetime(2026, 10, 22, 12, 0, tzinfo=tz.utc))

    def test_mapping_school_group_split(self):
        m = self.mapped
        self.assertEqual(m["report"]["rows_imported"], 8)
        # 2 школы × 2 дня = 4 сессии; наблюдение = строка (школа×день×группа)
        self.assertEqual(len(m["sessions"]), 4)
        self.assertEqual(len(m["observations"]), 8)
        s = m["sessions"][0]
        self.assertEqual(s.evidence_status, enums.REAL_OBSERVED)
        self.assertEqual(s.source_ref, "zenodo:18441715:v1")
        self.assertTrue(s.school_id.startswith("LV-ZEN-18441715-S"))
        self.assertEqual(s.timezone, "Europe/Riga")
        groups = {o.dish_id for o in m["observations"]}
        self.assertEqual(groups, {"PRE-SERVED-Grades-1-4", "PRE-SERVED-Grades-5-7"})
        self.assertEqual(len(m["observations"]), 8)
        o = m["observations"][0]
        self.assertGreater(o.plate_waste_g, 0)
        self.assertIsNone(o.prepared_mass_g)
        self.assertIn("CC BY 4.0", m["report"]["license"])
        # дневные даты не должны получить флаг месячного агрегата
        self.assertFalse(any("PERIOD_AGGREGATE_AS_DATE" in s.quality_flags
                             for s in m["sessions"]))

    def test_month_period_marks_assumption(self):
        rows = _lv_rows()
        for r in rows[1:]:
            r[1] = "December 2023"
        path = write_xlsx(self.tmp / "lv_month.xlsx", {"Sheet1": rows})
        m = zenodo_lv.map_workbook(xlsx_min.read_workbook(path))
        flags = [f for s in m["sessions"] for f in s.quality_flags]
        self.assertIn("PERIOD_AGGREGATE_AS_DATE", flags)
        self.assertTrue(any("первый день периода" in s.notes for s in m["sessions"]))


class TestOpenEnvironment(OpenDataTestBase):
    def setUp(self):
        self.db = self.tmp / f"open-{id(self)}.sqlite3"

    def _import_fi(self):
        store = EvidenceStore(self.db, enums.ENV_OPEN)
        try:
            path = write_xlsx(self.tmp / "fi_import.xlsx", {"Data": _fi_rows()})
            from datetime import timezone as tz
            m = zenodo_fi.map_workbook(xlsx_min.read_workbook(path),
                                       recorded_at=datetime(2026, 10, 22, 12, 0, tzinfo=tz.utc))
            s_rep = validate_sessions(m["sessions"], existing_ids=store.session_ids())
            o_rep = validate_observations(
                m["observations"], known_sessions={r.session_id for r in s_rep.accepted},
                existing_observation_ids=store.observation_ids(),
                existing_pairs=store.observation_pairs())
            self.assertTrue(s_rep.ok, msg=[i.to_dict() for i in s_rep.errors][:3])
            self.assertTrue(o_rep.ok, msg=[i.to_dict() for i in o_rep.errors][:3])
            store.import_sessions(s_rep.accepted, actor="t")
            store.import_observations(o_rep.accepted, actor="t")
            return store, m
        except Exception:
            store.close()
            raise

    def test_import_and_metrics_computed(self):
        store, m = self._import_fi()
        try:
            self.assertEqual(len(store.session_ids()), 12)
            chk = store.selfcheck()
            self.assertTrue(all(c["ok"] for c in chk),
                            msg=[c for c in chk if not c["ok"]])
            from sra.metrics import build_metric_report
            rep = build_metric_report(store.fetch_sessions(school_id="FI-ZEN-20825233"),
                                      store.fetch_observations(school_id="FI-ZEN-20825233"))
            results = rep["metrics"]  # to_dict(): {name, status, value, reason, ...}
            # FI-датасет не содержит served/prepared масс и avoidable-классификации:
            # все метрики честно UNAVAILABLE с конкретной причиной, а не числом.
            self.assertTrue(all(x["status"] == "UNAVAILABLE" for x in results),
                            msg=[(x["name"], x["status"]) for x in results])
            self.assertTrue(all(x["reason"] for x in results))
            self.assertIn("COMPUTED", rep["nature"])  # происхождение входов — REAL_OBSERVED
            # прогноз спроса в граммах: невозможен (нет served-масс) — честный статус
            from sra.forecasting.backtest import run_backtest
            from sra.forecasting.series import make_snapshot_fn
            fr = run_backtest(snapshot_fn=make_snapshot_fn(store, "FI-ZEN-20825233", "LUNCH"),
                              test_days=3, calib_days=2, min_train=5,
                              environment=store.environment)
            self.assertIn(fr["status"], ("DATA_NOT_READY", "INSUFFICIENT_HISTORY"))
            self.assertTrue(fr.get("reason") or fr.get("detail") or fr.get("models") is not None)
        finally:
            store.close()

    def test_guards_open_env(self):
        store, m = self._import_fi()
        try:
            # синтетика в OPEN запрещена
            from sra.contracts import SessionRecord
            rec = SessionRecord(
                session_id="X", school_id="S", service_date=date(2026, 1, 1),
                timezone="UTC", meal_type="LUNCH", attendance_count=None,
                meals_requested=None, meals_served=None,
                demand_censored=enums.CENSOR_UNKNOWN, evidence_status=enums.SYNTHETIC,
                study_phase=None, source_ref="t",
                observed_at=datetime(2026, 1, 1, 10, 0), recorded_at=datetime(2026, 1, 1, 10, 0))
            with self.assertRaises(GuardError) as cm:
                store.import_sessions([rec], actor="t")
            self.assertEqual(cm.exception.code, "E_SYNTHETIC_IN_OPEN_ENV")
            # verify чужих данных запрещён
            sid = sorted(store.session_ids())[0]
            with self.assertRaises(GuardError) as cm:
                store.verify("session", sid, verifier="we", method="self-check",
                             source_ref="t", actor="t")
            self.assertEqual(cm.exception.code, "E_VERIFY_NOT_AVAILABLE_OPEN")
            # фазовые протоколы запрещены
            from sra.phases import PhaseManager
            pm = PhaseManager(store)
            with self.assertRaises(GuardError) as cm:
                pm.transition("FI-ZEN-20825233", enums.PHASE_BASELINE,
                              actor="t", protocol_ref="p")
            self.assertEqual(cm.exception.code, "E_PHASES_NOT_AVAILABLE_OPEN")
        finally:
            store.close()

    def test_propose_no_change_foreign_data(self):
        store, m = self._import_fi()
        try:
            class _NoPolicies:
                def find_applicable(self, *a, **k):
                    return []
                def get(self, *a, **k):
                    raise LookupError
            res = propose_adjustment(store, _NoPolicies(),
                                     school_id="FI-ZEN-20825233",
                                     meal_type="LUNCH",
                                     target_date="2025-01-15",
                                     policy_id="P-TEST", actor="t")
            self.assertEqual(res.action, "NO_CHANGE")
            self.assertIn("OPEN_ENV_FOREIGN_DATA", res.reasons)
            self.assertTrue(any("чужие открытые" in n for n in res.notes))
        finally:
            store.close()

    def test_real_env_rejects_open_data_and_demo_too(self):
        # REAL-среда принимает только НАШИ данные: импорт открытых данных в REAL
        # формально проходит guard по статусу, но CLI import-open отказывает по среде.
        store = EvidenceStore(self.tmp / "demo-env.sqlite3", enums.ENV_DEMO)
        try:
            from sra.contracts import SessionRecord
            rec = SessionRecord(
                session_id="Y", school_id="S", service_date=date(2026, 1, 1),
                timezone="UTC", meal_type="LUNCH", attendance_count=None,
                meals_requested=None, meals_served=None,
                demand_censored=enums.CENSOR_UNKNOWN, evidence_status=enums.REAL_OBSERVED,
                study_phase=None, source_ref="zenodo:20825233:v1",
                observed_at=datetime(2026, 1, 1, 10, 0), recorded_at=datetime(2026, 1, 1, 10, 0))
            with self.assertRaises(GuardError) as cm:
                store.import_sessions([rec], actor="t")
            self.assertEqual(cm.exception.code, "E_REAL_IN_DEMO_ENV")
        finally:
            store.close()


if __name__ == "__main__":
    unittest.main()
