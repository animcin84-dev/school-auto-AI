"""Тесты read-only дашборда (sra/dashboard.py, P3).

Проверяются: страницы отдают 200 с контентом, чужая школа — 404,
POST — 405 (только чтение), HTML-экранирование пользовательского текста
(название школы с HTML-инъекцией), отсутствие school_id — 400.
"""
from __future__ import annotations

import http.client
import tempfile
import threading
import unittest
from datetime import date, datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path

from sra import enums
from sra.contracts import SessionRecord
from sra.dashboard import _make_handler
from sra.store import EvidenceStore

TZ = timezone.utc


def _mk_session(i, school_id, served=100, notes=None):
    d = date(2026, 3, 1) + timedelta(days=i)
    dt = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc) + timedelta(days=i)
    return SessionRecord(
        session_id=f"SES-{school_id}-{i}", school_id=school_id, service_date=d,
        timezone="Asia/Almaty", meal_type="LUNCH", attendance_count=served,
        meals_requested=None, meals_served=served,
        demand_censored=enums.CENSOR_FALSE, evidence_status=enums.SYNTHETIC,
        study_phase=None, source_ref="t:dash", observed_at=dt, recorded_at=dt,
        notes=notes)


class TestDashboard(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        db = cls.tmp / "dash.sqlite3"
        store = EvidenceStore(db, enums.ENV_DEMO)
        try:
            store.import_sessions([_mk_session(i, "SCH-T") for i in range(4)],
                                  actor="t")
            # школа с HTML-инъекцией в имени (проверка экранирования)
            store.import_sessions([_mk_session(0, "SCH<b>X")], actor="t")
        finally:
            store.close()
        cls.httpd = ThreadingHTTPServer(
            ("127.0.0.1", 0), _make_handler(str(db), enums.ENV_DEMO))
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever,
                                      daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def _get(self, path, method="GET"):
        con = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        con.request(method, path)
        resp = con.getresponse()
        body = resp.read().decode("utf-8")
        con.close()
        return resp.status, body

    def test_index_lists_escaped_school(self):
        status, body = self._get("/")
        self.assertEqual(status, 200)
        self.assertIn("SCH-T", body)
        # инъекция экранирована
        self.assertIn("SCH&lt;b&gt;X", body)
        self.assertNotIn("SCH<b>X", body)
        self.assertIn("СРЕДА: DEMO", body)
        self.assertIn("SYNTHETIC", body)

    def test_pages_ok(self):
        for page in ("digest", "ml-status", "quality", "norms", "alerts"):
            status, body = self._get(f"/{page}?school_id=SCH-T")
            self.assertEqual(status, 200, page)
        status, body = self._get("/digest?school_id=SCH-T")
        self.assertIn("Дайджест", body)

    def test_foreign_school_404(self):
        status, body = self._get("/digest?school_id=SCH-OTHER")
        self.assertEqual(status, 404)
        self.assertIn("не найдена", body)

    def test_missing_school_id_400(self):
        status, _ = self._get("/digest")
        self.assertEqual(status, 400)

    def test_post_rejected_read_only(self):
        status, body = self._get("/", method="POST")
        self.assertEqual(status, 405)
        self.assertIn("только читает", body)

    def test_unknown_page_404(self):
        status, _ = self._get("/nope")
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
