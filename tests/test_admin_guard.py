from __future__ import annotations

import io
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from utils.input_sim import InputSimulator
from utils import privilege

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DIAGNOSTICS_DIR = PROJECT_ROOT / "diagnostics"

if str(DIAGNOSTICS_DIR) not in sys.path:
    sys.path.insert(0, str(DIAGNOSTICS_DIR))


class PrivilegeHelperTests(unittest.TestCase):
    def test_require_windows_admin_raises_permission_error(self) -> None:
        with patch("utils.privilege.is_windows_platform", return_value=True), \
             patch("utils.privilege.is_windows_admin", return_value=False):
            with self.assertRaises(PermissionError) as ctx:
                privilege.require_windows_admin("Stella Sora Bot")

        message = str(ctx.exception)
        self.assertIn("系統管理員", message)
        self.assertIn("視窗化", message)
        self.assertIn("權限等級一致", message)

    def test_exit_if_not_windows_admin_prints_message_and_exits(self) -> None:
        stderr = io.StringIO()
        with patch("utils.privilege.is_windows_platform", return_value=True), \
             patch("utils.privilege.is_windows_admin", return_value=False), \
             patch("sys.stderr", stderr):
            with self.assertRaises(SystemExit) as ctx:
                privilege.exit_if_not_windows_admin("Stella Sora Bot")

        self.assertEqual(ctx.exception.code, privilege.ADMIN_REQUIRED_EXIT_CODE)
        self.assertIn("系統管理員", stderr.getvalue())


class InputSimulatorAdminGuardTests(unittest.TestCase):
    def test_attach_requires_admin_before_window_lookup(self) -> None:
        sim = InputSimulator(window_name="StellaSora", mode="foreground")
        with patch("utils.input_sim.require_windows_admin", side_effect=PermissionError("admin required")), \
             patch("utils.input_sim.win32gui.FindWindow") as find_window:
            with self.assertRaises(PermissionError):
                sim.attach()

        find_window.assert_not_called()

    def test_click_requires_admin_even_if_hwnd_is_already_attached(self) -> None:
        sim = InputSimulator(window_name="StellaSora", mode="foreground")
        sim.hwnd = 123
        with patch("utils.input_sim.require_windows_admin", side_effect=PermissionError("admin required")), \
             patch.object(sim, "click_foreground") as click_foreground:
            with self.assertRaises(PermissionError):
                sim.click(100, 200)

        click_foreground.assert_not_called()


class EntryPointAdminGateTests(unittest.TestCase):
    def test_main_exits_before_logging_or_bot_init_when_not_admin(self) -> None:
        import main as bot_main

        with patch.object(sys, "argv", ["main.py"]), \
             patch("main.exit_if_not_windows_admin", side_effect=SystemExit(privilege.ADMIN_REQUIRED_EXIT_CODE)), \
             patch("main.setup_logging") as setup_logging, \
             patch("main.StateMachine") as state_machine:
            with self.assertRaises(SystemExit) as ctx:
                bot_main.main()

        self.assertEqual(ctx.exception.code, privilege.ADMIN_REQUIRED_EXIT_CODE)
        setup_logging.assert_not_called()
        state_machine.assert_not_called()

    def test_auto_execute_exits_before_session_setup_when_not_admin(self) -> None:
        from diagnostics import auto_execute_bot

        with patch.object(sys, "argv", ["auto_execute_bot.py"]), \
             patch("diagnostics.auto_execute_bot.exit_if_not_windows_admin", side_effect=SystemExit(privilege.ADMIN_REQUIRED_EXIT_CODE)), \
             patch("diagnostics.auto_execute_bot.setup_logging") as setup_logging, \
             patch("diagnostics.auto_execute_bot._build_session_paths") as build_session_paths, \
             patch("diagnostics.auto_execute_bot.StateMachine") as state_machine:
            with self.assertRaises(SystemExit) as ctx:
                auto_execute_bot.main()

        self.assertEqual(ctx.exception.code, privilege.ADMIN_REQUIRED_EXIT_CODE)
        setup_logging.assert_not_called()
        build_session_paths.assert_not_called()
        state_machine.assert_not_called()

    def test_active_probe_exits_before_loading_config_when_not_admin(self) -> None:
        from diagnostics import active_window_probe

        with patch.object(sys, "argv", ["active_window_probe.py"]), \
             patch("diagnostics.active_window_probe.exit_if_not_windows_admin", side_effect=SystemExit(privilege.ADMIN_REQUIRED_EXIT_CODE)), \
             patch("builtins.open") as mocked_open:
            with self.assertRaises(SystemExit) as ctx:
                active_window_probe.main()

        self.assertEqual(ctx.exception.code, privilege.ADMIN_REQUIRED_EXIT_CODE)
        mocked_open.assert_not_called()

    def test_manual_probe_exits_before_loading_config_when_not_admin(self) -> None:
        from diagnostics import manual_capture_probe

        with patch.object(sys, "argv", ["manual_capture_probe.py"]), \
             patch("diagnostics.manual_capture_probe.exit_if_not_windows_admin", side_effect=SystemExit(privilege.ADMIN_REQUIRED_EXIT_CODE)), \
             patch("builtins.open") as mocked_open:
            with self.assertRaises(SystemExit) as ctx:
                manual_capture_probe.main()

        self.assertEqual(ctx.exception.code, privilege.ADMIN_REQUIRED_EXIT_CODE)
        mocked_open.assert_not_called()
