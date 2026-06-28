from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import yaml

from utils.input_sim import InputSimulator
from core.bot import StateMachine


class InputSimulatorModeTests(unittest.TestCase):
    def test_foreground_mode_bypasses_background_clicks(self) -> None:
        sim = InputSimulator(window_name="StellaSora", mode="foreground")
        sim.click_foreground = Mock(return_value=True)
        sim.click_background_with_cursor = Mock(return_value=True)

        with patch("utils.input_sim.require_windows_admin"):
            result = sim.click(100, 200)

        self.assertTrue(result)
        sim.click_foreground.assert_called_once_with(100, 200, 0.05)
        sim.click_background_with_cursor.assert_not_called()

    def test_auto_mode_uses_background_when_available(self) -> None:
        sim = InputSimulator(window_name="StellaSora", mode="auto")
        sim.click_foreground = Mock(return_value=True)
        sim.click_background_with_cursor = Mock(return_value=True)

        with patch("utils.input_sim.require_windows_admin"):
            result = sim.click(100, 200)

        self.assertTrue(result)
        sim.click_background_with_cursor.assert_called_once_with(100, 200, 0.05)
        sim.click_foreground.assert_not_called()

    def test_auto_mode_falls_back_to_foreground(self) -> None:
        sim = InputSimulator(window_name="StellaSora", mode="auto")
        sim.click_foreground = Mock(return_value=True)
        sim.click_background_with_cursor = Mock(return_value=False)

        with patch("utils.input_sim.require_windows_admin"):
            result = sim.click(100, 200)

        self.assertTrue(result)
        sim.click_background_with_cursor.assert_called_once_with(100, 200, 0.05)
        sim.click_foreground.assert_called_once_with(100, 200, 0.05)


class StateMachineInputModeTests(unittest.TestCase):
    def test_build_context_reads_input_mode_from_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.yaml"
            config_path.write_text(
                yaml.safe_dump(
                    {
                        "window": {"name": "StellaSora", "capture_mode": "auto"},
                        "input": {"mode": "auto"},
                        "run": {"max_runs": 1},
                        "bot": {"poll_interval": 0.8, "max_reconnect_attempts": 5},
                        "ocr": {"languages": ["ch_tra", "en"], "gpu": False},
                    },
                    allow_unicode=True,
                ),
                encoding="utf-8",
            )

            class DummyWindowManager:
                def __init__(self, window_name: str = "StellaSora"):
                    self.window_name = window_name
                    self.hwnd = 1

                def find_window(self):
                    return self.hwnd

            class DummyInputSimulator:
                def __init__(self, window_name: str = "StellaSora", mode: str = "foreground", prefer_background=None):
                    self.window_name = window_name
                    self.mode = mode

                def attach(self):
                    return 1

            class DummyDecisionEngine:
                def __init__(self, config_path: str = "config.yaml"):
                    self.config_path = config_path

            with patch("core.bot.WindowManager", DummyWindowManager), \
                 patch("core.bot.InputSimulator", DummyInputSimulator), \
                 patch("core.bot.DecisionEngine", DummyDecisionEngine), \
                 patch("core.bot.OcrEngine", side_effect=RuntimeError("skip ocr")), \
                 patch("core.bot.TemplateMatcher", side_effect=RuntimeError("skip matcher")), \
                 patch.object(StateMachine, "_register_signal_handlers", return_value=None):
                machine = StateMachine(config_path=str(config_path))

            self.assertEqual(machine.ctx.input.mode, "auto")
