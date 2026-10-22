"""Тесты репетиции учебного года (sra/rehearsal.py, AUDIT §D.18).

Проверяются: полный протокол на синтетике (генерация → фазы → импорт →
настоящие ворота PhaseManager → сравнение фаз → backtest'ы → нормы →
дайджест), намеренная блокировка INTERVENTION с перечнем условий, соответствие
сценарного эффекта границе фаз (эффект генератора начинается с INTERVENTION),
маркировка SYNTHETIC/DEMO во всём отчёте.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from sra import enums
from sra.rehearsal import PHASE_PLAN, _intervention_start


class TestRehearsalPlan(unittest.TestCase):
    def test_intervention_start_matches_plan(self):
        # CALIBRATION 5 + BASELINE 60 + SHADOW 20 = 85; INTERVENTION не в сумме
        self.assertEqual(_intervention_start(), 85)

    def test_phase_of_boundaries(self):
        self.assertEqual(PHASE_PLAN[0], (enums.PHASE_CALIBRATION, 5))
        from sra.rehearsal import _phase_of
        self.assertEqual(_phase_of(0), enums.PHASE_CALIBRATION)
        self.assertEqual(_phase_of(4), enums.PHASE_CALIBRATION)
        self.assertEqual(_phase_of(5), enums.PHASE_BASELINE)
        self.assertEqual(_phase_of(84), enums.PHASE_SHADOW)
        self.assertEqual(_phase_of(85), enums.PHASE_INTERVENTION)
        self.assertEqual(_phase_of(144), enums.PHASE_INTERVENTION)
        self.assertEqual(_phase_of(145), enums.PHASE_FOLLOWUP)


class TestRehearsalRun(unittest.TestCase):
    """Полный прогон (170 дней) — секунды на stdlib."""

    def test_full_protocol(self):
        from sra.rehearsal import run_rehearsal
        tmp = Path(tempfile.mkdtemp())
        r = run_rehearsal(tmp, seed=7, days=170)
        self.assertEqual(r["data_nature"], enums.SYNTHETIC)
        self.assertEqual(r["environment"], enums.ENV_DEMO)
        steps = r["steps"]
        self.assertTrue(steps["generate"]["ok"])
        self.assertTrue(steps["import"]["ok"])

        # фазовые переходы: три вперёд OK, INTERVENTION заблокирован
        tr = {t["phase"]: t for t in steps["phases"]["transitions"]}
        self.assertEqual(tr[enums.PHASE_CALIBRATION]["result"], "OK")
        self.assertEqual(tr[enums.PHASE_BASELINE]["result"], "OK")
        self.assertEqual(tr[enums.PHASE_SHADOW]["result"], "OK")
        self.assertEqual(tr[enums.PHASE_INTERVENTION]["result"], "BLOCKED")
        self.assertEqual(tr[enums.PHASE_INTERVENTION]["code"],
                         "E_INTERVENTION_BLOCKED_STAGE5")
        self.assertEqual(len(tr[enums.PHASE_INTERVENTION]["conditions"]),
                         len(__import__("sra.phases", fromlist=["INTERVENTION_CONDITIONS"])
                             .INTERVENTION_CONDITIONS))

        # сценарный эффект: граница фаз совпадает с границей эффекта
        cp = steps["compare_phases"]
        self.assertEqual(cp["status"], "OK")
        self.assertLess(cp["diff_pct_of_baseline"], -10.0)  # фактор 0.75
        self.assertLess((cp.get("welch") or {}).get("p", 1.0), 0.01)

        # backtest'ы на годе — данных достаточно
        self.assertEqual(steps["diners_backtest"]["status"], "OK")
        self.assertEqual(steps["composite_backtest"]["status"], "OK")

        # нормы: порции генератора в диапазонах Санправил
        self.assertEqual(steps["norms"]["status"], "OK")
        # дайджест присутствует и честно помечен
        dg = steps["digest"]
        self.assertTrue(dg["read_only"])

        # отчёт записан и читается
        rep_file = Path(r["report_file"])
        self.assertTrue(rep_file.exists())
        data = json.loads(rep_file.read_text(encoding="utf-8"))
        self.assertEqual(data["steps"]["phases"]["transitions"][-1]["result"],
                         "BLOCKED")
        self.assertTrue(any("РЕПЕТИЦИЯ" in w for w in r["warnings"]))


if __name__ == "__main__":
    unittest.main()
