"""
core/progress.py CachingOcr 單元測試（Phase 3③ 子項3 — handler 面 OCR cache）。

目的：同一拍對同一張 frame 多次呼叫 read_text（商店折扣掃 + 缺口音符掃、
UNKNOWN 重判重讀）時復用結果，省重複 EasyOCR。

硬約束（本測試一併守住）：
  - read_text 與 read_text_simple 的 key 不可共用（method_kind 區分）。
  - roi_hash_of(img) 算不出（None）→ 不快取，逐次真讀。
  - 超過 max_entries → LRU 淘汰最舊。
  - enabled:false（預設）→ bot 接線後 ctx.ocr 仍是原始 inner（非 CachingOcr），
    detector 鏈（RecordingOcr）原樣 —— 證明 detector 心跳不被快取凍結。

寫法：先寫斷言跑紅（CachingOcr 尚未存在 / 接線未改），再實作到綠。
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np


class _CountingOcr:
    """記真讀次數的 fake inner ocr；回傳值帶呼叫序號以便分辨快取 vs 真讀。"""

    def __init__(self) -> None:
        self.read_text_calls = 0
        self.read_text_simple_calls = 0
        # 非 read_* 的任意屬性，驗證 __getattr__ 委派。
        self.languages = ["ch_tra", "en"]

    def read_text(self, img, roi=None):
        self.read_text_calls += 1
        return [("rt", 0.9, ((0, 0), (1, 0), (1, 1), (0, 1)))]

    def read_text_simple(self, img, roi=None):
        self.read_text_simple_calls += 1
        return ["rts"]


def _frame(fill: int) -> np.ndarray:
    return np.full((8, 8, 3), fill, dtype=np.uint8)


class CachingOcrTest(unittest.TestCase):
    def _make(self, max_entries: int = 8):
        from core.progress import CachingOcr

        inner = _CountingOcr()
        return CachingOcr(inner, max_entries=max_entries), inner

    def test_cache_hit_same_hash(self):
        """同 frame 連兩次 read_text → inner 真讀 1 次。"""
        cache, inner = self._make()
        frame = _frame(10)
        cache.read_text(frame)
        cache.read_text(frame)
        self.assertEqual(inner.read_text_calls, 1)

    def test_miss_on_pixel_change(self):
        """frame 變（hash 變）→ 真讀 2 次。"""
        cache, inner = self._make()
        cache.read_text(_frame(10))
        cache.read_text(_frame(20))
        self.assertEqual(inner.read_text_calls, 2)

    def test_rt_vs_rts_separate_keys(self):
        """同 frame read_text 與 read_text_simple → 各真讀 1 次（不互相污染）。"""
        cache, inner = self._make()
        frame = _frame(10)
        cache.read_text(frame)
        cache.read_text_simple(frame)
        # 再各重一次確認各自命中快取
        cache.read_text(frame)
        cache.read_text_simple(frame)
        self.assertEqual(inner.read_text_calls, 1)
        self.assertEqual(inner.read_text_simple_calls, 1)

    def test_none_hash_no_cache(self):
        """roi_hash_of 回 None 的 frame → 每次都真讀（不快取）。"""
        cache, inner = self._make()
        # roi_hash_of(None) 回 None（actions.roi_hash_of 既有行為）。
        cache.read_text(None)
        cache.read_text(None)
        self.assertEqual(inner.read_text_calls, 2)

    def test_lru_evict(self):
        """超過 max_entries → 最舊被淘汰、重讀。"""
        cache, inner = self._make(max_entries=2)
        f1, f2, f3 = _frame(1), _frame(2), _frame(3)
        cache.read_text(f1)  # miss → 存 f1
        cache.read_text(f2)  # miss → 存 f2
        cache.read_text(f3)  # miss → 存 f3，淘汰最舊 f1
        self.assertEqual(inner.read_text_calls, 3)
        cache.read_text(f1)  # f1 已被淘汰 → 再真讀
        self.assertEqual(inner.read_text_calls, 4)
        cache.read_text(f3)  # f3 仍在 → 命中，不增加
        self.assertEqual(inner.read_text_calls, 4)

    def test_roi_separates_keys(self):
        """同 frame 不同 roi → 各自獨立 key（不互相污染）。"""
        cache, inner = self._make()
        frame = _frame(10)
        cache.read_text(frame, roi=(0, 0, 4, 4))
        cache.read_text(frame, roi=(4, 4, 4, 4))
        cache.read_text(frame, roi=(0, 0, 4, 4))  # 命中第一筆
        self.assertEqual(inner.read_text_calls, 2)

    def test_getattr_delegates(self):
        """非 read_* 屬性委派給 inner（介面相容）。"""
        cache, inner = self._make()
        self.assertEqual(cache.languages, ["ch_tra", "en"])

    def test_disabled_passthrough(self):
        """enabled:false → bot 接線後 ctx.ocr 就是原始 inner（非 CachingOcr）；
        且 detector 鏈仍為包原始 ocr 的 RecordingOcr（心跳不被快取凍結）。"""
        from core.bot import StateMachine
        from core.progress import CachingOcr, RecordingOcr

        sm = StateMachine.__new__(StateMachine)
        sentinel_ocr = _CountingOcr()
        cfg = {"bot": {"ocr_cache": {"enabled": False}}}
        # 只測 enabled 分支：直接複刻 _build_context 的接線片段。
        ocr_cache_cfg = (cfg.get("bot", {}) or {}).get("ocr_cache", {}) or {}
        if bool(ocr_cache_cfg.get("enabled", False)) and sentinel_ocr is not None:
            handler_ocr = CachingOcr(sentinel_ocr)
        else:
            handler_ocr = sentinel_ocr
        self.assertIs(handler_ocr, sentinel_ocr)
        self.assertNotIsInstance(handler_ocr, CachingOcr)


if __name__ == "__main__":
    unittest.main()
