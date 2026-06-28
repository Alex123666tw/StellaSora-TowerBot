from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path

import cv2
import yaml

from _bootstrap import PROJECT_ROOT

sys.path.insert(0, str(PROJECT_ROOT))

from core.bot import StateMachine
from main import setup_logging
from utils.privilege import exit_if_not_windows_admin


logger = logging.getLogger(__name__)


def _load_config(config_path: Path) -> dict:
    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _build_session_paths(session_id: str) -> Path:
    run_dir = PROJECT_ROOT / "logs" / "session_runs" / session_id
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir


def _run_preflight(machine: StateMachine, session_dir: Path, window_name: str) -> tuple[bool, str | None]:
    logger.info("[AUTO] preflight start")
    machine.ctx.wm.window_name = window_name
    machine.ctx.input.window_name = window_name

    try:
        machine.ctx.wm.find_window()
        machine.ctx.input.attach()
    except Exception as e:
        logger.error(f"[AUTO] preflight window lock failed: {e}")
        machine.finalize_failure("preflight_window_lock_failed", {"error": str(e)})
        return False, None

    try:
        frame, method = machine.ctx.wm.capture()
        machine.ctx.last_frame = frame
        machine.ctx.preflight_frame = frame.copy()
        preflight_path = session_dir / "preflight_frame.png"
        cv2.imwrite(str(preflight_path), frame)
        logger.info(f"[AUTO] preflight capture ok: {method} -> {preflight_path}")
    except Exception as e:
        logger.error(f"[AUTO] preflight capture failed: {e}")
        machine.finalize_failure("preflight_capture_failed", {"error": str(e)})
        return False, None

    detected_state = None
    detector = getattr(machine, "_detector", None)
    if detector is not None:
        try:
            detection = detector.detect(machine.ctx.preflight_frame, machine.ctx.current_state)
            # Phase 1.2：detect() 回傳 DetectionResult；summary JSON 只存 state 字串
            detected_state = getattr(detection, "state", detection)
            machine.ctx.preflight_detected_state = detected_state
            logger.info(f"[AUTO] preflight detected state: {detected_state}")
        except Exception as e:
            logger.warning(f"[AUTO] preflight state detect failed: {e}")
    return True, detected_state


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the real-window bot with preflight checks")
    parser.add_argument("--config", default="config.yaml", help="Path to config.yaml")
    parser.add_argument("--window", default=None, help="Override target window title")
    parser.add_argument("--input-mode", choices=["foreground", "auto"], default=None, help="Override input.mode")
    args = parser.parse_args()

    exit_if_not_windows_admin("Stella Sora Auto Execute")

    setup_logging("INFO")

    config_path = Path(args.config)
    cfg = _load_config(config_path)
    window_name = args.window or cfg.get("window", {}).get("name", "StellaSora")
    input_mode = args.input_mode or cfg.get("input", {}).get("mode", "foreground")
    session_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    session_dir = _build_session_paths(session_id)

    machine = StateMachine(config_path=str(config_path))
    machine.ctx.input.mode = input_mode
    machine.set_session_metadata(session_id=session_id, session_run_dir=str(session_dir))

    ok, detected_state = _run_preflight(machine, session_dir, window_name)
    if not ok:
        return 1

    machine.set_session_metadata(
        session_id=session_id,
        session_run_dir=str(session_dir),
        preflight_frame=machine.ctx.preflight_frame,
        preflight_detected_state=detected_state,
    )

    machine.run()

    if machine.ctx.failure_reason:
        logger.error(f"[AUTO] session failed: {machine.ctx.failure_reason}")
        return 1

    summary_path = machine.write_session_summary(reason="completed")
    logger.info(f"[AUTO] session completed -> {summary_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
