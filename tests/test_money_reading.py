"""Phase 2.1 金錢讀取 — 先紅後綠回放測試（REPAIR_PLAN §4 2.1 / GAME_MECHANICS C3·C7）。

語料勘查結論（tests/replays/ocr_cache/shop__*.json）：
  4 張「商店完整 HUD」語料（20260308_141258 / 20260602_215411 last·preflight /
  20260602_215757 preflight）右上角金幣圖示旁的餘額分別 OCR 成 900 / 930 / 930 / 930，
  bbox 固定在 x∈[1203,1239]、y∈[21,41]（1280x720 → 相對 x≈0.94、y≈0.04）。
  另 2 張（20260531_201841 / 20260602_215007）是購買彈窗畫面，沒有 HUD 餘額。
  icon_money 模板用 production 單尺度 matcher.match() 在語料上 conf 僅 0.69–0.765，
  且與非商店畫面（event/potential 0.70–0.73）重疊，無法當作門檻 → 餘額改讀固定 HUD ROI。

斷言重點：
  1. _read_money_via_icon 在 HUD 語料上讀出正確餘額（非 0）。
  2. 商品單價（160/200/120）位在貨架格（y≈250–480），不得被誤讀成餘額。
  3. 金額不足時 _should_enter_shop 不進店；購買前 ctx.current_money 被更新。
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import cv2

import core.states as states
from tests.fakes import FakeInput

_FRAMES = Path(__file__).resolve().parent / "replays" / "frames"
_OCR_CACHE = Path(__file__).resolve().parent / "replays" / "ocr_cache"


def _imread(path: Path):
    # cv2.imread 在含 CJK 字元的路徑（本專案目錄名為中文）上回傳 None，
    # 改以位元組讀檔後 imdecode（與實機截圖同為 BGR）。
    buf = np.frombuffer(path.read_bytes(), dtype=np.uint8)
    return cv2.imdecode(buf, cv2.IMREAD_COLOR)


def _load_cache_items(name: str) -> list[tuple[str, float, tuple]]:
    data = json.loads((_OCR_CACHE / f"{name}.json").read_text(encoding="utf-8"))
    items: list[tuple[str, float, tuple]] = []
    for it in data["items"]:
        bbox = tuple((int(p[0]), int(p[1])) for p in it["bbox"])
        items.append((it["text"], float(it["confidence"]), bbox))
    return items


class CachedOCR:
    """以語料 OCR cache 重現 read_text/read_text_simple，含 ROI 裁切過濾（不跑 EasyOCR）。"""

    def __init__(self, items: list[tuple[str, float, tuple]]) -> None:
        self.items = list(items)

    def _filter(self, roi):
        if roi is None:
            return list(self.items)
        rx, ry, rw, rh = roi
        out = []
        for text, conf, bbox in self.items:
            xs = [p[0] for p in bbox]
            ys = [p[1] for p in bbox]
            cx = sum(xs) / len(xs)
            cy = sum(ys) / len(ys)
            if rx <= cx <= rx + rw and ry <= cy <= ry + rh:
                out.append((text, conf, bbox))
        return out

    def read_text(self, img, roi=None):
        return self._filter(roi)

    def read_text_simple(self, img, roi=None):
        return [text for text, _conf, _bbox in self._filter(roi)]


# 商店完整 HUD 語料 → 期望餘額。
_HUD_CASES = {
    "shop__20260308_141258__last.png": 900,
    "shop__20260602_215411__last.png": 930,
    "shop__20260602_215411__preflight.png": 930,
    "shop__20260602_215757__preflight.png": 930,
}


def _make_ctx(frame_name: str) -> SimpleNamespace:
    scene = _imread(_FRAMES / frame_name)
    assert scene is not None, f"missing corpus frame {frame_name}"
    h, w = scene.shape[:2]
    return SimpleNamespace(
        ocr=CachedOCR(_load_cache_items(frame_name)),
        last_frame=scene,
        input=FakeInput(),
        frame_w=w,
        frame_h=h,
        current_money=0,
    )


class MoneyReadingTests(unittest.TestCase):
    def test_reads_balance_from_hud_corpus(self) -> None:
        for frame_name, expected in _HUD_CASES.items():
            with self.subTest(frame=frame_name):
                ctx = _make_ctx(frame_name)
                value = states._read_money_via_icon(ctx)
                self.assertEqual(
                    value, expected,
                    f"{frame_name}: 期望讀出餘額 {expected}，實得 {value}",
                )

    def test_does_not_read_product_price_as_balance(self) -> None:
        # 20260308 貨架商品單價含 160/200/400/320/72/90 等，餘額是 900；
        # 讀出的值必須是右上 HUD 餘額，不能是任何商品單價。
        ctx = _make_ctx("shop__20260308_141258__last.png")
        value = states._read_money_via_icon(ctx)
        self.assertEqual(value, 900)
        self.assertNotIn(value, {160, 200, 400, 320, 72, 90, 100})

    def test_should_enter_shop_blocks_when_broke(self) -> None:
        # Phase 2.6 移除 upgrade_price 死參數;Phase 2.2 移除 current_floor 終層死分支
        # (_is_last_shop_floor 因 current_floor 恆 0 從不觸發;終層改靠 SHOP_CHOICE「離開星塔」
        # + STATE_RESULT 簽名收尾)。_should_enter_shop 只剩「破產不進店」gate:
        # 餘額 0（連最便宜商品都買不起）→ 不進店、直接上樓;有餘額 → 進店（店內買法由
        # handle_shop affordability 過濾把關)。
        self.assertFalse(states._should_enter_shop(current_money=0))
        self.assertTrue(states._should_enter_shop(current_money=300))

    def test_handle_shop_updates_current_money_from_hud(self) -> None:
        # handle_shop 進入後應把讀到的真實餘額寫回 ctx.current_money（修復前恆 0）。
        scene = _imread(_FRAMES / "shop__20260602_215411__last.png")
        h, w = scene.shape[:2]
        ctx = SimpleNamespace(
            current_state="STATE_SHOP",
            ocr=CachedOCR(_load_cache_items("shop__20260602_215411__last.png")),
            last_frame=scene,
            input=FakeInput(),
            matcher=None,
            frame_w=w,
            frame_h=h,
            current_money=0,
            current_floor=5,
            target_notes={},
            current_notes={},
            shop_refresh_count=0,
            config={"bot": {"max_shop_refresh": 0}},
            card_counter_enabled=False,
            card_counter_current_total=0,
            card_counter_target_total=0,
        )
        with patch.object(states.time, "sleep", return_value=None):
            states.handle_shop(ctx)
        self.assertEqual(ctx.current_money, 930)


if __name__ == "__main__":
    unittest.main()
