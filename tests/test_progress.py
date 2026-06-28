"""
core/progress.py 單元測試（Phase 1.4 共用進度訊號模組）。

涵蓋：
  - verified_click_progress：click_verified 成功 / verify 失敗批次 /
    EXPECT_NONE / 舊式未驗證點擊的判別。
  - OCR 文字集合：正規化、Jaccard 距離、抖動低於閾值不算進度。
  - sample_counters / sample_click_count：新舊 watchdog 樣本 schema 相容。
  - StuckDetector：豁免狀態、基準累積漂移、poll_limit 觸發。
"""
from __future__ import annotations

import unittest

from core.progress import (
    StuckConfig,
    StuckDetector,
    counters_changed,
    jaccard_distance,
    normalized_text_set,
    sample_click_count,
    sample_counters,
    stuck_config_from,
    verified_click_progress,
)


def _verified_entry(**overrides) -> dict:
    entry = {
        "timestamp": 1.0,
        "source": "potential_reroll",
        "x": 100,
        "y": 200,
        "success": True,
        "target": "text:重抽",
        "expect": "roi_change:full_frame",
        "attempt": 1,
    }
    entry.update(overrides)
    return entry


class VerifiedClickProgressTests(unittest.TestCase):
    def test_successful_verified_click_counts(self) -> None:
        self.assertTrue(verified_click_progress([_verified_entry()]))

    def test_expect_none_click_does_not_count(self) -> None:
        # EXPECT_NONE：找得到才點，但未做點後驗證 → 非強進度訊號
        self.assertFalse(verified_click_progress([_verified_entry(expect="none")]))

    def test_legacy_unverified_click_does_not_count(self) -> None:
        # 舊式 _click_text_or_fallback / _click_with_trace 條目沒有 expect 欄位
        legacy = {
            "timestamp": 1.0,
            "source": "ocr_text",
            "x": 1,
            "y": 2,
            "success": True,
            "keywords": ["出發"],
            "fallback": False,
        }
        self.assertFalse(verified_click_progress([legacy]))

    def test_verify_failed_batch_cancels_attempt_clicks(self) -> None:
        # click_verified verify 失敗：兩次 attempt 點擊 success=True，
        # 但尾隨 click_verified_verify_failed → 同 target 不算進度
        batch = [
            _verified_entry(attempt=1),
            _verified_entry(attempt=2),
            {
                "timestamp": 2.0,
                "source": "click_verified_verify_failed",
                "x": 100,
                "y": 200,
                "success": False,
                "original_source": "potential_reroll",
                "target": "text:重抽",
                "expect": "roi_change:full_frame",
                "attempts": 2,
            },
        ]
        self.assertFalse(verified_click_progress(batch))

    def test_empty_or_none_entries(self) -> None:
        self.assertFalse(verified_click_progress([]))
        self.assertFalse(verified_click_progress(None))


class TextSetTests(unittest.TestCase):
    def test_normalized_text_set_none_is_silent(self) -> None:
        self.assertIsNone(normalized_text_set(None))

    def test_normalization_absorbs_whitespace_and_case(self) -> None:
        a = normalized_text_set(["請選擇 1個", "Reroll"])
        b = normalized_text_set(["請選擇1個", "reroll"])
        self.assertEqual(a, b)

    def test_jaccard_distance_bounds(self) -> None:
        self.assertEqual(jaccard_distance(frozenset(), frozenset()), 0.0)
        self.assertEqual(
            jaccard_distance(frozenset({"a"}), frozenset({"b"})), 1.0
        )
        # 10 詞集合差 1 詞 → 距離 2/11 ≈ 0.18 < 預設閾值 0.3（OCR 抖動容忍）
        ten = frozenset(str(i) for i in range(10))
        ten_jitter = frozenset(list(ten - {"0"}) + ["x"])
        self.assertLess(jaccard_distance(ten, ten_jitter), 0.3)


class SampleSchemaTests(unittest.TestCase):
    def test_new_schema_counters_passthrough(self) -> None:
        sample = {"counters": {"run_count": 1}, "state": "STATE_SHOP"}
        self.assertEqual(sample_counters(sample), {"run_count": 1})

    def test_legacy_schema_counters_extracted(self) -> None:
        # 20260612_211534 watchdog_samples.jsonl 的平鋪欄位
        sample = {
            "state": "STATE_POTENTIAL_SELECT",
            "run_count": 0,
            "success_count": 0,
            "current_floor": 0,
            "current_money": 0,
            "shop_refresh_count": 0,
            "current_notes": {},
            "click_count": 20,
            "roi_hash": 4140859820,
            "card_counter": {"enabled": True, "current_total": 0, "target_total": 78},
        }
        counters = sample_counters(sample)
        self.assertEqual(counters["run_count"], 0)
        self.assertEqual(counters["card_counter_current_total"], 0)
        self.assertNotIn("roi_hash", counters)

    def test_sample_click_count_prefers_uncapped_total(self) -> None:
        self.assertEqual(
            sample_click_count({"total_click_count": 35, "click_count": 20}), 35
        )
        self.assertEqual(sample_click_count({"click_count": 20}), 20)
        self.assertEqual(sample_click_count({}), 0)

    def test_counters_changed_none_is_silent(self) -> None:
        self.assertFalse(counters_changed(None, {"run_count": 1}))
        self.assertTrue(counters_changed({"run_count": 0}, {"run_count": 1}))


class StuckDetectorTests(unittest.TestCase):
    def _observe_static(self, detector: StuckDetector, n: int, **kw) -> bool:
        stuck = False
        for _ in range(n):
            stuck = detector.observe(
                state=kw.get("state", "STATE_SHOP"),
                counters=kw.get("counters", {"run_count": 0}),
                texts=kw.get("texts", ["商店", "購買"]),
                click_entries=kw.get("click_entries"),
            )
        return stuck

    def test_poll_limit_triggers_after_k_static_polls(self) -> None:
        detector = StuckDetector(StuckConfig(poll_limit=5))
        # 第 1 拍 state_changed（初始）重置 → 之後 5 拍無進度 → 觸發
        self.assertFalse(self._observe_static(detector, 1))
        self.assertFalse(self._observe_static(detector, 4))
        self.assertTrue(self._observe_static(detector, 1))
        self.assertEqual(detector.streak, 5)

    def test_exempt_state_never_accumulates(self) -> None:
        detector = StuckDetector(StuckConfig(poll_limit=3))
        for _ in range(10):
            self.assertFalse(
                detector.observe(
                    state="STATE_FAST_BATTLE",
                    counters={"run_count": 0},
                    texts=None,
                )
            )
        self.assertEqual(detector.streak, 0)

    def test_text_jitter_below_threshold_is_not_progress(self) -> None:
        detector = StuckDetector(StuckConfig(poll_limit=4, text_jaccard=0.3))
        base = [str(i) for i in range(10)]
        jitter = base[1:] + ["x"]  # 差 1 詞，距離 ~0.18 < 0.3
        detector.observe(state="STATE_EVENT", counters={}, texts=base)  # 初始重置
        for i in range(3):
            self.assertFalse(
                detector.observe(
                    state="STATE_EVENT",
                    counters={},
                    texts=jitter if i % 2 else base,
                )
            )
        self.assertTrue(
            detector.observe(state="STATE_EVENT", counters={}, texts=base)
        )

    def test_gradual_drift_accumulates_against_baseline(self) -> None:
        # 緩慢但真實的畫面變化：相對基準累積、最終越過閾值算進度
        detector = StuckDetector(StuckConfig(poll_limit=4, text_jaccard=0.3))
        base = [str(i) for i in range(10)]
        detector.observe(state="STATE_EVENT", counters={}, texts=base)
        drift1 = base[2:] + ["a", "b"]          # 距離 ~0.33 > 0.3
        self.assertFalse(
            detector.observe(state="STATE_EVENT", counters={}, texts=drift1)
        )
        self.assertEqual(detector.streak, 0, "越過閾值的真實變化必須重置計數")

    def test_counters_change_resets_streak(self) -> None:
        detector = StuckDetector(StuckConfig(poll_limit=3))
        self._observe_static(detector, 3)  # 初始重置 + streak 2
        self.assertEqual(detector.streak, 2)
        self.assertFalse(
            detector.observe(
                state="STATE_SHOP",
                counters={"run_count": 1},
                texts=["商店", "購買"],
            )
        )
        self.assertEqual(detector.streak, 0)

    def test_stuck_config_from_reads_bot_keys(self) -> None:
        cfg = stuck_config_from(
            {
                "bot": {
                    "stuck_poll_limit": 7,
                    "stuck_text_jaccard": 0.5,
                    "stuck_exempt_states": ["STATE_FAST_BATTLE", "STATE_HOME"],
                }
            }
        )
        self.assertEqual(cfg.poll_limit, 7)
        self.assertEqual(cfg.text_jaccard, 0.5)
        self.assertEqual(cfg.exempt_states, frozenset({"STATE_FAST_BATTLE", "STATE_HOME"}))

    def test_stuck_config_defaults_on_garbage(self) -> None:
        cfg = stuck_config_from({"bot": {"stuck_poll_limit": "abc", "stuck_text_jaccard": None}})
        self.assertEqual(cfg.poll_limit, 12)
        self.assertEqual(cfg.text_jaccard, 0.3)
        self.assertEqual(cfg.exempt_states, frozenset({"STATE_FAST_BATTLE"}))


if __name__ == "__main__":
    unittest.main()
