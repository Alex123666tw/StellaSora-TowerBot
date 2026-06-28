from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import numpy as np

from core.bot import StateMachine


class AutoExecuteTests(unittest.TestCase):
    def test_finalize_failure_writes_complete_evidence_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path.cwd()
            project_dir = Path(tmp)
            (project_dir / "logs").mkdir()
            (project_dir / "bot.log").write_text("bot log", encoding="utf-8")
            try:
                import os

                os.chdir(project_dir)
                machine = StateMachine.__new__(StateMachine)
                machine._failure_finalized = False
                frame = np.zeros((16, 16, 3), dtype=np.uint8)
                machine.ctx = SimpleNamespace(
                    session_id="session_test",
                    session_run_dir=None,
                    current_state="STATE_EVENT",
                    run_count=0,
                    max_runs=1,
                    success_count=0,
                    current_floor=7,
                    current_money=123,
                    target_notes={"A": 2},
                    current_notes={"A": 1},
                    card_counter_enabled=False,
                    card_counter_initial_total=None,
                    card_counter_target_total=None,
                    card_counter_current_total=None,
                    preflight_detected_state="STATE_HOME",
                    click_trace=[{"source": "ocr_text", "x": 1, "y": 2}],
                    ocr_trace=[{"purpose": "click_text", "matched_text": "拿走"}],
                    state_trace=[{"source": "detector", "previous": "STATE_HOME", "current": "STATE_EVENT"}],
                    last_frame=frame,
                    preflight_frame=frame,
                    failure_reason=None,
                    failure_dir=None,
                    running=True,
                )

                failure_dir = machine.finalize_failure("handler_exception", {"error": "boom"})
                self.assertIsNotNone(failure_dir)
                failure_path = Path(failure_dir)
                self.assertTrue((failure_path / "summary.json").exists())
                self.assertTrue((failure_path / "click_trace.json").exists())
                self.assertTrue((failure_path / "ocr_trace.json").exists())
                self.assertTrue((failure_path / "state_trace.json").exists())
                self.assertTrue((failure_path / "last_frame.png").exists())
                self.assertTrue((failure_path / "preflight_frame.png").exists())
                self.assertTrue((failure_path / "bot.log").exists())

                summary = json.loads((failure_path / "summary.json").read_text(encoding="utf-8"))
                self.assertEqual(summary["reason"], "handler_exception")
                self.assertEqual(summary["extra"]["error"], "boom")
            finally:
                os.chdir(cwd)

    def test_write_session_summary_creates_normal_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path.cwd()
            project_dir = Path(tmp)
            try:
                import os

                os.chdir(project_dir)
                machine = StateMachine.__new__(StateMachine)
                machine.ctx = SimpleNamespace(
                    session_id="session_ok",
                    session_run_dir=str(project_dir / "logs" / "session_runs" / "session_ok"),
                    current_state="STATE_RESULT",
                    run_count=1,
                    max_runs=1,
                    success_count=1,
                    current_floor=20,
                    current_money=0,
                    target_notes={},
                    current_notes={},
                    card_counter_enabled=False,
                    card_counter_initial_total=None,
                    card_counter_target_total=None,
                    card_counter_current_total=None,
                    preflight_detected_state="STATE_HOME",
                )

                summary_path = machine.write_session_summary(reason="completed")
                self.assertTrue(summary_path.exists())
                summary = json.loads(summary_path.read_text(encoding="utf-8"))
                self.assertEqual(summary["reason"], "completed")
                self.assertEqual(summary["run_count"], 1)
            finally:
                os.chdir(cwd)
