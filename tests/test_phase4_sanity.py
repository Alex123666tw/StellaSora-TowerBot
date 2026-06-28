from __future__ import annotations

import ast
import json
import unittest
from pathlib import Path

import core.states as states


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class Phase4SanityTests(unittest.TestCase):
    def test_core_files_parse(self) -> None:
        for rel_path in ("core/states.py", "core/bot.py", "vision/state_detector.py"):
            source = (PROJECT_ROOT / rel_path).read_text(encoding="utf-8")
            ast.parse(source, filename=rel_path)

    def test_state_handlers_exist(self) -> None:
        expected = [
            "handle_home", "handle_lobby", "handle_prepare", "handle_fast_battle",
            "handle_tap_continue", "handle_note_acquired", "handle_potential_select",
            "handle_event", "handle_shop_choice", "handle_shop",
            "handle_explore_complete", "handle_result", "handle_settlement", "handle_reconnect",
        ]
        for fn_name in expected:
            self.assertTrue(hasattr(states, fn_name), fn_name)

    def test_quiz_answer_schema_has_version(self) -> None:
        data = json.loads((PROJECT_ROOT / "data" / "quiz_answers.json").read_text(encoding="utf-8"))
        self.assertIn("version", data)
