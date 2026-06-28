from __future__ import annotations

import json
import sys
import types
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "diagnostics"))

from diagnostics.safe_single_round_test import _made_progress, _watchdog, ACTIONABLE_STATES

# 20260612_211534 動畫盲區實機樣本（watchdog_samples.jsonl 索引 315–405 段；
# logs/ 不進版控，故抽出固定段落存為 tests/replays fixture）。
ANIMATION_FIXTURE = (
    PROJECT_ROOT / "tests" / "replays" / "watchdog_animation_20260612_211534.jsonl"
)


def _sample(**overrides):
    payload = {
        "state": "STATE_POTENTIAL_SELECT",
        "run_count": 0,
        "success_count": 0,
        "current_floor": 0,
        "current_money": 0,
        "shop_refresh_count": 0,
        "current_notes": {},
        "click_count": 1,
        "roi": (10, 20, 30, 40),
        "roi_hash": 123,
        "card_counter": {
            "current_total": 0,
        },
    }
    payload.update(overrides)
    return payload


def test_visual_roi_change_alone_no_longer_counts_as_progress() -> None:
    """Phase 1.4：純 roi_hash 變化會被畫面動畫擊穿（實機證據 20260612_211534，
    事件畫面動畫使 roi_hash 每 ~3 秒變一次、bot 凍結 14 分鐘無人收屍），
    外部 watchdog 不得再把 roi_hash 變化當進度。修復前此測試為紅。"""
    assert not _made_progress(_sample(), _sample(roi_hash=456))


def test_click_count_advance_counts_as_progress() -> None:
    """Phase 1.4 取捨：外部 watchdog 拿不到 OCR，進度只看 state / 業務計數 /
    click_count 前進（Phase 1.3 之後點擊已是「看到才點」，盲點路徑已拆除），
    並由 --max-duration 硬上限雙保險。修復前（click_count 不算進度）此測試為紅。"""
    assert _made_progress(_sample(), _sample(click_count=2))


def test_business_counter_change_counts_as_progress() -> None:
    assert _made_progress(_sample(), _sample(shop_refresh_count=1))
    assert _made_progress(_sample(), _sample(current_notes={"激昂": 1}))


def test_reroll_execution_registers_as_business_counter_progress() -> None:
    """session 20260613_225241：上樓後 3 卡無紅字 → bot 連按 Q 重抽（卡組真的換新，
    Q reroll 實機生效）；但外部 watchdog 拿不到 OCR 文字集合 → reroll 既無 state 變、
    無 click、無計數變 → 30s 誤判 stuck，比第 4 組出紅字取卡早 4 秒收屍。修：reroll
    改記業務計數 reroll_count，讓粗粒度的外部 watchdog 也看得到原地推進。修復前
    progress_counters 無此鍵 → 此測試為紅。"""
    from types import SimpleNamespace

    from core import progress as progress_mod

    counters = progress_mod.progress_counters(SimpleNamespace(reroll_count=2))
    assert counters["reroll_count"] == 2
    # reroll_count 前進 → 業務計數變化 → 外部 watchdog 算進度（不再 30s 誤殺連抽）。
    prev = {"state": "STATE_POTENTIAL_SELECT", "counters": {"reroll_count": 0}, "total_click_count": 0}
    cur = {"state": "STATE_POTENTIAL_SELECT", "counters": {"reroll_count": 1}, "total_click_count": 0}
    assert _made_progress(prev, cur)


def test_real_animation_samples_report_no_progress() -> None:
    """紅測試（動畫盲區，真實樣本）：直接取 20260612_211534 的
    watchdog_samples.jsonl 動畫段（91 筆：state 恆 POTENTIAL_SELECT、
    click_count 凍結 20、roi_hash 每 ~3 秒變化）餵 _made_progress，
    整段必須全部回報「無進度」→ 外部 watchdog 的 stuck timeout 才追得上。
    修復前 roi_hash 變化被當進度 → 此測試為紅。"""
    lines = ANIMATION_FIXTURE.read_text(encoding="utf-8").splitlines()
    samples = [json.loads(line) for line in lines if line.strip()]
    assert len(samples) >= 30, "fixture 應涵蓋 ≥30 秒的動畫盲區樣本"

    progressed_at = [
        i for i in range(1, len(samples)) if _made_progress(samples[i - 1], samples[i])
    ]
    assert progressed_at == [], (
        f"動畫段不得有任何樣本被視為進度，違規索引：{progressed_at[:10]}"
    )


# ---------------------------------------------------------------------------
# Phase 0.4: max_duration 總時長硬上限測試
# ---------------------------------------------------------------------------

class _FakeMachine:
    """最小假 StateMachine，足以驅動 _watchdog 的 max_duration 分支。"""

    def __init__(self, samples_sequence: list[dict]) -> None:
        self._samples = list(samples_sequence)
        self._sample_idx = 0
        self.finalized_reason: str | None = None
        self.finalized_extra: dict = {}

        # ctx 屬性
        ctx = types.SimpleNamespace()
        ctx.running = True
        ctx.failure_dir = None
        ctx.click_trace = []
        ctx.state_trace = []
        # 以下屬性讓 _sample() 在真實機器上能跑，但本測試直接 mock _sample
        ctx.current_state = "STATE_POTENTIAL_SELECT"
        ctx.run_count = 0
        ctx.success_count = 0
        ctx.current_floor = 0
        ctx.current_money = 0
        ctx.shop_refresh_count = 0
        ctx.current_notes = {}
        ctx.last_frame = None
        ctx.frame_w = 1920
        ctx.frame_h = 1080
        ctx.card_counter_enabled = False
        ctx.card_counter_current_total = 0
        ctx.card_counter_target_total = 0
        self.ctx = ctx

    def finalize_failure(self, reason: str, extra: dict | None = None) -> None:
        self.finalized_reason = reason
        self.finalized_extra = extra or {}
        self.ctx.running = False  # 讓 watchdog 迴圈感知到機器已停


class _FakeClock:
    """可注入的假時鐘；每次呼叫 tick() 推進指定秒數。"""

    def __init__(self, start: float = 1_000_000.0) -> None:
        self._t = start

    def now(self) -> float:
        return self._t

    def advance(self, seconds: float) -> None:
        self._t += seconds


def _build_stuck_animation_samples(
    fake_clock: _FakeClock,
    count: int,
    interval: float = 3.0,
) -> list[dict]:
    """
    製造「state 恆 POTENTIAL_SELECT、click 凍結 20、roi 固定、roi_hash 每筆不同」
    的樣本序列，模擬卡死動畫場景。
    """
    base_roi = (38, 57, 1203, 561)
    samples = []
    for i in range(count):
        samples.append({
            "timestamp": fake_clock.now(),
            "state": "STATE_POTENTIAL_SELECT",
            "run_count": 0,
            "success_count": 0,
            "current_floor": 0,
            "current_money": 0,
            "shop_refresh_count": 0,
            "current_notes": {},
            "click_count": 20,          # 凍結不增加
            "roi": base_roi,            # roi 固定
            "roi_hash": 9000 + i,       # hash 每筆都不同(動畫)
            "card_counter": {"current_total": 0},
        })
        fake_clock.advance(interval)
    return samples


def test_max_duration_exceeded_triggers_failure(tmp_path: Path) -> None:
    """
    卡死動畫場景：state 恆 ACTIONABLE、click 凍結、roi 固定、roi_hash 持續變化。
    _made_progress() 每筆都回 True（roi_hash 不同），因此 stuck_timeout 不會觸發。
    但超過 max_duration 後，watchdog 必須呼叫 finalize_failure("max_duration_exceeded")。
    測試使用 fake clock，完全不 sleep。
    """
    fake_clock = _FakeClock(start=1_000_000.0)

    # 製造 30 筆樣本，每筆間隔 3 秒 → 總跨度 87 秒
    samples = _build_stuck_animation_samples(fake_clock, count=30, interval=3.0)

    # Phase 1.4 之後：動畫樣本（僅 roi_hash 變化）不得再被視為進度；
    # 本測試改用 stuck_timeout=300 > 樣本跨度 87 秒，讓 per-state stuck
    # 不觸發，單獨驗證 max_duration 硬上限仍會收屍（雙保險彼此獨立）。
    for i in range(1, len(samples)):
        assert not _made_progress(samples[i - 1], samples[i]), (
            f"樣本 {i} 不得被視為有進度(僅 roi_hash 不同 = 動畫)"
        )

    machine = _FakeMachine(samples)

    # 使用 fake clock 驅動 _watchdog；max_duration=60 秒，樣本跨度 87 秒，應超限
    _watchdog(
        machine=machine,
        run_dir=tmp_path,
        timeout_s=300,          # stuck_timeout 大於樣本跨度，不應觸發
        interval_s=0.0,         # 測試用，不真的 sleep
        max_duration_s=60,      # 60 秒硬上限
        _sample_fn=lambda m: samples[m._sample_idx],
        _sleep_fn=lambda _: _advance_sample_idx(machine),
        _time_fn=fake_clock.now,
        _start_time=1_000_000.0,  # watchdog 啟動時刻 = 樣本序列起點
    )

    assert machine.finalized_reason == "max_duration_exceeded", (
        f"預期 finalize_failure('max_duration_exceeded')，實際: {machine.finalized_reason!r}"
    )


def _advance_sample_idx(machine: _FakeMachine) -> None:
    """fake sleep 回呼：推進樣本索引，讓下一輪迴圈取下一筆。"""
    machine._sample_idx = min(machine._sample_idx + 1, len(machine._samples) - 1)
