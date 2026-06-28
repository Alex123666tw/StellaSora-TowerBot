from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

from _bootstrap import PROJECT_ROOT

# cp950 主控台印不出 log tail 內的特殊字元(如 '▶'),統一改寬容輸出
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(errors="replace")


RUNNER_ROOT = PROJECT_ROOT / "logs" / "elevated_runner"
REQUEST_DIR = RUNNER_ROOT / "requests"
RESULT_DIR = RUNNER_ROOT / "results"
STATUS_PATH = RUNNER_ROOT / "status.json"


def _request_id() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _write_json(path: Path, payload: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _tail(path: Path, lines: int) -> str:
    if lines <= 0 or not path.exists():
        return ""
    content = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(content[-lines:])


def _print_status() -> int:
    if not STATUS_PATH.exists():
        print("[RUNNER] status: not started")
        return 1
    print(STATUS_PATH.read_text(encoding="utf-8"))
    return 0


def _send_request(payload: dict, timeout: int, tail_lines: int) -> int:
    REQUEST_DIR.mkdir(parents=True, exist_ok=True)
    RESULT_DIR.mkdir(parents=True, exist_ok=True)
    request_id = payload["request_id"]
    result_path = RESULT_DIR / f"{request_id}.json"
    request_path = REQUEST_DIR / f"{request_id}.json"
    if result_path.exists():
        result_path.unlink()
    _write_json(request_path, payload)
    print(f"[RUNNER] request queued: {request_id}")

    deadline = time.time() + timeout
    while time.time() < deadline:
        if result_path.exists():
            result = json.loads(result_path.read_text(encoding="utf-8"))
            print(json.dumps(result, ensure_ascii=False, indent=2))
            output_path = Path(result.get("output_path", ""))
            tail_text = _tail(output_path, tail_lines)
            if tail_text:
                print("\n[RUNNER] output tail")
                print(tail_text)
            return int(result.get("exit_code", 1))
        time.sleep(0.5)

    print(f"[RUNNER] timed out waiting for result: {request_id}")
    return 124


def main() -> int:
    parser = argparse.ArgumentParser(description="Send whitelist requests to the elevated test runner")
    sub = parser.add_subparsers(dest="command", required=True)

    run_parser = sub.add_parser("run-safe-single-round")
    run_parser.add_argument("--input-mode", choices=["foreground", "auto"], default="foreground")
    run_parser.add_argument("--stuck-timeout", type=int, default=30)
    run_parser.add_argument("--sample-interval", type=float, default=1.0)
    run_parser.add_argument(
        "--max-duration",
        type=int,
        default=600,
        help=(
            "Hard wall-clock upper limit for the test run in seconds (60-1800, default 600). "
            "Pass 0 to disable the hard limit and let the bot run a full round to completion "
            "(stops on max_runs=1 / stuck detector; monitor it yourself)."
        ),
    )
    run_parser.add_argument(
        "--timeout",
        type=int,
        default=None,
        help=(
            "Client-side wait timeout in seconds for the result to appear. "
            "Defaults to max(720, max_duration + 120) so the client never times out before "
            "the test's own hard limit fires."
        ),
    )
    run_parser.add_argument(
        "--continue-run",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "Continue-run / never-reset mode (default on for L3): keep playing from the "
            "current screen, disable the card counter, and dump per-state corpus. "
            "Use --no-continue-run to disable."
        ),
    )
    run_parser.add_argument("--tail", type=int, default=220)

    stop_parser = sub.add_parser("stop")
    stop_parser.add_argument("--timeout", type=int, default=30)
    stop_parser.add_argument("--tail", type=int, default=0)

    sub.add_parser("status")

    args = parser.parse_args()
    if args.command == "status":
        return _print_status()

    request_id = _request_id()
    if args.command == "run-safe-single-round":
        max_duration = args.max_duration
        # Client wait must exceed the test hard limit; default ensures this automatically.
        # max_duration=0 (disabled, no hard limit) → default to a generous 3600s client wait so
        # the client survives a full multi-floor round; override with --timeout if a round needs more.
        if args.timeout is not None:
            client_timeout = args.timeout
        elif max_duration == 0:
            client_timeout = 3600
        else:
            client_timeout = max(720, max_duration + 120)
        return _send_request(
            {
                "request_id": request_id,
                "command": "run_safe_single_round",
                "input_mode": args.input_mode,
                "stuck_timeout": args.stuck_timeout,
                "sample_interval": args.sample_interval,
                "max_duration": max_duration,
                "continue_run": args.continue_run,
            },
            timeout=client_timeout,
            tail_lines=args.tail,
        )

    if args.command == "stop":
        return _send_request(
            {
                "request_id": request_id,
                "command": "stop",
            },
            timeout=args.timeout,
            tail_lines=args.tail,
        )

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
