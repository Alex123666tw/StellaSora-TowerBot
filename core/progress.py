"""
進度訊號共用模組 (core/progress.py) — REPAIR_PLAN Phase 1.4，解 R4。

R4：舊 `NO_PROGRESS_STATES` 恰好不含實際卡死的狀態（SHOP / POTENTIAL_SELECT /
EVENT），bot 只能靠外部 watchdog 收屍；而外部 watchdog 的「roi_hash 變化 =
有進度」會被**畫面持續動畫**擊穿（實機證據 20260612_211534：事件畫面動畫使
roi_hash 每 ~3 秒變一次，bot 凍結 14 分鐘、click_count 卡 20，watchdog 永不
觸發）。

本模組是 core/bot.py（內建卡死偵測）與 diagnostics/safe_single_round_test.py
（外部 watchdog 雙保險）的單一進度定義來源：

實質進度訊號（任一成立即重置卡死計數）：
  1. 狀態改變（state 前進；含 handler 轉移與 detector 判定切換）。
  2. 業務計數變化（run_count / success_count / shop_visit_count /
     shop_refresh_count / current_notes / pending 卡計數 / 已購槽位 /
     card_counter 等，見 progress_counters()）。
  3. 成功且通過驗證的點擊（Phase 1.3 click_verified 回 True 的 click_trace
     特徵；reroll 換卡屬合法原地進度，正好被此涵蓋）。
  4. OCR 文字集合的實質變化（正規化文字集合 vs 基準的 Jaccard 距離超過
     閾值）— **取代純 roi_hash 變化**；動畫光效不產生文字，不會誤算進度。

外部 watchdog 拿不到 OCR 與 click_trace 細節，故其進度定義縮為
「state / 業務計數 / click_count 前進」（roi_hash 僅留紀錄欄位，不再參與
判定），並由 --max-duration 硬上限雙保險。
"""
from __future__ import annotations

import logging
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Iterable

from core import actions
from vision import signatures

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────
# 預設值（config.yaml bot.stuck_* 可覆寫）
# ─────────────────────────────────────────────────────────────

# 同一狀態連續 K 次輪詢無實質進度 → finalize_failure("state_stuck_no_progress")。
# 換算時間 ≈ K × (poll_interval + 每拍 OCR 時間)；預設 12 × (0.8s + ~1s) ≦ 30 秒。
DEFAULT_STUCK_POLL_LIMIT = 12

# OCR 文字集合 Jaccard 距離 > 此值才算實質變化（吸收 EasyOCR 同畫面抖動）。
DEFAULT_STUCK_TEXT_JACCARD = 0.3

# 豁免狀態：等待是設計行為、且時長由遊戲決定（快速戰鬥可合法超過 30 秒、
# 期間零點擊零計數變化）。與外部 watchdog 的 ACTIONABLE_STATES（不含
# FAST_BATTLE / HOME）一致；極端情況由外部 --max-duration 硬上限兜底。
DEFAULT_STUCK_EXEMPT_STATES = frozenset({"STATE_FAST_BATTLE"})


# ─────────────────────────────────────────────────────────────
# 業務計數（bot 內建偵測與外部 watchdog 共用）
# ─────────────────────────────────────────────────────────────


def progress_counters(ctx) -> dict:
    """取 BotContext 的業務計數快照（JSON 可序列化；變化 = 實質進度）。"""
    notes = getattr(ctx, "current_notes", None) or {}
    purchased = getattr(ctx, "shop_purchased_slots", None) or ()
    return {
        "run_count": int(getattr(ctx, "run_count", 0) or 0),
        "success_count": int(getattr(ctx, "success_count", 0) or 0),
        "current_floor": int(getattr(ctx, "current_floor", 0) or 0),
        "current_money": int(getattr(ctx, "current_money", 0) or 0),
        "shop_visit_count": int(getattr(ctx, "shop_visit_count", 0) or 0),
        "shop_refresh_count": int(getattr(ctx, "shop_refresh_count", 0) or 0),
        "current_notes": dict(notes),
        "pending_card_count": getattr(ctx, "pending_card_count", None),
        # reroll 走 Q 鍵（無 state 變、無 click），外部 watchdog 拿不到 OCR 文字集合
        # → 連抽的原地推進原本不可見 → 30s 誤殺（session 20260613_225241）。納入
        # 業務計數後，每次 reroll = 計數變化 = 進度。
        "reroll_count": int(getattr(ctx, "reroll_count", 0) or 0),
        "pending_shop_card_slot_key": getattr(ctx, "pending_shop_card_slot_key", None),
        "shop_purchased_slots": sorted(purchased),
        "card_counter_current_total": int(
            getattr(ctx, "card_counter_current_total", 0) or 0
        ),
        "reconnect_attempts": int(getattr(ctx, "reconnect_attempts", 0) or 0),
    }


def counters_changed(prev: dict | None, cur: dict | None) -> bool:
    """業務計數是否變化。任一邊缺資料（None）→ 無法證明進度 → False。"""
    if prev is None or cur is None:
        return False
    return prev != cur


# watchdog 樣本（dict）的計數抽取：新 schema 用 "counters"，
# 舊 schema（歷史 watchdog_samples.jsonl，如 20260612_211534）為平鋪欄位。
_LEGACY_SAMPLE_COUNTER_KEYS = (
    "run_count",
    "success_count",
    "current_floor",
    "current_money",
    "shop_visit_count",
    "shop_refresh_count",
    "pending_card_count",
    "pending_shop_card_slot_key",
)


def sample_counters(sample: dict) -> dict:
    """從 watchdog 樣本 dict 取業務計數（相容新舊 schema）。"""
    counters = sample.get("counters")
    if isinstance(counters, dict):
        return counters
    legacy = {key: sample.get(key) for key in _LEGACY_SAMPLE_COUNTER_KEYS if key in sample}
    if "current_notes" in sample:
        legacy["current_notes"] = sample.get("current_notes")
    card_counter = sample.get("card_counter")
    if isinstance(card_counter, dict):
        legacy["card_counter_current_total"] = card_counter.get("current_total")
    return legacy


def sample_click_count(sample: dict) -> int:
    """從 watchdog 樣本取點擊計數（優先用不封頂的 total_click_count；
    舊 schema 的 click_count = len(click_trace) 受 deque maxlen=20 封頂，
    僅供舊樣本相容）。"""
    value = sample.get("total_click_count")
    if value is None:
        value = sample.get("click_count")
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


# ─────────────────────────────────────────────────────────────
# OCR 文字集合（取代純 roi_hash 的視覺進度判定）
# ─────────────────────────────────────────────────────────────


def normalized_text_set(texts: Iterable[str] | None) -> frozenset[str] | None:
    """OCR 文字 → 正規化集合（signatures.normalize_text；去空白標點、轉小寫）。

    texts=None 代表「本拍沒有新 OCR 結果」→ 回 None（訊號靜默，
    不算進度也不算停滯證據）。
    """
    if texts is None:
        return None
    normalized = {signatures.normalize_text(text) for text in texts}
    normalized.discard("")
    return frozenset(normalized)


def jaccard_distance(a: frozenset[str], b: frozenset[str]) -> float:
    """Jaccard 距離 = 1 - |A∩B| / |A∪B|；雙空集合視為相同（0.0）。"""
    if not a and not b:
        return 0.0
    union = a | b
    return 1.0 - (len(a & b) / len(union))


# ─────────────────────────────────────────────────────────────
# 驗證通過的點擊（click_verified 強進度訊號）
# ─────────────────────────────────────────────────────────────


def verified_click_progress(click_entries: Iterable[dict] | None) -> bool:
    """本拍 click_trace 新增條目中，是否存在「成功且通過驗證」的點擊。

    判定依據 core/actions.py click_verified 的 trace 特徵：
      - 成功點擊條目：success=True 且帶 "expect" 欄位（click_verified 專屬）。
      - expect == "none"（EXPECT_NONE）未做點後驗證 → 不算強進度。
      - 整次呼叫 verify 失敗時會額外寫入
        source="click_verified_verify_failed"（含 original_source/target）→
        同批中對應 target 的成功條目不算（attempt 點擊雖 success=True，
        但畫面未如預期變化）。
      - 舊式 _click_text_or_fallback / _click_with_trace 條目無 "expect"
        欄位 → 一律不算（未驗證的點擊不構成進度證據）。
    """
    entries = [e for e in (click_entries or ()) if isinstance(e, dict)]
    failed_pairs = {
        (e.get("original_source"), e.get("target"))
        for e in entries
        if e.get("source") == "click_verified_verify_failed"
    }
    for entry in entries:
        if entry.get("source") == "click_verified_verify_failed":
            continue
        if not entry.get("success"):
            continue
        expect = entry.get("expect")
        if not expect or expect == "none":
            continue
        if (entry.get("source"), entry.get("target")) in failed_pairs:
            continue
        return True
    return False


# ─────────────────────────────────────────────────────────────
# RecordingOcr — 讓主迴圈零成本取得每拍 OCR 文字集合
# ─────────────────────────────────────────────────────────────


class RecordingOcr:
    """OcrEngine 代理：記錄最近一次全畫面 read_text_simple 的結果。

    StateDetector 每拍會對全畫面（roi=None）呼叫一次 read_text_simple；
    主迴圈據此取得 OCR 文字集合做進度判定，**不必為此重跑一次 OCR**
    （EasyOCR 全畫面一次約 1–2 秒）。其餘屬性/方法全部委派給內層引擎，
    handler 持有的 ctx.ocr 不經此代理、互不影響。
    """

    def __init__(self, inner) -> None:
        self._inner = inner
        self.last_texts: list[str] | None = None
        self.generation: int = 0  # 每次成功的全畫面 OCR +1（供呼叫端判斷新鮮度）

    def read_text_simple(self, img, roi=None):
        texts = self._inner.read_text_simple(img, roi=roi)
        if roi is None:
            self.last_texts = list(texts)
            self.generation += 1
        return texts

    def __getattr__(self, name):
        return getattr(self._inner, name)


# ─────────────────────────────────────────────────────────────
# CachingOcr — handler 面同拍 OCR 復用（Phase 3③ 子項3）
# ─────────────────────────────────────────────────────────────


class CachingOcr:
    """OcrEngine 代理：同一張 frame 同一 roi 的 OCR 結果做 LRU 快取。

    解決同一拍對同一張 frame 多次跑 EasyOCR 的浪費（商店折扣掃 + 缺口音符掃、
    UNKNOWN 重判重讀）。**只**包 handler 鏈（BotContext.ocr）；detector 鏈
    （RecordingOcr，其 generation 是 StuckDetector 的進度心跳）絕不可包進來，
    否則心跳被快取凍結會讓 L3 兜底瞎掉。預設 config 不啟用 → 不會被建立。

    快取 key = (method_kind, roi_hash, roi_tuple)：
      - method_kind 區分 read_text / read_text_simple（兩者結果型別不同，
        嚴禁共用 key）。
      - roi_hash = actions.roi_hash_of(img)（整圖 crc32）。算不出（None）→
        不快取，直接真讀（不污染、不誤命中）。
      - roi_tuple = tuple(roi) 或 None（不同 roi 互不干擾）。

    其餘屬性/方法（reader、languages、自訂方法等）全部委派 inner，保持與真
    OcrEngine 介面相容。
    """

    def __init__(self, inner, max_entries: int = 8) -> None:
        self._inner = inner
        self._max_entries = max(1, int(max_entries))
        self._cache: "OrderedDict[tuple, object]" = OrderedDict()

    @staticmethod
    def _roi_key(roi):
        return tuple(roi) if roi is not None else None

    def _cached(self, method_kind: str, method, img, roi):
        roi_hash = actions.roi_hash_of(img)
        if roi_hash is None:
            # hash 算不出（None / 空 frame）→ 無法當 key，直接真讀、不快取。
            return method(img, roi=roi)
        key = (method_kind, roi_hash, self._roi_key(roi))
        if key in self._cache:
            self._cache.move_to_end(key)  # LRU：命中即標記為最近使用
            return self._cache[key]
        result = method(img, roi=roi)
        self._cache[key] = result
        self._cache.move_to_end(key)
        while len(self._cache) > self._max_entries:
            self._cache.popitem(last=False)  # 淘汰最舊（least-recently-used）
        return result

    def read_text(self, img, roi=None):
        return self._cached("read_text", self._inner.read_text, img, roi)

    def read_text_simple(self, img, roi=None):
        return self._cached("read_text_simple", self._inner.read_text_simple, img, roi)

    def __getattr__(self, name):
        return getattr(self._inner, name)


# ─────────────────────────────────────────────────────────────
# StuckDetector — 主迴圈內建卡死偵測
# ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class StuckConfig:
    poll_limit: int = DEFAULT_STUCK_POLL_LIMIT
    text_jaccard: float = DEFAULT_STUCK_TEXT_JACCARD
    exempt_states: frozenset[str] = field(
        default_factory=lambda: frozenset(DEFAULT_STUCK_EXEMPT_STATES)
    )


def stuck_config_from(config: dict | None) -> StuckConfig:
    """從 config dict（config.yaml 載入結果）建 StuckConfig，容錯回預設。"""
    bot_cfg = (config or {}).get("bot", {}) or {}
    try:
        poll_limit = int(bot_cfg.get("stuck_poll_limit", DEFAULT_STUCK_POLL_LIMIT))
    except (TypeError, ValueError):
        poll_limit = DEFAULT_STUCK_POLL_LIMIT
    try:
        text_jaccard = float(bot_cfg.get("stuck_text_jaccard", DEFAULT_STUCK_TEXT_JACCARD))
    except (TypeError, ValueError):
        text_jaccard = DEFAULT_STUCK_TEXT_JACCARD
    exempt_raw = bot_cfg.get("stuck_exempt_states")
    if isinstance(exempt_raw, (list, tuple, set, frozenset)):
        exempt = frozenset(str(state) for state in exempt_raw)
    else:
        exempt = frozenset(DEFAULT_STUCK_EXEMPT_STATES)
    return StuckConfig(
        poll_limit=max(1, poll_limit),
        text_jaccard=min(1.0, max(0.0, text_jaccard)),
        exempt_states=exempt,
    )


class StuckDetector:
    """同一狀態連續 K 次輪詢無實質進度 → 卡死（呼叫端 finalize）。

    用法（core/bot.py 主迴圈，僅在非 UNKNOWN 輪詢呼叫；UNKNOWN 有自己的
    streak 與 finalize 路徑，兩者互不干擾）：

        if detector.observe(state=..., counters=..., texts=..., click_entries=...):
            finalize_failure("state_stuck_no_progress", ...)

    進度基準（counters / 文字集合）只在「有進度」時更新，因此緩慢但真實的
    畫面變化會相對基準累積、最終越過閾值算進度；而停滯畫面（含純動畫）
    相對基準恆無變化 → 計數一路累加到 poll_limit。
    """

    def __init__(self, config: StuckConfig | None = None) -> None:
        self.config = config or StuckConfig()
        self._last_state: str | None = None
        self._baseline_counters: dict | None = None
        self._baseline_text_set: frozenset[str] | None = None
        self._streak: int = 0
        self.last_progress_signals: tuple[str, ...] = ()

    @property
    def streak(self) -> int:
        return self._streak

    def observe(
        self,
        *,
        state: str,
        counters: dict,
        texts: Iterable[str] | None = None,
        click_entries: Iterable[dict] | None = None,
    ) -> bool:
        """回報一次輪詢觀測。回傳 True = 已達 poll_limit，呼叫端應 finalize。

        Args:
            state:         本拍結束時的狀態（含 handler 轉移結果）。
            counters:      progress_counters(ctx) 快照。
            texts:         本拍 OCR 文字（RecordingOcr.last_texts）；
                           None = 本拍無新 OCR（訊號靜默）。
            click_entries: 本拍新增的 click_trace 條目。
        """
        signals: list[str] = []
        text_set = normalized_text_set(texts)

        if state in self.config.exempt_states:
            signals.append(f"exempt_state:{state}")
        else:
            if self._last_state is None or state != self._last_state:
                signals.append("state_changed")
            if counters_changed(self._baseline_counters, counters):
                signals.append("counters_changed")
            if verified_click_progress(click_entries):
                signals.append("verified_click")
            if (
                text_set is not None
                and self._baseline_text_set is not None
                and jaccard_distance(self._baseline_text_set, text_set)
                > self.config.text_jaccard
            ):
                signals.append("ocr_text_set_changed")

        self._last_state = state

        if signals:
            self._streak = 0
            self._baseline_counters = counters
            if text_set is not None:
                self._baseline_text_set = text_set
            self.last_progress_signals = tuple(signals)
            return False

        # 無進度：補建尚未初始化的基準（不算進度也不漏接後續變化）
        if self._baseline_counters is None:
            self._baseline_counters = counters
        if self._baseline_text_set is None and text_set is not None:
            self._baseline_text_set = text_set
        self._streak += 1
        self.last_progress_signals = ()
        return self._streak >= self.config.poll_limit
