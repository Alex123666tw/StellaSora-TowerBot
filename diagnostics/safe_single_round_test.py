from __future__ import annotations

import argparse
import json
import logging
import os
import threading
import time
import zlib
from datetime import datetime
from pathlib import Path

import cv2
import yaml

from _bootstrap import PROJECT_ROOT

import sys

sys.path.insert(0, str(PROJECT_ROOT))

from core import progress
from core.bot import StateMachine
from main import setup_logging
from utils.privilege import exit_if_not_windows_admin, is_windows_admin


logger = logging.getLogger(__name__)

ACTIONABLE_STATES = {
    "STATE_LOBBY",
    "STATE_FORMATION",
    "STATE_PREPARE",
    "STATE_POTENTIAL_SELECT",
    "STATE_EVENT",
    "STATE_TAP_CONTINUE",
    "STATE_NOTE_ACQUIRED",
    "STATE_SHOP_CHOICE",
    "STATE_SHOP",
    "STATE_EXPLORE_COMPLETE",
    "STATE_RESULT",
}


def _load_config(config_path: Path) -> dict:
    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _session_dir(session_id: str) -> Path:
    run_dir = PROJECT_ROOT / "logs" / "session_runs" / session_id
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir


def _frame_hash(frame, roi: tuple[int, int, int, int] | None = None) -> int | None:
    if frame is None:
        return None
    try:
        if roi is not None:
            x, y, w, h = roi
            frame = frame[y:y+h, x:x+w]
        small = cv2.resize(frame, (64, 36), interpolation=cv2.INTER_AREA)
        return zlib.adler32(small.tobytes())
    except Exception:
        return None


def _write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _preflight(machine: StateMachine, session_dir: Path, window_name: str) -> tuple[bool, str | None]:
    machine.ctx.wm.window_name = window_name
    machine.ctx.input.window_name = window_name

    try:
        machine.ctx.wm.find_window()
        machine.ctx.input.attach()
    except Exception as e:
        machine.finalize_failure("preflight_window_lock_failed", {"error": str(e)})
        return False, None

    try:
        frame, method = machine.ctx.wm.capture()
        machine.ctx.last_frame = frame
        machine.ctx.preflight_frame = frame.copy()
        cv2.imwrite(str(session_dir / "preflight_frame.png"), frame)
        logger.info("[SAFE] preflight capture ok: %s", method)
    except Exception as e:
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
            logger.info(
                "[SAFE] preflight detected state: %s (confidence=%s)",
                detected_state,
                getattr(detection, "confidence", None),
            )
        except Exception as e:
            logger.warning("[SAFE] preflight state detect failed: %s", e)
    return True, detected_state


def _state_roi(ctx) -> tuple[int, int, int, int]:
    w = int(getattr(ctx, "frame_w", 0) or 0)
    h = int(getattr(ctx, "frame_h", 0) or 0)
    if w <= 0 or h <= 0:
        return (0, 0, 0, 0)
    state = str(getattr(ctx, "current_state", ""))
    if state == "STATE_EVENT":
        return (int(w * 0.45), int(h * 0.12), int(w * 0.50), int(h * 0.76))
    if state in {"STATE_SHOP", "STATE_SHOP_CHOICE"}:
        return (int(w * 0.20), int(h * 0.08), int(w * 0.78), int(h * 0.82))
    if state == "STATE_POTENTIAL_SELECT":
        return (int(w * 0.03), int(h * 0.08), int(w * 0.94), int(h * 0.78))
    return (0, 0, w, h)


def _sample(machine: StateMachine) -> dict:
    ctx = machine.ctx
    roi = _state_roi(ctx)
    total_click_count = getattr(ctx, "total_click_count", None)
    if total_click_count is None:
        # 舊版 ctx 無不封頂計數時退回 len(click_trace)（受 deque maxlen 封頂）
        total_click_count = len(ctx.click_trace)
    return {
        "timestamp": time.time(),
        "state": ctx.current_state,
        # 業務計數統一走 core/progress.py（與內建卡死偵測同一來源）
        "counters": progress.progress_counters(ctx),
        "total_click_count": int(total_click_count),
        "click_count": len(ctx.click_trace),
        "state_trace_count": len(ctx.state_trace),
        # frame/roi hash 僅留作事後分析欄位，不再參與進度判定
        #（動畫盲區實機證據 20260612_211534：roi_hash 每 ~3 秒被動畫改變）
        "frame_hash": _frame_hash(ctx.last_frame),
        "roi": roi,
        "roi_hash": _frame_hash(ctx.last_frame, roi=roi),
        "card_counter": {
            "enabled": ctx.card_counter_enabled,
            "current_total": ctx.card_counter_current_total,
            "target_total": ctx.card_counter_target_total,
        },
    }


def _made_progress(prev: dict | None, cur: dict) -> bool:
    """外部 watchdog 的進度定義（Phase 1.4 改版）。

    roi_hash 變化**不再算進度**：持續動畫的畫面會讓 roi_hash 每 ~3 秒變一次，
    舊定義因此永不觸發 stuck（實機證據 20260612_211534，bot 凍結 14 分鐘）。
    外部 watchdog 拿不到 OCR，改看：
      - state 前進；
      - 業務計數變化（core/progress.py 同一來源；相容新舊樣本 schema）；
      - click_count 前進（Phase 1.3 之後點擊已是「看到才點」，且改用
        不封頂的 total_click_count；重複點擊掩蓋真卡死的風險由內建
        StuckDetector 與 --max-duration 硬上限兜底）。
    """
    if prev is None:
        return True
    if prev.get("state") != cur.get("state"):
        return True
    if progress.sample_counters(prev) != progress.sample_counters(cur):
        return True
    return progress.sample_click_count(cur) > progress.sample_click_count(prev)


def _repair_request(machine: StateMachine, reason: str, samples: list[dict]) -> dict:
    ctx = machine.ctx
    return {
        "reason": reason,
        "failure_dir": ctx.failure_dir,
        "current_state": ctx.current_state,
        "last_samples": samples[-10:],
        "last_clicks": list(ctx.click_trace)[-10:],
        "last_states": list(ctx.state_trace)[-10:],
        "acceptance": [
            "Run unit tests for decision, selection, shop, and watchdog paths.",
            "Rerun diagnostics/safe_single_round_test.py with max_runs forced to 1.",
            "No actionable state may remain unchanged for the stuck timeout without progress.",
        ],
        "reset_status": "not_attempted",
        "reset_note": (
            "Stop the bot script first. If recovery is needed, use manual or Computer Use clicks "
            "to advance to the nearest shop, then exit from the shop flow. Automated reset remains "
            "disabled until the click whitelist is confirmed."
        ),
    }


def _write_repair_requests(machine: StateMachine, run_dir: Path, reason: str, samples: list[dict]) -> None:
    payload = _repair_request(machine, reason, samples)
    _write_json(run_dir / "repair_request.json", payload)
    failure_dir = getattr(machine.ctx, "failure_dir", None)
    if failure_dir:
        _write_json(Path(failure_dir) / "repair_request.json", payload)


def _dump_corpus(machine: StateMachine, corpus_dir: Path, index: int) -> None:
    """Best-effort 語料傾印：把當前 frame + 全畫面 OCR 文字落地供校準用。

    在 watchdog thread 內呼叫,讀 ctx.last_frame / _ocr_recorder.last_texts 屬於
    跨執行緒讀取,容許輕微 race;frame 先 .copy() 再寫,避免寫到一半被主迴圈覆寫。
    任何失敗只記 debug,不得影響 watchdog 主邏輯。
    """
    try:
        ctx = machine.ctx
        frame = ctx.last_frame
        if frame is None:
            return
        frame = frame.copy()
        prefix = f"{index:03d}_{ctx.current_state}_{int(time.time())}"
        # CJK-safe 寫檔：本專案路徑含中文,cv2.imwrite 在部分環境(elevated runner/
        # watchdog thread)對 CJK 路徑會靜默回 False → 只吐 JSON 沒 PNG(看板「Task1
        # corpus 空目錄」bug)。改用 imencode + numpy.tofile(吃 Unicode 路徑)。
        ok, buf = cv2.imencode(".png", frame)
        if ok:
            buf.tofile(str(corpus_dir / f"{prefix}.png"))
        else:
            logger.debug("[SAFE] corpus PNG encode failed (index=%s)", index)
        rec = getattr(machine, "_ocr_recorder", None)
        texts = list(getattr(rec, "last_texts", []) or [])
        _write_json(
            corpus_dir / f"{prefix}.json",
            {
                "state": ctx.current_state,
                "frame_w": getattr(ctx, "frame_w", None),
                "frame_h": getattr(ctx, "frame_h", None),
                "roi": _state_roi(ctx),
                "ocr_texts": texts,
                "timestamp": time.time(),
            },
        )
    except Exception as e:  # pragma: no cover - best-effort, defensive
        logger.debug("[SAFE] corpus dump failed (index=%s): %s", index, e)


def _watchdog(
    machine: StateMachine,
    run_dir: Path,
    timeout_s: int,
    interval_s: float,
    max_duration_s: int = 0,
    *,
    corpus_dir: Path | None = None,
    _sample_fn=None,
    _sleep_fn=None,
    _time_fn=None,
    _start_time: float | None = None,
) -> None:
    """External watchdog loop.

    Parameters
    ----------
    max_duration_s:
        Hard wall-clock upper limit (seconds) for the entire watchdog run.
        0 means disabled.  When elapsed >= max_duration_s and the bot has
        not completed, ``finalize_failure("max_duration_exceeded")`` is
        called and the loop exits.
    corpus_dir:
        Continue-run 模式的語料傾印目錄;None = 關閉(預設,既有呼叫不受影響)。
        每當進入一個與「上次已傾印」不同的 ACTIONABLE state 時,傾印一組
        frame + OCR 語料(同一 state 連續出現只傾印一次)。
    _sample_fn, _sleep_fn, _time_fn, _start_time:
        Injection points for unit-testing (fake clock / fake sampler).
        Production callers leave these as None.
    """
    sample_fn = _sample_fn if _sample_fn is not None else _sample
    sleep_fn = _sleep_fn if _sleep_fn is not None else time.sleep
    time_fn = _time_fn if _time_fn is not None else time.time

    samples: list[dict] = []
    samples_path = run_dir / "watchdog_samples.jsonl"
    last_progress_sample: dict | None = None
    last_progress_time = time_fn()
    watchdog_start = _start_time if _start_time is not None else time_fn()
    last_dumped_state: str | None = None
    corpus_index = 0

    while machine.ctx.running:
        cur = sample_fn(machine)
        samples.append(cur)
        try:
            with samples_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(cur, ensure_ascii=False) + "\n")
        except Exception:
            pass

        # --- 語料傾印(continue-run 模式):state 變到不同的 ACTIONABLE 才印一次 ---
        if (
            corpus_dir is not None
            and cur["state"] in ACTIONABLE_STATES
            and cur["state"] != last_dumped_state
        ):
            _dump_corpus(machine, corpus_dir, corpus_index)
            last_dumped_state = cur["state"]
            corpus_index += 1

        if _made_progress(last_progress_sample, cur):
            last_progress_sample = cur
            last_progress_time = cur["timestamp"]

        # --- hard wall-clock upper limit (Phase 0.4) ---
        if max_duration_s > 0:
            elapsed = time_fn() - watchdog_start
            if elapsed >= max_duration_s:
                reason = "max_duration_exceeded"
                logger.error(
                    "[SAFE] max_duration %ss exceeded (elapsed %.1fs) in %s",
                    max_duration_s,
                    elapsed,
                    cur["state"],
                )
                machine.finalize_failure(
                    reason,
                    {
                        "watchdog_sample": cur,
                        "max_duration_s": max_duration_s,
                        "elapsed_s": elapsed,
                    },
                )
                _write_repair_requests(machine, run_dir, reason, samples)
                break

        # --- per-state stuck timeout ---
        if cur["state"] in ACTIONABLE_STATES and cur["timestamp"] - last_progress_time >= timeout_s:
            reason = "external_watchdog_stuck"
            logger.error("[SAFE] stuck detected in %s after %ss", cur["state"], timeout_s)
            machine.finalize_failure(reason, {"watchdog_sample": cur, "timeout_s": timeout_s})
            _write_repair_requests(machine, run_dir, reason, samples)
            break

        sleep_fn(interval_s)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run one safe real-window bot round with external watchdog")
    parser.add_argument("--config", default="config.yaml", help="Path to config.yaml")
    parser.add_argument("--window", default=None, help="Override target window title")
    parser.add_argument("--input-mode", choices=["foreground", "auto"], default=None, help="Override input.mode")
    parser.add_argument("--stuck-timeout", type=int, default=30, help="Seconds without progress before failure")
    parser.add_argument("--sample-interval", type=float, default=1.0, help="Watchdog sample interval")
    parser.add_argument(
        "--max-duration",
        type=int,
        default=600,
        help="Hard wall-clock upper limit in seconds for the whole watchdog run (0 = disabled, default 600)",
    )
    parser.add_argument(
        "--skip-admin-check",
        action="store_true",
        help="Allow a non-admin smoke run when the game is at the same privilege level.",
    )
    parser.add_argument(
        "--continue-run",
        action="store_true",
        default=False,
        help=(
            "Continue-run / never-reset mode: keep playing from the current screen, "
            "disable the card counter, and dump per-state corpus (frame + OCR) for calibration."
        ),
    )
    args = parser.parse_args()

    os.chdir(PROJECT_ROOT)
    if not args.skip_admin_check:
        exit_if_not_windows_admin("Stella Sora Safe Single Round Test")
    setup_logging("INFO")
    if args.skip_admin_check and not is_windows_admin():
        logger.warning(
            "[SAFE] --skip-admin-check is smoke-only; live foreground clicks may fail. "
            "Use run_safe_single_round.bat or an Administrator shell for a real game test."
        )

    config_path = Path(args.config)
    cfg = _load_config(config_path)
    window_name = args.window or cfg.get("window", {}).get("name", "StellaSora")
    input_mode = args.input_mode or cfg.get("input", {}).get("mode", "foreground")
    session_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = _session_dir(session_id)

    machine = StateMachine(config_path=str(config_path))
    machine.ctx.max_runs = 1
    machine.ctx.config.setdefault("run", {})["max_runs"] = 1
    machine.ctx.input.mode = input_mode
    if args.skip_admin_check:
        machine.ctx.input.allow_non_admin = True
    machine.set_session_metadata(session_id=session_id, session_run_dir=str(run_dir))

    corpus_dir: Path | None = None
    if args.continue_run:
        # continue-run 改吃「真經濟」(2026-06-14 使用者拍板,商店 config 化):
        #   card_counter 開啟(讓卡片總等級計數驅動 cards→notes 切換);
        #   不再強制 shop_buy_all,改吃 config shop.buy.strategy(預設 cards_then_notes)。
        # 永不卡死:單輪自然在塔頂結算結束。達到 target_total(78)只是切買音符,不中途停;
        #   停止條件仍只在 max_runs(回大廳計數,Phase 2.3 已改),與買法無關。
        machine.ctx.card_counter_enabled = True
        machine.ctx.shop_buy_all = False
        logger.info(
            "[SAFE] continue-run 模式：真經濟(card_counter 開啟、buy strategy=%s)、語料傾印開啟",
            machine.ctx.shop_buy_strategy,
        )
        corpus_dir = run_dir / "corpus"
        corpus_dir.mkdir(parents=True, exist_ok=True)

    ok, detected_state = _preflight(machine, run_dir, window_name)
    if not ok:
        return 1
    machine.set_session_metadata(
        session_id=session_id,
        session_run_dir=str(run_dir),
        preflight_frame=machine.ctx.preflight_frame,
        preflight_detected_state=detected_state,
    )

    watchdog_thread = threading.Thread(
        target=_watchdog,
        args=(machine, run_dir, args.stuck_timeout, args.sample_interval, args.max_duration),
        kwargs={"corpus_dir": corpus_dir},
        daemon=True,
    )
    watchdog_thread.start()

    machine.run()
    machine.ctx.running = False
    watchdog_thread.join(timeout=5)

    if machine.ctx.failure_reason:
        if not (run_dir / "repair_request.json").exists():
            _write_repair_requests(machine, run_dir, machine.ctx.failure_reason, [])
        logger.error("[SAFE] session failed: %s", machine.ctx.failure_reason)
        return 1

    summary_path = machine.write_session_summary(reason="completed")
    logger.info("[SAFE] session completed -> %s", summary_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
