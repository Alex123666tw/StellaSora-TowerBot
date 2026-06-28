from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "diagnostics"))

from diagnostics.safe_single_round_test import _watchdog


class _FakeMachine:
    """最小假 StateMachine,足以驅動 _watchdog 的語料傾印分支。

    沿用 tests/test_safe_single_round.py 的 _FakeMachine 風格(SimpleNamespace 假 ctx),
    但補齊 _dump_corpus 需要的欄位:last_frame / current_state / frame_w/h / _ocr_recorder。
    """

    def __init__(self) -> None:
        self.finalized_reason: str | None = None
        self.finalized_extra: dict = {}

        ctx = types.SimpleNamespace()
        ctx.running = True
        ctx.failure_dir = None
        ctx.click_trace = []
        ctx.state_trace = []
        ctx.current_state = "STATE_LOBBY"
        ctx.run_count = 0
        ctx.success_count = 0
        ctx.current_floor = 0
        ctx.current_money = 0
        ctx.shop_refresh_count = 0
        ctx.current_notes = {}
        # 小張非 None frame,讓 cv2.imwrite 真的落地 .png
        ctx.last_frame = np.zeros((36, 64, 3), dtype=np.uint8)
        ctx.frame_w = 64
        ctx.frame_h = 36
        ctx.card_counter_enabled = False
        ctx.card_counter_current_total = 0
        ctx.card_counter_target_total = 0
        self.ctx = ctx

        # 全畫面 OCR 來源(_dump_corpus 會讀 .last_texts)
        self._ocr_recorder = types.SimpleNamespace(last_texts=["大廳", "出發"])

    def finalize_failure(self, reason: str, extra: dict | None = None) -> None:  # no-op 防呆
        self.finalized_reason = reason
        self.finalized_extra = extra or {}
        self.ctx.running = False


def _make_sample(state: str) -> dict:
    return {
        "timestamp": 1_000_000.0,
        "state": state,
        "run_count": 0,
        "success_count": 0,
        "current_floor": 0,
        "current_money": 0,
        "shop_refresh_count": 0,
        "current_notes": {},
        "click_count": 1,
        "roi": (0, 0, 64, 36),
        "roi_hash": 1,
        "card_counter": {"current_total": 0},
    }


def _drive_watchdog(machine: _FakeMachine, states: list[str], corpus_dir: Path | None, tmp_path: Path) -> None:
    """以注入的 _sample_fn / _sleep_fn 驅動 _watchdog 走完 states 序列後收尾。

    每拍:_sample_fn 依索引回傳對應 state 的樣本,並同步把 ctx.current_state 設成該 state
    (這樣 _dump_corpus 取到的 prefix 才與當拍 state 一致);_sleep_fn 推進索引,到底設 running=False。
    """
    idx = {"i": 0}

    def sample_fn(m: _FakeMachine) -> dict:
        state = states[idx["i"]]
        m.ctx.current_state = state
        return _make_sample(state)

    def sleep_fn(_interval: float) -> None:
        idx["i"] += 1
        if idx["i"] >= len(states):
            machine.ctx.running = False
            idx["i"] = len(states) - 1  # 防止越界(收尾前最後一次 sample 已取過)

    _watchdog(
        machine=machine,
        run_dir=tmp_path,
        timeout_s=300,        # 不觸發 stuck
        interval_s=0.0,
        max_duration_s=0,     # 不觸發硬上限
        corpus_dir=corpus_dir,
        _sample_fn=sample_fn,
        _sleep_fn=sleep_fn,
        _time_fn=lambda: 1_000_000.0,
        _start_time=1_000_000.0,
    )


def test_corpus_dump_on_state_change(tmp_path: Path) -> None:
    """state 變化時各傾印一組 .png + .json,連續重複的同一 state 不重複印。

    序列 LOBBY→LOBBY→FORMATION→FORMATION→PREPARE → 應產生 3 組
    (LOBBY / FORMATION / PREPARE),不因連續重複而多印。
    """
    corpus_dir = tmp_path / "corpus"
    corpus_dir.mkdir(parents=True, exist_ok=True)

    machine = _FakeMachine()
    states = [
        "STATE_LOBBY",
        "STATE_LOBBY",
        "STATE_FORMATION",
        "STATE_FORMATION",
        "STATE_PREPARE",
    ]
    _drive_watchdog(machine, states, corpus_dir, tmp_path)

    pngs = sorted(corpus_dir.glob("*.png"))
    jsons = sorted(corpus_dir.glob("*.json"))

    assert len(pngs) == 3, f"預期 3 張 .png(LOBBY/FORMATION/PREPARE),實際 {len(pngs)}: {[p.name for p in pngs]}"
    assert len(jsons) == 3, f"預期 3 份 .json,實際 {len(jsons)}: {[p.name for p in jsons]}"

    # 三組 state 各一次,順序由檔名前綴 index 保證
    dumped_states = []
    for j in jsons:
        payload = json.loads(j.read_text(encoding="utf-8"))
        dumped_states.append(payload["state"])
        # json 內含 state / roi / ocr_texts
        assert "state" in payload
        assert "roi" in payload and isinstance(payload["roi"], (list, tuple))
        assert "ocr_texts" in payload and payload["ocr_texts"] == ["大廳", "出發"]
    assert dumped_states == ["STATE_LOBBY", "STATE_FORMATION", "STATE_PREPARE"]


def test_corpus_dump_disabled_when_none(tmp_path: Path) -> None:
    """corpus_dir=None 時:不產生任何檔案、不報錯(回歸保險)。"""
    machine = _FakeMachine()
    states = ["STATE_LOBBY", "STATE_FORMATION", "STATE_PREPARE"]
    _drive_watchdog(machine, states, None, tmp_path)

    # tmp_path 內不應出現任何 corpus 子目錄/語料檔(只會有 watchdog_samples.jsonl)
    assert not (tmp_path / "corpus").exists()
    assert list(tmp_path.glob("*.png")) == []
    leftover_json = [p for p in tmp_path.glob("*.json")]
    assert leftover_json == [], f"corpus_dir=None 不應傾印任何 json,實際: {leftover_json}"
