"""
主狀態機 (BotContext + Bot FSM) — core/bot.py

有限狀態機 (FSM) 控制器，每隔 N 秒截圖一次，
透過 vision 模組辨識當前遊戲狀態，再派發給對應的 handle_* 函式執行動作。

狀態清單:
  STATE_HOME             — 首頁/等待，點擊進入星塔探索
  STATE_FAST_BATTLE      — 快速戰鬥執行中
  STATE_POTENTIAL_SELECT — 潛能三選一（呼叫 DecisionEngine）
  STATE_SETTLEMENT       — 結算畫面（判定達標 → 儲存/解散）
  STATE_SHOP             — 商店頁面
  STATE_EVENT            — 隨機事件
  STATE_RECONNECT        — 斷線重連

狀態追蹤變數（每輪探索開始時重置）:
  current_floor (int)          — 當前層數
  current_money (int)          — 當前金錢（花費前 OCR 讀取）
  shop_visit_count (int)       — 本輪累計遇到商店的次數
  shop_refresh_count (int)     — 本次商店內重置次數
  target_notes (dict)          — 協奏觸發所需音符目標
  current_notes (dict)         — 目前已累積的音符數量
"""
from __future__ import annotations

import json
import logging
import shutil
import signal
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

import numpy as np
import yaml

from core.decision_engine import DecisionEngine
from core import progress
from core import states
from utils.window_mgr import WindowManager
from utils.input_sim import InputSimulator
from vision.ocr_engine import OcrEngine
from vision.state_detector import DetectionResult, StateDetector, STATE_UNKNOWN
from vision.matcher import TemplateMatcher

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────
# 狀態常數
# ─────────────────────────────────────────────────────────────

STATE_HOME             = "STATE_HOME"
STATE_LOBBY            = "STATE_LOBBY"
STATE_FORMATION        = "STATE_FORMATION"
STATE_PREPARE          = "STATE_PREPARE"
STATE_FAST_BATTLE      = "STATE_FAST_BATTLE"
STATE_TAP_CONTINUE     = "STATE_TAP_CONTINUE"
STATE_NOTE_ACQUIRED    = "STATE_NOTE_ACQUIRED"
STATE_POTENTIAL_SELECT = "STATE_POTENTIAL_SELECT"
STATE_EVENT            = "STATE_EVENT"
STATE_SHOP_CHOICE      = "STATE_SHOP_CHOICE"
STATE_SHOP             = "STATE_SHOP"
STATE_LEAVE_TOWER_CONFIRM = "STATE_LEAVE_TOWER_CONFIRM"  # 「是否離開星塔?」確認彈窗
STATE_DISCARD_CONFIRM  = "STATE_DISCARD_CONFIRM"  # 「是否確定解散?」解散確認彈窗(結算丟棄)
STATE_EXPLORE_COMPLETE = "STATE_EXPLORE_COMPLETE"
STATE_RESULT           = "STATE_RESULT"
STATE_SETTLEMENT       = "STATE_SETTLEMENT"  # 舊版別名，相容保留
STATE_RECONNECT        = "STATE_RECONNECT"
# STATE_UNKNOWN 自 vision.state_detector import（Phase 1.2）：
# 無法辨識畫面時的狀態，主迴圈對此不執行 handler、不點擊。

# 連續 UNKNOWN 達此次數 → finalize_failure("state_unknown_persistent")
# （config: bot.max_unknown_streak 可覆寫）
DEFAULT_MAX_UNKNOWN_STREAK = 4

DETECTOR_GATED_TRANSITIONS = {
    STATE_LOBBY,
    STATE_FORMATION,
    STATE_PREPARE,
    STATE_SHOP_CHOICE,
    STATE_RESULT,
    STATE_SETTLEMENT,
    STATE_RECONNECT,
}

# Phase 1.4（解 R4）：舊 NO_PROGRESS_STATES 加速名單已整組刪除 ——
# 它恰好不含實際卡死的 SHOP / POTENTIAL_SELECT / EVENT，且只認「狀態不變」
# 一種訊號。現由 core/progress.py 的 StuckDetector 取代：覆蓋**所有**狀態
#（豁免名單見 config bot.stuck_exempt_states），進度訊號 = 狀態改變 /
# 業務計數變化 / click_verified 成功 / OCR 文字集合實質變化
#（純 roi_hash／畫面動畫不算進度，實機證據 20260612_211534）。


# ─────────────────────────────────────────────────────────────
# BotContext — 執行期的全域共享資料容器
# ─────────────────────────────────────────────────────────────

@dataclass
class BotContext:
    """
    Bot 執行期間的所有共享狀態，StateMachine 與各 handle_* 函式
    均透過此物件讀寫資料，避免全域變數。

    Attributes:
        engine:               潛能選擇決策引擎實例。
        config:               從 config.yaml 載入的設定字典。
        running:              是否繼續運行主迴圈。
        current_state:        當前狀態字串。

        ── 執行計數 ──
        run_count:            本次啟動的總執行次數（無論達標與否）。
        success_count:        本次啟動中達標儲存的次數。
        max_runs:             達到此次數後停止。

        ── 每輪探索追蹤變數（每輪重置） ──
        current_floor:        當前層數。樓層 OCR 讀取未實作 → 恆 0，僅供監控/GUI 顯示佔位;
                              不參與決策（終層收尾靠 SHOP_CHOICE「離開星塔」+ 簽名,REPAIR_PLAN 2.2）。
        current_money:        當前金錢（花費前 OCR 更新）。
        shop_visit_count:     本輪遇到商店的累計次數。
        shop_refresh_count:   本次商店內重置次數（離開歸零）。
        target_notes:         協奏觸發所需音符 {名稱: 數量}。
        current_notes:        目前累積音符 {名稱: 數量}。

        ── 硬體層 ──
        wm:                   WindowManager 實例（截圖）。
        input:                InputSimulator 實例（點擊）。
        last_frame:           最新截圖的 BGR 陣列（None = 尚未截圖）。

        ── 例外處理 ──
        reconnect_attempts:   本次重連嘗試次數（成功後歸零）。
        max_reconnect_attempts: 超過此次數後終止 Bot。
    """
    engine: DecisionEngine
    config: dict

    # 硬體層（由 StateMachine._build_context 初始化）
    wm: WindowManager = field(default_factory=WindowManager)
    input: InputSimulator = field(default_factory=InputSimulator)
    last_frame: np.ndarray = field(default=None)

    # 視覺層（OCR + 模板比對）
    ocr: OcrEngine = field(default=None)
    matcher: TemplateMatcher = field(default=None)

    # 目前截圖解析度（供各 handler 計算比例座標）
    frame_w: int = 1920
    frame_h: int = 1080

    running: bool = True
    current_state: str = STATE_HOME

    # 執行計數
    run_count: int = 0
    success_count: int = 0
    max_runs: int = 30

    # 每輪探索追蹤變數
    current_floor: int = 0
    current_money: int = 0
    shop_visit_count: int = 0
    shop_refresh_count: int = 0
    target_notes: dict = field(default_factory=dict)
    current_notes: dict = field(default_factory=dict)
    pending_card_count: int | None = None
    # 本輪累計 reroll（重抽潛能卡）執行次數。reroll 走 Q 鍵 → 無 state 變、無 click，
    # 外部 watchdog 拿不到 OCR 文字集合 → 原本看不到連抽的原地推進 → 30s 誤判 stuck
    # （session 20260613_225241）。納入 progress_counters 後，每次 reroll = 業務計數
    # 變化 = 進度，watchdog 不再誤殺；連抽上限由 decision engine（max_reroll）+ 達上限
    # fallback 取卡兜底，極端情況由 --max-duration 硬上限收尾。
    reroll_count: int = 0
    pending_shop_card_level: int | None = None
    pending_shop_card_text: str | None = None
    pending_shop_card_slot_key: str | None = None
    shop_purchased_slots: set[str] = field(default_factory=set)
    card_counter_enabled: bool = False
    card_counter_initial_total: int = 0
    card_counter_target_total: int = 0
    card_counter_current_total: int = 0
    # 測試模式旗標：True 時 handle_shop 走「買全部可買商品（特飲 + 音符）各一次、
    # 去重、買完離開」的隔離路徑（_handle_shop_buy_all），不動正常經濟邏輯。
    # 等價於 shop_buy_strategy='all'（向後相容保留）。
    shop_buy_all: bool = False
    # 商店買法策略（GAME_MECHANICS C6,config shop.buy.strategy）：
    #   cards_then_notes（預設）/ all / cards_only / notes_only。
    # 由 _build_context 從 config 帶上;states._shop_buy_strategy 優先讀此屬性,
    # 缺則回退讀 config，再缺退 cards_then_notes。
    shop_buy_strategy: str = "cards_then_notes"
    # 「最近一次進商店已買完/沒東西可買」信號（修無限重進空商店迴圈）。
    # buy-all 沒未購商品可買時設 True；handle_shop_choice 見 True 直接選「不要了
    # 直接上樓」（略過免費強化與去商店購物），上樓 verified 成功後重置為 False。
    shop_done: bool = False
    # buy-all 連續「進商店發現沒東西可買」的次數（穩健的重進防護，獨立於 shop_done）。
    # session 20260613_232705：拿完免費強化後 shop_done 信號在交錯流程失效（empirically
    # 讀成 False）→ SHOP_CHOICE 反覆「去商店購物」重進空商店無限迴圈。改由本計數兜底：
    # buy-all 沒貨可買時 +1、找到貨可買時歸 0；SHOP_CHOICE 見 >=1 直接上樓。上樓 verified
    # 或新一輪（reset_round）歸 0。
    shop_emptied_streak: int = 0

    # 結算畫面（STATE_RESULT,Phase 2.3）每輪暫存：評分/角色潛能總等級讀值 + 處理旗標
    # （_result_accounted=本輪是否已計數、_result_keep=達標決策、_result_unlock_done=丟棄前
    # 是否已解鎖）。每輪起點（reset_round / handle_lobby）重置,避免跨輪沿用上一輪決策。
    result_rating: int = 0
    result_potential_total: int = 0
    _result_accounted: bool = False
    _result_keep: bool = True
    _result_unlock_done: bool = False
    # 該輪結算決策已下、但整輪(含儲存/丟棄)尚未跑完回大廳 → 待 handle_lobby 計入 run_count。
    _result_outcome_pending: bool = False

    # 例外處理
    reconnect_attempts: int = 0
    max_reconnect_attempts: int = 5
    # 不封頂的累計點擊數（click_trace 受 deque maxlen=20 封頂，len() 在
    # 20 之後失真 —— 20260612_211534 的 click_count 凍結 20 即此故；
    # 外部 watchdog 的進度判定改用本欄位）。
    total_click_count: int = 0
    click_trace: deque = field(default_factory=lambda: deque(maxlen=20))
    ocr_trace: deque = field(default_factory=lambda: deque(maxlen=20))
    state_trace: deque = field(default_factory=lambda: deque(maxlen=20))
    failure_reason: str | None = None
    failure_dir: str | None = None
    preflight_frame: np.ndarray = field(default=None)
    preflight_detected_state: str | None = None
    session_id: str | None = None
    session_run_dir: str | None = None

    def record_click(self, source: str, x: int, y: int, **details) -> None:
        self.total_click_count += 1
        self.click_trace.append({
            "timestamp": time.time(),
            "source": source,
            "x": int(x),
            "y": int(y),
            **details,
        })

    def record_ocr_hit(self, purpose: str, matched_text: str, bbox=None, **details) -> None:
        serializable_bbox = None
        if bbox is not None:
            serializable_bbox = [[int(p[0]), int(p[1])] for p in bbox]
        self.ocr_trace.append({
            "timestamp": time.time(),
            "purpose": purpose,
            "matched_text": matched_text,
            "bbox": serializable_bbox,
            **details,
        })

    def record_state_transition(self, source: str, previous: str, current: str, **details) -> None:
        self.state_trace.append({
            "timestamp": time.time(),
            "source": source,
            "previous": previous,
            "current": current,
            **details,
        })

    def reset_round(self) -> None:
        """重置每輪探索的臨時追蹤變數（進入 STATE_HOME 時呼叫）。"""
        self.current_floor = 0
        self.current_money = 0
        self.shop_visit_count = 0
        self.shop_refresh_count = 0
        self.target_notes = {}
        self.current_notes = {}
        self.pending_card_count = None
        self.reroll_count = 0
        self.pending_shop_card_level = None
        self.pending_shop_card_text = None
        self.pending_shop_card_slot_key = None
        self.shop_purchased_slots.clear()
        self.shop_done = False
        self.shop_emptied_streak = 0
        self.result_rating = 0
        self.result_potential_total = 0
        self._result_accounted = False
        self._result_keep = True
        self._result_unlock_done = False
        self._result_outcome_pending = False
        self.card_counter_current_total = self.card_counter_initial_total
        self.engine.reset_state()
        logger.info("[Context] 🔄 新一輪探索開始，狀態已重置。")

    def required_potentials_satisfied(self) -> bool:
        """
        判斷所有 required 與 level_required 潛能是否已達成累計目標。

        Returns:
            True 若所有必選潛能均已達標
        """
        if getattr(self.engine, "_mode", "legacy") == "recommendation_badge":
            if not self.card_counter_enabled:
                return True
            return self.card_counter_current_total >= self.card_counter_target_total

        acc = self.engine.state.accumulated_levels
        REQUIRED_MAX = DecisionEngine.REQUIRED_TARGET_LEVEL

        for name in self.engine._required:
            if acc.get(name, 0) < REQUIRED_MAX:
                return False

        for name, target in self.engine._level_required.items():
            if acc.get(name, 0) < target:
                return False

        return True

    def current_notes_satisfied(self) -> bool:
        """
        判斷當前音符是否滿足所有協奏技能的觸發條件。

        Returns:
            True 若 current_notes[k] >= target_notes[k] 對所有 k 成立
        """
        if not self.target_notes:
            # 尚未讀取目標（STATE_PREPARE 未完成），視為不滿足
            return False
        for note_name, required_qty in self.target_notes.items():
            if self.current_notes.get(note_name, 0) < required_qty:
                return False
        return True


# ─────────────────────────────────────────────────────────────
# StateMachine — 主迴圈控制器
# ─────────────────────────────────────────────────────────────

class StateMachine:
    """
    有限狀態機主控制器。

    負責：
      1. 每隔 poll_interval 秒截圖一次
      2. 呼叫 vision 模組辨識當前狀態（骨架版跳過，直接使用 ctx.current_state）
      3. 派發給對應的 handle_* 函式
      4. 根據回傳值更新狀態
      5. 在達到終止條件時安全退出
    """

    # 狀態 → 處理函式 的路由表
    STATE_HANDLERS: dict[str, Callable] = {
        STATE_HOME:             states.handle_home,
        STATE_LOBBY:            states.handle_lobby,
        STATE_FORMATION:        states.handle_formation,
        STATE_PREPARE:          states.handle_prepare,
        STATE_FAST_BATTLE:      states.handle_fast_battle,
        STATE_TAP_CONTINUE:     states.handle_tap_continue,
        STATE_NOTE_ACQUIRED:    states.handle_note_acquired,
        STATE_POTENTIAL_SELECT: states.handle_potential_select,
        STATE_EVENT:            states.handle_event,
        STATE_SHOP_CHOICE:      states.handle_shop_choice,
        STATE_SHOP:             states.handle_shop,
        STATE_LEAVE_TOWER_CONFIRM: states.handle_leave_tower_confirm,
        STATE_DISCARD_CONFIRM:  states.handle_discard_confirm,
        STATE_EXPLORE_COMPLETE: states.handle_explore_complete,
        STATE_RESULT:           states.handle_result,
        STATE_SETTLEMENT:       states.handle_settlement,
        STATE_RECONNECT:        states.handle_reconnect,
    }

    def __init__(self, config_path: str = "config.yaml"):
        self._config_path = config_path
        self.ctx = self._build_context()
        self._poll_interval: float = self.ctx.config.get("bot", {}).get("poll_interval", 2.0)
        self._failure_finalized = False
        # Phase 1.4：內建卡死偵測（取代 NO_PROGRESS_STATES）
        self._stuck_detector = progress.StuckDetector(
            progress.stuck_config_from(self.ctx.config)
        )
        self._register_signal_handlers()
        logger.info(
            f"[FSM] 狀態機初始化完成。"
            f"最大執行次數={self.ctx.max_runs}，"
            f"輪詢間隔={self._poll_interval}s"
        )

    def _build_context(self) -> BotContext:
        """從 config.yaml 建立 BotContext，初始化硬體層與辨識器。"""
        with open(self._config_path, 'r', encoding='utf-8') as f:
            cfg = yaml.safe_load(f)

        engine = DecisionEngine(config_path=self._config_path)
        run_cfg  = cfg.get("run", {})
        win_cfg  = cfg.get("window", {})
        bot_cfg  = cfg.get("bot", {})
        ocr_cfg  = cfg.get("ocr", {})
        input_cfg = cfg.get("input", {})
        vision_cfg = cfg.get("vision", {}) or {}
        card_counter_cfg = cfg.get("card_counter", {})
        shop_cfg = cfg.get("shop", {}) or {}
        shop_buy_cfg = shop_cfg.get("buy", {}) if isinstance(shop_cfg, dict) else {}
        shop_buy_strategy = str((shop_buy_cfg or {}).get("strategy", "cards_then_notes") or "cards_then_notes")

        window_name = win_cfg.get("name", "StellaSora")
        wm    = WindowManager(window_name=window_name)
        inp   = InputSimulator(
            window_name=window_name,
            mode=input_cfg.get("mode", "foreground"),
        )

        # 嘗試鎖定遊戲視窗（失敗時僅警告，不中斷啟動）
        try:
            wm.find_window()
            inp.attach()
            logger.info(f"[FSM] 已鎖定遊戲視窗「{window_name}」")
        except Exception as e:
            logger.warning(f"[FSM] 未找到遊戲視窗（{e}），稍後重試。")

        # 初始化 OCR 引擎、狀態辨識器與模板比對器
        ocr_instance: OcrEngine = None
        try:
            ocr_instance = OcrEngine(
                languages=ocr_cfg.get("languages", ["ch_tra", "en"]),
                gpu=ocr_cfg.get("gpu", False),
            )
            detector_mode = str(vision_cfg.get("detector", "v2"))
            # Phase 1.4：detector 的 OCR 經 RecordingOcr 代理，主迴圈據此
            # 取得每拍 OCR 文字集合做進度判定（零額外 OCR 成本）。
            # ctx.ocr 仍持原引擎，handler 不受影響。
            self._ocr_recorder = progress.RecordingOcr(ocr_instance)
            self._detector = StateDetector(ocr_engine=self._ocr_recorder, mode=detector_mode)
            logger.info(
                f"[FSM] OCR 引擎與 StateDetector 初始化完成（mode={self._detector.mode}）。"
            )
        except Exception as e:
            logger.warning(f"[FSM] OCR 引擎初始化失敗（{e}），狀態辨識停用。")
            self._detector = None
            self._ocr_recorder = None

        template_dir = str(Path(self._config_path).parent / "assets" / "templates")
        try:
            matcher = TemplateMatcher(template_dir=template_dir)
        except Exception as e:
            logger.warning(f"[FSM] TemplateMatcher 初始化失敗（{e}），模板比對停用。")
            matcher = None

        # Phase 3③ 子項3：handler 面 OCR 快取（同拍同 frame 多次 read_text 復用，
        # 省重複 EasyOCR）。**只**換 handler 鏈（ctx.ocr）；detector 鏈
        # （self._ocr_recorder = RecordingOcr，generation 是 StuckDetector 進度
        # 心跳）維持包原始 ocr_instance 不變 —— 兩條鏈嚴禁交叉，被快取凍結會讓
        # L3 兜底瞎掉。enabled:false（預設）→ handler_ocr 就是原 ocr_instance，
        # 接線等同沒改（逐位元舊行為）。
        ocr_cache_cfg = (cfg.get("bot", {}) or {}).get("ocr_cache", {}) or {}
        if bool(ocr_cache_cfg.get("enabled", False)) and ocr_instance is not None:
            handler_ocr = progress.CachingOcr(
                ocr_instance,
                max_entries=int(ocr_cache_cfg.get("max_entries", 8) or 8),
            )
            logger.info("[FSM] handler 面 OCR 快取已啟用（detector 鏈不受影響）。")
        else:
            handler_ocr = ocr_instance

        ctx = BotContext(
            engine=engine,
            config=cfg,
            wm=wm,
            input=inp,
            ocr=handler_ocr,
            matcher=matcher,
            max_runs=int(run_cfg.get("max_runs", 30)),
            card_counter_enabled=bool(card_counter_cfg.get("enabled", False)),
            card_counter_initial_total=int(card_counter_cfg.get("initial_total", 0) or 0),
            card_counter_target_total=int(card_counter_cfg.get("target_total", 0) or 0),
            card_counter_current_total=int(card_counter_cfg.get("initial_total", 0) or 0),
            shop_buy_strategy=shop_buy_strategy,
        )
        return ctx

    def _register_signal_handlers(self) -> None:
        """註冊 Ctrl-C / SIGTERM 的安全退出處理。"""
        def _shutdown(sig, frame):
            logger.warning("[FSM] 收到終止信號，正在安全退出...")
            self.ctx.running = False
        try:
            import signal
            signal.signal(signal.SIGINT, _shutdown)
            signal.signal(signal.SIGTERM, _shutdown)
        except ValueError:
            # 當於非主執行緒 (如 GUI 的 QThread) 啟動時會拋出此錯誤，忽略即可，由主程式處理關閉
            pass

    def _detect_state(self) -> DetectionResult:
        """
        截圖並透過 StateDetector OCR 辨識當前遊戲狀態。

        截圖優先使用 PrintWindow 背景截圖，失敗時降級為 mss 前台截圖。
        截圖結果儲存至 ctx.last_frame 供各 handler 使用，避免重複截圖。

        Returns:
            DetectionResult: 判定結果（state / confidence / evidence）。
              - 截圖失敗或辨識器不可用 → 保持當前狀態（confidence 0.0）。
              - v2 模式無 signature 命中 → STATE_UNKNOWN（主迴圈走 UNKNOWN 分支）。
        """
        # 1. 嘗試截圖（視窗未找到時直接重試）
        try:
            frame, method = self.ctx.wm.capture()
            self.ctx.last_frame = frame
            # 解析度單一咽喉:每拍截圖後即由當前 frame 動態推導 frame_w/h,
            # 絕不沿用寫死的預設值(否則非 1920×1080 解析度時 handler 會以錯誤
            # 比例切 ROI/算點擊座標 → 越界錯位卡死)。所有 ROI 皆比例制(_px)。
            self.ctx.frame_h, self.ctx.frame_w = frame.shape[:2]
            logger.debug(f"[FSM] 截圖成功 ({method}) shape={frame.shape}")
        except Exception as e:
            logger.warning(f"[FSM] 截圖失敗: {e}，保持當前狀態。")
            if self.ctx.last_frame is None:
                self.finalize_failure("capture_failed_without_valid_frame", {"error": str(e)})
            return DetectionResult(
                state=self.ctx.current_state, confidence=0.0, evidence=("capture_failed",)
            )

        # 2. 若 OCR 辨識器可用，交由 StateDetector 判斷；否則保持當前狀態
        if self._detector is not None:
            result = self._detector.detect(frame, self.ctx.current_state)
            self.ctx.record_state_transition(
                source="detector",
                previous=self.ctx.current_state,
                current=result.state,
                method="ocr_state_detector",
                confidence=round(float(result.confidence), 3),
                evidence=list(result.evidence),
            )
            return result

        return DetectionResult(state=self.ctx.current_state, confidence=0.0, evidence=())

    def _check_stop_conditions(self) -> bool:
        """
        檢查是否應停止主迴圈。

        Returns:
            True → 應停止
        """
        if not self.ctx.running:
            logger.info("[FSM] running=False，準備退出。")
            return True

        if self.ctx.run_count >= self.ctx.max_runs:
            logger.info(
                f"[FSM] 達到最大執行次數 {self.ctx.max_runs}，"
                f"成功次數 {self.ctx.success_count}，正常退出。"
            )
            return True

        # 動態終止條件：達標次數或等級（由 config 控制，Phase 3 完善）
        stop_level = self.ctx.config.get("run", {}).get("stop_on_target_level", 0)
        if stop_level > 0:
            # TODO (Phase 3): OCR 讀取當前等級並比對
            pass

        return False

    def _interruptible_sleep(self, duration: float) -> None:
        """支援被 self.ctx.running 中斷的睡眠函式"""
        end_time = time.time() + duration
        while time.time() < end_time and self.ctx.running:
            time.sleep(0.1)

    def run(self) -> None:
        """
        主迴圈。

        每次迭代：
          1. 檢查終止條件
          2. 辨識當前狀態
          3. 執行對應 handler
          4. 更新 current_state
          5. 等待 poll_interval 秒
        """
        logger.info("[FSM] ▶ 主迴圈啟動！")
        print("=" * 55)
        print("  星塔旅人自動化 Bot 啟動")
        print("  按 Ctrl+C 可安全停止")
        print("=" * 55)

        window_retry_count = 0
        max_window_retries = 10
        self._last_active_time = time.time()
        unknown_streak = 0
        # Phase 1.4 內建卡死偵測（手工組裝的測試機器無 __init__，這裡補建）
        stuck_detector = getattr(self, "_stuck_detector", None)
        if stuck_detector is None:
            stuck_detector = progress.StuckDetector(
                progress.stuck_config_from(self.ctx.config)
            )
            self._stuck_detector = stuck_detector
        ocr_recorder = getattr(self, "_ocr_recorder", None)
        # 同步起始 generation：preflight 的 OCR 結果不屬於本迴圈任何一拍
        last_ocr_generation = getattr(ocr_recorder, "generation", 0) if ocr_recorder else 0
        try:
            max_unknown_streak = int(
                self.ctx.config.get("bot", {}).get(
                    "max_unknown_streak", DEFAULT_MAX_UNKNOWN_STREAK
                )
            )
        except (TypeError, ValueError):
            max_unknown_streak = DEFAULT_MAX_UNKNOWN_STREAK

        while not self._check_stop_conditions():
            # ── [Watchdog] 異常卡死偵測 (發呆逾30秒且非休息狀態) ──
            if time.time() - self._last_active_time > 30.0:
                if self.ctx.current_state not in (STATE_HOME, STATE_LOBBY):
                    logger.error(f"[Watchdog] 嚴重錯誤：在狀態「{self.ctx.current_state}」發呆超過 30 秒，發生卡死！啟動 Crash Dump...")
                    self.finalize_failure("watchdog_timeout")
                    break

            # ── 0. 防呆：檢查與重試遊戲視窗 ──────────────────
            if not self.ctx.wm.hwnd:
                window_retry_count += 1
                logger.warning(f"[FSM] 遊戲視窗尚未鎖定，嘗試重試 ({window_retry_count}/{max_window_retries})...")
                try:
                    self.ctx.wm.find_window()
                    self.ctx.input.attach()
                    logger.info("[FSM] 成功重新鎖定遊戲視窗！")
                    window_retry_count = 0
                except Exception as e:
                    if window_retry_count >= max_window_retries:
                        logger.error("[FSM] 無法找到遊戲視窗，達到最大重試次數，優雅退出。")
                        self.finalize_failure(
                            "window_lock_retry_exhausted",
                            {"error": str(e), "retry_count": window_retry_count},
                        )
                        break
                    # 若仍未找到，釋放 CPU 並重試
                    self._interruptible_sleep(self._poll_interval)
                    continue

            # 額外確認視窗控制代碼是否仍有效 (遊戲可能中途被關閉)
            try:
                import win32gui
                if self.ctx.wm.hwnd and not win32gui.IsWindow(self.ctx.wm.hwnd):
                    self.ctx.wm.hwnd = None
                    logger.warning("[FSM] 視窗控制代碼已失效 (遊戲可能被關閉)，準備重新鎖定。")
                    self._interruptible_sleep(self._poll_interval)
                    continue
            except ImportError:
                pass

            # ── 1. 辨識當前狀態 ──────────────────────────────
            poll_started_at = time.time()  # 本拍起點（click_trace 新條目以此切分）
            detection = self._detect_state()
            if not self.ctx.running:
                break
            detected = detection.state

            # ── 1.5 STATE_UNKNOWN 分支（Phase 1.2，解 R2）────
            # 無法辨識畫面時不執行任何 handler、不點擊：
            #   連續 1 次 → 重拍重判（下一輪輪詢自然重新截圖）
            #   連續 2 次起 → _settle_and_refresh 後重判
            #   連續 max_unknown_streak 次 → finalize_failure 留 bundle
            if detected == STATE_UNKNOWN:
                unknown_streak += 1
                logger.warning(
                    f"[FSM] 畫面無法辨識（UNKNOWN 連續 {unknown_streak}/"
                    f"{max_unknown_streak} 次），不執行 handler。"
                )
                if unknown_streak >= max_unknown_streak:
                    self.finalize_failure(
                        "state_unknown_persistent",
                        {
                            "streak": unknown_streak,
                            "last_known_state": self.ctx.current_state,
                            "last_evidence": list(detection.evidence),
                        },
                    )
                    break
                if unknown_streak >= 2:
                    states._settle_and_refresh(self.ctx)
                self._interruptible_sleep(self._poll_interval)
                continue
            unknown_streak = 0

            # ── 1.6 取本拍 OCR 文字（Phase 1.4 進度判定用）────
            # RecordingOcr 在 _detect_state 期間記錄 detector 的全畫面 OCR；
            # generation 未前進（截圖失敗 / detector 停用）→ None（訊號靜默）。
            poll_texts = None
            if ocr_recorder is not None and getattr(ocr_recorder, "generation", 0) != last_ocr_generation:
                last_ocr_generation = ocr_recorder.generation
                poll_texts = ocr_recorder.last_texts

            if detected != self.ctx.current_state:
                logger.info(
                    f"[FSM] 狀態切換：{self.ctx.current_state} → {detected}"
                )
                self.ctx.current_state = detected
                self._last_active_time = time.time()

            current = self.ctx.current_state

            # 【防呆】如果截圖完全失敗，避免傳入 None 導致 handler 當機
            if self.ctx.last_frame is None:
                logger.warning("[FSM] 尚未取得有效截圖，跳過本次處理。")
                self._interruptible_sleep(self._poll_interval)
                continue

            handler = self.STATE_HANDLERS.get(current)

            if handler is None:
                logger.error(f"[FSM] 未知狀態「{current}」，強制回到 STATE_HOME。")
                self.ctx.current_state = STATE_HOME
                self._interruptible_sleep(self._poll_interval)
                continue

            # 每一輪從 HOME 開始時重置追蹤變數
            if current == STATE_HOME and self.ctx.run_count > 0:
                # 只在「不是第一次進 HOME」時重置（避免啟動時誤重置）
                if not hasattr(self, "_last_reset_at") or self._last_reset_at != self.ctx.run_count:
                    self.ctx.reset_round()
                    self._last_reset_at = self.ctx.run_count

            # ── 2. 執行 handler ──────────────────────────────
            logger.debug(f"[FSM] 執行 {current} handler")
            try:
                next_state = handler(self.ctx)
                # 動作成功執行完畢，若並非閒置等待，微幅更新活躍時間避免超時
                if current not in (STATE_HOME, STATE_LOBBY):
                    self._last_active_time = time.time()
            except Exception as e:
                logger.exception(f"[FSM] {current} handler 發生例外：{e}")
                self.finalize_failure(
                    "handler_exception",
                    {
                        "state": current,
                        "error": str(e),
                    },
                )
                break

            # ── 3. 更新狀態 ──────────────────────────────────
            if (
                next_state is not None
                and next_state != self.ctx.current_state
                and current not in DETECTOR_GATED_TRANSITIONS
            ):
                previous = self.ctx.current_state
                logger.info(
                    f"[FSM] {previous} → {next_state}"
                )
                self.ctx.current_state = next_state
                self.ctx.record_state_transition(
                    source="handler",
                    previous=previous,
                    current=next_state,
                )
                self._last_active_time = time.time()

            # ── 3.5 內建卡死偵測（Phase 1.4，解 R4）──────────
            # 同一狀態連續 K 次輪詢無實質進度（狀態改變 / 業務計數變化 /
            # click_verified 成功 / OCR 文字集合實質變化，任一成立即重置）
            # → finalize。純畫面動畫（roi/frame hash 變化）不算進度。
            new_click_entries = [
                entry for entry in self.ctx.click_trace
                if float(entry.get("timestamp", 0.0) or 0.0) >= poll_started_at
            ]
            is_stuck = stuck_detector.observe(
                state=self.ctx.current_state,
                counters=progress.progress_counters(self.ctx),
                texts=poll_texts,
                click_entries=new_click_entries,
            )
            if stuck_detector.streak >= 2:  # 單拍等待（動畫 settle）很常見，不刷警告
                logger.warning(
                    f"[FSM] 無實質進度（{stuck_detector.streak}/"
                    f"{stuck_detector.config.poll_limit}）in {self.ctx.current_state}"
                )
            if is_stuck:
                logger.error(
                    f"[FSM] 卡死：{self.ctx.current_state} 連續 "
                    f"{stuck_detector.streak} 次輪詢無實質進度，啟動 Crash Dump。"
                )
                self.finalize_failure(
                    "state_stuck_no_progress",
                    {
                        "state": self.ctx.current_state,
                        "streak": stuck_detector.streak,
                        "poll_limit": stuck_detector.config.poll_limit,
                        "progress_signals": (
                            "state_change/business_counters/verified_click/"
                            "ocr_text_set_jaccard"
                        ),
                    },
                )
                break

            # ── 4. 等待下次輪詢 ──────────────────────────────
            self._interruptible_sleep(self._poll_interval)

        self._shutdown_report()

    def set_session_metadata(
        self,
        session_id: str,
        session_run_dir: str | None = None,
        preflight_frame=None,
        preflight_detected_state: str | None = None,
    ) -> None:
        self.ctx.session_id = session_id
        self.ctx.session_run_dir = session_run_dir
        self.ctx.preflight_frame = preflight_frame
        self.ctx.preflight_detected_state = preflight_detected_state

    def _build_summary(self, reason: str, extra: dict | None = None) -> dict:
        return {
            "session_id": self.ctx.session_id,
            "reason": reason,
            "current_state": self.ctx.current_state,
            "run_count": self.ctx.run_count,
            "max_runs": self.ctx.max_runs,
            "success_count": self.ctx.success_count,
            "current_floor": self.ctx.current_floor,
            "current_money": self.ctx.current_money,
            "total_click_count": int(getattr(self.ctx, "total_click_count", 0) or 0),
            "target_notes": self.ctx.target_notes,
            "current_notes": self.ctx.current_notes,
            "card_counter": {
                "enabled": self.ctx.card_counter_enabled,
                "initial_total": self.ctx.card_counter_initial_total,
                "target_total": self.ctx.card_counter_target_total,
                "current_total": self.ctx.card_counter_current_total,
            },
            "preflight_detected_state": self.ctx.preflight_detected_state,
            "extra": extra or {},
        }

    def finalize_failure(self, reason: str, extra: dict | None = None) -> Path | None:
        if self._failure_finalized:
            return Path(self.ctx.failure_dir) if self.ctx.failure_dir else None

        session_id = self.ctx.session_id or datetime.now().strftime("%Y%m%d_%H%M%S")
        failure_dir = Path("logs") / "session_failures" / session_id
        failure_dir.mkdir(parents=True, exist_ok=True)

        self.ctx.failure_reason = reason
        self.ctx.failure_dir = str(failure_dir)
        self.ctx.running = False
        self._failure_finalized = True

        summary = self._build_summary(reason=reason, extra=extra)
        (failure_dir / "summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (failure_dir / "click_trace.json").write_text(
            json.dumps(list(self.ctx.click_trace), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (failure_dir / "ocr_trace.json").write_text(
            json.dumps(list(self.ctx.ocr_trace), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (failure_dir / "state_trace.json").write_text(
            json.dumps(list(self.ctx.state_trace), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        if self.ctx.last_frame is not None:
            import cv2
            cv2.imwrite(str(failure_dir / "last_frame.png"), self.ctx.last_frame)

        if self.ctx.preflight_frame is not None:
            import cv2
            cv2.imwrite(str(failure_dir / "preflight_frame.png"), self.ctx.preflight_frame)

        bot_log = Path("bot.log")
        if bot_log.exists():
            shutil.copy2(bot_log, failure_dir / "bot.log")

        logger.error(f"[FSM] failure finalized: {reason} -> {failure_dir}")
        return failure_dir

    def write_session_summary(self, reason: str = "completed") -> Path:
        session_id = self.ctx.session_id or datetime.now().strftime("%Y%m%d_%H%M%S")
        run_dir = Path(self.ctx.session_run_dir or (Path("logs") / "session_runs" / session_id))
        run_dir.mkdir(parents=True, exist_ok=True)
        summary = self._build_summary(reason=reason)
        (run_dir / "summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return run_dir / "summary.json"

    def _shutdown_report(self) -> None:
        """停止時輸出執行摘要。"""
        print("\n" + "=" * 55)
        print("  Bot 已停止")
        print(f"  總執行次數：{self.ctx.run_count}")
        print(f"  成功儲存次數：{self.ctx.success_count}")
        print("=" * 55)
        logger.info(
            f"[FSM] 執行結束。runs={self.ctx.run_count}, "
            f"successes={self.ctx.success_count}"
        )
