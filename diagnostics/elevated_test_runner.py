from __future__ import annotations

import argparse
import json
import logging
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from _bootstrap import PROJECT_ROOT

sys.path.insert(0, str(PROJECT_ROOT))

from utils.privilege import exit_if_not_windows_admin


RUNNER_ROOT = PROJECT_ROOT / "logs" / "elevated_runner"
REQUEST_DIR = RUNNER_ROOT / "requests"
RESULT_DIR = RUNNER_ROOT / "results"
RUN_DIR = RUNNER_ROOT / "runs"
STATUS_PATH = RUNNER_ROOT / "status.json"

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _ensure_dirs() -> None:
    REQUEST_DIR.mkdir(parents=True, exist_ok=True)
    RESULT_DIR.mkdir(parents=True, exist_ok=True)
    RUN_DIR.mkdir(parents=True, exist_ok=True)


def _write_json(path: Path, payload: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _safe_request_id(value: str | None) -> str:
    if value and re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", value):
        return value
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _write_status(state: str, **extra) -> None:
    payload = {
        "state": state,
        "pid": None,
        "updated_at": _now(),
        **extra,
    }
    try:
        import os

        payload["pid"] = os.getpid()
    except Exception:
        pass
    _write_json(STATUS_PATH, payload)


def _request_files() -> list[Path]:
    return sorted(REQUEST_DIR.glob("*.json"), key=lambda path: path.stat().st_mtime)


def _read_request(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_result(request_id: str, payload: dict) -> None:
    _write_json(
        RESULT_DIR / f"{request_id}.json",
        {
            "request_id": request_id,
            "finished_at": _now(),
            **payload,
        },
    )


def _run_safe_single_round(request_id: str, request: dict) -> dict:
    input_mode = request.get("input_mode", "foreground")
    if input_mode not in {"foreground", "auto"}:
        input_mode = "foreground"

    try:
        stuck_timeout = max(5, min(300, int(request.get("stuck_timeout", 30))))
    except Exception:
        stuck_timeout = 30

    try:
        sample_interval = max(0.2, min(10.0, float(request.get("sample_interval", 1.0))))
    except Exception:
        sample_interval = 1.0

    try:
        raw_md = int(request.get("max_duration", 600))
        # max_duration=0 → 拔掉硬上限（disabled）。bot 靠內建 stuck detector + max_runs=1
        # （完成一輪自停）+ 外部 watchdog stuck_timeout 兜底,改由監控者固定時脈盯（使用者
        # 2026-06-15 拍板:跑完整一輪即可,不被 600s 切斷在半途）。非 0 仍 clamp [60,1800]。
        max_duration = 0 if raw_md == 0 else max(60, min(1800, raw_md))
    except Exception:
        max_duration = 600

    continue_run = bool(request.get("continue_run", False))

    work_dir = RUN_DIR / request_id
    work_dir.mkdir(parents=True, exist_ok=True)
    output_path = work_dir / "safe_single_round_output.txt"

    cmd = [
        sys.executable,
        str(PROJECT_ROOT / "diagnostics" / "safe_single_round_test.py"),
        "--input-mode",
        input_mode,
        "--stuck-timeout",
        str(stuck_timeout),
        "--sample-interval",
        str(sample_interval),
        "--max-duration",
        str(max_duration),
    ]
    if continue_run:
        cmd.append("--continue-run")
    _write_status("running", current_request_id=request_id, output_path=str(output_path), command="run_safe_single_round")

    with output_path.open("w", encoding="utf-8", errors="replace") as out:
        completed = subprocess.run(
            cmd,
            cwd=PROJECT_ROOT,
            stdout=out,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
        )

    return {
        "command": "run_safe_single_round",
        "exit_code": completed.returncode,
        "output_path": str(output_path),
        "runner_log_dir": str(work_dir),
    }


def _handle_request(path: Path) -> bool:
    try:
        request = _read_request(path)
    except Exception as exc:
        request_id = _safe_request_id(path.stem)
        _write_result(request_id, {"exit_code": 2, "error": f"invalid request: {exc}"})
        path.unlink(missing_ok=True)
        return True

    request_id = _safe_request_id(str(request.get("request_id") or path.stem))
    command = str(request.get("command") or "")
    path.unlink(missing_ok=True)

    try:
        if command == "run_safe_single_round":
            result = _run_safe_single_round(request_id, request)
            _write_result(request_id, result)
            return True
        if command == "stop":
            _write_result(request_id, {"command": "stop", "exit_code": 0})
            _write_status("stopping", current_request_id=request_id)
            return False
        _write_result(request_id, {"command": command, "exit_code": 2, "error": "unsupported command"})
        return True
    except Exception as exc:
        logger.exception("request failed: %s", request_id)
        _write_result(request_id, {"command": command, "exit_code": 1, "error": str(exc)})
        return True


def main() -> int:
    parser = argparse.ArgumentParser(description="Elevated whitelist runner for Stella Sora tests")
    parser.add_argument("--poll-interval", type=float, default=0.5)
    parser.add_argument("--idle-timeout", type=int, default=3600, help="Seconds before the idle runner exits; 0 disables.")
    args = parser.parse_args()

    exit_if_not_windows_admin("Stella Sora Elevated Test Runner")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    _ensure_dirs()
    logger.info("elevated test runner started at %s", RUNNER_ROOT)
    _write_status("idle")

    last_work = time.time()
    while True:
        files = _request_files()
        if files:
            last_work = time.time()
            keep_running = _handle_request(files[0])
            if not keep_running:
                return 0
            _write_status("idle")
            continue

        if args.idle_timeout > 0 and time.time() - last_work >= args.idle_timeout:
            _write_status("exited", reason="idle_timeout")
            return 0
        time.sleep(max(0.1, args.poll_interval))


if __name__ == "__main__":
    raise SystemExit(main())
