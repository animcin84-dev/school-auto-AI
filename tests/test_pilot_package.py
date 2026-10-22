"""Тесты генератора пакета пилота (этап 5b).

Ключевое свойство: документы создаются КОДОМ из sra.phases и
sra.evaluation.design, поэтому содержимое всегда синхронно с воротами.
"""

import shutil
import tempfile
import unittest
from pathlib import Path

from sra.phases import INTERVENTION_CONDITIONS, SHADOW_GATE_READINESS
from sra.pilot.package import FILES, build_package


class TestPilotPackage(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="sra-pilot-"))
        build_package(cls.tmp / "a")
        build_package(cls.tmp / "b")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_all_files_deterministic(self):
        for name in FILES:
            a = (self.tmp / "a" / name).read_bytes()
            b = (self.tmp / "b" / name).read_bytes()
            self.assertEqual(a, b, msg=name)
            self.assertGreater(len(a), 500, msg=name)

    def test_consent_contains_legally_required_honesty(self):
        text = (self.tmp / "a" / "01-soglasie-shkoly.md").read_text(encoding="utf-8")
        self.assertIn("ШАБЛОН", text)
        self.assertIn("юристом", text)                 # честная оговорка о статусе
        self.assertIn("Персональные данные учеников НЕ собираются", text)
        self.assertIn("Отозвать согласие", text)
        self.assertIn("NO_CHANGE", text)

    def test_protocol_matches_contract_and_gates(self):
        text = (self.tmp / "a" / "02-protokol-izmereniy.md").read_text(encoding="utf-8")
        self.assertIn("Asia/Almaty", text)
        self.assertIn("ТОЛЬКО граммы", text)
        self.assertIn("demand_censored=true", text)    # пропуск ≠ 0 спроса
        self.assertIn("НЕ ставить 0 вместо отсутствия измерения", text)
        self.assertIn(f'min_train={SHADOW_GATE_READINESS["min_train"]}', text)
        self.assertIn("protocol_ref", text)

    def test_checklist_quotes_code_conditions_verbatim(self):
        text = (self.tmp / "a" / "03-cheklist-vorot.md").read_text(encoding="utf-8")
        for condition in INTERVENTION_CONDITIONS:
            self.assertIn(condition, text)
        self.assertIn("E_INTERVENTION_BLOCKED_STAGE5", text)
        self.assertIn("НИКОГДА не исполняются", text)

    def test_effect_plan_marks_foreign_numbers(self):
        text = (self.tmp / "a" / "04-plan-ocenki-effekta.md").read_text(encoding="utf-8")
        self.assertIn("НЕ переносить как ожидания", text)
        self.assertIn("EMPIRICAL_STUDY", text)
        self.assertIn("NORM_KZ", text)
        self.assertIn("~65 дней/группу", text)          # наш расчёт по реальным данным FI
        self.assertIn("«код готов» ≠ «пилот готов» ≠ «эффект доказан»", text)


if __name__ == "__main__":
    unittest.main()
