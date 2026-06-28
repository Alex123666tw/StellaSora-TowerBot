"""take_button recapture settle 的 config 化加速單測(數據驅動安全加速)。

背景:_recapture_then_click_take_button(選卡後、點「拿走」前牌面 highlight 沉澱)
原本固定 settle_delay=0.9 且呼叫端硬傳 0.9,每輪觸發 ~28 次。量測支持壓到 0.5
省 ~11s/輪。本檔鎖死「config 讀取 + 下限 clamp + 缺設定退 0.9 + 呼叫端顯式值不被覆蓋」。

設計:monkeypatch states._settle_and_refresh 記下實際吃到的 delay 引數,
並 monkeypatch _click_take_button 回 True(隔離點擊),純驗 delay 解析。
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

import core.states as states


def _ctx(bot_cfg: dict | None) -> SimpleNamespace:
    """造一個最小 ctx;bot_cfg=None 代表完全沒有 config 屬性(模擬舊 ctx)。"""
    if bot_cfg is None:
        return SimpleNamespace()
    return SimpleNamespace(config={"bot": bot_cfg})


class TakeSettleConfigTests(unittest.TestCase):
    def _run_recapture(self, ctx, **kwargs) -> float:
        """呼叫 _recapture_then_click_take_button,回傳 _settle_and_refresh 實收到的 delay。"""
        captured: dict = {}

        def _fake_settle(c, delay):
            captured["delay"] = delay
            return True

        with patch.object(states, "_settle_and_refresh", side_effect=_fake_settle), \
             patch.object(states, "_click_take_button", return_value=True):
            states._recapture_then_click_take_button(ctx, **kwargs)
        return captured["delay"]

    # 1) config 有值 → 用 config 值
    def test_take_settle_reads_config(self) -> None:
        ctx = _ctx({"take_settle_delay": 0.5})
        delay = self._run_recapture(ctx)
        self.assertAlmostEqual(delay, 0.5)

    # 2) 無此 config → 退 0.9
    def test_take_settle_defaults_when_missing(self) -> None:
        # 空 bot 區塊
        self.assertAlmostEqual(self._run_recapture(_ctx({})), 0.9)
        # 完全沒有 config 屬性
        self.assertAlmostEqual(self._run_recapture(_ctx(None)), 0.9)

    # 3) config 設過低(0.1)→ clamp 到下限(0.3)
    def test_take_settle_clamps_floor(self) -> None:
        ctx = _ctx({"take_settle_delay": 0.1})
        self.assertAlmostEqual(self._run_recapture(ctx), 0.3)

    # 4) 呼叫端明確傳 settle_delay=X → 用 X(不被 config 覆蓋)
    def test_explicit_delay_respected(self) -> None:
        ctx = _ctx({"take_settle_delay": 0.5})
        self.assertAlmostEqual(self._run_recapture(ctx, settle_delay=0.85), 0.85)

    # 5) config 非數字/壞型別 → 退 0.9(容錯)
    def test_take_settle_bad_type_falls_back(self) -> None:
        self.assertAlmostEqual(self._run_recapture(_ctx({"take_settle_delay": "fast"})), 0.9)
        self.assertAlmostEqual(self._run_recapture(_ctx({"take_settle_delay": None})), 0.9)
        # config 非 dict
        self.assertAlmostEqual(self._run_recapture(SimpleNamespace(config="oops")), 0.9)

    # 6) config <=0 → 退 0.9(讀不到/無意義值不採用)
    def test_take_settle_nonpositive_falls_back(self) -> None:
        self.assertAlmostEqual(self._run_recapture(_ctx({"take_settle_delay": 0})), 0.9)
        self.assertAlmostEqual(self._run_recapture(_ctx({"take_settle_delay": -1.0})), 0.9)

    # 7) _take_settle_delay 直接呼叫的契約(讀取器本身)
    def test_take_settle_delay_helper_contract(self) -> None:
        self.assertAlmostEqual(states._take_settle_delay(_ctx({"take_settle_delay": 0.5})), 0.5)
        self.assertAlmostEqual(states._take_settle_delay(_ctx({})), 0.9)
        self.assertAlmostEqual(states._take_settle_delay(_ctx({"take_settle_delay": 0.1})), 0.3)


if __name__ == "__main__":
    unittest.main()
