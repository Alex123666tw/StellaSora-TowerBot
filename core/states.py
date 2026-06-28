from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING

try:
    import yaml as _yaml_mod
    _YAML_AVAILABLE = True
except ImportError:
    _YAML_AVAILABLE = False

from core import actions
from vision import signatures
from vision import note_reader

if TYPE_CHECKING:
    from core.bot import BotContext

logger = logging.getLogger(__name__)

_quiz_db: dict | None = None
_event_rules_cache: list | None = None
_notes_id_cache: dict[str, str] | None = None
_CARD_LEVEL_MIN = 1
_CARD_LEVEL_MAX = 6
# 啟動條件數量讀不到時的預設(語料觀察多為 15)。數量不驅動決策,僅需 need>current。
_DEFAULT_ACTIVATION_COUNT = 15
# 單一祕紋啟動需求的合理上限(觀察 10~15)。OCR 讀到 >此值多為把鄰近 Lv/雜訊讀進來 → 退預設。
_ACTIVATION_COUNT_MAX = 50


def _load_quiz_db() -> dict:
    global _quiz_db
    if _quiz_db is None:
        path = Path(__file__).resolve().parents[1] / 'data' / 'quiz_answers.json'
        if path.exists():
            with open(path, 'r', encoding='utf-8') as f:
                _quiz_db = json.load(f)
        else:
            _quiz_db = {'answers': []}
    return _quiz_db



def _get_event_rules_path() -> Path:
    """回傳 data/event_rules.yaml 的絕對路徑(以本檔為錨點)。

    測試可 patch 此函數替換路徑,不影響生產行為。
    """
    return Path(__file__).resolve().parents[1] / 'data' / 'event_rules.yaml'


def _load_event_rules() -> list:
    """載入 data/event_rules.yaml 的 overrides 清單。

    仿 _load_quiz_db:
    - 找不到檔案 → 空 list。
    - yaml 格式錯 / overrides 非 list / 缺 overrides key → 空 list。
    - 絕不拋例外。
    使用模組級快取(_event_rules_cache),重載模組即清除。
    """
    global _event_rules_cache
    if _event_rules_cache is not None:
        return _event_rules_cache
    path = _get_event_rules_path()
    if not path.exists():
        _event_rules_cache = []
        return _event_rules_cache
    if not _YAML_AVAILABLE:
        logger.warning('event_rules: pyyaml 未安裝,event_rules.yaml 將被忽略')
        _event_rules_cache = []
        return _event_rules_cache
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = _yaml_mod.safe_load(f)
        overrides = (data or {}).get('overrides', []) if isinstance(data, dict) else []
        if not isinstance(overrides, list):
            overrides = []
        _event_rules_cache = overrides
    except Exception:
        logger.warning('event_rules: 讀取 %s 失敗,使用空規則', path, exc_info=True)
        _event_rules_cache = []
    return _event_rules_cache


def _has_discount_keyword(text: str) -> bool:
    # 提示字單一來源(鐵則2):優惠/折扣字定義於 signatures.SHOP_DISCOUNT_TOKENS。
    return signatures.text_has_any(text, signatures.SHOP_DISCOUNT_TOKENS)



def _should_enter_shop(current_money: int) -> bool:
    """是否點「去商店購物」進商店。

    Phase 2.6 移除 upgrade_price 死參數;Phase 2.2 移除 current_floor 終層死分支
    （原 `if _is_last_shop_floor(current_floor): return True`）。current_floor 從未被 OCR 更新、
    恆 0 → _is_last_shop_floor 恆 False → 該分支從不觸發（移除不改線上行為）；且終層收尾本就
    不靠樓層數，而靠 SHOP_CHOICE「離開星塔」+ STATE_RESULT 簽名（L3 完整一輪實證,樓層數非必要,
    REPAIR_PLAN 2.2「先刪後補」之「刪」）。current_floor 欄位保留作監控/GUI 顯示佔位（恆 0,
    樓層 OCR 讀取未實作）。

    現行進店把關:
      - 餘額 > 0 才進（餘額 0 = 破產,連最便宜商品都買不起,進店也空手 → 直接上樓更省）;
      - 店內「買得起才買」由 handle_shop 的 affordability 過濾把關,「買完不重進」由
        handle_shop_choice 的 shopped(shop_done/emptied_streak)信號把關。
    """
    return current_money > 0


# 升級機（強化）次數的預設值（無 config 時的 fallback）：第 1 次遇商店強化 2 次、
# 第 2 次強化 3 次、其餘 0。可由 config shop.upgrade.times_by_visit 覆寫（C2 可調）。
_DEFAULT_UPGRADE_TIMES_BY_VISIT: dict[int, int] = {1: 2, 2: 3}


def _shop_cfg(ctx: 'BotContext') -> dict:
    """取 config 的 shop 區塊（缺則回空 dict）。"""
    cfg = getattr(ctx, 'config', None) or {}
    shop = cfg.get('shop') if isinstance(cfg, dict) else None
    return shop if isinstance(shop, dict) else {}


def _event_cfg(ctx: 'BotContext') -> dict:
    """取 config 的 event 區塊（缺則回空 dict）。仿 _shop_cfg 三層防呆。"""
    cfg = getattr(ctx, 'config', None) or {}
    event = cfg.get('event') if isinstance(cfg, dict) else None
    return event if isinstance(event, dict) else {}


def _prepare_cfg(ctx: 'BotContext') -> dict:
    """取 config 的 prepare 區塊（缺則回空 dict）。仿 _shop_cfg 三層防呆。

    目前無生產呼叫端（純骨架，供後續 prepare 相關 config 項使用）。
    """
    cfg = getattr(ctx, 'config', None) or {}
    prepare = cfg.get('prepare') if isinstance(cfg, dict) else None
    return prepare if isinstance(prepare, dict) else {}


def _shop_upgrade_times(ctx: 'BotContext', shop_visit_count: int) -> int:
    """第 N 次遇商店要強化幾次（C2,config 可調）。

    讀 config shop.upgrade：enabled=False → 一律 0;否則查 times_by_visit（key 為
    造訪次數,可為 int 或字串）,缺設定退回 {1:2, 2:3} 預設,未列出的次數 = 0。
    """
    upgrade_cfg = _shop_cfg(ctx).get('upgrade', {}) or {}
    if isinstance(upgrade_cfg, dict) and upgrade_cfg.get('enabled', True) is False:
        return 0
    times_by_visit = None
    if isinstance(upgrade_cfg, dict):
        times_by_visit = upgrade_cfg.get('times_by_visit')
    if not isinstance(times_by_visit, dict) or not times_by_visit:
        times_by_visit = _DEFAULT_UPGRADE_TIMES_BY_VISIT
    # YAML 的 key 可能是 int 或字串,兩種都比對。
    for key in (shop_visit_count, str(shop_visit_count)):
        if key in times_by_visit:
            try:
                return int(times_by_visit[key])
            except (TypeError, ValueError):
                return 0
    return 0


def _shop_order(ctx: 'BotContext', visit_count: int | None = None) -> str:
    """商店三選一的優先順序：upgrade_first（預設,先強化）/ shop_first（先進商店再強化）。

    order_by_visit（GUI_DESIGN_SPEC §3.4）：先查該次造訪的指定順序（key=造訪次數,int
    或字串都比對,比照 _shop_upgrade_times）;缺 / 空 / visit_count=None / 值無效 → 退全域
    shop.order。預設 order_by_visit 空 → 全退全域 → byte-identical 現行。
    """
    shop_cfg = _shop_cfg(ctx)
    if visit_count is not None:
        by_visit = shop_cfg.get('order_by_visit')
        if isinstance(by_visit, dict) and by_visit:
            for key in (visit_count, str(visit_count)):
                if key in by_visit:
                    val = str(by_visit[key] or '').strip()
                    if val in ('upgrade_first', 'shop_first'):
                        return val
                    break  # 命中但值無效 → 退全域（不再試另一種 key 型別）
    order = str(shop_cfg.get('order', 'upgrade_first') or 'upgrade_first').strip()
    return order if order in ('upgrade_first', 'shop_first') else 'upgrade_first'


def _shop_buy_strategy(ctx: 'BotContext') -> str:
    """商店買法策略（C6 真經濟,config shop.buy.strategy 可調）。

    cards_then_notes（預設,使用者拍板）/ all / cards_only / notes_only。
    優先吃 ctx.shop_buy_strategy（由 StateMachine 從 config 帶上）;缺則讀 config;
    再缺退回預設。舊測試的 ctx 可能無此屬性 → 預設 cards_then_notes（= 現行真經濟）。
    """
    valid = ('cards_then_notes', 'all', 'cards_only', 'notes_only')
    strategy = getattr(ctx, 'shop_buy_strategy', None)
    if not strategy:
        buy_cfg = _shop_cfg(ctx).get('buy', {}) or {}
        if isinstance(buy_cfg, dict):
            strategy = buy_cfg.get('strategy')
    strategy = str(strategy or 'cards_then_notes').strip()
    return strategy if strategy in valid else 'cards_then_notes'


def _parse_upgrade_price(text: str) -> int | None:
    """從「強化」選項 OCR 文字解析真實價格（C3 接真實價）。

    回傳：免費（命中「免費/免费」）→ 0;含數字 → 該數字（強化價,如「強化 (120C)」→120）;
    無價格資訊（純「強化」）→ None（呼叫端視為未知,照常強化,保守不漏強化）。
    """
    if not text:
        return None
    if signatures.text_has_any(text, signatures.SHOP_UPGRADE_FREE_TOKENS):
        return 0
    m = re.search(r'(\d[\d,]*)', text)
    if m:
        return int(m.group(1).replace(',', ''))
    return None



def _compute_note_gaps(target_notes: dict[str, int], current_notes: dict[str, int]) -> dict[str, int]:
    return {
        note: need - current_notes.get(note, 0)
        for note, need in target_notes.items()
        if need > current_notes.get(note, 0)
    }


def _decision_mode(ctx: 'BotContext') -> str:
    return str(getattr(getattr(ctx, 'engine', None), '_mode', '') or '').strip()


def _card_counter_needs_cards(ctx: 'BotContext') -> bool:
    if not getattr(ctx, 'card_counter_enabled', False):
        return False
    target = int(getattr(ctx, 'card_counter_target_total', 0) or 0)
    current = int(getattr(ctx, 'card_counter_current_total', 0) or 0)
    return target > 0 and current < target


def _card_target_met(ctx: 'BotContext') -> bool:
    """卡片總等級是否已達 target（target=0 視同已達:無計數需求,直接進音符階段）。"""
    target = int(getattr(ctx, 'card_counter_target_total', 0) or 0)
    if target <= 0:
        return True
    current = int(getattr(ctx, 'card_counter_current_total', 0) or 0)
    return current >= target


def _strategy_wants_cards(ctx: 'BotContext') -> bool:
    """真經濟:依 strategy 判斷此刻是否該買卡片（特飲）。

    cards_then_notes / cards_only:卡片總等級 current < target（target>0）才買卡片;
    達標（或 target=0 無計數需求）即停買卡片。all / notes_only:不在此路徑買卡片。
    不再被 card_counter_enabled gate 掉（gate 改由 strategy + 計數切換）。
    """
    strategy = _shop_buy_strategy(ctx)
    if strategy not in ('cards_then_notes', 'cards_only'):
        return False
    target = int(getattr(ctx, 'card_counter_target_total', 0) or 0)
    current = int(getattr(ctx, 'card_counter_current_total', 0) or 0)
    return target > 0 and current < target


def _strategy_wants_notes(ctx: 'BotContext') -> bool:
    """真經濟:依 strategy 判斷此刻是否該買協奏缺口音符。

    cards_then_notes:卡片達標後（_card_target_met）才買音符;notes_only:一律買音符;
    cards_only:永不買音符;all:不在此路徑買音符。
    """
    strategy = _shop_buy_strategy(ctx)
    if strategy == 'notes_only':
        return True
    if strategy == 'cards_then_notes':
        return _card_target_met(ctx)
    return False


def _increment_card_counter(ctx: 'BotContext', level: int, source: str, **details) -> None:
    if not getattr(ctx, 'card_counter_enabled', False):
        return
    gain = max(0, int(level or 0))
    if gain <= 0:
        return
    before = int(getattr(ctx, 'card_counter_current_total', 0) or 0)
    ctx.card_counter_current_total = before + gain
    logger.info(
        '[CardCounter] %s +%s -> %s/%s',
        source,
        gain,
        ctx.card_counter_current_total,
        getattr(ctx, 'card_counter_target_total', 0),
    )
    trace_text = str(details.pop('matched_text', source))
    _record_ocr_trace(
        ctx,
        purpose='card_counter',
        matched_text=trace_text,
        level=gain,
        before=before,
        current_total=ctx.card_counter_current_total,
        target_total=getattr(ctx, 'card_counter_target_total', 0),
        **details,
    )



def _px(ctx: 'BotContext', rx: float, ry: float) -> tuple[int, int]:
    return int(ctx.frame_w * rx), int(ctx.frame_h * ry)



def _ocr_center(bbox: tuple) -> tuple[int, int]:
    xs = [p[0] for p in bbox]
    ys = [p[1] for p in bbox]
    return int(sum(xs) / 4), int(sum(ys) / 4)



def _update_frame_size(ctx: 'BotContext') -> None:
    if getattr(ctx, 'last_frame', None) is not None:
        h, w = ctx.last_frame.shape[:2]
        ctx.frame_w = w
        ctx.frame_h = h


def _refresh_frame(ctx: 'BotContext') -> bool:
    wm = getattr(ctx, 'wm', None)
    if wm is None or not hasattr(wm, 'capture'):
        return False
    try:
        frame, _method = wm.capture()
    except Exception:
        return False
    ctx.last_frame = frame
    _update_frame_size(ctx)
    return True


def _adaptive_settle_cfg(ctx: 'BotContext') -> dict | None:
    """讀 bot.adaptive_settle 設定;關閉(或結構不對)回 None 代表走舊行為。

    容錯:ctx 無 config / config 非 dict / 區塊缺失 / enabled 非 True
    一律回 None(關閉訊號)。預設關閉 → 逐位元等同舊 _settle_and_refresh。
    """
    cfg = (getattr(ctx, 'config', None) or {})
    if not isinstance(cfg, dict):
        return None
    bot_cfg = cfg.get('bot', {})
    if not isinstance(bot_cfg, dict):
        return None
    adaptive = bot_cfg.get('adaptive_settle', {})
    if not isinstance(adaptive, dict):
        return None
    if adaptive.get('enabled') is not True:
        return None
    return adaptive


def _settle_adaptive(ctx: 'BotContext', max_delay: float, cfg: dict, roi=None) -> bool:
    """畫面 roi_hash 連續穩定即提早返回、不空等(adaptive settle 啟用路徑)。

    max_delay = 呼叫端原 delay,作為硬上限:畫面持續變動就退化成等值舊行為、
    絕不更慢;畫面提早靜止(連續 stable_count 拍整圖 hash 不變且 elapsed>=min_delay)
    就提早返回 True。回傳語意同舊:True=有成功重拍、False=重拍失敗。
    """
    stable_count = max(1, int(cfg.get('stable_count', 2)))
    sample_interval = max(0.0, float(cfg.get('sample_interval', 0.1)))
    min_delay = max(0.0, float(cfg.get('min_delay', 0.25)))
    min_delay = min(min_delay, max_delay)

    _refresh_frame(ctx)
    start = time.time()
    baseline = None
    stable = 0
    while True:
        h = actions.roi_hash_of(getattr(ctx, 'last_frame', None))
        elapsed = time.time() - start
        if h is None:
            # 算不出 hash(截圖失敗/空畫面)不算穩定,重置累計
            stable = 0
            baseline = None
        elif h == baseline:
            stable += 1
            if stable >= stable_count and elapsed >= min_delay:
                return True
        else:
            baseline = h
            stable = 1
        if elapsed >= max_delay:
            break
        time.sleep(min(sample_interval, max(0.0, max_delay - elapsed)))
        _refresh_frame(ctx)
    # 逾時退場:補最後一拍,回傳語意同舊 _settle_and_refresh
    return _refresh_frame(ctx)


def _settle_and_refresh(ctx: 'BotContext', delay: float = 0.85) -> bool:
    cfg = _adaptive_settle_cfg(ctx)
    if cfg is not None:
        return _settle_adaptive(ctx, max_delay=delay, cfg=cfg, roi=None)
    time.sleep(delay)
    return _refresh_frame(ctx)



def _record_click_trace(ctx: 'BotContext', source: str, x: int, y: int, **details) -> None:
    recorder = getattr(ctx, 'record_click', None)
    if callable(recorder):
        recorder(source=source, x=x, y=y, **details)



def _record_ocr_trace(ctx: 'BotContext', purpose: str, matched_text: str, bbox=None, **details) -> None:
    recorder = getattr(ctx, 'record_ocr_hit', None)
    if callable(recorder):
        recorder(purpose=purpose, matched_text=matched_text, bbox=bbox, **details)



def _safe_blank_point(ctx: 'BotContext', variant: str = 'default') -> tuple[int, int]:
    if variant == 'note':
        return _px(ctx, 0.88, 0.72)
    return _px(ctx, 0.88, 0.78)


def _choice_panel_roi(ctx: 'BotContext') -> tuple[int, int, int, int]:
    # 事件三/四選一選項搜尋 ROI（event-only;商店走 _shop_choice_panel_roi,不受此影響）。
    # y-start 0.30（=216@720p）：要讓最上一列**選項標題**進候選。
    # L3 20260615_183043「花錢買音符」事件第一列選項標題 cy≈245（bbox top y=232），
    # 舊 y-start 0.34（=244）+ exclude_top_ratio=0.10（再切 cy<280）把它切掉 → 第一列只剩
    # [成本,獎勵] → _event_option_groups 把「消耗140C」誤當標題 → _pick_event_click_target
    # 殘列無標題 fallback 點到成本「消耗140C」(1038,287) → 不推進、反覆重點卡死。
    # 0.30 仍遠低於問題標題「旋律…如何影響著你的命運?」cy≈161（由 ROI 上緣排除,不入選項）,
    # 故只放進選項標題、不放進問題標題。呼叫端（_select_event_option）配合改 exclude_top_ratio=0。
    return (
        int(ctx.frame_w * 0.50),
        int(ctx.frame_h * 0.30),
        int(ctx.frame_w * 0.45),
        int(ctx.frame_h * 0.54),
    )


def _shop_choice_panel_roi(ctx: 'BotContext') -> tuple[int, int, int, int]:
    # Phase 2 修：商店三選一專屬的選項搜尋 ROI（比共用的 _choice_panel_roi 更寬、更靠左）。
    # 實機回放（logs/session_failures/20260613_195227 商店三選一）的選項文字左對齊，
    # 中心 cx≈458–551，落在共用 ROI（x-start 0.50 -> x:[640..1216]）左邊外面 → 找不到 →
    # 0 點擊卡死。此處 x-start 放寬到 0.20（width 0.75 -> x:[~256..1216]），y 沿用共用版。
    # 事件選項（cx~783）仍走 _choice_panel_roi，不受影響（見既有 handle_event 測試）。
    return (
        int(ctx.frame_w * 0.20),
        int(ctx.frame_h * 0.34),
        int(ctx.frame_w * 0.75),
        int(ctx.frame_h * 0.50),
    )


def _popup_bottom_button_roi(ctx: 'BotContext') -> tuple[int, int, int, int]:
    """確認/取消彈窗底部按鈕列的搜尋 ROI（像素 x,y,w,h），排除中段內文。

    L3 20260616_190456:「征途票根達上限」解散確認彈窗的內文第三行為
    「是否確認解散?」(**含「確認」二字**,真實 OCR center=(636,363),y≤0.53h)。
    handle_discard_confirm 的 TextTarget(CONFIRM_BUTTON_TOKENS) 原無 ROI 限制 →
    OCR 由上而下先讀到內文「是否確認解散?」→ 點內文(636,363)無效 →
    ExpectRoiChange 永不觸發 → 12 次無進度 state_stuck_no_progress。
    底部按鈕列在 y≈0.69h（取消/確認 center y≈507/1280x720),勾選列「今日不再提醒」
    在 y≈0.59h。此 ROI 由 0.62h 起到 0.90h（全寬）→ 排除全部內文 + 勾選列、只含底部
    按鈕,讓「確認」TextTarget 只會命中底部按鈕而非內文。比例座標,1280x720/1920x1080 通用。
    """
    return (
        0,
        int(ctx.frame_h * 0.62),
        int(ctx.frame_w * 1.00),
        int(ctx.frame_h * 0.28),
    )


def _take_settle_delay(ctx: 'BotContext') -> float:
    """選卡→點「拿走」前牌面 highlight 沉澱秒數,讀 bot.take_settle_delay。

    容錯:ctx 無 config / config 非 dict / 區塊缺失 / 非數字 / <=0 → 退 0.9(保守舊值)。
    clamp 下限 max(0.3, value):量測警告壓太狠會在牌面 highlight 未渲染時就點空,故設地板。
    """
    cfg = (getattr(ctx, 'config', None) or {})
    if not isinstance(cfg, dict):
        return 0.9
    bot_cfg = cfg.get('bot', {})
    if not isinstance(bot_cfg, dict):
        return 0.9
    raw = bot_cfg.get('take_settle_delay', 0.9)
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return 0.9
    value = float(raw)
    if value <= 0:
        return 0.9
    return max(0.3, value)


def _recapture_then_click_take_button(
    ctx: 'BotContext',
    selected_x: int | None = None,
    settle_delay: float | None = None,
    sleep: float = 0.5,
) -> bool:
    # settle_delay=None → 從 config(bot.take_settle_delay)讀,缺設定退 0.9;
    # 呼叫端顯式傳值則尊重該值(不被 config 覆蓋)。
    delay = settle_delay if settle_delay is not None else _take_settle_delay(ctx)
    _settle_and_refresh(ctx, delay=delay)
    return _click_take_button(ctx, selected_x=selected_x, sleep=sleep)


def _question_panel_roi(ctx: 'BotContext') -> tuple[int, int, int, int]:
    return (
        int(ctx.frame_w * 0.54),
        int(ctx.frame_h * 0.18),
        int(ctx.frame_w * 0.34),
        int(ctx.frame_h * 0.14),
    )


def _selection_header_roi(ctx: 'BotContext') -> tuple[int, int, int, int]:
    return (
        int(ctx.frame_w * 0.22),
        int(ctx.frame_h * 0.04),
        int(ctx.frame_w * 0.56),
        int(ctx.frame_h * 0.18),
    )


def _take_button_roi(ctx: 'BotContext', selected_x: int | None = None) -> tuple[int, int, int, int]:
    if selected_x is None:
        return (
            int(ctx.frame_w * 0.12),
            int(ctx.frame_h * 0.72),
            int(ctx.frame_w * 0.76),
            int(ctx.frame_h * 0.20),
        )

    roi_w = int(ctx.frame_w * 0.24)
    x = max(0, min(ctx.frame_w - roi_w, int(selected_x - roi_w / 2)))
    return (
        x,
        int(ctx.frame_h * 0.72),
        roi_w,
        int(ctx.frame_h * 0.20),
    )


def _bbox_size(bbox: tuple) -> tuple[int, int]:
    xs = [int(p[0]) for p in bbox]
    ys = [int(p[1]) for p in bbox]
    return max(xs) - min(xs), max(ys) - min(ys)


def _read_ocr_candidates(
    ctx: 'BotContext',
    roi: tuple[int, int, int, int],
    exclude_top_ratio: float = 0.0,
) -> list[dict]:
    if getattr(ctx, 'ocr', None) is None or getattr(ctx, 'last_frame', None) is None:
        return []

    results = ctx.ocr.read_text(ctx.last_frame, roi=roi)
    candidates: list[dict] = []
    for text, conf, bbox in results:
        clean = (text or '').strip()
        if not clean:
            continue
        cx, cy = _ocr_center(bbox)
        local_y = cy - roi[1]
        if exclude_top_ratio > 0 and local_y < int(roi[3] * exclude_top_ratio):
            continue
        bw, bh = _bbox_size(bbox)
        candidates.append({
            'text': clean,
            'confidence': round(conf, 4),
            'bbox': tuple((int(p[0]), int(p[1])) for p in bbox),
            'center_x': cx,
            'center_y': cy,
            'width': bw,
            'height': bh,
        })
    return candidates


def _group_option_rows(ctx: 'BotContext', candidates: list[dict]) -> list[list[dict]]:
    if not candidates:
        return []
    ordered = sorted(candidates, key=lambda item: (item['center_y'], item['center_x']))
    rows: list[list[dict]] = []
    tolerance = max(24, int(ctx.frame_h * 0.06))
    for item in ordered:
        if not rows or abs(item['center_y'] - rows[-1][0]['center_y']) > tolerance:
            rows.append([item])
        else:
            rows[-1].append(item)
    for row in rows:
        row.sort(key=lambda item: item['center_x'])
    return rows


def _pick_row_primary_text(ctx: 'BotContext', row: list[dict]) -> dict:
    preferred = [
        item for item in row
        if int(ctx.frame_w * 0.54) <= item['center_x'] <= int(ctx.frame_w * 0.82)
    ]
    if preferred:
        return min(preferred, key=lambda item: item['center_x'])
    return min(row, key=lambda item: item['center_x'])


def _pick_option_from_rows(
    ctx: 'BotContext',
    rows: list[list[dict]],
    keywords: list[str] | None = None,
) -> dict | None:
    if not rows:
        return None
    primaries = [_pick_row_primary_text(ctx, row) for row in rows]
    if keywords:
        for item in primaries:
            if any(keyword in item['text'] for keyword in keywords):
                return item
        for row in rows:
            for item in row:
                if any(keyword in item['text'] for keyword in keywords):
                    return item
        # keywords 給了卻全不命中 → 回 None（R3 不盲點頂部選項）。session 20260614_001957：
        # skip 分支找「直接上樓」沒命中時,舊版盲 fallback 回 primaries[0]=「去商店購物」
        # → 點 enter 重進空商店 → 無限迴圈（且這正是 232705「shop_done 失效」的真因）。
        return None
    return primaries[0]


def _click_with_trace(
    ctx: 'BotContext',
    source: str,
    x: int,
    y: int,
    delay: float = 0.05,
    **details,
) -> bool:
    result = ctx.input.click(x, y, delay=delay)
    success = result is not False
    _record_click_trace(ctx, source=source, x=x, y=y, success=success, **details)
    if not success:
        raise RuntimeError(f'input click failed: source={source}, x={x}, y={y}')
    return True



def _click_template_or_fallback(
    ctx: 'BotContext',
    template_name: str,
    rx: float,
    ry: float,
    sleep: float = 0.6,
) -> bool:
    """.. deprecated:: Phase 1.3
        找不到模板時會無條件點備援座標（R3 同型問題）。目前無呼叫端；
        新程式一律改用 core.actions.click_verified(TemplateTarget(...))。
    """
    logger.warning(
        '[deprecated] _click_template_or_fallback(%s) 已棄用（R3 盲點備援），'
        '請改用 core.actions.click_verified', template_name,
    )
    if getattr(ctx, 'matcher', None) is not None and getattr(ctx, 'last_frame', None) is not None:
        try:
            res = ctx.matcher.match(ctx.last_frame, template_name)
            if res.found:
                _click_with_trace(
                    ctx,
                    source='template',
                    x=res.center_x,
                    y=res.center_y,
                    template_name=template_name,
                    fallback=False,
                    confidence=round(res.confidence, 4),
                )
                time.sleep(sleep)
                return True
        except KeyError:
            pass

    x, y = _px(ctx, rx, ry)
    _click_with_trace(ctx, source='template', x=x, y=y, template_name=template_name, fallback=True)
    time.sleep(sleep)
    return False



def _ocr_read_number(text: str) -> int:
    m = re.search(r'(\d[\d,]*)', text)
    if m:
        return int(m.group(1).replace(',', ''))
    return 0



def _read_number_near_rect(ctx: 'BotContext', x: int, y: int, w: int, h: int) -> int:
    if getattr(ctx, 'ocr', None) is None or getattr(ctx, 'last_frame', None) is None:
        return 0
    fh, fw = ctx.last_frame.shape[:2]
    rx = max(0, x - w // 2)
    ry = max(0, y - h // 2)
    rw = min(fw - rx, w * 4)
    rh = min(fh - ry, h * 3)
    roi = (rx, ry, rw, rh)
    for t in ctx.ocr.read_text_simple(ctx.last_frame, roi=roi):
        n = _ocr_read_number(t)
        if n > 0:
            return n
    return 0



def _notes_id_to_name() -> dict[str, str]:
    global _notes_id_cache
    if _notes_id_cache is not None:
        return _notes_id_cache
    path = Path(__file__).resolve().parents[1] / 'data' / 'notes_map.json'
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        _notes_id_cache = {e['id']: e['name'] for e in data.get('notes', [])}
    except Exception as e:
        logger.warning(f'[Notes] failed to load notes_map.json: {e}')
        _notes_id_cache = {f'note_{i}': f'note_{i}' for i in range(1, 14)}
    return _notes_id_cache



def _prepare_card_note_rois(ctx: 'BotContext') -> list[tuple[int, int, int, int]]:
    """三張主位祕紋的「啟動條件」圖示列 ROI(相對座標,版面固定)。
    語料 prepare_current_20260614_192942 量測:圖示列位於卡片下緣 y≈0.625~0.695,
    三卡 x 起點 0.02/0.255/0.50、各寬 ~0.215。取窄帶只含圖示列,減少角色立繪雜訊。"""
    row_y = int(ctx.frame_h * 0.620)
    row_h = max(1, int(ctx.frame_h * 0.080))
    card_w = int(ctx.frame_w * 0.215)
    return [
        (int(ctx.frame_w * 0.020), row_y, card_w, row_h),
        (int(ctx.frame_w * 0.255), row_y, card_w, row_h),
        (int(ctx.frame_w * 0.500), row_y, card_w, row_h),
    ]


def _prepare_total_note_roi(ctx: 'BotContext') -> tuple[int, int, int, int]:
    return (
        int(ctx.frame_w * 0.88),
        int(ctx.frame_h * 0.28),
        int(ctx.frame_w * 0.10),
        int(ctx.frame_h * 0.44),
    )


def _match_note_templates(ctx: 'BotContext', roi: tuple[int, int, int, int], threshold: float = 0.72) -> list[dict]:
    """辨識 roi 內的音符圖示。

    GAME_MECHANICS D1/D2:整圖 template matching 在 16~24px 失效 → 改用
    vision/note_reader 的 glyph(白色內符號)+ 色相 + 元素懲罰辨識器(對啟動條件列
    語料 10/10)。回傳格式與舊版相同(供 _load_prepare_target_notes 等共用)。
    threshold 參數保留相容性但不再使用(辨識器有自己的信心門檻)。
    """
    if getattr(ctx, 'last_frame', None) is None:
        return []
    x, y, w, h = roi
    if w <= 0 or h <= 0:
        return []
    known_elements = getattr(ctx, 'known_note_elements', None)
    try:
        return note_reader.get_reader().find_icons(
            ctx.last_frame, (x, y, w, h),
            frame_h=int(getattr(ctx, 'frame_h', 720) or 720),
            known_elements=known_elements,
        )
    except Exception as e:  # 辨識器不可用時不可弄壞主迴圈
        logger.warning('[Notes] note_reader.find_icons failed: %s', e)
        return []


def _load_prepare_target_notes(ctx: 'BotContext') -> dict[str, int]:
    totals: dict[str, int] = {}
    for idx, roi in enumerate(_prepare_card_note_rois(ctx), start=1):
        for match in _match_note_templates(ctx, roi):
            qty = _read_number_near_rect(ctx, match['center_x'], match['center_y'], match['width'], match['height'])
            if not (1 <= qty <= _ACTIVATION_COUNT_MAX):
                # 啟動條件數字是小白字、與圖示重疊,EasyOCR 多半讀不到(<=0);最右圖示
                # 的數字 ROI 還會把鄰近「Lv 90」之類讀進來變爆量(L3 20260614_213030
                # 實證 強攻=430)。兩種都不可信 → 退預設。數量不驅動買音符決策
                # (_compute_note_gaps 只看 need>current、shop 靠名稱命中),絕不因數量掉 identity。
                qty = _DEFAULT_ACTIVATION_COUNT
            totals[match['note_name']] = totals.get(match['note_name'], 0) + qty
            _record_ocr_trace(
                ctx,
                purpose='prepare_target_note',
                matched_text=match['note_name'],
                bbox=((match['rect'][0], match['rect'][1]), (match['rect'][2], match['rect'][1]), (match['rect'][2], match['rect'][3]), (match['rect'][0], match['rect'][3])),
                confidence=match['confidence'],
                center=(match['center_x'], match['center_y']),
                quantity=qty,
                card_index=idx,
            )
    return totals


def _load_prepare_current_notes(ctx: 'BotContext') -> dict[str, int]:
    totals: dict[str, int] = {}
    roi = _prepare_total_note_roi(ctx)
    for match in _match_note_templates(ctx, roi):
        qty = _read_number_near_rect(ctx, match['center_x'], match['center_y'], match['width'], match['height'])
        if qty <= 0:
            continue
        totals[match['note_name']] = qty
        _record_ocr_trace(
            ctx,
            purpose='prepare_current_note',
            matched_text=match['note_name'],
            bbox=((match['rect'][0], match['rect'][1]), (match['rect'][2], match['rect'][1]), (match['rect'][2], match['rect'][3]), (match['rect'][0], match['rect'][3])),
            confidence=match['confidence'],
            center=(match['center_x'], match['center_y']),
            quantity=qty,
        )
    return totals


def _valid_card_level(value: int) -> bool:
    return _CARD_LEVEL_MIN <= value <= _CARD_LEVEL_MAX


def _extract_card_level_from_text(text: str, allow_standalone: bool = False) -> int:
    candidates: list[int] = []
    marker_patterns = (
        r'(?:等級|Lv\.?)\s*(\d+)\s*(?:[-–—]?\s*[>＞→]|至|到)\s*(\d+)',
        r'(?:等級|Lv\.?)\s*(\d+)',
        r'\+\s*(\d+)',
    )
    for pattern in marker_patterns:
        for match in re.finditer(pattern, text or '', re.IGNORECASE):
            value = int(match.group(match.lastindex or 1))
            if _valid_card_level(value):
                candidates.append(value)
        if candidates:
            return candidates[-1]

    if allow_standalone:
        for match in re.finditer(r'(?<!\d)([1-6])(?!\d)', text or ''):
            value = int(match.group(1))
            if _valid_card_level(value):
                return value
    return 0


def _parse_level(texts: list[str]) -> int:
    for text in texts:
        level = _extract_card_level_from_text(text)
        if level > 0:
            return level
    return 1


def _has_level_marker(texts: list[str]) -> bool:
    return any(_extract_card_level_from_text(text) > 0 for text in texts)


def _slot_recommendation_info(
    frame,
    roi: tuple[int, int, int, int],
    results: list[tuple[str, float, tuple]],
) -> tuple[bool, str]:
    _x, y, _w, h = roi
    badge_bottom = y + int(h * 0.24)
    for text, _conf, bbox in results:
        if _ocr_center(bbox)[1] <= badge_bottom and signatures.is_recommendation_text(text):
            return True, text.strip()
    if signatures.recommendation_badge_color_hit(frame, roi):
        return True, 'red_badge_color'
    return False, ''


def _shop_goods_roi(ctx: 'BotContext') -> tuple[int, int, int, int]:
    return (
        int(ctx.frame_w * 0.25),
        int(ctx.frame_h * 0.10),
        int(ctx.frame_w * 0.72),
        int(ctx.frame_h * 0.72),
    )


def _shop_slot_key(cx: int, cy: int, ctx: 'BotContext' | None = None) -> str:
    if ctx is None:
        return f'{int(cx) // 120}:{int(cy) // 120}'
    roi_x, roi_y, _roi_w, _roi_h = _shop_goods_roi(ctx)
    col_w = max(120, int(ctx.frame_w * 0.10))
    row_h = max(180, int(ctx.frame_h * 0.24))
    col = max(0, (int(cx) - roi_x) // col_w)
    row = max(0, (int(cy) - roi_y) // row_h)
    return f'{col}:{row}'


def _shop_purchased_slots(ctx: 'BotContext') -> set[str]:
    slots = getattr(ctx, 'shop_purchased_slots', None)
    if not isinstance(slots, set):
        slots = set(slots or [])
        ctx.shop_purchased_slots = slots
    return slots


def _clear_pending_shop_card(ctx: 'BotContext') -> None:
    ctx.pending_shop_card_level = None
    ctx.pending_shop_card_text = None
    ctx.pending_shop_card_slot_key = None


def _extract_shop_card_level(text: str, allow_standalone: bool = False) -> int:
    return _extract_card_level_from_text(text, allow_standalone=allow_standalone)


def _has_shop_card_title_text(text: str) -> bool:
    for part in re.split(r'\s+', text or ''):
        clean = _normalize_text(part)
        if not clean or re.fullmatch(r'[\dxX]+', clean):
            continue
        if any(marker in clean for marker in ('等級', '等级', 'Lv', 'LV', 'lv')):
            continue
        # 優惠/折扣兩字外部化到 signatures.SHOP_DISCOUNT_TOKENS(鐵則2);其餘 UI 排除字不變。
        # 仍走裸 substring `in`(此處原本即裸 in,優惠/折扣為 CJK 無標點 → 與 text_has_any 同效)。
        if any(token in clean for token in ('音符', '之音', '等級', '等级', 'Lv', '刷新', '重置', '離開', '返回', '確認', '金幣', '星幣') + signatures.SHOP_DISCOUNT_TOKENS):
            continue
        if re.search(r'[\u4e00-\u9fffA-Za-z]', clean):
            return True
    return False


def _looks_like_shop_card_text(text: str) -> bool:
    if not text:
        return False
    # 優惠/折扣兩字外部化到 signatures.SHOP_DISCOUNT_TOKENS(鐵則2);其餘排除字與裸 in 行為不變。
    if any(token in text for token in ('音符', '之音', '刷新', '重置', '離開', '返回', '確認') + signatures.SHOP_DISCOUNT_TOKENS):
        return False
    if _extract_shop_card_level(text) > 0:
        return True
    if any(token in text for token in ('潛能', '特飲')):
        return True
    return _has_shop_card_title_text(text) and _extract_shop_card_level(text, allow_standalone=True) > 0


def _group_shop_candidates_by_slot(ctx: 'BotContext', candidates: list[dict]) -> list[tuple[str, list[dict]]]:
    grouped: dict[str, list[dict]] = {}
    for item in candidates:
        key = _shop_slot_key(item['center_x'], item['center_y'], ctx)
        grouped.setdefault(key, []).append(item)

    def order_key(entry: tuple[str, list[dict]]) -> tuple[int, int]:
        first = min(entry[1], key=lambda value: (value['center_y'], value['center_x']))
        return first['center_y'], first['center_x']

    ordered = sorted(grouped.items(), key=order_key)
    for _key, items in ordered:
        items.sort(key=lambda value: (value['center_y'], value['center_x']))
    return ordered


def _pick_shop_slot_click_target(ctx: 'BotContext', items: list[dict]) -> dict:
    title_like = []
    non_numeric = []
    for item in items:
        clean = _normalize_text(item['text'])
        if not clean or re.fullmatch(r'[\dxX]+', clean):
            continue
        non_numeric.append(item)
        if _extract_shop_card_level(item['text'], allow_standalone=False) > 0:
            continue
        if len(clean) >= 3:
            title_like.append(item)
    if title_like:
        return max(title_like, key=lambda item: (item['center_y'], len(_normalize_text(item['text']))))
    if non_numeric:
        return max(non_numeric, key=lambda item: (item['center_y'], len(_normalize_text(item['text']))))
    preferred = [
        item for item in items
        if _has_shop_card_title_text(item['text']) and not re.fullmatch(r'[\d\sxX]+', _normalize_text(item['text']))
    ]
    if preferred:
        return max(preferred, key=lambda item: (len(_normalize_text(item['text'])), -item['center_y']))
    return min(items, key=lambda item: (item['center_y'], item['center_x']))


def _screen_has_event_choice(ctx: 'BotContext') -> bool:
    if getattr(ctx, 'ocr', None) is None or getattr(ctx, 'last_frame', None) is None:
        return False
    try:
        return any(signatures.event_choice_text(text) for text, _conf, _bbox in ctx.ocr.read_text(ctx.last_frame))
    except Exception:
        return False


def _screen_has_potential_select_text(ctx: 'BotContext') -> bool:
    if getattr(ctx, 'ocr', None) is None or getattr(ctx, 'last_frame', None) is None:
        return False
    try:
        return any(signatures.potential_select_text(text) for text, _conf, _bbox in ctx.ocr.read_text(ctx.last_frame))
    except Exception:
        return False


def _screen_has_potential_select_visual(ctx: 'BotContext') -> bool:
    if _screen_has_shop_purchase_modal(ctx) or _screen_has_shop_screen(ctx):
        return False
    frame = getattr(ctx, 'last_frame', None)
    if not signatures.has_bottom_teal_action_button(frame):
        return False

    if getattr(ctx, 'ocr', None) is None:
        return False
    try:
        combined = ''.join(text for text, _conf, _bbox in ctx.ocr.read_text(frame))
    except Exception:
        return False
    return signatures.potential_card_text(combined)


def _screen_has_shop_purchase_modal(ctx: 'BotContext') -> bool:
    if getattr(ctx, 'ocr', None) is None or getattr(ctx, 'last_frame', None) is None:
        return False
    try:
        combined = ''.join(text for text, _conf, _bbox in ctx.ocr.read_text(ctx.last_frame))
    except Exception:
        return False
    return signatures.shop_purchase_modal_text(combined)


def _screen_has_shop_screen(ctx: 'BotContext') -> bool:
    if getattr(ctx, 'ocr', None) is None or getattr(ctx, 'last_frame', None) is None:
        return False
    try:
        combined = ''.join(text for text, _conf, _bbox in ctx.ocr.read_text(ctx.last_frame))
    except Exception:
        return False
    return signatures.shop_screen_text(combined)


def _click_shop_purchase_button(ctx: 'BotContext') -> bool:
    """點購買彈窗的「購買」鈕（Phase 1.3 遷移）。

    找不到購買文字時**不再**盲點 (0.51, 0.72) 固定座標（R3）；
    expect=ExpectRoiChange：點擊後彈窗應關閉（畫面 hash 改變），
    沒變化代表點擊未生效，原地重試一次是安全的。
    """
    roi = (
        int(ctx.frame_w * 0.30),
        int(ctx.frame_h * 0.58),
        int(ctx.frame_w * 0.40),
        int(ctx.frame_h * 0.24),
    )
    return actions.click_verified(
        ctx,
        actions.TextTarget(tuple(signatures.SHOP_BUY_TOKENS), roi=roi),
        expect=actions.ExpectRoiChange(),
        timeout=1.6,
        source='shop_purchase_modal',
    )


def _confirm_shop_purchase_modal(ctx: 'BotContext') -> None:
    pending_level = getattr(ctx, 'pending_shop_card_level', None)
    pending_text = getattr(ctx, 'pending_shop_card_text', None)
    pending_slot_key = getattr(ctx, 'pending_shop_card_slot_key', None)
    if pending_level is None and getattr(ctx, 'ocr', None) is not None and getattr(ctx, 'last_frame', None) is not None:
        try:
            combined = ''.join(text for text, _conf, _bbox in ctx.ocr.read_text(ctx.last_frame))
            parsed_level = _extract_shop_card_level(combined, allow_standalone=True)
            if parsed_level > 0:
                pending_level = parsed_level
                pending_text = combined
        except Exception:
            pass

    if not _click_shop_purchase_button(ctx):
        # 找不到購買鈕（或驗證失敗）→ 不計數、不標格位，保留 pending 留待下一輪重判。
        logger.warning('[SHOP] purchase modal buy button not found/verified; retry next poll')
        return
    if pending_level is not None and pending_level > 0:
        _increment_card_counter(
            ctx,
            int(pending_level),
            source='shop_card',
            matched_text=pending_text or '',
        )
    if pending_slot_key:
        _shop_purchased_slots(ctx).add(str(pending_slot_key))
    _clear_pending_shop_card(ctx)


# affordability（買得起才點,L3 20260614_162359）：商店價格 OCR 過濾範圍。
# 低於 MIN 多為等級/數量標記(等級1~6、x5);高於 MAX 多為 OCR 黏連雜訊
# (如「298100」「29o160」抓成 29160)→ 兩者皆不視為可信價格(= 未知,不過濾)。
_SHOP_PRICE_MIN = 30
_SHOP_PRICE_MAX = 5000


def _shop_affordability_enabled(ctx: 'BotContext') -> bool:
    """是否啟用「買得起才點」過濾(config shop.buy.affordability,預設 True;可關閉防誤殺)。"""
    buy_cfg = _shop_cfg(ctx).get('buy', {}) or {}
    if isinstance(buy_cfg, dict):
        return buy_cfg.get('affordability', True) is not False
    return True


def _extract_shop_prices(text: str) -> list[int]:
    """從一段商店文字抽出落在 [MIN, MAX] 的整數價格候選(排除等級/數量與 OCR 黏連雜訊)。"""
    prices: list[int] = []
    for token in re.findall(r'\d[\d,]*', text or ''):
        try:
            value = int(token.replace(',', ''))
        except ValueError:
            continue
        if _SHOP_PRICE_MIN <= value <= _SHOP_PRICE_MAX:
            prices.append(value)
    return prices


def _shop_slot_prices(ctx: 'BotContext', candidates: list[dict]) -> dict[str, int]:
    """每個商店格位的價格估計:取該格所有可信價格候選的**最小值**(保守 = 較不會誤跳
    買得起的卡;折扣價通常小於被劃掉的原價)。讀不到價格的格位不入表(= 未知,不過濾)。"""
    buckets: dict[str, list[int]] = {}
    for item in candidates:
        slot_key = _shop_slot_key(item['center_x'], item['center_y'], ctx)
        for price in _extract_shop_prices(item['text']):
            buckets.setdefault(slot_key, []).append(price)
    return {slot: min(values) for slot, values in buckets.items() if values}


def _note_spree_cfg(ctx: 'BotContext') -> tuple[bool, list[str], int]:
    """達標後狂買特定音符設定(step9/D,GUI 旋鈕,opt-in)。

    回傳 (enabled, notes, max_spend)。三層防呆:shop.post_target.note_spree 缺/非 dict/
    enabled 非真 → (False, [], 0)。預設 enabled=false ⇒ 呼叫端快速退出、零 OCR(byte-identical)。
    notes=名稱子字串清單(過濾空白);max_spend=本店狂買金錢上限(每進店重置,0=不限,壞型別退 0)。
    """
    pt = _shop_cfg(ctx).get('post_target', {})
    spree = pt.get('note_spree', {}) if isinstance(pt, dict) else {}
    if not isinstance(spree, dict) or not bool(spree.get('enabled', False)):
        return False, [], 0
    notes = (
        [str(n) for n in spree.get('notes', []) if str(n).strip()]
        if isinstance(spree.get('notes'), list) else []
    )
    try:
        max_spend = max(0, int(spree.get('max_spend', 0)))
    except (TypeError, ValueError):
        max_spend = 0
    return True, notes, max_spend


def _try_note_spree(ctx: 'BotContext') -> bool:
    """卡片達標後依清單狂買特定音符(堆超過缺口,強化特定協奏)。買到一個 → True。

    未啟用/清單空 → 第一行快速 return False(零 OCR/零點擊 → byte-identical)。否則掃商店
    貨架 OCR,依 notes 清單**順序**找含該音符名的格:已購格去重跳過;價格已知時套
    affordability(price>money>0 跳過)與 max_spend(spent+price 超上限跳過該格、續找下一個)。
    命中可買 → 仿缺口音符點擊(OcrPoint+EXPECT_NONE → 標已購 → settle → 點「確認」),
    記本店狂買花費 ctx.shop_spree_spent,return True;掃完清單無可買 → return False。
    音符總數一律由 STATE_NOTE_ACQUIRED 覆蓋(D3),此處不動 current_notes。
    """
    enabled, notes, max_spend = _note_spree_cfg(ctx)
    if not enabled or not notes:
        return False   # 未啟用 / 無清單 → 零 OCR 快速退出（byte-identical）
    candidates = _read_ocr_candidates(ctx, _shop_goods_roi(ctx))
    if not candidates:
        return False
    slot_prices = _shop_slot_prices(ctx, candidates)
    purchased = _shop_purchased_slots(ctx)
    spent = int(getattr(ctx, 'shop_spree_spent', 0) or 0)
    money = int(getattr(ctx, 'current_money', 0) or 0)
    # 依清單順序:先把第一個 spree 音符在貨架上的所有格掃完,再換下一個音符。
    for note in notes:
        for item in candidates:
            if note not in item['text']:
                continue
            cx, cy = item['center_x'], item['center_y']
            slot_key = _shop_slot_key(cx, cy, ctx)
            if slot_key in purchased:
                continue   # 本店此格已買過 → 去重跳過（沿用既有機制，防連點卡死）
            price = slot_prices.get(slot_key)
            if price is not None:
                if money > 0 and price > money:
                    logger.info('[SHOP] note_spree skip unaffordable slot %s price=%s money=%s', slot_key, price, money)
                    continue   # 買不起 → 跳過該格，找清單後續
                if max_spend > 0 and spent + price > max_spend:
                    logger.info('[SHOP] note_spree slot %s price=%s would exceed max_spend %s (spent=%s); skip', slot_key, price, max_spend, spent)
                    continue   # 超本店狂買上限 → 跳過該格，找清單後續
            _record_ocr_trace(ctx, purpose='shop_note_spree', matched_text=item['text'], center=(cx, cy), target_note=note)
            actions.click_verified(
                ctx,
                actions.OcrPoint(cx, cy, matched_text=item['text']),
                expect=actions.EXPECT_NONE,
                source='shop_note_spree',
            )
            purchased.add(slot_key)   # 比照缺口音符:點了就標已購(沒彈窗也算,防重點)
            _settle_and_refresh(ctx, delay=0.5)
            actions.click_verified(
                ctx,
                actions.TextTarget(('確認',)),
                expect=actions.ExpectRoiChange(),
                timeout=1.6,
                source='shop_note_spree_confirm',
            )
            ctx.shop_spree_spent = spent + (price or 0)
            logger.info('[SHOP] note_spree bought %s (slot %s price=%s); spent=%s/%s', note, slot_key, price, ctx.shop_spree_spent, max_spend or 'unlimited')
            return True
    return False   # 清單掃完無可買 → fall-through 到既有刷新/離場（byte-identical）


def _prefer_discount_for_cards(ctx: 'BotContext') -> bool:
    """買卡時是否優先選「有優惠標記」的格位（step7/c，GUI 旋鈕）。

    config shop.buy.prefer_discount(預設 False)+ discount_scope(預設 notes_only)：
      - prefer_discount=false / 非 dict / 缺 → False（買卡不優先優惠 = 現行）。
      - prefer_discount=true 且 scope in {cards, all} → True（買卡優先優惠）。
      - scope=notes_only（預設）→ False：只音符階段掃優惠（現行 notes_only 行為），買卡不優先。
    """
    buy = _shop_cfg(ctx).get('buy', {}) or {}
    if not (isinstance(buy, dict) and bool(buy.get('prefer_discount', False))):
        return False
    scope = str(buy.get('discount_scope', 'notes_only') or 'notes_only').strip()
    return scope in ('cards', 'all')   # notes_only(預設) → 買卡不優先優惠（現行）


def _buy_non_discounted_enabled(ctx: 'BotContext') -> bool:
    """是否購買「非特價（無優惠標記）」商品（GUI_DESIGN_SPEC §0b 第5點,shop.buy.buy_non_discounted）。

    預設 True = 現行（特價/原價都買）;False = 只買有優惠標記的卡,跳過原價（全無優惠 → 不買）。
    buy 非 dict / 缺 → True（byte-identical 現行）。
    """
    buy = _shop_cfg(ctx).get('buy', {}) or {}
    if not isinstance(buy, dict):
        return True
    return bool(buy.get('buy_non_discounted', True))


def _shop_slot_discounted(ctx: 'BotContext', candidates: list[dict]) -> set[str]:
    """掃 candidates，回傳「該格 OCR 文字命中優惠/折扣字」的格位 key 集合。

    僅在 _prefer_discount_for_cards(ctx) 為 True 時被呼叫；prefer 關（預設）時呼叫端
    一律給空集，使選卡排序退回原 (center_y, center_x)，逐位元同現行。
    """
    discounted: set[str] = set()
    for item in candidates:
        if _has_discount_keyword(item['text']):
            discounted.add(_shop_slot_key(item['center_x'], item['center_y'], ctx))
    return discounted


def _select_shop_card_to_buy(ctx: 'BotContext') -> tuple[int, int, int, str] | None:
    if getattr(ctx, 'ocr', None) is None or getattr(ctx, 'last_frame', None) is None:
        return None
    candidates = _read_ocr_candidates(ctx, _shop_goods_roi(ctx))
    if not candidates:
        return None

    purchased_slots = _shop_purchased_slots(ctx)
    # affordability：只在能讀到餘額(>0)且未關閉時過濾;價格未知(未解析/糊字)→ 不過濾,
    # 改由購買 modal 把關(點了沒開彈窗 → 既有 skip 機制處理),零回歸。
    money = int(getattr(ctx, 'current_money', 0) or 0)
    slot_prices = (
        _shop_slot_prices(ctx, candidates)
        if (money > 0 and _shop_affordability_enabled(ctx)) else {}
    )

    def _unaffordable(slot_key: str) -> bool:
        price = slot_prices.get(slot_key)
        if price is not None and price > money:
            logger.info('[SHOP] skip unaffordable card slot %s price=%s money=%s', slot_key, price, money)
            return True
        return False

    # step7/c 優惠優先：prefer 開(scope=cards/all)才算優惠格集合,否則空集。
    # 排序時加一個「最前維度」= 該格不在 discounted（False 排前 → 優惠優先）;
    # discounted=set() 時所有 `slot not in set() == True` → 第一維全 True → 排序退回原
    # (center_y, center_x),逐位元同現行（byte-identical）。
    # buy_non_discounted=False（§0b 第5點）：只買有優惠標記的卡 → 需算優惠格集合以過濾原價;
    # prefer_discount 亦需此集合排序。任一需要才算,否則空集（byte-identical 現行）。
    buy_non_discounted = _buy_non_discounted_enabled(ctx)
    discounted = (
        _shop_slot_discounted(ctx, candidates)
        if (_prefer_discount_for_cards(ctx) or not buy_non_discounted) else set()
    )

    for item in sorted(
        candidates,
        key=lambda value: (
            _shop_slot_key(value['center_x'], value['center_y'], ctx) not in discounted,
            value['center_y'],
            value['center_x'],
        ),
    ):
        text = item['text']
        if not _looks_like_shop_card_text(text):
            continue
        slot_key = _shop_slot_key(item['center_x'], item['center_y'], ctx)
        if not buy_non_discounted and slot_key not in discounted:
            logger.debug('[SHOP] skip non-discounted card slot %s (buy_non_discounted=False)', slot_key)
            continue
        if slot_key in purchased_slots:
            logger.debug('[SHOP] skip already purchased card slot %s text=%s', slot_key, text)
            continue
        if _unaffordable(slot_key):
            continue
        level = _extract_shop_card_level(text, allow_standalone=True)
        if level <= 0:
            level = 1
        return item['center_x'], item['center_y'], level, text

    # 分組 fallback 同樣加優惠優先最前維度。_group_shop_candidates_by_slot 已按各格
    # 左上 (center_y, center_x) 排序;sorted 穩定 → 只用布林 key 重排,優惠格提前、其餘
    # 維持原順序語意。discounted=set() 時布林全 True → 不改順序,byte-identical。
    grouped_slots = _group_shop_candidates_by_slot(ctx, candidates)
    for slot_key, items in sorted(grouped_slots, key=lambda entry: entry[0] not in discounted):
        combined = _row_combined_text(items)
        if not _looks_like_shop_card_text(combined):
            continue
        if not buy_non_discounted and slot_key not in discounted:
            logger.debug('[SHOP] skip non-discounted grouped card slot %s (buy_non_discounted=False)', slot_key)
            continue
        level = _extract_shop_card_level(combined, allow_standalone=True)
        if level <= 0:
            level = 1
        if slot_key in purchased_slots:
            logger.debug('[SHOP] skip already purchased card grouped slot %s text=%s', slot_key, combined)
            continue
        if _unaffordable(slot_key):
            continue
        primary = _pick_shop_slot_click_target(ctx, items)
        return primary['center_x'], primary['center_y'], level, combined
    return None


# ─────────────────────────────────────────────────────────────
# 測試模式 shop_buy_all：買全部可買商品（潛能特飲 + 音符）各一次、去重、買完離開。
# 與正常經濟邏輯隔離（只在 ctx.shop_buy_all 為 True 時走），去重沿用既有
# _shop_slot_key / _shop_purchased_slots / pending_shop_card_slot_key 機制。
# ─────────────────────────────────────────────────────────────

# 音符商品判別字（「之音」涵蓋體力/專注/風/幸運之音等；「音符」為通用詞）。
_SHOP_NOTE_GOODS_MARKERS = ('音符', '之音')
# 貨架上的 UI 控制字（非商品，永遠不買）。優惠/折扣兩字外部化到 signatures.SHOP_DISCOUNT_TOKENS
# （鐵則2）；其餘 UI 字維持本地（坦回=「返回」OCR 糊字變體等,非畫面提示字單一來源範疇）。
_SHOP_GOOD_UI_TOKENS = ('刷新', '重置', '離開', '返回', '坦回', '確認') + signatures.SHOP_DISCOUNT_TOKENS


def _looks_like_shop_good_text(text: str) -> bool:
    """測試模式「可買商品」判別：接受『卡/特飲』或『音符/之音』，排除 UI 控制字。

    與 _looks_like_shop_card_text 的差異：後者明確把含『音符/之音』的列排除
    （正常模式音符走 note_gaps 分支），本函式則把音符也視為可買。
    """
    if not text:
        return False
    if any(token in text for token in _SHOP_GOOD_UI_TOKENS):
        return False
    if any(token in text for token in _SHOP_NOTE_GOODS_MARKERS):
        return True
    return _looks_like_shop_card_text(text)


def _select_shop_good_to_buy_any(ctx: 'BotContext') -> tuple[int, int, int, str] | None:
    """挑一個尚未購買的可買商品（特飲或音符）。複製 _select_shop_card_to_buy 的
    掃描/分組結構，僅把判別函式換成 _looks_like_shop_good_text。"""
    if getattr(ctx, 'ocr', None) is None or getattr(ctx, 'last_frame', None) is None:
        return None
    candidates = _read_ocr_candidates(ctx, _shop_goods_roi(ctx))
    if not candidates:
        return None

    purchased_slots = _shop_purchased_slots(ctx)
    for item in sorted(candidates, key=lambda value: (value['center_y'], value['center_x'])):
        text = item['text']
        if not _looks_like_shop_good_text(text):
            continue
        slot_key = _shop_slot_key(item['center_x'], item['center_y'], ctx)
        if slot_key in purchased_slots:
            logger.debug('[SHOP] buy-all skip already purchased good slot %s text=%s', slot_key, text)
            continue
        level = _extract_shop_card_level(text, allow_standalone=True)
        if level <= 0:
            level = 1
        return item['center_x'], item['center_y'], level, text

    for slot_key, items in _group_shop_candidates_by_slot(ctx, candidates):
        combined = _row_combined_text(items)
        if not _looks_like_shop_good_text(combined):
            continue
        if slot_key in purchased_slots:
            logger.debug('[SHOP] buy-all skip already purchased grouped good slot %s text=%s', slot_key, combined)
            continue
        level = _extract_shop_card_level(combined, allow_standalone=True)
        if level <= 0:
            level = 1
        primary = _pick_shop_slot_click_target(ctx, items)
        return primary['center_x'], primary['center_y'], level, combined
    return None


def _leave_shop(ctx: 'BotContext') -> str | None:
    """穩定離開商店：按 ESC 鍵離場（遊戲設計，使用者實機確認）。

    ESC 走 input.press_esc（pydirectinput / SendInput 掃描碼，遊戲讀得到）。
    ESC 不是點擊 → 不灌水 click_count；若 ESC 沒效、stuck 會由 watchdog 正常觸發。
    ESC 後不做 click_verified 驗證（沒有點擊），靠下一輪 FSM 重判新狀態。
    後備：舊環境無鍵盤能力 → 維持文字離場（含「坦回」OCR 糊字變體），仍 R3 不盲點。
    """
    inp = getattr(ctx, 'input', None)
    press = getattr(inp, 'press_esc', None)
    if callable(press):
        logger.info('[SHOP] leave via ESC key')
        press()
        time.sleep(0.5)   # 讓畫面切換；下一輪 FSM 重判新狀態
        return None
    # 後備：舊環境無鍵盤能力 → 維持文字離場（含坦回變體），仍 R3 不盲點。
    if actions.click_verified(
        ctx,
        actions.TextTarget(tuple(signatures.SHOP_LEAVE_TOKENS)),
        expect=actions.ExpectRoiChange(),
        timeout=1.6,
        source='shop_leave',
    ):
        return None
    logger.info('[SHOP] leave: no ESC capability and leave text not found; skip (R3)')
    return None


def _refresh_trigger_allows(ctx: 'BotContext') -> bool:
    """shop.refresh.trigger（B，GUI 旋鈕）：在「買無可買」離場前是否允許刷新貨架。
    實際刷新次數仍受 bot.max_shop_refresh 限制；本函式只決定「時機條件」。
      exhausted（預設，現行）= 買無可買就刷（只受次數限制），byte-identical。
      never        → 一律不刷。
      when_gap     → 仍有協奏音符缺口才刷（期待刷出缺口音符）。
      before_target→ 卡片總等級未達標才刷（期待刷出更多特飲卡）。
      always       → 同 exhausted（「每進店先刷」需改 handler 時序、另開子項，本批不做）。
      未知值        → 保守退 exhausted（允許，不靜默關掉刷新）。
    start_from_visit（GUI_DESIGN_SPEC §3.3）：造訪次數未達此值前一律不刷,優先於上述
    trigger。預設 1 → start_from>1 恆 False → 守衛不啟用 → byte-identical 現行。
    """
    refresh = _shop_cfg(ctx).get('refresh', {})
    if isinstance(refresh, dict):
        start_from = int(refresh.get('start_from_visit', 1) or 1)
        if start_from > 1 and int(getattr(ctx, 'shop_visit_count', 0) or 0) < start_from:
            return False
    trigger = (
        str(refresh.get('trigger', 'exhausted') or 'exhausted').strip()
        if isinstance(refresh, dict) else 'exhausted'
    )
    if trigger == 'never':
        return False
    if trigger == 'when_gap':
        return bool(_compute_note_gaps(
            getattr(ctx, 'target_notes', {}) or {},
            getattr(ctx, 'current_notes', {}) or {},
        ))
    if trigger == 'before_target':
        return not _card_target_met(ctx)
    return True  # exhausted（預設）/ always / 未知值


def _refresh_shop(ctx: 'BotContext') -> bool:
    """刷新商店貨架：按 Q 鍵（與選卡 reroll 同熱鍵，使用者實機確認 2026-06-14）。

    刷新鈕實機是無文字 icon + 熱鍵「Q」（同選卡 reroll，session 20260613_223142 類比）→
    原本走 SHOP_REFRESH_TOKENS 文字點擊永遠抓不到 → 形同永不刷新。改送 Q 鍵
    （input.press_key，與 reroll/ESC 同機制）。Q 不是點擊 → 不灌水 click_count；
    shop_refresh_count（呼叫端 ++）已在 progress_counters → 不會被 watchdog 誤殺。
    回傳 True=已送出刷新（呼叫端記次數 + 清本店已購格位）;False=無鍵盤能力且後備文字鈕
    也找不到（呼叫端 R3 不盲點、改離場）。後備：舊環境無鍵盤能力 → 文字點擊。
    """
    inp = getattr(ctx, 'input', None)
    press = getattr(inp, 'press_key', None)
    if callable(press):
        logger.info('[SHOP] refresh via Q key')
        press('q')
        time.sleep(0.5)   # 讓貨架刷新；下一輪 FSM 重判新貨
        return True
    # 後備：舊環境無鍵盤能力 → 文字點擊（刷新鈕多半無文字，找不到仍 R3 不盲點）。
    if actions.click_verified(
        ctx,
        actions.TextTarget(tuple(signatures.SHOP_REFRESH_TOKENS)),
        expect=actions.ExpectRoiChange(),
        timeout=1.6,
        source='shop_refresh',
    ):
        return True
    logger.info('[SHOP] refresh: no keyboard capability and refresh text not found; skip (R3)')
    return False


def _handle_shop_buy_all(ctx: 'BotContext') -> str | None:
    """測試模式：買全部可買商品各一次、去重、買完離開。自足，不動正常經濟邏輯。"""
    # pending_slot 但沒彈窗 → settle 後再判一次彈窗；有就 confirm；沒有 → 把
    # pending_slot 記進已購集、清 pending（沿用 handle_shop 1684-1693 的保險）。
    pending_slot_key = getattr(ctx, 'pending_shop_card_slot_key', None)
    if pending_slot_key:
        if _settle_and_refresh(ctx, delay=0.8) and _screen_has_shop_purchase_modal(ctx):
            logger.info('[SHOP] buy-all: delayed purchase modal detected; confirming purchase')
            _confirm_shop_purchase_modal(ctx)
            return None
        logger.info('[SHOP] buy-all: previous click did not open purchase modal; skip slot %s', pending_slot_key)
        _shop_purchased_slots(ctx).add(str(pending_slot_key))
        _clear_pending_shop_card(ctx)
        return None

    good = _select_shop_good_to_buy_any(ctx)
    if good is not None:
        # 商店還有未購商品可買 → 還沒買完，清掉 shop_done（避免下一個 SHOP_CHOICE 誤上樓）
        # 並把 empty-streak 歸 0（商店有貨，不是空的）。
        ctx.shop_done = False
        ctx.shop_emptied_streak = 0
        cx, cy, level, text = good
        _record_ocr_trace(
            ctx,
            purpose='shop_buy_all',
            matched_text=text,
            center=(cx, cy),
            level=level,
        )
        # expect=EXPECT_NONE：購買彈窗是否彈出由跨輪詢的 pending-slot 機制驗證
        # （沒出現會跳過該格位），重複點同一格有害。
        if actions.click_verified(
            ctx,
            actions.OcrPoint(cx, cy, matched_text=text),
            expect=actions.EXPECT_NONE,
            source='shop_card',
        ):
            ctx.pending_shop_card_level = level
            ctx.pending_shop_card_text = text
            ctx.pending_shop_card_slot_key = _shop_slot_key(cx, cy, ctx)
        return None

    # 沒有未購商品可買（售完/全已購）→ 標記 shop_done，讓下一個 SHOP_CHOICE 選「不要了
    # 直接上樓」而非重進空商店（修無限重進迴圈，session 20260613_221637）。並累計
    # empty-streak 作為穩健兜底（shop_done 在拿免費強化的交錯流程會失效，session
    # 20260613_232705）。
    ctx.shop_done = True
    ctx.shop_emptied_streak = int(getattr(ctx, 'shop_emptied_streak', 0) or 0) + 1
    _clear_pending_shop_card(ctx)
    return _leave_shop(ctx)


def _normalize_text(text: str) -> str:
    return re.sub(r'[\s\u3000:：,，.。!！?？>\-▶]+', '', text or '')



def _looks_like_potential_title(text: str) -> bool:
    if not text:
        return False
    if re.search(r'[\d%+]', text):
        return False
    noise_tokens = (
        '造成', '傷害', '提升', '持續', '冷卻', '技能', '目標', '範圍',
        '自動', '學會', '命中', '爆炸', '每秒', '效果', '攻擊', '全隊',
    )
    return not any(token in text for token in noise_tokens)



def _pick_slot_name(results: list[tuple[str, float, tuple]], slot_h: int, idx: int) -> str:
    preferred: list[tuple[int, str]] = []
    fallback: list[tuple[int, str]] = []
    title_y_min = int(slot_h * 0.50)
    title_y_max = int(slot_h * 0.82)

    for text, _conf, bbox in results:
        clean = text.strip()
        if not clean:
            continue
        center_y = _ocr_center(bbox)[1]
        score = len(_normalize_text(clean))
        if _looks_like_potential_title(clean):
            if title_y_min <= center_y <= title_y_max:
                preferred.append((score, clean))
            else:
                fallback.append((score, clean))

    if preferred:
        preferred.sort(key=lambda item: item[0])
        return preferred[0][1]
    if fallback:
        fallback.sort(key=lambda item: item[0])
        return fallback[0][1]

    texts = [text for text, _, _ in results]
    return next((t for t in texts if '+' not in t and 'Lv' not in t), f'option_{idx + 1}')


def _selection_slot_layout(ctx: 'BotContext', card_count: int) -> list[tuple[int, int, int, int, int, int]]:
    h, w = ctx.frame_h, ctx.frame_w
    if card_count <= 1:
        # 置中單卡(免費強化「選擇一張」):x∈[~346,934]@1280,完整涵蓋 cx≈642 的卡片。
        centers = [0.50]
        width_ratio = 0.46
    elif card_count <= 2:
        centers = [0.33, 0.67]
        width_ratio = 0.28
    else:
        centers = [1 / 6, 0.50, 5 / 6]
        width_ratio = 1 / 3

    layouts: list[tuple[int, int, int, int, int, int]] = []
    slot_w = max(1, int(w * width_ratio))
    for ratio in centers:
        center_x = int(w * ratio)
        x0 = max(0, min(w - slot_w, center_x - slot_w // 2))
        layouts.append((x0, 0, slot_w, h, center_x, int(h * 0.42)))
    return layouts


def _selection_card_band_roi(ctx: 'BotContext') -> tuple[int, int, int, int]:
    """選卡畫面的「卡片等級標記」橫帶(全寬、卡片中段),供數實際卡數用。"""
    h, w = ctx.frame_h, ctx.frame_w
    return (0, int(h * 0.40), w, int(h * 0.32))


def _count_selection_cards(ctx: 'BotContext', fallback: int = 1) -> int:
    """數選卡畫面實際卡數:全寬 OCR 卡片等級標記帶,以 x 群集判卡數。

    用於免費強化「選擇一張潛能卡片強化吧!」畫面 —— 該 header 的「一張」是「挑一張」,
    卡數可為 1~3（session 20260614_001041：2 卡被當置中單卡 → 點中間空隙(640,302)
    卡死）。每張卡恰有一個「等級 N→M」標記,相鄰標記 x 間距 > 卡寬一半視為不同卡。
    """
    frame = getattr(ctx, 'last_frame', None)
    ocr = getattr(ctx, 'ocr', None)
    if frame is None or ocr is None:
        return fallback
    try:
        results = ocr.read_text(frame, roi=_selection_card_band_roi(ctx))
    except Exception:
        return fallback
    xs = sorted(
        sum(p[0] for p in bbox) / 4.0
        for text, _conf, bbox in results
        if _extract_card_level_from_text(text) > 0
    )
    if not xs:
        return fallback
    min_gap = ctx.frame_w * 0.15   # 相鄰等級標記間距 > 卡寬一半 → 不同卡
    clusters = 1
    for prev, cur in zip(xs, xs[1:]):
        if cur - prev > min_gap:
            clusters += 1
    return min(max(clusters, 1), 3)


def _detect_expected_card_count(ctx: 'BotContext', fallback: int = 3) -> int:
    hinted = getattr(ctx, 'pending_card_count', None)
    header_text = ''
    if getattr(ctx, 'ocr', None) is not None and getattr(ctx, 'last_frame', None) is not None:
        header_text = ''.join(ctx.ocr.read_text_simple(ctx.last_frame, roi=_selection_header_roi(ctx)))

    # 免費強化「選擇一張潛能卡片強化吧!」:「一張」是「挑一張」,卡數可為 1~3。
    # 置中單卡若被當 2/3 卡會落槽位空隙 → 0 點擊卡死(session 20260613_201218);
    # 反之 2 卡若被當置中單卡則點中間空隙 → 卡死(session 20260614_001041)。
    # 故不盲信,改數實際卡數(全寬掃等級標記、x 群集);數不到時保守回置中單卡 1。
    if any(keyword in header_text for keyword in signatures.UPGRADE_SINGLE_CARD_HEADER_TOKENS):
        return _count_selection_cards(ctx, fallback=1)
    if hinted in (1, 2, 3):
        return hinted
    if not header_text:
        return fallback
    if any(keyword in header_text for keyword in signatures.UPGRADE_HEADER_TOKENS):
        return 2
    three_card_hints = (
        signatures.POTENTIAL_SELECT_KEYWORDS
        + signatures.POTENTIAL_CARD_HINTS
        + signatures.TAKE_BUTTON_TOKENS
        + signatures.REROLL_BUTTON_TOKENS
    )
    if any(keyword in header_text for keyword in three_card_hints):
        return 3
    return fallback



def _extract_slot_options(ctx: 'BotContext') -> list:
    options = []
    if getattr(ctx, 'last_frame', None) is None:
        return options

    frame = ctx.last_frame
    card_count = _detect_expected_card_count(ctx)

    ScreenOption = None
    try:
        from core.decision_engine import ScreenOption as _ScreenOption
        ScreenOption = _ScreenOption
    except Exception:
        ScreenOption = None

    for idx, (x0, y0, slot_w, slot_h, center_x, center_y) in enumerate(_selection_slot_layout(ctx, card_count)):
        roi = (x0, y0, slot_w, slot_h)
        results = []
        if getattr(ctx, 'ocr', None) is not None:
            results = ctx.ocr.read_text(frame, roi=roi)
        texts = [text for text, _, _ in results]
        name = _pick_slot_name(results, slot_h, idx)
        level = _parse_level(texts)
        has_level_marker = _has_level_marker(texts)
        recommended, recommendation_text = _slot_recommendation_info(frame, roi, results)
        target_level = signatures.parse_recommendation_target_level(recommendation_text)
        is_pink = not has_level_marker
        if ScreenOption is not None:
            options.append(ScreenOption(
                name=name,
                level=level,
                position=(center_x, center_y),
                recommended=recommended,
                is_pink=is_pink,
                recommendation_text=recommendation_text,
                recommendation_target_level=target_level,
            ))
        else:
            options.append(type('ScreenOption', (), {
                'name': name,
                'level': level,
                'position': (center_x, center_y),
                'recommended': recommended,
                'is_pink': is_pink,
                'recommendation_text': recommendation_text,
                'recommendation_target_level': target_level,
            })())
    return options



def _click_take_button(ctx: 'BotContext', selected_x: int | None = None, sleep: float = 0.5) -> bool:
    """點「拿走」確認鈕（Phase 1.3 遷移）。

    找不到按鈕文字時**不再**盲點選中卡片欄位的固定座標（R3）；
    expect=EXPECT_NONE：拿走後的下一畫面機制未驗證（GAME_MECHANICS B4 ❓），
    且呼叫端（_recapture_then_click_take_button）已自帶點擊前的重拍節奏。
    """
    clicked = actions.click_verified(
        ctx,
        actions.TextTarget(
            tuple(signatures.TAKE_BUTTON_TOKENS),
            roi=_take_button_roi(ctx, selected_x=selected_x),
        ),
        expect=actions.EXPECT_NONE,
        source='take_button',
    )
    time.sleep(sleep)
    return clicked



def _record_selection_if_needed(ctx: 'BotContext', choice) -> None:
    try:
        from core.decision_engine import DecisionEngine as _DecisionEngine
        if isinstance(getattr(ctx, 'engine', None), _DecisionEngine):
            return
    except Exception:
        pass
    recorder = getattr(getattr(ctx, 'engine', None), 'state', None)
    recorder = getattr(recorder, 'record_selection', None)
    if callable(recorder):
        recorder(choice)



def _extract_note_update(ctx: 'BotContext') -> tuple[str | None, int]:
    updates = _extract_note_updates(ctx)
    if not updates:
        return None, 0
    first_name = next(iter(updates))
    return first_name, updates[first_name]


def _extract_note_updates(ctx: 'BotContext') -> dict[str, int]:
    if getattr(ctx, 'ocr', None) is None or getattr(ctx, 'last_frame', None) is None:
        return {}

    results = ctx.ocr.read_text(ctx.last_frame)
    if not results:
        return {}

    updates: dict[str, int] = {}
    normalized_names = [(_normalize_text(name), name) for name in _notes_id_to_name().values()]
    for text, conf, bbox in results:
        normalized_text = _normalize_text(text)
        if not normalized_text:
            continue
        for normalized_name, note_name in normalized_names:
            if normalized_name and normalized_name in normalized_text:
                numbers = re.findall(r'(\d+)', text)
                quantity = int(numbers[-1]) if numbers else 0
                if quantity <= 0:
                    continue
                cx, cy = _ocr_center(bbox)
                _record_ocr_trace(
                    ctx,
                    purpose='note_acquired',
                    matched_text=text,
                    bbox=bbox,
                    confidence=round(conf, 4),
                    center=(cx, cy),
                    note_name=note_name,
                    quantity=quantity,
                )
                updates[note_name] = quantity
                break
    return updates


def _note_totals_hud_roi(ctx: 'BotContext') -> tuple[int, int, int, int]:
    """「獲得音符」畫面頂列(每個音符圖示旁標當前持有總量)的 ROI(比例座標)。

    GAME_MECHANICS D3 強化(best-effort):頂列橫排 ~9 種音符圖示+各自總量,可用來
    重新同步全部音符總量(類似金錢固定 HUD ROI)。語料(1280x720)上變化列在畫面
    中央偏下、效果說明在其下;頂列總量帶位於畫面上緣 ~8%~26% 高、橫跨中央 ~80% 寬。
    取寬帶以容忍版面飄移;讀數仍以 _match_note_templates 對映圖示→音符名為準。
    """
    return (
        int(ctx.frame_w * 0.10),
        int(ctx.frame_h * 0.08),
        int(ctx.frame_w * 0.80),
        max(1, int(ctx.frame_h * 0.18)),
    )


def _read_note_totals_via_icons(ctx: 'BotContext') -> dict[str, int]:
    """best-effort:用頂列音符圖示模板對映「圖示→音符名」並讀緊鄰的當前總量。

    GAME_MECHANICS D3:成功時回傳 {音符名: 當前總量}(覆蓋語意,類似金錢 HUD)。
    模板不可靠(matcher 缺席 / 無命中 / 讀不到數字)時回傳 {},呼叫端退回變化列
    覆蓋路徑(_extract_note_updates)—— 務必不弄壞既有變化列行為。
    """
    if getattr(ctx, 'matcher', None) is None or getattr(ctx, 'last_frame', None) is None:
        return {}

    roi = _note_totals_hud_roi(ctx)
    totals: dict[str, int] = {}
    for match in _match_note_templates(ctx, roi):
        qty = _read_number_near_rect(ctx, match['center_x'], match['center_y'], match['width'], match['height'])
        if qty <= 0:
            continue
        totals[match['note_name']] = qty
        _record_ocr_trace(
            ctx,
            purpose='note_totals_hud',
            matched_text=match['note_name'],
            bbox=((match['rect'][0], match['rect'][1]), (match['rect'][2], match['rect'][1]), (match['rect'][2], match['rect'][3]), (match['rect'][0], match['rect'][3])),
            confidence=match['confidence'],
            center=(match['center_x'], match['center_y']),
            quantity=qty,
        )
    return totals


def _extract_shop_note_quantity(text: str) -> int:
    for pattern in (r'[xX×*]\s*(\d+)', r'(\d+)\s*個'):
        match = re.search(pattern, text)
        if match:
            return max(1, int(match.group(1)))

    numbers = [int(value) for value in re.findall(r'(\d+)', text)]
    small_candidates = [value for value in numbers if 1 <= value <= 10]
    if small_candidates:
        return min(small_candidates)
    return 1


def _iter_quiz_answers() -> list[tuple[str, str]]:
    raw = _load_quiz_db().get('answers', [])
    pairs: list[tuple[str, str]] = []
    if isinstance(raw, dict):
        for question, answer in raw.items():
            pairs.append((str(question), str(answer)))
        return pairs
    for entry in raw:
        if isinstance(entry, dict):
            question = str(entry.get('question', '') or entry.get('prompt', '')).strip()
            answer = str(entry.get('answer', '') or entry.get('option', '')).strip()
            if question and answer:
                pairs.append((question, answer))
    return pairs


def _row_combined_text(row: list[dict]) -> str:
    seen: set[str] = set()
    parts: list[str] = []
    for item in row:
        text = item['text'].strip()
        if text and text not in seen:
            parts.append(text)
            seen.add(text)
    return ' '.join(parts)


def _event_reward_detail_text(text: str) -> bool:
    clean = _normalize_text(text)
    return any(token in clean for token in (
        '機率',
        '概率',
        '獲得',
        '获得',
        '恢復',
        '恢复',
        '損失',
        '损失',
        '生命值',
    )) or bool(re.search(r'\d+%', clean))


def _pick_event_click_target(ctx: 'BotContext', row: list[dict]) -> dict | None:
    # 點選項時的目標選擇,優先序:
    #   1) 真正的選項標題（既非獎勵明細、亦非成本標籤）—— 正常情況點這個。
    #   2) 標題缺失（OCR 漏讀）時 fallback：點該列「非成本」文字（含獎勵/隨機標籤亦可,
    #      點在選項列可點區內仍能選中該選項）。
    #   3) 整列**只有成本標籤**（消耗140C / 消耗音符）→ 回 None —— 絕不點成本標籤。
    # L3 20260615_001510 / 183043「花錢買協奏音符」事件:第一列標題被 ROI/exclude 切掉後,
    # 舊 fallback 回全列 → 點到成本「消耗140C」(1038,287) → 不推進、無限重點卡 1 分 40 秒。
    # 關鍵是**永不點成本標籤**;點獎勵標籤雖非最佳但仍落在該選項可點區、能推進,可接受。
    # 整列只剩成本時回 None,由呼叫端改挑另一個有可點目標的選項組（不空轉重點成本）。
    non_detail = [
        item for item in row
        if not _event_reward_detail_text(item['text'])
        and not _event_has_money_cost(item['text'])
        and not _event_has_note_cost(item['text'])
    ]
    if non_detail:
        return _pick_row_primary_text(ctx, non_detail)
    non_cost = [
        item for item in row
        if not _event_has_money_cost(item['text'])
        and not _event_has_note_cost(item['text'])
    ]
    if non_cost:
        return _pick_row_primary_text(ctx, non_cost)
    return None


def _event_has_note_cost(text: str) -> bool:
    """選項「消耗/扣除 音符」(音符要留給協奏,兩種策略皆拒)。

    必須是「消耗…音符」鄰近(消耗 後短窗內出現 音符/之音),不能只看共現 ——
    傾聽事件「消耗50,獲得5個隨機音符」是花錢換音符,音符在獎勵側,不該被當消耗音符。

    OCR 變體:EasyOCR 多次把「音符」的『符』(U+7B26)誤判成『苻』(U+82FB)
    (L3 20260615_191325「消耗5個隨機音苻」),故『符苻』皆收 —— 否則漏拒花音符選項,
    湊音符策略失效。注意只認「消耗/扣除…」前綴,獎勵側「獲得…音苻」不受影響。
    """
    return bool(re.search(r'(消耗|扣除)[^。!,，]{0,6}(音[符苻]|之音)', text or ''))


def _event_has_money_cost(text: str) -> bool:
    """選項有金錢/輝光幣成本。遊戲economy多以裸數字表示(「消耗100」不寫幣別),
    故 消耗/扣除/支付/花費 後接數字即視為金錢成本(已先排除「消耗音符」)。"""
    if _event_has_note_cost(text):
        return False
    return bool(re.search(r'(消耗|扣除|支付|花費)[^。!,，]{0,4}(\d|金|幣|錢|💰)', text or ''))


def _event_has_money_loss(text: str) -> bool:
    """選項含「(機率)損失/失去 金錢」= 有金錢下行風險(保守策略要排除)。
    遊戲多以裸數字表示金額(「失去100」),故 損失/失去 後接數字即算金錢損失,
    但排除緊接 生命/% 的(那是生命損失,由 _event_has_hp_loss 認)。"""
    for m in re.finditer(r'(損失|失去|损失)\s*(\d+)', text or ''):
        tail = (text[m.end():m.end() + 3] or '')
        if any(u in tail for u in ('%', '生命', '個', '个', '音符', '之音', '潛能', '潜能')):
            continue
        return True
    return bool(re.search(r'(損失|失去|损失)[^。!,，]{0,4}(金|幣|錢|💰)', text or ''))


def _event_has_hp_loss(text: str) -> bool:
    """選項含「(機率)損失/失去 生命」= 有生命下行風險(保守策略要排除)。
    注意「恢復…生命值」是上行,不可誤判(故只認損失/失去前綴)。"""
    return bool(re.search(r'(損失|失去|损失)[^。!,，]{0,6}(生命)', text or ''))


def _event_has_any_cost(text: str) -> bool:
    """任何「消耗/扣除/支付/花費」成本(金錢或音符)。保守策略用以排除花成本的選項。"""
    return _event_has_note_cost(text) or _event_has_money_cost(text)


def _event_is_npc_dialogue(combined: str) -> bool:
    """判斷一個「選項組」合併文字是否為純 NPC 敘述台詞(非可選項)。

    依據:真選項一定帶「結果」—— 不是成本明細(消耗/扣除/支付/花費)就是獎勵明細
    (獲得/機率/恢復/損失/生命值/N%);純情境台詞兩者皆無。
    (L3 20260615_191325「我迷路了 得循著聲音才知道怎麼走...」綠髮女僕對話事件:
     commit 153d23c 把選項 ROI 上緣 0.34→0.30 修好買音符事件,卻把 cy≈270 的這句
     NPC 台詞也納入選項候選 → generic 同分時 order 最小的台詞列勝 → 點台詞卡死。)

    只用在「選項組(已合併標題+相鄰成本/獎勵明細)」層級,不可用在單列 ——
    真選項標題(如「認真傾聽。」)在單列亦無成本/獎勵字,其明細在同組相鄰列。
    quiz(純數字答案,如「1,024」)由 _select_event_option 上游先處理、提早 return,
    不會走到這裡,故不受此規則影響。
    """
    return not _event_has_any_cost(combined) and not _event_reward_detail_text(combined)


def _event_is_free_random(text: str) -> bool:
    return any(keyword in text for keyword in ('隨機', '機率', '概率')) and not (
        _event_has_note_cost(text) or _event_has_money_cost(text)
    )


def _event_reward_rank(text: str) -> int:
    """報酬分級(數字小=報酬好,供挑「報酬最好」用)。使用者 2026-06-14 拍板序:
    稀有潛能 > 潛能 > 普通潛能 > 音符 > 金錢 > 其他。
    注意「普通潛能」「稀有潛能」是「潛能」的特例,必須先判特例字,否則被通用
    「潛能」吃掉(普通潛能要排在潛能之後)。簡繁變體一併認。"""
    clean = text or ''
    if '稀有潛能' in clean or '稀有潜能' in clean:
        return 0
    if '普通潛能' in clean or '普通潜能' in clean:
        return 2
    if '潛能' in clean or '潜能' in clean:
        return 1
    if '音符' in clean or '之音' in clean:
        return 3
    if any(keyword in clean for keyword in ('金幣', '星幣', '💰', '金錢', '輝光幣')):
        return 4
    return 5


def _score_event_row(text: str) -> tuple[int, int] | None:
    if _event_has_note_cost(text):
        return None
    if _event_is_free_random(text):
        return (0, _event_reward_rank(text))
    if not _event_has_money_cost(text):
        return (1, _event_reward_rank(text))
    return (2, _event_reward_rank(text))


def _event_gamble_gain(text: str) -> int:
    """機率/賭博事件選項的「純金錢獲得」金額(輝光幣);非金錢的「獲得1個潛能/X音符/
    生命值」一律不計(回 0)。一列可能有多個「獲得 N」,取最大。

    用於「選錢多的」決策(使用者 2026-06-14 拍板):只計**機率閘控的純金錢**獲得
    (「N% 機率獲得 ⟨數字⟩」),這是賭博事件的鑑別特徵:
      - 鏡類事件「機率獲得 1 個潛能 / 恢復生命」→ 非金錢,排除(回 0)。
      - 一般獎勵明細的平白「獲得 30」(無機率前綴)/ 消耗成本列「消耗…獲得 150」
        → 非機率金錢賭注,回 0 → 不誤判成金錢賭博(維持既有 E1 拒成本/拒音符邏輯)。
    一列可能有多個,取最大。
    """
    best = 0
    for m in re.finditer(r'(?:機率|机率)\s*(?:獲得|获得)\s*(\d+)', text or ''):
        tail = (text[m.end():m.end() + 3] or '')
        if any(u in tail for u in ('個', '个', '潛能', '潜能', '音符', '之音', '生命', '%')):
            continue
        best = max(best, int(m.group(1)))
    return best


def _event_strategy(ctx: 'BotContext') -> str:
    """事件選項策略(使用者 2026-06-14 拍板,預設激進)。GAME_MECHANICS E1。

    config event.strategy = 'aggressive'(激進,預設)| 'conservative'(保守)。
    回讀舊 key event.gamble_prefer == 'max_money' 當激進的別名(向後相容):
      舊版只有「賭博選錢多」一條規則,語意 = 激進(追最高報酬),故映射到 aggressive。
    """
    event_cfg = _event_cfg(ctx)
    strategy = str(event_cfg.get('strategy', '') or '').strip().lower()
    if strategy in ('aggressive', 'balanced', 'conservative'):
        return strategy
    # 向後相容:舊 key gamble_prefer=max_money → 激進。
    if str(event_cfg.get('gamble_prefer', '') or '').strip().lower() == 'max_money':
        return 'aggressive'
    return 'aggressive'


def _refuse_note_cost(ctx: 'BotContext') -> bool:
    """事件選項是否拒「消耗音符」(音符留協奏)。預設 True=現行。"""
    return bool(_event_cfg(ctx).get('refuse_note_cost', True))


def _aggressive_gamble_mode(ctx: 'BotContext') -> bool:
    """aggressive 策略對純金錢機率賭注是否走「選錢多的」。預設 True=現行。"""
    return bool(_event_cfg(ctx).get('aggressive_gamble_mode', True))


def _same_option_repeat_limit(ctx: 'BotContext') -> int:
    """同選項座標連續點幾次仍不推進就放棄(R3 防卡死)。預設 3=現行;壞值/<1 退 3。"""
    try:
        v = int(_event_cfg(ctx).get('same_option_repeat_limit', 3))
    except (TypeError, ValueError):
        return 3
    return v if v >= 1 else 3


def _event_option_groups(ctx: 'BotContext', rows: list[list[dict]]) -> list[tuple[int, str]]:
    """把選項列分組成「選項」:每個獎勵/成本明細列歸給其所屬「標題列」(走 y 序,
    記最近的標題列),回傳 [(標題列 index, 該選項合併文字), ...]。

    選項可能換行(標題在上、機率獎勵/成本在下;43px 分列門檻在邊界時合時分),
    故不能逐列當選項;標題列 = 含「非獎勵明細」文字的列(相信運氣 / 認真傾聽 /
    積極出手吧 等)。同源於既有 gamble 的 title 歸併邏輯,改成回傳合併文字供
    strategy 評分共用。
    """
    if not rows:
        return []
    order = sorted(range(len(rows)), key=lambda j: min(it['center_y'] for it in rows[j]))
    rows_combined = [_row_combined_text(rows[i]) for i in range(len(rows))]
    group_text: dict[int, list[str]] = {}
    group_order: list[int] = []
    cur_title: int | None = None
    for i in order:
        # 標題列 = 含「既非獎勵明細、亦非成本標籤」的文字列。成本標籤（消耗140C / 消耗音符）
        # 不算標題 —— 否則 OCR 漏掉真標題時殘列「消耗140C」會被誤當選項標題（L3 20260615_183043）。
        has_title = any(
            not _event_reward_detail_text(it['text'])
            and not _event_has_money_cost(it['text'])
            and not _event_has_note_cost(it['text'])
            and len(_normalize_text(it['text'])) >= 2
            for it in rows[i]
        )
        if has_title:
            cur_title = i
            if i not in group_text:
                group_text[i] = []
                group_order.append(i)
        target = cur_title if cur_title is not None else i
        if target not in group_text:
            group_text[target] = []
            group_order.append(target)
        group_text[target].append(rows_combined[i])
    return [(idx, ' '.join(group_text[idx])) for idx in group_order]


def _select_event_option(ctx: 'BotContext') -> tuple[int, int, str] | None:
    if getattr(ctx, 'ocr', None) is None or getattr(ctx, 'last_frame', None) is None:
        return None

    # exclude_top_ratio=0：上緣排除已由 _choice_panel_roi 的 y-start（0.30）負責;再疊 0.10
    # 會把最上一列選項標題（L3 20260615_183043 第一列 cy≈245）切掉 → 殘列誤判（見故障鏈）。
    option_candidates = _read_ocr_candidates(ctx, _choice_panel_roi(ctx), exclude_top_ratio=0.0)
    if not option_candidates:
        return None

    rows = _group_option_rows(ctx, option_candidates)
    question_candidates = _read_ocr_candidates(ctx, _question_panel_roi(ctx))
    full_question = ' '.join(item['text'] for item in question_candidates)

    # ── event_rules 比對(§4 config 化,優先於 quiz/升級/strategy)──────────────
    # 取畫面所有 OCR 文字(問題 + 選項皆計),用於 match_any 比對。
    all_ocr_texts = full_question + ' ' + ' '.join(
        item['text'] for row in rows for item in row
    )
    for rule in _load_event_rules():
        if not isinstance(rule, dict):
            continue
        rule_id = rule.get('id', '')
        match_any = rule.get('match_any', [])
        pick_any = rule.get('pick_any', [])
        if not isinstance(match_any, list) or not match_any:
            continue
        if not isinstance(pick_any, list) or not pick_any:
            continue
        # match_any: 畫面文字含任一關鍵字 → 命中
        if not any(kw and kw in all_ocr_texts for kw in match_any):
            continue
        # 在選項列中找含 pick_any 任一字的選項
        for row in rows:
            row_combined = _row_combined_text(row)
            if not any(pk and pk in row_combined for pk in pick_any):
                continue
            selected = _pick_event_click_target(ctx, row)
            if selected is None:
                continue
            reason_str = f'event_rule:{rule_id}'
            _record_ocr_trace(
                ctx,
                purpose='event_option',
                matched_text=selected['text'],
                bbox=selected['bbox'],
                confidence=selected['confidence'],
                center=(selected['center_x'], selected['center_y']),
                question=full_question,
                row_text=row_combined,
                rule=reason_str,
            )
            return selected['center_x'], selected['center_y'], reason_str
    # ── event_rules 比對結束 ─────────────────────────────────────────────────

    for question_keyword, answer_keyword in _iter_quiz_answers():
        if question_keyword and question_keyword in full_question:
            for row in rows:
                for item in row:
                    if answer_keyword and answer_keyword in item['text']:
                        _record_ocr_trace(
                            ctx,
                            purpose='event_option',
                            matched_text=item['text'],
                            bbox=item['bbox'],
                            confidence=item['confidence'],
                            center=(item['center_x'], item['center_y']),
                            question=full_question,
                            answer_keyword=answer_keyword,
                        )
                        return item['center_x'], item['center_y'], item['text']

    # 強化/升級事件（一次性，bundle 20260613_191440）針對性規則：
    # 此事件三個升級選項（變強!/變得更強!/變成最強的存在吧!）的獎勵都是「隨機獲得1個X潛能」，
    # generic 評分把它們都算成免費隨機 (0, rank0) 平手 → 挑 row index 最小 = 第一列「變強!」，
    # 但使用者定案要選稀有潛能（變成最強的存在吧!）。偵測到此事件 marker 時，直接挑
    # 「合併文字含『稀有』」那一列；找不到才落回 generic 評分（不動 generic，魔鏡類 E2 事件不受影響）。
    rows_combined = [_row_combined_text(row) for row in rows]
    is_upgrade_event = any(
        any(marker in combined for marker in signatures.UPGRADE_EVENT_MARKER_TOKENS)
        for combined in rows_combined
    )
    if is_upgrade_event:
        for row, combined in zip(rows, rows_combined):
            if any(token in combined for token in signatures.UPGRADE_EVENT_RARE_REWARD_TOKENS):
                selected = _pick_event_click_target(ctx, row)
                if selected is None:
                    continue  # 該列無可點標題（只有成本/獎勵明細）→ 換下一個含稀有的列。
                _record_ocr_trace(
                    ctx,
                    purpose='event_option',
                    matched_text=selected['text'],
                    bbox=selected['bbox'],
                    confidence=selected['confidence'],
                    center=(selected['center_x'], selected['center_y']),
                    question=full_question,
                    row_text=combined,
                    rule='upgrade_event_rare',
                )
                return selected['center_x'], selected['center_y'], selected['text']

    # 事件選項策略(使用者 2026-06-14 拍板,預設激進)。GAME_MECHANICS E1。
    # 先把選項列分組成「選項」(標題+其換行的成本/獎勵明細),再依 strategy 評分挑列。
    strategy = _event_strategy(ctx)
    groups = _event_option_groups(ctx, rows)

    # 排除純 NPC 敘述台詞組(既無成本明細、亦無獎勵明細)—— 它們不是可選項。
    # (L3 20260615_191325:對話事件 NPC 台詞「我迷路了…」被納入候選 → generic 同分時
    #  order 最小的台詞列勝 → 點台詞 roi 不變卡死。)quiz/upgrade 已在上游先處理、提早
    # return,不會走到這。fallback:若全部都是台詞(無真選項)則不過濾,保留下游
    # _pick_event_click_target / 末段 reversed(rows) 守門,避免正常事件被砍成 0 選項。
    real_groups = [g for g in groups if not _event_is_npc_dialogue(g[1])]
    if real_groups:
        groups = real_groups

    # 機率/純金錢賭注(「N% 機率獲得 ⟨純金錢⟩」):激進沿用「選錢多的」(獲得金額最高)。
    # 只在金錢賭博(_event_gamble_gain>0)時生效;鏡類事件(機率獲得潛能/生命,非金錢)回 0
    # → 不觸發,落回下方 generic 評分。memory event-gamble-prefer-most-money。
    if strategy == 'aggressive' and _aggressive_gamble_mode(ctx):
        title_gain: dict[int, int] = {}
        for idx, combined in groups:
            gain = _event_gamble_gain(combined)
            if gain > 0:
                title_gain[idx] = max(title_gain.get(idx, 0), gain)
        if title_gain and any(v > 0 for v in title_gain.values()):
            # 依金額高→低逐個試;選第一個「有可點標題」的（無標題者跳過,不點成本/獎勵）。
            group_text_by_idx = {g[0]: g[1] for g in groups}
            for best_idx in sorted(title_gain, key=lambda i: (title_gain[i], -i), reverse=True):
                selected = _pick_event_click_target(ctx, rows[best_idx])
                if selected is None:
                    continue
                _record_ocr_trace(
                    ctx,
                    purpose='event_option',
                    matched_text=selected['text'],
                    bbox=selected['bbox'],
                    confidence=selected['confidence'],
                    center=(selected['center_x'], selected['center_y']),
                    question=full_question,
                    row_text=group_text_by_idx.get(best_idx, ''),
                    rule='gamble_max_money',
                    gain=title_gain[best_idx],
                )
                return selected['center_x'], selected['center_y'], selected['text']

    # 依 strategy 對「選項組」評分挑報酬最好的:
    #   激進 = 追最高報酬、接受風險:在所有「非消耗音符」選項中挑報酬最好(接受消耗
    #          金錢、接受機率損失)。音符要留給協奏故仍拒消耗音符。
    #   保守 = 絕不冒損失:排除「機率損失/失去金錢」「機率損失/失去生命」「消耗金錢」
    #          「消耗音符」,在剩下(無下行、保證/免費)選項中挑報酬最好。
    # 報酬好壞 = _event_reward_rank(稀有潛能>潛能>普通潛能>音符>金錢),數字小=好。
    scored_groups: list[tuple[int, int, int, str]] = []  # (rank, order_idx, row_idx, combined)
    for order_idx, (row_idx, combined) in enumerate(groups):
        if _refuse_note_cost(ctx) and _event_has_note_cost(combined):
            continue  # 兩種策略皆拒消耗音符（refuse_note_cost 可關，預設 True）。
        if strategy == 'conservative' and (
            _event_has_money_loss(combined)
            or _event_has_hp_loss(combined)
            or _event_has_money_cost(combined)
        ):
            continue  # 保守:任何金錢/生命下行 or 消耗金錢的選項皆排除。
        if strategy == 'balanced' and (
            _event_has_money_loss(combined)
            or _event_has_hp_loss(combined)
        ):
            continue  # balanced:拒機率損失（金錢/生命），但接受確定消耗金錢換確定報酬。
        scored_groups.append((_event_reward_rank(combined), order_idx, row_idx, combined))

    # 依 (rank, order) 由好到差逐個試;跳過「無可點標題」的選項組（只有成本/獎勵明細,
    # _pick_event_click_target 回 None）→ 換報酬次佳且有標題的。不點成本/獎勵標籤（卡死主因）。
    for _rank, _order, best_row, combined in sorted(scored_groups, key=lambda item: (item[0], item[1])):
        selected = _pick_event_click_target(ctx, rows[best_row])
        if selected is None:
            continue
        _record_ocr_trace(
            ctx,
            purpose='event_option',
            matched_text=selected['text'],
            bbox=selected['bbox'],
            confidence=selected['confidence'],
            center=(selected['center_x'], selected['center_y']),
            question=full_question,
            row_text=combined,
            rule=f'strategy_{strategy}',
            reward_rank=_rank,
            fallback=False,
        )
        return selected['center_x'], selected['center_y'], selected['text']

    # 最後防線:由下往上找第一個「有可點標題」的列（迴避/保底列通常在最下且有標題）。
    # 仍不點成本/獎勵明細 —— 全無標題就回 None,交還上層（settle 重判 / R3 放棄,不空轉重點）。
    for row in reversed(rows):
        selected = _pick_event_click_target(ctx, row)
        if selected is None:
            continue
        combined = _row_combined_text(row)
        _record_ocr_trace(
            ctx,
            purpose='event_option',
            matched_text=selected['text'],
            bbox=selected['bbox'],
            confidence=selected['confidence'],
            center=(selected['center_x'], selected['center_y']),
            question=full_question,
            row_text=combined,
            fallback=True,
            fallback_reason='last_row_guardrail',
        )
        return selected['center_x'], selected['center_y'], selected['text']

    return None


def _select_shop_choice_option(
    ctx: 'BotContext',
    keywords: list[str] | None,
    trace_mode: str,
) -> tuple[int, int, str] | None:
    """挑選商店三選項的點擊目標；找不到任何 OCR 候選時回 None（Phase 1.3，不再回備援列座標）。"""
    option_candidates = _read_ocr_candidates(ctx, _shop_choice_panel_roi(ctx), exclude_top_ratio=0.10)
    rows = _group_option_rows(ctx, option_candidates)
    selected = _pick_option_from_rows(ctx, rows, keywords=keywords)
    if selected is None:
        return None
    _record_ocr_trace(
        ctx,
        purpose='shop_choice_option',
        matched_text=selected['text'],
        bbox=selected['bbox'],
        confidence=selected['confidence'],
        center=(selected['center_x'], selected['center_y']),
        mode=trace_mode,
    )
    return selected['center_x'], selected['center_y'], selected['text']


def _do_upgrade(ctx: 'BotContext', times: int = 1) -> None:
    for _ in range(times):
        ctx.pending_card_count = 2
        options = _extract_slot_options(ctx)
        if not options:
            ctx.pending_card_count = None
            return
        choice = ctx.engine.decide(options)
        if choice is None:
            ctx.pending_card_count = None
            return
        x, y = choice.position
        _click_with_trace(ctx, source='upgrade_card', x=x, y=y, option_name=choice.name, level=choice.level)
        _record_selection_if_needed(ctx, choice)
        if _recapture_then_click_take_button(ctx, selected_x=x, sleep=0.5) and not getattr(choice, 'is_pink', False):
            _increment_card_counter(ctx, choice.level, source='upgrade_card', option_name=choice.name)
        ctx.pending_card_count = None



def _money_hud_roi(ctx: 'BotContext') -> tuple[int, int, int, int]:
    """金錢餘額所在的右上角 HUD 條（比例座標）。

    語料勘查（tests/replays/ocr_cache/shop__*.json，1280x720）：餘額數字固定在
    x∈[1203,1239]、y∈[21,41]，金幣圖示緊鄰其左（icon_money 多尺度命中中心約 x=1164）。
    ROI 取畫面頂部 ~9% 高、右側 ~28% 寬，足以涵蓋圖示+數字，又排除貨架商品的價格數字
    （位於 y≈250–480 的格子內，不會落入此頂部窄帶）。
    """
    return (
        int(ctx.frame_w * 0.72),
        0,
        int(ctx.frame_w * 0.28),
        max(1, int(ctx.frame_h * 0.09)),
    )


def _read_money_from_roi(ctx: 'BotContext', roi: tuple[int, int, int, int]) -> int:
    """在指定 ROI 內讀出最大的純數字（餘額；忽略夾雜文字的雜訊）。"""
    if getattr(ctx, 'ocr', None) is None or getattr(ctx, 'last_frame', None) is None:
        return 0
    best = 0
    try:
        results = ctx.ocr.read_text(ctx.last_frame, roi=roi)
    except Exception:
        return 0
    for text, _conf, _bbox in results:
        clean = (text or '').strip()
        # 只接受「純數字（可帶千分位逗號）」，排除商品名/單位夾雜的字串。
        if not re.fullmatch(r'\d[\d,]*', clean):
            continue
        value = _ocr_read_number(clean)
        if value > best:
            best = value
    return best


def _read_money_via_icon(ctx: 'BotContext') -> int:
    """讀取商店/HUD 右上角的金錢餘額（Phase 2.1，REPAIR_PLAN §4 2.1 / GAME_MECHANICS C7）。

    舊版為死碼（`return ctx.current_money` 自我循環，恆 0）。現以固定 HUD ROI 讀數：

    1. 若 icon_money 模板在當前 frame 被穩定命中（found=True），取圖示右側緊鄰區域；
       語料上 production 單尺度 matcher 多半 found=False，故此為加分項而非必要條件。
    2. 否則退回固定右上 HUD ROI（`_money_hud_roi`）。
    兩種情況都用 OCR 取數字。讀不到時回傳 0（呼叫端視為未知餘額）。
    """
    if getattr(ctx, 'ocr', None) is None or getattr(ctx, 'last_frame', None) is None:
        return int(getattr(ctx, 'current_money', 0) or 0)

    matcher = getattr(ctx, 'matcher', None)
    if matcher is not None:
        try:
            res = matcher.match(ctx.last_frame, 'icon_money')
        except KeyError:
            res = None
        if res is not None and getattr(res, 'found', False):
            rect = getattr(res, 'rect', ()) or ()
            if len(rect) == 4:
                icon_right = rect[2]
                icon_cy = (rect[1] + rect[3]) // 2
                icon_h = max(1, rect[3] - rect[1])
            else:
                icon_right = int(getattr(res, 'center_x', 0))
                icon_cy = int(getattr(res, 'center_y', 0))
                icon_h = max(1, int(ctx.frame_h * 0.04))
            # 數字緊貼圖示右側：以圖示右緣為起點往右取一段窄帶。
            roi = (
                icon_right,
                max(0, icon_cy - icon_h),
                int(ctx.frame_w * 0.12),
                icon_h * 2,
            )
            value = _read_money_from_roi(ctx, roi)
            if value > 0:
                _record_ocr_trace(ctx, purpose='money', matched_text=str(value), value=value, source='icon_money')
                return value

    roi = _money_hud_roi(ctx)
    value = _read_money_from_roi(ctx, roi)
    if value > 0:
        _record_ocr_trace(ctx, purpose='money', matched_text=str(value), value=value, source='hud_roi')
    return value



def handle_home(ctx: 'BotContext') -> str | None:
    logger.info('[HOME] advance to STATE_LOBBY')
    return 'STATE_LOBBY'



def handle_lobby(ctx: 'BotContext') -> str | None:
    _update_frame_size(ctx)
    # 剛處理完一輪結算(儲存/丟棄)回到大廳/開始畫面 → 此時才計入該輪(延後計數,見 handle_result):
    # 確保儲存/丟棄流程已完整跑完才算一輪 + 觸發 max_runs,不會在丟棄中途就停。
    if getattr(ctx, '_result_outcome_pending', False):
        ctx.run_count = getattr(ctx, 'run_count', 0) + 1
        if getattr(ctx, '_result_keep', True):
            ctx.success_count = getattr(ctx, 'success_count', 0) + 1
        ctx._result_outcome_pending = False
        logger.info('[LOBBY] round complete -> run %s, success %s',
                    ctx.run_count, getattr(ctx, 'success_count', 0))
    if getattr(ctx, 'run_count', 0) >= getattr(ctx, 'max_runs', 1):
        ctx.running = False
        return None
    # 新一輪開始：清除上一輪結算處理的暫存旗標（HOME 不一定會經過,大廳是可靠的起點）。
    _reset_result_round_state(ctx)
    logger.info(f'[LOBBY] start run {getattr(ctx, "run_count", 0) + 1}')
    actions.click_verified(
        ctx,
        actions.TextTarget(signatures.FAST_BATTLE_BUTTON_TOKENS),
        expect=actions.ExpectRoiChange(),
        timeout=1.6,
        source='lobby_start',
    )
    return 'STATE_FORMATION'



def handle_formation(ctx: 'BotContext') -> str | None:
    _update_frame_size(ctx)
    logger.info('[FORMATION] click next')
    actions.click_verified(
        ctx,
        actions.TextTarget(signatures.NEXT_STEP_BUTTON_TOKENS),
        expect=actions.ExpectRoiChange(),
        timeout=1.6,
        source='formation_next',
    )
    return 'STATE_PREPARE'



def handle_prepare(ctx: 'BotContext') -> str | None:
    _update_frame_size(ctx)
    parsed_target_notes = _load_prepare_target_notes(ctx)
    parsed_current_notes = _load_prepare_current_notes(ctx)
    ctx.target_notes = parsed_target_notes or getattr(ctx, 'target_notes', {}) or {}
    ctx.current_notes = parsed_current_notes or getattr(ctx, 'current_notes', {}) or {}
    logger.info('[PREPARE] target_notes=%s current_notes=%s', ctx.target_notes, ctx.current_notes)
    logger.info('[PREPARE] advance to battle')
    actions.click_verified(
        ctx,
        actions.TextTarget(signatures.PREPARE_START_BUTTON_TOKENS),
        expect=actions.ExpectRoiChange(),
        timeout=1.6,
        source='prepare_start',
    )
    return 'STATE_FAST_BATTLE'



def handle_fast_battle(ctx: 'BotContext') -> str | None:
    logger.info('[FAST_BATTLE] waiting for state change')
    return None



def handle_tap_continue(ctx: 'BotContext') -> str | None:
    _update_frame_size(ctx)
    x, y = _safe_blank_point(ctx)
    _click_with_trace(ctx, source='tap_continue', x=x, y=y)
    return None



def handle_note_acquired(ctx: 'BotContext') -> str | None:
    _update_frame_size(ctx)
    # GAME_MECHANICS D3(2026-06-14 拍板):音符總數一律由本畫面**覆蓋**讀取,不累加
    # (shop 端已停止 += )。
    #   1. 變化列(例「幸運之音 6→9」)= 權威值:該畫面明確標出的本次變動後總量。
    #   2. 頂列圖示+總量 = best-effort 重新同步全部音符(類似金錢固定 HUD 覆蓋);
    #      模板不可靠時回 {} → 自動退回只用變化列,不弄壞既有行為。
    # 合併順序:先鋪頂列(resync 全部),再以變化列覆蓋 → 變化列永遠勝過較不可靠的頂列。
    change_row = _extract_note_updates(ctx)
    top_row = _read_note_totals_via_icons(ctx)
    updates: dict[str, int] = {}
    updates.update(top_row)
    updates.update(change_row)
    if updates:
        if not getattr(ctx, 'current_notes', None):
            ctx.current_notes = {}
        ctx.current_notes.update(updates)
        logger.info(
            '[NOTE] updated current_notes = %s (change_row=%s, top_row=%s)',
            updates, change_row, top_row,
        )
    x, y = _safe_blank_point(ctx, variant='note')
    _click_with_trace(
        ctx,
        source='note_acquired_ack',
        x=x,
        y=y,
        notes_updated=updates,
    )
    return None



def _take_single_upgrade_card(ctx: 'BotContext', choice) -> str | None:
    """免費強化「選擇一張」置中單卡:無 reroll/略過鈕,直接拿下唯一卡。

    序列與多卡成功路徑(handle_potential_select 點卡→重拍→拿走→計數)完全一致,
    只是跳過 decide/reroll —— 該畫面沒有 reroll 鈕,進 reroll 會找不到鈕 → 0 點擊卡死。
    保留 R3:唯一槽完全無 OCR 證據(name 以 option_ 開頭)→ 不點、回 None。
    """
    ctx.pending_card_count = None
    if str(getattr(choice, 'name', '')).startswith('option_'):
        logger.info('[POTENTIAL] single-card upgrade: no OCR evidence; skip without clicking (R3)')
        return None
    logger.info('[POTENTIAL] single-card upgrade: take the only card「%s」(+%s)',
                choice.name, getattr(choice, 'level', None))
    x, y = choice.position
    _click_with_trace(ctx, source='upgrade_card', x=x, y=y, option_name=choice.name, level=choice.level)
    _record_selection_if_needed(ctx, choice)
    if _recapture_then_click_take_button(ctx, selected_x=x, sleep=0.5) and not getattr(choice, 'is_pink', False):
        _increment_card_counter(ctx, choice.level, source='upgrade_card', option_name=choice.name)
    return None


def _reroll_potential_cards(ctx: 'BotContext') -> None:
    """重抽潛能卡：按 Q 鍵（遊戲熱鍵「Q 更新」，使用者實機畫面）。

    右下角 reroll 鈕是圓形 🔄 icon（無文字、旁標花費「40」），底部熱鍵列才標
    「Q 更新」（實機 last_frame，session 20260613_223142）—— 原本走
    REROLL_BUTTON_TOKENS 文字點擊永遠抓不到 icon 文字 → target_not_found → 卡死。
    改送 Q 鍵（input.press_key，與離場 ESC 同機制）。Q 不是點擊 → 不灌水 click_count。
    無限連抽由 decision_engine recommendation_badge 的 reroll 上限 + fallback 取卡兜底
    （永不卡死）。後備：舊環境無鍵盤能力 → 退回文字點擊（找不到 icon 文字仍 R3 不盲點）。
    """
    inp = getattr(ctx, 'input', None)
    press = getattr(inp, 'press_key', None)
    if callable(press):
        logger.info('[POTENTIAL] reroll via Q key')
        press('q')
        _mark_reroll_executed(ctx)
        time.sleep(0.5)   # 讓畫面切換；下一輪 FSM 重判新卡組
        return
    # 後備：舊環境無鍵盤能力 → 文字點擊（找不到 icon 文字仍 R3 不盲點右下角）。
    if actions.click_verified(
        ctx,
        actions.TextTarget(tuple(signatures.REROLL_BUTTON_TOKENS)),
        expect=actions.ExpectRoiChange(),
        timeout=1.8,
        source='reroll',
    ):
        _mark_reroll_executed(ctx)
        return
    logger.info('[POTENTIAL] reroll: no keyboard capability and reroll text not found; skip (R3)')


def _mark_reroll_executed(ctx: 'BotContext') -> None:
    """記一次 reroll 執行（業務計數，供卡死偵測辨識原地推進）。

    reroll 走 Q 鍵不產生 click、不換 state，外部 watchdog 又拿不到 OCR 文字集合 →
    原本看不到連抽推進 → 30s 誤殺（session 20260613_225241）。納入 progress_counters
    後每次 reroll = 計數變化 = 進度。連抽上限由 decision engine（max_reroll）兜底。
    """
    ctx.reroll_count = int(getattr(ctx, 'reroll_count', 0) or 0) + 1


def handle_potential_select(ctx: 'BotContext') -> str | None:
    _update_frame_size(ctx)
    if _screen_has_event_choice(ctx):
        logger.info('[POTENTIAL] event choice screen detected; hand off to STATE_EVENT')
        ctx.current_state = 'STATE_EVENT'
        return 'STATE_EVENT'

    if _screen_has_shop_purchase_modal(ctx):
        logger.info('[POTENTIAL] shop purchase modal detected; hand off to STATE_SHOP')
        ctx.current_state = 'STATE_SHOP'
        return 'STATE_SHOP'

    if _screen_has_shop_screen(ctx):
        logger.info('[POTENTIAL] shop screen detected; hand off to STATE_SHOP')
        ctx.current_state = 'STATE_SHOP'
        return 'STATE_SHOP'

    options = _extract_slot_options(ctx)
    if not options:
        ctx.pending_card_count = None
        return None
    # 單卡強制免費強化「選擇一張潛能卡片強化吧!」= 置中單卡、無 reroll/略過鈕。
    # 直接拿下唯一卡(不進 decide/reroll —— 否則 reroll 在無 reroll 鈕的畫面找不到鈕
    # → 0 點擊卡死,session 20260613_201218)。
    if len(options) == 1:
        return _take_single_upgrade_card(ctx, options[0])
    logger.info('[POTENTIAL] extracted options: %s', [
        {'name': opt.name, 'level': opt.level, 'position': opt.position}
        for opt in options
    ])
    preview = None
    preview_fn = getattr(ctx.engine, 'preview_decision', None)
    if callable(preview_fn):
        preview = preview_fn(options)
    should_retry = preview is None if callable(preview_fn) else any(
        getattr(opt, 'name', '').startswith('option_') for opt in options
    )
    if should_retry:
        logger.info('[POTENTIAL] first scan produced no viable choice, settle and retry once')
        _settle_and_refresh(ctx, delay=0.85)
        options = _extract_slot_options(ctx)
        logger.info('[POTENTIAL] retry extracted options: %s', [
            {'name': opt.name, 'level': opt.level, 'position': opt.position}
            for opt in options
        ])

    # Phase 1.3（R3）：卡槽完全沒有 OCR 證據（全為 option_N 佔位名）時，
    # 視同「找不到目標」——不點卡、不點拿走，留待下一輪重判。
    if not options or all(str(getattr(opt, 'name', '')).startswith('option_') for opt in options):
        logger.info('[POTENTIAL] no OCR evidence in any slot; skip without clicking (R3)')
        ctx.pending_card_count = None
        return None

    choice = ctx.engine.decide(options)
    ctx.pending_card_count = None
    if choice is None:
        logger.info('[POTENTIAL] decision=reroll')
        _reroll_potential_cards(ctx)
        return None
    x, y = choice.position
    _click_with_trace(ctx, source='potential_card', x=x, y=y, option_name=choice.name, level=choice.level)
    _record_selection_if_needed(ctx, choice)
    if _recapture_then_click_take_button(ctx, selected_x=x, sleep=0.5) and not getattr(choice, 'is_pink', False):
        _increment_card_counter(ctx, choice.level, source='potential_card', option_name=choice.name)
    return None



def handle_event(ctx: 'BotContext') -> str | None:
    _update_frame_size(ctx)
    selected = _select_event_option(ctx)
    if selected is None:
        logger.info('[EVENT] first scan produced no option, settle and retry once')
        _settle_and_refresh(ctx, delay=0.80)
        selected = _select_event_option(ctx)
    if selected is None:
        # Phase 1.3（R3）：找不到任何選項文字 → 不點固定列座標，留待下一輪重判。
        logger.info('[EVENT] no option text found after retry; skip without clicking (R3)')
        return None
    x, y, text = selected
    # 防無限重點(watchdog 盲區:持續點擊被當進度、外部 watchdog 不收屍 → L3 20260615_001510
    # 同選項點 18+ 次卡 1 分 40 秒)。同一選項座標連續點 3 次仍不推進(畫面 ROI 沒變)→ R3 放棄、
    # 不再點 → 讓 stuck detector 誠實收屍,不空轉。點到會推進的選項時座標會變 → 計數自然歸零。
    last_xy = getattr(ctx, '_event_last_option_xy', None)
    repeat = int(getattr(ctx, '_event_option_repeat', 0) or 0) + 1 if last_xy == (x, y) else 0
    ctx._event_last_option_xy = (x, y)
    ctx._event_option_repeat = repeat
    if repeat >= _same_option_repeat_limit(ctx):
        logger.info('[EVENT] same option (%s,%s)「%s」clicked %d×無推進 → 放棄不再點(R3)', x, y, text, repeat)
        return None
    actions.click_verified(
        ctx,
        actions.OcrPoint(x, y, matched_text=text),
        expect=actions.ExpectRoiChange(),
        timeout=1.6,
        source='event_option',
    )
    return None



def handle_leave_tower_confirm(ctx: 'BotContext') -> str | None:
    """「是否離開星塔?」離塔確認彈窗:點「確認」離塔(回大廳/結算)。

    不可點空白 —— 此彈窗點空白=取消 → 退回 SHOP_CHOICE 無限迴圈(session 20260614_004610)。
    主路徑:click_verified 點「確認」+ ExpectRoiChange(彈窗關閉/離塔→畫面必變)。
    後備:找不到「確認」文字 → 按 Space(畫面標「Space 確認」,input.press_key)。
    """
    _update_frame_size(ctx)
    if actions.click_verified(
        ctx,
        actions.TextTarget(tuple(signatures.CONFIRM_BUTTON_TOKENS)),
        expect=actions.ExpectRoiChange(),
        timeout=1.6,
        source='leave_tower_confirm',
    ):
        return None
    inp = getattr(ctx, 'input', None)
    press = getattr(inp, 'press_key', None)
    if callable(press):
        logger.info('[CONFIRM] confirm button not found; leave tower via Space key')
        press('space')
        time.sleep(0.5)
        return None
    logger.info('[CONFIRM] confirm button not found and no keyboard capability; skip (R3)')
    return None


def handle_discard_confirm(ctx: 'BotContext') -> str | None:
    """「解散目前紀錄將獲得以下道具 是否確定解散?」解散確認彈窗（結算不達標點垃圾桶後彈出）。

    類 handle_leave_tower_confirm（問句不同）:點「確認」解散該紀錄,不可點取消。
    主路徑:click_verified 點底部按鈕「確認」+ ExpectRoiChange（彈窗關閉/畫面必變）。
    後備:找不到「確認」文字 → 按 Space（彈窗另標 Space 確認,input.press_key）。
    解散後 →「獲得道具!」(STATE_TAP_CONTINUE 點空白,已支援) → 回大廳。

    定位限制（L3 20260616_190456）:「確認」TextTarget 限定在**底部按鈕 ROI**
    （_popup_bottom_button_roi,排除中段內文）。票券版彈窗內文第三行「是否確認解散?」
    含「確認」二字且在 y≈363 → 無 ROI 時 OCR 先命中內文 → 點內文無效 → 永不推進 →
    state_stuck_no_progress。限定底部 ROI 後只會命中底部按鈕「確認」(y≈507)。
    （handle_leave_tower_confirm 內文「是否離開星塔?」不含「確認」,故不需此限制、不受影響。）
    """
    _update_frame_size(ctx)
    if actions.click_verified(
        ctx,
        actions.TextTarget(
            tuple(signatures.CONFIRM_BUTTON_TOKENS),
            roi=_popup_bottom_button_roi(ctx),
        ),
        expect=actions.ExpectRoiChange(),
        timeout=1.6,
        source='discard_confirm',
    ):
        return None
    inp = getattr(ctx, 'input', None)
    press = getattr(inp, 'press_key', None)
    if callable(press):
        logger.info('[DISCARD] confirm button not found; confirm discard via Space key')
        press('space')
        time.sleep(0.5)
        return None
    logger.info('[DISCARD] confirm button not found and no keyboard capability; skip (R3)')
    return None


def _try_shop_upgrade(ctx: 'BotContext', visit_count: int) -> str:
    """嘗試在商店三選一點「強化」（C2/C3）。

    回傳:
      'clicked'        — 找到強化選項且（免費或強化價 < ceiling）→ 已點（verified 時記造訪 + 排卡）。
      'too_expensive'  — 強化價 >= price_ceiling（ceiling>0）→ 不點,呼叫端往下走。
      'not_found'      — 畫面找不到強化選項文字（R3 不盲點）→ 呼叫端結束本拍。
    """
    selected = _select_shop_choice_option(
        ctx,
        keywords=list(signatures.SHOP_CHOICE_UPGRADE_OPTION_TOKENS),
        trace_mode='upgrade',
    )
    if selected is None:
        logger.info('[SHOP_CHOICE] upgrade option text not found; skip without clicking (R3)')
        return 'not_found'
    x, y, matched_text = selected
    price = _parse_upgrade_price(matched_text)
    ceiling = int((_shop_cfg(ctx).get('upgrade', {}) or {}).get('price_ceiling', 540) or 0)
    # 免費（price==0）一律強化;price 未知（None）保守照常強化;ceiling=0 表示不限。
    if price is not None and price > 0 and ceiling > 0 and price >= ceiling:
        logger.info('[SHOP_CHOICE] upgrade price %s >= ceiling %s; skip upgrade (C3)', price, ceiling)
        return 'too_expensive'
    if actions.click_verified(
        ctx,
        actions.OcrPoint(x, y, matched_text=matched_text),
        expect=actions.ExpectRoiChange(),
        timeout=1.6,
        source='shop_choice_option',
    ):
        ctx.shop_visit_count = visit_count
        ctx.pending_card_count = 2
    return 'clicked'


def _try_enter_shop(ctx: 'BotContext', visit_count: int) -> str | None:
    """嘗試點「去商店購物」進商店（C3 由 _should_enter_shop 把關）。

    回傳 'STATE_SHOP'（進商店 verified 成功）/ None（不進、找不到選項、或 verified 失敗）。
    None 代表呼叫端應繼續往下嘗試其他選項或結束本拍。
    """
    current_money = _read_money_via_icon(ctx)
    ctx.current_money = current_money
    if not _should_enter_shop(current_money):
        return None
    selected = _select_shop_choice_option(
        ctx,
        keywords=list(signatures.SHOP_CHOICE_ENTER_OPTION_TOKENS),
        trace_mode='enter_shop',
    )
    if selected is None:
        logger.info('[SHOP_CHOICE] enter-shop option text not found; skip without clicking (R3)')
        return None
    x, y, matched_text = selected
    if actions.click_verified(
        ctx,
        actions.OcrPoint(x, y, matched_text=matched_text),
        expect=actions.ExpectStateIn(states=('STATE_SHOP',)),
        timeout=1.6,
        source='shop_choice_option',
    ):
        ctx.shop_visit_count = visit_count
        ctx.pending_card_count = None
        _clear_pending_shop_card(ctx)
        _shop_purchased_slots(ctx).clear()
        ctx.shop_spree_spent = 0   # 每進店重置本店狂買花費（max_spend 為單店上限，step9/D）
        return 'STATE_SHOP'
    return None


def handle_shop_choice(ctx: 'BotContext') -> str | None:
    _update_frame_size(ctx)
    if _screen_has_shop_purchase_modal(ctx):
        logger.info('[SHOP_CHOICE] shop purchase modal detected; hand off to STATE_SHOP')
        ctx.current_state = 'STATE_SHOP'
        return 'STATE_SHOP'

    if _screen_has_potential_select_text(ctx) or _screen_has_potential_select_visual(ctx):
        logger.info('[SHOP_CHOICE] potential select screen detected; hand off to STATE_POTENTIAL_SELECT')
        ctx.current_state = 'STATE_POTENTIAL_SELECT'
        return 'STATE_POTENTIAL_SELECT'

    # Phase 1.3（R3）：選項文字未命中 → 不點、不累計造訪次數，留待下一輪重判。
    visit_count = int(getattr(ctx, 'shop_visit_count', 0) or 0) + 1
    upgrade_times = _shop_upgrade_times(ctx, visit_count)
    emptied_streak = int(getattr(ctx, 'shop_emptied_streak', 0) or 0)
    shopped = bool(getattr(ctx, 'shop_done', False)) or emptied_streak >= 1
    order = _shop_order(ctx, visit_count)
    logger.info(
        '[SHOP_CHOICE] shop_done=%s emptied_streak=%s visit_count=%s upgrade_times=%s order=%s',
        getattr(ctx, 'shop_done', False), emptied_streak, visit_count, upgrade_times, order,
    )

    # order=shop_first：本商店尚未逛過（shopped=False）→ 先「去商店購物」買完再回來強化。
    # 逛完（shopped=True）後落到下方的強化分支。upgrade_first（預設）= 維持原行為：先強化。
    if order == 'shop_first' and not shopped:
        entered = _try_enter_shop(ctx, visit_count)
        if entered is not None:
            return entered

    # 強化分支（C2/C3）：本商店該強化的次數還沒用完才嘗試（upgrade_first 一進門就做;
    # shop_first 是逛完商店回來才做）。強化價 >= price_ceiling → 略過往下走（C3 真實價）。
    if upgrade_times > 0:
        outcome = _try_shop_upgrade(ctx, visit_count)
        if outcome != 'too_expensive':
            # 'clicked'（已點強化）或 'not_found'（R3 不點）都直接結束本拍。
            return None
        # 'too_expensive'：強化價過高 → 不強化，繼續往下（去商店 / 上樓）。

    # shop_done 信號 或 buy-all 已回報「商店空」（emptied_streak>=1）：最近一次進商店已
    # 買完/沒東西可買 → 直接選「不要了直接上樓」，略過「去商店購物」（否則
    # _should_enter_shop 在有餘額時回 True → 重進空商店 → 無限迴圈，
    # session 20260613_221637；shop_done 在拿免費強化的交錯流程會失效，故並用
    # emptied_streak 穩健兜底，session 20260613_232705）。免費強化仍可先做（此區塊在
    # upgrade 分支之後），用完才不再進商店。
    if shopped:
        selected = _select_shop_choice_option(
            ctx,
            keywords=list(signatures.SHOP_CHOICE_SKIP_OPTION_TOKENS),
            trace_mode='skip_shop_done',
        )
        if selected is None:
            logger.info('[SHOP_CHOICE] shop_done but skip option text not found; skip without clicking (R3)')
            return None
        x, y, matched_text = selected
        if actions.click_verified(
            ctx,
            actions.OcrPoint(x, y, matched_text=matched_text),
            expect=actions.ExpectRoiChange(),
            timeout=1.6,
            source='shop_choice_option',
        ):
            # 上樓 verified 成功（離開 SHOP_CHOICE，往新一層）→ 重置 shop_done 與
            # emptied_streak，讓下一層商店重新開始（可再進、可再買）。
            ctx.shop_visit_count = visit_count
            ctx.pending_card_count = None
            ctx.shop_done = False
            ctx.shop_emptied_streak = 0
        # verified 失敗 → 保持 shop_done / emptied_streak，下一輪重試上樓（不 fallback 去重進商店）。
        return None

    entered = _try_enter_shop(ctx, visit_count)
    if entered is not None:
        return entered

    selected = _select_shop_choice_option(
        ctx,
        keywords=list(signatures.SHOP_CHOICE_SKIP_OPTION_TOKENS),
        trace_mode='skip_shop',
    )
    if selected is None:
        logger.info('[SHOP_CHOICE] skip option text not found; skip without clicking (R3)')
        return None
    x, y, matched_text = selected
    if actions.click_verified(
        ctx,
        actions.OcrPoint(x, y, matched_text=matched_text),
        expect=actions.ExpectRoiChange(),
        timeout=1.6,
        source='shop_choice_option',
    ):
        ctx.shop_visit_count = visit_count
        ctx.pending_card_count = None
    return None



def _note_priority(ctx: 'BotContext') -> list:
    """音符購買優先序（GUI_DESIGN_SPEC §0b 第5點,shop.buy.note_priority）。

    回傳去空白後的音符名清單;預設空 / buy 非 dict / note_priority 非 list → []（現行,byte-identical）。
    """
    buy = _shop_cfg(ctx).get('buy', {}) or {}
    if not isinstance(buy, dict):
        return []
    pri = buy.get('note_priority', [])
    if not isinstance(pri, list):
        return []
    return [str(x).strip() for x in pri if str(x).strip()]


def _order_gaps_by_priority(note_gaps, note_priority: list) -> list:
    """依 note_priority 重排缺口音符的購買順序（§0b 第5點）。

    priority 命中（精確或子字串雙向）的音符按 priority 序排前;其餘缺口按原 dict 序接後;
    priority 含不在 gaps 的音符 → 忽略。priority 空時呼叫端走現行分支,本函式不被呼叫。
    """
    ordered: list = []
    for name in note_priority:
        for gap in note_gaps:
            if (name == gap or name in gap or gap in name) and gap not in ordered:
                ordered.append(gap)
    for gap in note_gaps:
        if gap not in ordered:
            ordered.append(gap)
    return ordered


def _try_buy_gap_note_at(ctx: 'BotContext', gap_note: str, text: str, conf, bbox, purchased_slots: set) -> bool:
    """在 (text,bbox) 處嘗試買缺口音符 gap_note。

    slot 已購 → False（呼叫端跳過該格）;買成功 → True（呼叫端 return None）。
    買邏輯（去重 + 點擊 + 確認 + D3 不累加）與原 handle_shop 內聯版逐行等價。
    """
    cx, cy = _ocr_center(bbox)
    slot_key = _shop_slot_key(cx, cy, ctx)
    if slot_key in purchased_slots:
        # 本店此格已買過 → 跳過,避免重複點同一音符 → 無進度卡死
        # (L3 20260614_213030:買缺口音符無去重 → 連點同張「強攻之音*15」12 次 state_stuck)。
        return False
    _record_ocr_trace(ctx, purpose='shop_gap_note', matched_text=text, bbox=bbox, confidence=round(conf, 4), center=(cx, cy), target_note=gap_note)
    actions.click_verified(
        ctx,
        actions.OcrPoint(cx, cy, matched_text=text),
        expect=actions.EXPECT_NONE,
        source='shop_gap_note',
    )
    # 比照買卡片:點了就標本店此格已購(沒彈窗也算,保險防重點)。
    purchased_slots.add(slot_key)
    _settle_and_refresh(ctx, delay=0.5)
    actions.click_verified(
        ctx,
        actions.TextTarget(('確認',)),
        expect=actions.ExpectRoiChange(),
        timeout=1.6,
        source='shop_gap_note_confirm',
    )
    # GAME_MECHANICS D3(2026-06-14 拍板):音符總數一律由「獲得音符」畫面
    # (STATE_NOTE_ACQUIRED)覆蓋讀取,shop 端**不累加**(買音符後遊戲必跳該畫面覆蓋新總量,
    # 此處再 += 會重複計)。故只觸發購買、不在 shop 端動 current_notes。
    logger.info('[SHOP] bought gap note %s; total will be overwritten by STATE_NOTE_ACQUIRED (D3)', gap_note)
    return True


def handle_shop(ctx: 'BotContext') -> str | None:
    _update_frame_size(ctx)
    if _screen_has_shop_purchase_modal(ctx):
        logger.info('[SHOP] purchase modal detected; confirming purchase')
        _confirm_shop_purchase_modal(ctx)
        return None

    # 買法策略（C6,config shop.buy.strategy）。strategy=all 或舊 shop_buy_all 旗標 →
    # 走 buy-all 隔離路徑（買全部可買商品各一次、去重、買完離開）。其餘走真經濟分派。
    strategy = _shop_buy_strategy(ctx)
    if strategy == 'all' or getattr(ctx, 'shop_buy_all', False):
        return _handle_shop_buy_all(ctx)

    pending_slot_key = getattr(ctx, 'pending_shop_card_slot_key', None)
    if pending_slot_key and _strategy_wants_cards(ctx):
        if _settle_and_refresh(ctx, delay=0.8) and _screen_has_shop_purchase_modal(ctx):
            logger.info('[SHOP] delayed purchase modal detected; confirming purchase')
            _confirm_shop_purchase_modal(ctx)
            return None
        logger.info('[SHOP] previous card click did not open purchase modal; skip slot %s', pending_slot_key)
        _shop_purchased_slots(ctx).add(str(pending_slot_key))
        _clear_pending_shop_card(ctx)
        return None

    if _screen_has_event_choice(ctx):
        logger.info('[SHOP] event choice screen detected; hand off to STATE_EVENT')
        return 'STATE_EVENT'

    current_money = _read_money_via_icon(ctx)
    ctx.current_money = current_money

    if _strategy_wants_cards(ctx):
        selected_card = _select_shop_card_to_buy(ctx)
        if selected_card is not None:
            cx, cy, level, text = selected_card
            # 商店還有可買卡片 → 清掉 emptied 信號（鏡像 _handle_shop_buy_all 1053-1054,
            # 防免費強化交錯流程殘留的 shop_done 誤觸 SHOP_CHOICE 提早上樓）。
            ctx.shop_done = False
            ctx.shop_emptied_streak = 0
            _record_ocr_trace(
                ctx,
                purpose='shop_card',
                matched_text=text,
                center=(cx, cy),
                level=level,
                current_total=getattr(ctx, 'card_counter_current_total', 0),
                target_total=getattr(ctx, 'card_counter_target_total', 0),
            )
            # expect=EXPECT_NONE：購買彈窗是否彈出由既有的跨輪詢 pending-slot
            # 機制驗證（沒出現會跳過該格位），重複點同一張卡有害。
            if actions.click_verified(
                ctx,
                actions.OcrPoint(cx, cy, matched_text=text),
                expect=actions.EXPECT_NONE,
                source='shop_card',
            ):
                ctx.pending_shop_card_level = level
                ctx.pending_shop_card_text = text
                ctx.pending_shop_card_slot_key = _shop_slot_key(cx, cy, ctx)
            return None

    # 以下「優惠商品」+「協奏缺口音符」= 真經濟的音符階段（cards_then_notes 達標後 /
    # notes_only 一律）。cards_only 達標後、all/strategy 不要音符時不進此階段。
    wants_notes = _strategy_wants_notes(ctx)

    if wants_notes and getattr(ctx, 'ocr', None) is not None and getattr(ctx, 'last_frame', None) is not None:
        for text, conf, bbox in ctx.ocr.read_text(ctx.last_frame):
            if _has_discount_keyword(text):
                cx, cy = _ocr_center(bbox)
                _record_ocr_trace(ctx, purpose='shop_discount', matched_text=text, bbox=bbox, confidence=round(conf, 4), center=(cx, cy))
                actions.click_verified(
                    ctx,
                    actions.OcrPoint(cx, cy, matched_text=text),
                    expect=actions.EXPECT_NONE,
                    source='shop_discount',
                )
                # 重拍後在新畫面上找「確認」；找不到就不點（R3），下一輪輪詢再處理彈窗。
                _settle_and_refresh(ctx, delay=0.5)
                actions.click_verified(
                    ctx,
                    actions.TextTarget(('確認',)),
                    expect=actions.ExpectRoiChange(),
                    timeout=1.6,
                    source='shop_discount_confirm',
                )
                return None

    note_gaps = _compute_note_gaps(getattr(ctx, 'target_notes', {}), getattr(ctx, 'current_notes', {}))
    if wants_notes and note_gaps and getattr(ctx, 'ocr', None) is not None and getattr(ctx, 'last_frame', None) is not None:
        purchased_slots = _shop_purchased_slots(ctx)
        note_priority = _note_priority(ctx)
        if note_priority:
            # 音符購買優先序（§0b 第5點）：依 priority 重排,優先買排前的音符
            # （外層音符、內層 OCR;買到 → return,該音符全格已購 → 換下個音符）。
            for gap_note in _order_gaps_by_priority(note_gaps, note_priority):
                for text, conf, bbox in ctx.ocr.read_text(ctx.last_frame):
                    if gap_note in text and _try_buy_gap_note_at(ctx, gap_note, text, conf, bbox, purchased_slots):
                        return None
        else:
            # 現行（byte-identical）：外層 OCR、內層 gap,買畫面第一個匹配的缺口音符商品。
            for text, conf, bbox in ctx.ocr.read_text(ctx.last_frame):
                for gap_note in note_gaps:
                    if gap_note in text:
                        if _try_buy_gap_note_at(ctx, gap_note, text, conf, bbox, purchased_slots):
                            return None
                        break

    # 達標後狂買特定音符(step9/D,opt-in)。預設關 → _try_note_spree 第一行零 OCR 退 False。
    if _card_target_met(ctx) and _try_note_spree(ctx):
        return None   # 狂買到一個,本拍結束;下一拍續買
    # （未啟用/沒買到 → fall-through 到既有刷新/離場兜底,byte-identical）

    max_refresh = getattr(ctx, 'config', {}).get('bot', {}).get('max_shop_refresh', 0)
    if getattr(ctx, 'shop_refresh_count', 0) < max_refresh and _refresh_trigger_allows(ctx):
        if _refresh_shop(ctx):
            ctx.shop_refresh_count += 1
            _clear_pending_shop_card(ctx)
            _shop_purchased_slots(ctx).clear()
            return None
        logger.info('[SHOP] refresh unavailable (no keyboard / text not found); fall through to leave (R3)')

    # 真經濟本店已無可買（買完 / 買不起 / 售完 / 達標後無缺口音符）→ 設 shop_done +
    # emptied_streak 兜底信號,讓 SHOP_CHOICE 選「直接上樓」而非重進空商店（修真經濟
    # 無限重進迴圈,L3 20260614_162359;buy-all 路徑 _handle_shop_buy_all 1080-1083 早有
    # 此信號,真經濟 handle_shop 漏 → 永遠 shop_done=False emptied=0 → 重進）。
    # 只在「跑到離場處」才設（買到貨會在上方提早 return,不會到這）→ 等同「本拍沒買到任何東西」。
    ctx.shop_done = True
    ctx.shop_emptied_streak = int(getattr(ctx, 'shop_emptied_streak', 0) or 0) + 1
    _clear_pending_shop_card(ctx)
    # 統一走 ESC 離場（遊戲就是 ESC；文字離場本來就不對）。無鍵盤能力時 _leave_shop 退回文字。
    return _leave_shop(ctx)



# ─────────────────────────────────────────────────────────────
# 結算畫面（STATE_RESULT）感知層（Phase 2.3，GAME_MECHANICS F1/F1b/F2/F2b）
# 實機 L1 語料 result__20260614_005348__last.png（1280x720,OCR cache 已驗）：
#   評分六角徽章「27」conf 0.998（左上角 x≈104,y≈80）；角色潛能總等級
#   右側「潛能收集」清單 風影「29」/夏花「14」conf 0.96+；「已鎖定」conf 0.835（左下）。
#   每角色 ⊕ 數字直讀不可靠（⊕ 符號干擾、字小,讀成 22/14/1）→ 評分為主依據,
#   潛能總等級為輔（best-effort,合計右欄純數字）。
# ─────────────────────────────────────────────────────────────


def _result_rating_roi(ctx: 'BotContext') -> tuple[int, int, int, int]:
    """評分（六角徽章）所在的左上窄帶。只取最左 ~13% 寬,排除「未命名紀錄」(x≈0.12 起)
    與「評分 7965」分數(x≈0.20 起);徽章內唯一純數字即評分。"""
    return (
        int(ctx.frame_w * 0.02),
        int(ctx.frame_h * 0.06),
        int(ctx.frame_w * 0.11),
        int(ctx.frame_h * 0.10),
    )


def _read_result_rating(ctx: 'BotContext') -> int:
    """讀結算評分（六角徽章,目前版本 max~33）。讀不到回 0（呼叫端視為未知）。"""
    if getattr(ctx, 'ocr', None) is None or getattr(ctx, 'last_frame', None) is None:
        return 0
    value = _read_money_from_roi(ctx, _result_rating_roi(ctx))
    if value > 0:
        _record_ocr_trace(ctx, purpose='result_rating', matched_text=str(value), value=value)
    return value


def _result_potential_total_roi(ctx: 'BotContext') -> tuple[int, int, int, int]:
    """右側「潛能收集」清單每角色 ⊕ 總等級數字欄（x≈0.91–0.99,縱跨清單）。"""
    return (
        int(ctx.frame_w * 0.91),
        int(ctx.frame_h * 0.12),
        int(ctx.frame_w * 0.08),
        int(ctx.frame_h * 0.68),
    )


def _read_result_potential_total(ctx: 'BotContext') -> int:
    """角色潛能總等級合計（對標 [[B6]] 78）。best-effort：合計右欄所有純數字。
    清單可捲動 / 第三角色數字可能漏讀 → 不作為主決策依據,僅記錄與輔助門檻。
    讀不到回 0。"""
    if getattr(ctx, 'ocr', None) is None or getattr(ctx, 'last_frame', None) is None:
        return 0
    rx, ry, rw, rh = _result_potential_total_roi(ctx)
    try:
        results = ctx.ocr.read_text(ctx.last_frame, roi=(rx, ry, rw, rh))
    except Exception:
        return 0
    total = 0
    for text, _conf, _bbox in results:
        clean = (text or '').strip()
        if re.fullmatch(r'\d{1,3}', clean):
            total += int(clean)
    if total > 0:
        _record_ocr_trace(ctx, purpose='result_potential_total', matched_text=str(total), value=total)
    return total


def _result_is_locked(ctx: 'BotContext') -> bool:
    """結算紀錄是否鎖定中（左下「已鎖定」;評分高會自動上鎖,丟棄前須先解鎖,F2b）。"""
    if getattr(ctx, 'ocr', None) is None or getattr(ctx, 'last_frame', None) is None:
        return False
    try:
        texts = ctx.ocr.read_text_simple(ctx.last_frame)
    except Exception:
        return False
    joined = ' '.join(t for t in texts if t)
    return signatures.text_has_any(joined, signatures.RESULT_LOCKED_TOKENS)


def handle_explore_complete(ctx: 'BotContext') -> str | None:
    _update_frame_size(ctx)
    actions.click_verified(
        ctx,
        actions.TextTarget(signatures.EXPLORE_COMPLETE_NEXT_TOKENS),
        expect=actions.ExpectRoiChange(),
        timeout=1.6,
        source='explore_complete_next',
    )
    return 'STATE_RESULT'



def _result_meets_target(ctx: 'BotContext', rating: int, potential_total: int) -> bool:
    """結算達標判斷（Phase 2.3,GAME_MECHANICS F1b/F2）。

    主依據=評分（六角徽章,OCR 最可靠）：rating >= rating_threshold。
    輔依據=角色潛能總等級合計（對標 [[B6]] 78,best-effort）：設 potential_total_threshold>0
    時與評分取 OR（任一達標即達標）。
    兩項都無法判（評分讀不到 + 未設潛能門檻或讀不到）→ 退回既有 required_potentials_satisfied()
    （production card_counter）—— 保守:永不因讀數失敗而誤判丟棄。

    require_all_secrets（GUI_DESIGN_SPEC §3.1,祕聞全解,預設 False）：開啟時對上述 base
    結果再加一道 AND 閘 —— 最終達標 = base AND ctx.current_notes_satisfied()（即使評分/潛能
    達標,協奏音符沒全達標也判不達標 → 走丟棄）。current_notes_satisfied 讀不到（舊測試 ctx
    無此方法）或 target_notes 為空（STATE_PREPARE 未讀）時 → 該方法回 False → 保守判不達標。
    """
    cfg = (getattr(ctx, 'config', {}) or {}).get('result', {}) or {}
    rating_thr = int(cfg.get('rating_threshold', 30) or 0)
    total_thr = int(cfg.get('potential_total_threshold', 0) or 0)
    met: bool | None = None
    if rating > 0 and rating_thr > 0:
        met = rating >= rating_thr
    if total_thr > 0 and potential_total > 0:
        total_met = potential_total >= total_thr
        met = total_met if met is None else (met or total_met)
    if met is None:
        satisfied = getattr(ctx, 'required_potentials_satisfied', None)
        base = bool(satisfied()) if callable(satisfied) else True
    else:
        base = met
    if bool(cfg.get('require_all_secrets', False)):
        notes_ok = getattr(ctx, 'current_notes_satisfied', None)
        base = base and (bool(notes_ok()) if callable(notes_ok) else False)
    return base


def _reset_result_round_state(ctx: 'BotContext') -> None:
    """清除每輪結算處理的暫存旗標（新一輪開始時呼叫:reset_round / handle_lobby）。"""
    ctx._result_accounted = False
    ctx._result_keep = True
    ctx._result_unlock_done = False
    ctx._result_outcome_pending = False
    ctx.result_rating = 0
    ctx.result_potential_total = 0


def handle_result(ctx: 'BotContext') -> str | None:
    """結算畫面（STATE_RESULT,塔頂探索完成後）：判達標 → 儲存 / 丟棄。

    使用者拍板（2026-06-14,取代舊「無條件點下一步」死碼）:
      達標 → 點「儲存紀錄」（達 max_runs 即停）;
      不達標 → 丟棄流程（F2b 鎖定陷阱）:① 若「已鎖定」先點解鎖 → ② 點垃圾桶 →
      STATE_DISCARD_CONFIRM「是否確定解散?」→ 點確認 →「獲得道具!」(STATE_TAP_CONTINUE
      點空白) → 回大廳。STATE_RESULT 為 detector-gated,本 handler 回傳值不影響轉移,
      下一拍由 detector 重判;故每步點一個動作後 return None,靠 detector 推進。
    """
    _update_frame_size(ctx)

    rating = _read_result_rating(ctx)
    potential_total = _read_result_potential_total(ctx)
    if rating > 0:
        ctx.result_rating = rating
    ctx.result_potential_total = potential_total

    # 結算決策（同一結算畫面多拍只判一次)。計數刻意**延後**到整輪(含儲存/丟棄)跑完
    # 回大廳時才計(handle_lobby 的 _result_outcome_pending)—— 否則在此就 run_count++ +
    # max_runs 到 → session 會在丟棄流程中途(剛解鎖)就停,紀錄沒真的丟掉
    # (session 20260614_140140:停在 STATE_RESULT、只解鎖未丟)。取代死碼 handle_settlement。
    if not getattr(ctx, '_result_accounted', False):
        keep = _result_meets_target(ctx, rating, potential_total)
        ctx._result_keep = keep
        ctx._result_accounted = True
        ctx._result_outcome_pending = True
        logger.info(
            '[RESULT] rating=%s potential_total=%s -> %s',
            rating, potential_total, '達標→儲存' if keep else '不達標→丟棄',
        )
    keep = bool(getattr(ctx, '_result_keep', True))

    if keep:
        # 達標 → 儲存紀錄(計數延後到回大廳)。
        actions.click_verified(
            ctx,
            actions.TextTarget(signatures.RESULT_SAVE_BUTTON_TOKENS),
            expect=actions.ExpectRoiChange(),
            timeout=1.6,
            source='result_save',
        )
        return None

    # 不達標 → 丟棄流程。① 鎖定陷阱:評分高會自動上鎖,先點「已鎖定」解鎖才能丟（F2b）。
    if _result_is_locked(ctx) and not getattr(ctx, '_result_unlock_done', False):
        if actions.click_verified(
            ctx,
            actions.TextTarget(signatures.RESULT_LOCKED_TOKENS),
            expect=actions.ExpectRoiChange(),
            timeout=1.6,
            source='result_unlock',
        ):
            ctx._result_unlock_done = True
        return None  # 下一拍重判（已變「未鎖定」）

    # ② 已解鎖 → 點垃圾桶（無文字 icon,白名單座標）→ 解散確認彈窗。
    actions.click_verified(
        ctx,
        actions.PointTarget('result_trash'),
        expect=actions.ExpectStateIn(states=('STATE_DISCARD_CONFIRM',)),
        timeout=1.6,
        source='result_trash',
    )
    return None



def handle_settlement(ctx: 'BotContext') -> str | None:
    _update_frame_size(ctx)
    ctx.run_count = getattr(ctx, 'run_count', 0) + 1
    potentials_ok = ctx.required_potentials_satisfied()
    notes_ok = ctx.current_notes_satisfied()
    if potentials_ok and notes_ok:
        ctx.success_count = getattr(ctx, 'success_count', 0) + 1
        logger.info('[Settlement] round met targets (potentials=%s notes=%s) -> success %s/%s',
                    potentials_ok, notes_ok, ctx.success_count, ctx.run_count)
    else:
        logger.info('[Settlement] round did NOT meet targets (potentials=%s notes=%s) -> not counted as success',
                    potentials_ok, notes_ok)
    actions.click_verified(
        ctx,
        actions.TextTarget(signatures.SETTLEMENT_RETURN_TOKENS),
        expect=actions.ExpectRoiChange(),
        timeout=1.6,
        source='settlement_return',
    )
    if ctx.run_count >= getattr(ctx, 'max_runs', 1):
        ctx.running = False
        return None
    return 'STATE_HOME'



def handle_reconnect(ctx: 'BotContext') -> str | None:
    _update_frame_size(ctx)
    actions.click_verified(
        ctx,
        actions.TextTarget(signatures.RECONNECT_BUTTON_TOKENS),
        expect=actions.ExpectRoiChange(),
        timeout=1.6,
        source='reconnect',
    )
    return 'STATE_HOME'
