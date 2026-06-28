"""
畫面簽名單一事實來源 (vision/signatures.py) — REPAIR_PLAN Phase 1.1

所有「畫面提示字 / 顏色錨點」只允許定義在本模組：
  - vision/state_detector.py 與 core/states.py 一律 import 本模組，
    不得再各自內嵌提示字常數（鐵則 2 的最終解）。
  - 每個狀態以一個或多個 ScreenSignature 描述；priority 數字小者先判
    （彈窗類 > 全頁類），對應 v1 detect() 的 if 優先序。

提示字 tokens 的可編輯來源（可維護性）：
  - 純資料的 token tuple 常數已外部化到 ``data/screen_tokens.yaml``，
    遊戲版本更新改字時非工程師可直接改 YAML、不必動本檔。
  - 本檔 import 時以 ``_load_screen_tokens()`` 讀那份 YAML，把每組載入成
    module-level tuple 常數；每個常數都保留一份硬寫 fallback —— YAML 不存在/
    壞掉/缺某組時退回 fallback 並記 warning，絕不讓 import 失敗或行為改變。
  - 衍生/組合常數、色錨、文字判定函式、簽名表、評分邏輯仍定義於本檔。

評分函式輸入 = OCR 文字清單（可含座標）+ frame；輸出 = 各 signature 得分。
得分規則：
  - negative_keywords 任一命中 → 0 分（直接否決）。
  - keywords_all 為 AND-of-OR：每個子群至少命中一字，否則 0 分。
  - color_anchor 設定時必須命中，否則 0 分。
  - keywords_any：keywords_all 為空時為必要條件（至少命中一字）；
    keywords_all 非空時為加分項。
  - 通過全部必要條件 → 基礎分 1.0 + keywords_any 命中數加分（每個 +0.05，上限 +0.2）。
  - 得分 >= min_score 才算命中（預設 1.0）。
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────
# 提示字 tokens 外部資料載入（data/screen_tokens.yaml）
# ─────────────────────────────────────────────────────────────
#
# 純資料的 token tuple 常數定義於 data/screen_tokens.yaml（好編輯、改字不動
# .py）。本檔 import 時讀一次該檔，把各組載入成 module-level 常數。每個常數
# 都附硬寫 fallback：YAML 缺檔/壞掉/缺某組/某組空/型別不對 → 退回 fallback
# 並記 warning，import 絕不失敗、行為絕不改變。

_DEFAULT_SCREEN_TOKENS_PATH = Path(__file__).resolve().parents[1] / "data" / "screen_tokens.yaml"


def _resolve_screen_tokens_path() -> Path:
    """決定 YAML 路徑；環境變數 SCREEN_TOKENS_PATH 可覆寫（測試用）。"""
    override = os.environ.get("SCREEN_TOKENS_PATH")
    return Path(override) if override else _DEFAULT_SCREEN_TOKENS_PATH


# module-level 路徑（供診斷與測試觀察；實際載入以 _resolve_screen_tokens_path() 為準）。
_SCREEN_TOKENS_PATH = _resolve_screen_tokens_path()


def _load_screen_tokens() -> dict[str, list]:
    """讀 data/screen_tokens.yaml；任何失敗回空 dict（→ 各常數用 fallback）。"""
    path = _resolve_screen_tokens_path()
    try:
        import yaml

        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        if not isinstance(data, dict):
            logger.warning(
                "screen_tokens.yaml 內容非 mapping（%r），全部 token 退回硬寫 fallback",
                type(data).__name__,
            )
            return {}
        return data
    except FileNotFoundError:
        logger.warning(
            "找不到 %s，全部 token 退回硬寫 fallback", path,
        )
        return {}
    except Exception as exc:  # YAML 壞掉 / 編碼錯 / PyYAML 缺失 等
        logger.warning(
            "載入 %s 失敗（%s），全部 token 退回硬寫 fallback", path, exc,
        )
        return {}


_TOKENS: dict[str, list] = _load_screen_tokens()


def _tokens(key: str, fallback: tuple[str, ...]) -> tuple[str, ...]:
    """取 YAML 中 key 對應的 token 組；缺/空/型別不對則用 fallback 並記 warning。"""
    raw = _TOKENS.get(key)
    if raw is None:
        if _TOKENS:  # YAML 有載入成功但缺這組（整檔缺失時已在載入處警告，不重複刷）
            logger.warning("screen_tokens.yaml 缺少「%s」組，退回硬寫 fallback", key)
        return fallback
    if not isinstance(raw, (list, tuple)) or not raw or not all(isinstance(x, str) for x in raw):
        logger.warning("screen_tokens.yaml「%s」組內容無效，退回硬寫 fallback", key)
        return fallback
    return tuple(raw)

# ─────────────────────────────────────────────────────────────
# 正規化
# ─────────────────────────────────────────────────────────────

_NORMALIZE_RE = re.compile(r"[\s　:：,，.。!！?？>\-▶]+")


def normalize_text(text: str) -> str:
    """去除空白與常見標點並轉小寫，供關鍵字比對使用。"""
    return _NORMALIZE_RE.sub("", text or "").lower()


# ─────────────────────────────────────────────────────────────
# 資料結構
# ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ColorAnchor:
    """顏色錨點：在 frame 的比例區域內，HSV 範圍像素占比達標即命中。"""

    roi: tuple[float, float, float, float]  # (x0, y0, x1, y1) 比例座標
    hsv_lower: tuple[int, int, int]
    hsv_upper: tuple[int, int, int]
    min_ratio: float


@dataclass(frozen=True)
class ScreenSignature:
    state: str
    priority: int                                       # 數字小者先判（彈窗類 > 全頁類）
    keywords_all: tuple[tuple[str, ...], ...] = ()      # AND-of-OR
    keywords_any: tuple[str, ...] = ()                  # 任一命中（all 為空時為必要條件）
    negative_keywords: tuple[str, ...] = ()             # 命中即否決
    roi: tuple[float, float, float, float] | None = None  # 只在此比例區域內找關鍵字
    color_anchor: ColorAnchor | None = None
    template_anchor: str | None = None                  # assets/templates 固定 UI 元素
    min_score: float = 1.0
    name: str = ""


# ─────────────────────────────────────────────────────────────
# 提示字資料（全專案唯一定義處）
# ─────────────────────────────────────────────────────────────

# 純資料 token 常數：值來自 data/screen_tokens.yaml，下方 tuple 為硬寫 fallback
# （YAML 缺失/壞掉時用此值，鎖定原行為；tests/test_screen_tokens.py 斷言兩者相等）。
EVENT_CHOICE_HINTS: tuple[str, ...] = _tokens("event_choice_hints", (
    "我獨愛",
    "我独爱",
    "我想聽",
    "我想听",
    "我聽所有",
    "我听所有",
    "傾聽所有",       # 實機 OCR 變體：「我傾聽所有」(event__20260531_195825)
    "隨機音符",
    "随机音符",
    # 單獨「隨機」= 隨機/賭博事件通用獎勵特徵(「或生命值隨機發生變化」「隨機獲得」「隨機抽取」等)。
    # 語料證實只在事件畫面出現(7/7 event_*,零 shop/potential/lobby 誤判;使用者拍板「用獎勵判斷」)。
    # L3 20260614_231656:綠髮 NPC「神奇遊戲機」交易事件(玩→隨機獲得金錢/生命值變化)漏判 →
    # potential_select_visual 弱命中誤判選卡 → 拿走 not_found → UNKNOWN×4 卡死。
    "隨機",
    "随机",
    "命運之鏡",
    "命运之镜",
    "踏入命運",
    "踏入命运",
    "魔鏡",
    "魔镜",
    "好危險",
    "好危险",
    "生命力",
    "籌碼",
    "筹码",
    "分我一些",
    "試試",
    "试试",
    "隨機獲得",
    "随机获得",
    # 強化/升級事件（一次性，bundle 20260613_191440）：NPC「你還可以變得更強大…」
    # + 四選項。選項標題不在上面任何字 → event_choice(30) 漏判，畫面又含「潛能」
    # + 底部青色行動按鈕 → potential_select_visual(50) 誤命中（STATE_EVENT<->
    # STATE_POTENTIAL_SELECT 飄移卡死）。挑高鑑別度標題字（不用太通用的「變強」）：
    "變成最強",
    "变成最强",
    "變得更強",
    "变得更强",
    "已經夠強",
    "已经够强",
    "你還可以變得更強",
    "你还可以变得更强",
    # 機率/賭博類事件(session 20260614_131730「理性的決斷」三選一,選項含
    # 「N% 機率獲得/失去 ⟨金額⟩」)。提示字原不在表內 → 判 UNKNOWN 卡死。
    # 「機率獲得」「機率失去/損失」是賭博事件通用高鑑別字(語料其他畫面皆無),
    # 任一命中即判 STATE_EVENT;決策由 handle_event 的金錢賭博規則處理(選錢多的)。
    "機率獲得",
    "机率获得",
    "機率失去",
    "机率失去",
    "機率損失",
    "机率损失",
    "理性的決斷",
    # 升級機 NPC 事件(花錢換潛能,2026-06-14)：「想用你的運氣獲得一些好處嗎?」
    # 三選一(謹慎出手/積極出手/還是算了)。選項標題不在上面任何字 → 漏判;
    # 問句「想用你的運氣」「獲得一些好處」是此事件高鑑別字(+簡體變體),任一命中即判
    # STATE_EVENT;決策由 handle_event 依 event.strategy 處理(激進→積極出手/保守→還是算了)。
    "想用你的運氣",
    "想用你的运气",
    "獲得一些好處",
    "获得一些好处",
))

# 強化/升級事件的「升級選項」判別字（core/states.py handle_event 針對性挑稀有潛能用；
# 提示字單一來源：states.py 一律 import，不得本地內嵌）。
UPGRADE_EVENT_MARKER_TOKENS: tuple[str, ...] = _tokens("upgrade_event_marker_tokens", (
    "變成最強",
    "变成最强",
    "變得更強",
    "变得更强",
    "已經夠強",
    "已经够强",
))
# 升級事件中「稀有潛能」獎勵列的判別字（= 變成最強的存在吧! 那一列）。
UPGRADE_EVENT_RARE_REWARD_TOKENS: tuple[str, ...] = _tokens("upgrade_event_rare_reward_tokens", (
    "稀有",
))

POTENTIAL_SELECT_HINTS: tuple[str, ...] = _tokens("potential_select_hints", (
    "拿走",
    "未收錄",
    "未收录",
    "更新",
    "reroll",
    "?輯粥",          # 實機 OCR 誤讀變體
    # 免費強化的升級卡選擇畫面標題「選擇一張潛能卡片強化吧!」(session 20260614_133706)。
    # 含「強化」→ 原被 shop_choice_keywords(priority 130)誤判 STATE_SHOP_CHOICE,handle_shop_choice
    # 把卡片當「強化」選項點 → 卡死。「選擇一張」「潛能卡片」是此畫面高鑑別字(商店無),
    # 放進 potential_select_text(priority 40 < 130)導向 STATE_POTENTIAL_SELECT(handle_potential_select
    # 已支援「選擇一張」多卡)。與 UPGRADE_SINGLE_CARD_HEADER_TOKENS 同源。
    "選擇一張",
    "选择一张",
    "潛能卡片",
    "潜能卡片",
))

POTENTIAL_CARD_HINTS: tuple[str, ...] = _tokens("potential_card_hints", (
    "等級",
    "等级",
    "蝑",             # 實機 OCR 誤讀變體
    "推薦",
    "推荐",
    "潛能",
    "潜能",
))

RECOMMEND_TEXT_TOKENS: tuple[str, ...] = _tokens("recommend_text_tokens", ("推薦", "推荐"))

# 視覺小說式「按 Space 推進」提示字（NPC 對話 / 一般點空白推進畫面共用）。
# 注意：此字是跨畫面通用 UI 提示（商店貨架、購買彈窗、事件、潛能選卡畫面
# 底部都有「Space」），單獨命中無鑑別度 —— 必須搭配 negative_keywords
# 排除所有「有自己內容」的已知畫面後，才足以判定「無內容的純對話/推進畫面」。
SPACE_CONTINUE_HINT_TOKENS: tuple[str, ...] = _tokens("space_continue_hint_tokens", ("Space",))

# 「啟動協奏技能!」音符/協奏技能啟動畫面（置中單卡 +底部「點擊空白處繼續」，
# session 20260613_225933）。「協奏技能」單字過於通用 —— 商店貨架描述「相關協奏
# 技能」亦含 → 該畫面被 shop_keywords(priority 140) 誤判 STATE_SHOP，bot 跑 buy-all
# + ESC 離場全無效 → 卡死。故以完整 header「啟動協奏技能」+ footer「點擊空白處繼續」
# 為錨，且簽名 priority 須**優先於** shop_keywords，導向 STATE_TAP_CONTINUE（點空白）。
CONCERT_SKILL_ACTIVATE_TOKENS: tuple[str, ...] = _tokens(
    "concert_skill_activate_tokens", ("啟動協奏技能", "點擊空白處繼續")
)

# 離塔後「默契提升」(好感度/羈絆)獎勵畫面（rapport_boost 簽名 → STATE_TAP_CONTINUE）。
# session 20260614_173024：bot 推到最終房間→點「離開星塔」(conf 0.98)後出現此全新畫面
# （角色 + 右側獎勵圖示），無簽名 → 先被 potential_select_visual(priority 50)弱色錨誤判,
# 再 STATE_UNKNOWN×4 → state_unknown_persistent 卡死。使用者證實「點空白推進」→ 導向
# STATE_TAP_CONTINUE,priority 須優先於 potential_select_visual。
RAPPORT_BOOST_TOKENS: tuple[str, ...] = _tokens(
    "rapport_boost_tokens", ("默契提升", "默契提昇", "默契值提升")
)

# 潛能選擇全頁關鍵字（v1 STATE_KEYWORDS["STATE_POTENTIAL_SELECT"] 的去重版）
POTENTIAL_SELECT_KEYWORDS: tuple[str, ...] = _tokens("potential_select_keywords", (
    "潛能選擇",
    "選擇潛能",
    "請選擇一個潛能",
    "重新抽取",
    "隊伍等級提升",
))

# 商店字群（彈窗 / 貨架共用）
SHOP_BUY_TOKENS: tuple[str, ...] = _tokens("shop_buy_tokens", ("購買", "购买"))
SHOP_PRICE_TOKENS: tuple[str, ...] = _tokens("shop_price_tokens", ("單價", "单价"))
SHOP_ITEM_TOKENS: tuple[str, ...] = _tokens("shop_item_tokens", ("潛能特飲", "潜能特饮", "特飲", "特饮"))
SHOP_CONTROL_TOKENS: tuple[str, ...] = _tokens(
    "shop_control_tokens", ("剩餘次", "剩余次", "背包", "返回", "查看", "更新")
)
# 離開商店的按鈕文字（handle_shop / _leave_shop 點擊用，單一來源）。
# 「返回」在實機右下角常被 EasyOCR 糊成「坦回」(conf≈0.21，session 20260613_212202)，
# 因此一併收錄該糊字變體，避免找不到離場鈕 → 0 點擊卡死。
SHOP_LEAVE_TOKENS: tuple[str, ...] = _tokens(
    "shop_leave_tokens", ("離開", "返回", "坦回", "離開商店", "離開星塔")
)
SHOP_NOTE_GOODS_TOKENS: tuple[str, ...] = _tokens("shop_note_goods_tokens", (
    "體力之音",
    "体力之音",
    "專注之音",
    "专注之音",
    "風之音",
    "风之音",
))
# 商店刷新貨架 = 快捷鍵 Q（使用者實機確認 2026-06-14,與選卡 reroll 同熱鍵）。
# 主路徑按 Q（_refresh_shop）；下列文字僅「舊環境無鍵盤能力」的後備點擊用（實機刷新鈕
# 多半是無文字 icon，找不到文字仍 R3 不盲點）。單一來源:states.py 一律 import。
SHOP_REFRESH_TOKENS: tuple[str, ...] = _tokens("shop_refresh_tokens", ("刷新", "刷新商店"))
# 商店「優惠/折扣」標記字（原價劃掉/紅圈優惠）。音符階段掃此字點購（notes_only 行為）;
# step7/c 起亦供「買卡優惠優先」用（core/states._has_discount_keyword / 選卡排序）。
# 單一來源:states.py 一律 import,不得本地內嵌裸字面。
SHOP_DISCOUNT_TOKENS: tuple[str, ...] = _tokens("shop_discount_tokens", ("優惠", "折扣"))

# ─────────────────────────────────────────────────────────────
# 按鈕 / 選項文字（core/states.py 點擊與畫面輔助判定用；
# Phase 1.3 click_verified 的 target 來源。畫面文字一律定義於本檔。）
# ─────────────────────────────────────────────────────────────

TAKE_BUTTON_TOKENS: tuple[str, ...] = _tokens("take_button_tokens", ("拿走",))
REROLL_BUTTON_TOKENS: tuple[str, ...] = _tokens(
    "reroll_button_tokens", ("Reroll", "重新抽取", "重新選取", "重抽", "更新")
)
UPGRADE_HEADER_TOKENS: tuple[str, ...] = _tokens("upgrade_header_tokens", ("強化", "强化", "升級", "升级"))
# 免費強化帶出的「選擇一張潛能卡片強化吧!」= 置中單卡(非 2/3 卡選單);
# 「一張」(量詞=卡片)用以與一般 3 卡選單「請選擇一個潛能」(量詞=個)區分。
UPGRADE_SINGLE_CARD_HEADER_TOKENS: tuple[str, ...] = _tokens(
    "upgrade_single_card_header_tokens", ("選擇一張", "选择一张")
)
SHOP_CHOICE_UPGRADE_OPTION_TOKENS: tuple[str, ...] = _tokens(
    "shop_choice_upgrade_option_tokens", ("強化", "免費")
)
# 強化選項文字內的「免費」標記（解析強化單價用；命中即視為免費=單價 0,一律強化）。
# 例「強化（免費）」「強化（免費6）」。單一來源：states.py 一律 import。
SHOP_UPGRADE_FREE_TOKENS: tuple[str, ...] = _tokens("shop_upgrade_free_tokens", ("免費", "免费"))
# 進商店(逛/購物)。最終房間用「去商店逛逛」(session 20260614_003320),一般房間
# 用「去商店購物」,一併收錄。
SHOP_CHOICE_ENTER_OPTION_TOKENS: tuple[str, ...] = _tokens("shop_choice_enter_option_tokens", (
    "去商店購物", "進入商店", "商店購物", "去商店逛逛", "逛逛",
))
# 離場/略過 = 不在此商店多做,往下一步(上樓 / 離塔)。最終房間的離場是「離開星塔」
# (離塔=完成本輪→回大廳/結算,session 20260614_003320),與一般房間「不要了直接上樓」
# 同屬 skip 語意,一併收錄。
SHOP_CHOICE_SKIP_OPTION_TOKENS: tuple[str, ...] = _tokens("shop_choice_skip_option_tokens", (
    "不要了", "直接上樓", "略過", "跳過", "離開星塔", "離塔", "離開",
))
FAST_BATTLE_BUTTON_TOKENS: tuple[str, ...] = _tokens("fast_battle_button_tokens", ("快速戰鬥",))
NEXT_STEP_BUTTON_TOKENS: tuple[str, ...] = _tokens("next_step_button_tokens", ("下一步",))
PREPARE_START_BUTTON_TOKENS: tuple[str, ...] = _tokens(
    "prepare_start_button_tokens", ("開始探索", "開始戰鬥", "戰鬥開始", "快速戰鬥", "確定")
)
RECONNECT_BUTTON_TOKENS: tuple[str, ...] = _tokens("reconnect_button_tokens", ("重新連線", "重試", "確認"))
EXPLORE_COMPLETE_NEXT_TOKENS: tuple[str, ...] = NEXT_STEP_BUTTON_TOKENS + ("繼續",)
RESULT_NEXT_TOKENS: tuple[str, ...] = NEXT_STEP_BUTTON_TOKENS + ("結算",)
SETTLEMENT_RETURN_TOKENS: tuple[str, ...] = _tokens("settlement_return_tokens", ("返回", "確認"))
# 「是否離開星塔?」離塔確認彈窗（點離開星塔後彈出,取消/確認,session 20260614_004610）。
# 「是否離開星塔」是彈窗問句（與 SHOP_CHOICE 的「離開星塔」按鈕區別在「是否」），
# 「尚有未使用」「輝光幣」為彈窗特有提示,任一命中即判離塔確認彈窗。
LEAVE_TOWER_CONFIRM_TOKENS: tuple[str, ...] = _tokens(
    "leave_tower_confirm_tokens", ("是否離開星塔", "尚有未使用", "輝光幣")
)
# 確認鈕文字（離塔確認彈窗「確認」；畫面另標快捷鍵 Space）。
CONFIRM_BUTTON_TOKENS: tuple[str, ...] = _tokens("confirm_button_tokens", ("確認", "确认"))

# ── 結算畫面（STATE_RESULT）按鈕 / 狀態文字（GAME_MECHANICS F1/F1b/F2/F2b）──
# 實機 L1 語料 result__20260614_005348__last.png（1280x720,OCR cache 已驗）。
# 結算/紀錄畫面高鑑別度標記字（result_keywords 簽名偵測 + potential_select_visual 否決,
# 單一來源）。此畫面「潛能收集/收藏」分頁含「潛能」+ 底部彩色圖示格偶命中 potential_select_visual
# 弱青色色錨 → 被該簽名(priority 50)搶在 result_keywords(110)前誤判 STATE_POTENTIAL_SELECT 卡死
# （L3 20260614_220443:底部青色 ratio 0.039 剛過 0.035 門檻；改色組的 result__20260614_005348
# ratio 0.024 則不命中 → 同畫面時對時錯）。這些字僅結算畫面有（語料全掃只 result__* 命中）,
# 真潛能選卡畫面絕無 → 作 potential_select_visual 的否決字,讓結算畫面確定落到 result_keywords。
RESULT_SCREEN_MARKER_TOKENS: tuple[str, ...] = _tokens("result_screen_marker_tokens", (
    "儲存紀錄",
    "未命名紀錄",
    "探索結束",
    "評分",
    "祕紋技能",
))
# 右下「儲存紀錄」鈕（達標 → 儲存）。
RESULT_SAVE_BUTTON_TOKENS: tuple[str, ...] = _tokens(
    "result_save_button_tokens", ("儲存紀錄", "保存紀錄", "儲存", "保存")
)
# 左下鎖定狀態切換鈕：「已鎖定」=鎖定中（評分高會自動上鎖）→ 點它解鎖才能丟棄；
# 顯示「未鎖定」則已可丟棄。丟棄前的鎖定陷阱見 GAME_MECHANICS F2b。
RESULT_LOCKED_TOKENS: tuple[str, ...] = _tokens("result_locked_tokens", ("已鎖定", "已锁定"))
RESULT_UNLOCKED_TOKENS: tuple[str, ...] = _tokens("result_unlocked_tokens", ("未鎖定", "未锁定"))
# 「解散目前紀錄將獲得以下道具 是否確定解散?」解散確認彈窗（點垃圾桶後彈出,取消/確認）。
# 類 leave_tower_confirm,問句不同。任一命中即判解散確認彈窗(待 L3 實機校準問句字)。
DISCARD_CONFIRM_TOKENS: tuple[str, ...] = _tokens(
    "discard_confirm_tokens", (
        "是否確定解散", "確定解散", "解散目前紀錄", "確定解散嗎",
        # 票券(征途票根)積到上限才出的解散確認彈窗,問句是「是否確認解散」(確認≠確定;使用者
        # 實機截圖 L3 20260615_001511)。未達上限是普通版「確定」、達上限是票券版「確認」,兩收。
        "是否確認解散", "確認解散",
    )
)

# ─────────────────────────────────────────────────────────────
# 顏色錨點
# ─────────────────────────────────────────────────────────────

# 底部青色行動按鈕（潛能選卡畫面的「確認/拿走」列）— 與 v1
# _has_bottom_teal_action_button 完全等價的參數。
BOTTOM_TEAL_ACTION_BUTTON = ColorAnchor(
    roi=(0.28, 0.72, 0.82, 0.92),
    hsv_lower=(75, 60, 120),
    hsv_upper=(105, 255, 255),
    min_ratio=0.035,
)


def color_anchor_hit(frame: np.ndarray | None, anchor: ColorAnchor) -> bool:
    if frame is None or getattr(frame, "size", 0) == 0:
        return False
    try:
        import cv2

        h, w = frame.shape[:2]
        x0, y0, x1, y1 = anchor.roi
        crop = frame[int(h * y0):int(h * y1), int(w * x0):int(w * x1)]
        if crop.size == 0:
            return False
        hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
        mask = cv2.inRange(hsv, anchor.hsv_lower, anchor.hsv_upper)
        return float(mask.mean() / 255.0) >= anchor.min_ratio
    except Exception:
        return False


def has_bottom_teal_action_button(frame: np.ndarray | None) -> bool:
    return color_anchor_hit(frame, BOTTOM_TEAL_ACTION_BUTTON)


def recommendation_badge_color_hit(frame, roi: tuple[int, int, int, int]) -> bool:
    """卡片右上角紅色「推薦」徽章的顏色判定（roi 為像素座標的卡片框）。

    自 core/states.py 移入（Phase 1.1），邏輯不變。
    """
    if frame is None or getattr(frame, "size", 0) == 0:
        return False

    try:
        import cv2

        x, y, w, h = roi
        frame_h, frame_w = frame.shape[:2]
        bx0 = max(0, min(frame_w, x + int(w * 0.42)))
        bx1 = max(0, min(frame_w, x + int(w * 1.02)))
        by0 = max(0, min(frame_h, y + int(h * 0.08)))
        by1 = max(0, min(frame_h, y + int(h * 0.32)))
        if bx1 <= bx0 or by1 <= by0:
            return False

        crop = frame[by0:by1, bx0:bx1]
        if crop.size == 0:
            return False

        hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
        red_a = cv2.inRange(hsv, (0, 80, 120), (10, 255, 255))
        red_b = cv2.inRange(hsv, (165, 80, 120), (179, 255, 255))
        mask = red_a | red_b

        component_count, _labels, stats, _centroids = cv2.connectedComponentsWithStats(mask, 8)
        crop_area = crop.shape[0] * crop.shape[1]
        min_area = max(80, int(crop_area * 0.004))
        max_area = max(min_area + 1, int(crop_area * 0.18))
        min_w = max(12, int(w * 0.025))
        max_w = max(min_w + 1, int(w * 0.30))
        min_h = max(10, int(h * 0.015))
        max_h = max(min_h + 1, int(h * 0.09))

        for idx in range(1, component_count):
            area = int(stats[idx, cv2.CC_STAT_AREA])
            comp_w = int(stats[idx, cv2.CC_STAT_WIDTH])
            comp_h = int(stats[idx, cv2.CC_STAT_HEIGHT])
            if not (min_area <= area <= max_area):
                continue
            if not (min_w <= comp_w <= max_w and min_h <= comp_h <= max_h):
                continue
            aspect = comp_w / max(1, comp_h)
            if 0.70 <= aspect <= 4.50:
                return True
    except Exception:
        return False
    return False


# ─────────────────────────────────────────────────────────────
# 文字判定函式（states.py / state_detector.py 共用）
# ─────────────────────────────────────────────────────────────


def text_has_any(text: str, keywords: tuple[str, ...]) -> bool:
    joined = normalize_text(text)
    if not joined:
        return False
    return any(normalize_text(kw) in joined for kw in keywords)


def event_choice_text(text: str) -> bool:
    return text_has_any(text, EVENT_CHOICE_HINTS)


def potential_select_text(text: str) -> bool:
    return text_has_any(text, POTENTIAL_SELECT_HINTS)


def potential_card_text(text: str) -> bool:
    return text_has_any(text, POTENTIAL_CARD_HINTS)


def is_recommendation_text(text: str) -> bool:
    return text_has_any(text, RECOMMEND_TEXT_TOKENS)


_RECOMMEND_LEVEL_RE = re.compile(r'推[薦荐]\s*(\d+)')   # 容忍「級」OCR 誤讀，只要「推薦/推荐」後跟數字


def parse_recommendation_target_level(text: str) -> int:
    """從推薦徽章文字解析「推薦N級」的目標等級 N（讀不到→0）。"""
    if not text:
        return 0
    m = _RECOMMEND_LEVEL_RE.search(str(text))
    return int(m.group(1)) if m else 0


def shop_purchase_modal_text(text: str) -> bool:
    """購買彈窗：(購買 且 (單價 或 特飲)) 或 (特飲 且 單價)。"""
    joined = normalize_text(text)
    has_buy = any(normalize_text(kw) in joined for kw in SHOP_BUY_TOKENS)
    has_price = any(normalize_text(kw) in joined for kw in SHOP_PRICE_TOKENS)
    has_item = any(normalize_text(kw) in joined for kw in SHOP_ITEM_TOKENS)
    return (has_buy and (has_price or has_item)) or (has_item and has_price)


def shop_screen_text(text: str) -> bool:
    """商店貨架：特飲商品 且 (操作列 或 音符商品)。"""
    joined = normalize_text(text)
    has_item = any(normalize_text(kw) in joined for kw in SHOP_ITEM_TOKENS)
    has_controls = any(normalize_text(kw) in joined for kw in SHOP_CONTROL_TOKENS)
    has_note_goods = any(normalize_text(kw) in joined for kw in SHOP_NOTE_GOODS_TOKENS)
    return has_item and (has_controls or has_note_goods)


# ─────────────────────────────────────────────────────────────
# NPC 對話 / 點空白推進畫面（STATE_TAP_CONTINUE）的否決字池
# ─────────────────────────────────────────────────────────────
#
# 設計（session 20260612_234815，L3 Phase1 驗收第二輪）：
#   商店點商店卡後可能插入「商店 NPC 招呼對話」（視覺小說式：名牌+對白氣泡
#   +右下『Space』提示）。對白內容隨 NPC / 場合變化，不可入簽名；NPC 名牌字
#   亦會變。唯一穩定 token 是右下『Space』提示，但「Space」是跨畫面通用 UI 字
#   （語料 36 張中 18 張含 Space：商店貨架、購買彈窗、事件、潛能選卡皆有）。
#   因此採「Space + 所有已知畫面高鑑別詞的否決」組合：只有在畫面**不含任何
#   已知畫面的內容字**時才命中（= 純對話 / 純推進畫面）。
#
# 已驗證（diagnostics OCR cache 全掃）：18 張含 Space 的語料中，除本畫面外
#   17 張全部命中下列否決字（每張 4–8 個），唯獨 NPC 對話畫面零否決 → 只它命中。
#   防呆重點：6 張 shop 語料（含購買彈窗 shop__20260531_201841）皆含
#   特飲/購買/單價/星塔商店/協奏技能/體力之音等字 → 必被否決，不會被誤判成
#   STATE_TAP_CONTINUE。
NPC_DIALOGUE_NEGATIVE_TOKENS: tuple[str, ...] = (
    # 商店（貨架 + 購買彈窗 + 全頁關鍵字）
    *SHOP_ITEM_TOKENS,
    *SHOP_BUY_TOKENS,
    *SHOP_PRICE_TOKENS,
    *SHOP_NOTE_GOODS_TOKENS,
    "星塔商店", "協奏技能", "相關協奏技能", "優惠", "刷新次數",
    "重置商店", "離開商店", "剩餘次", "剩余次", "背包",
    # 事件
    *EVENT_CHOICE_HINTS,
    "隨機事件", "選擇一個選項", "事件發生", "發生變化",
    # 潛能選卡
    *POTENTIAL_SELECT_HINTS,
    *POTENTIAL_CARD_HINTS,
    *POTENTIAL_SELECT_KEYWORDS,
    # 大廳 / 結算 / 探索完成 / 商店三選一
    "星塔探索", "難度選擇", "開始探索",
    "儲存紀錄", "未命名紀錄", "探索結束", "評分",
    "探索完成", "收集潛能",
    "遇到了星塔商店", "去商店購物", "強化", "直接上樓",
    # 音符 / 戰鬥 / 編隊 / 秘紋 / 斷線
    "獲得音符", "音符數量",
    "戰鬥中", "自動戰鬥", "跳過戰鬥",
    "編隊", "下一步", "自動編隊", "開始戰鬥",
    "秘紋組合", "啟動條件", "主位秘紋", "副位秘紋", "總計",
    "重新連線", "連接中斷", "網路異常",
)


# ─────────────────────────────────────────────────────────────
# 簽名表（priority 小者先判；對應 v1 detect() 的 if 優先序）
# ─────────────────────────────────────────────────────────────

SIGNATURES: tuple[ScreenSignature, ...] = (
    # ── 彈窗 / 高鑑別度文字（v1 的專用 if 分支）──
    ScreenSignature(
        name="shop_purchase_modal_buy",
        state="STATE_SHOP",
        priority=10,
        keywords_all=(SHOP_BUY_TOKENS, SHOP_PRICE_TOKENS + SHOP_ITEM_TOKENS),
    ),
    ScreenSignature(
        name="shop_purchase_modal_item_price",
        state="STATE_SHOP",
        priority=11,
        keywords_all=(SHOP_ITEM_TOKENS, SHOP_PRICE_TOKENS),
    ),
    # 「是否離開星塔?」離塔確認彈窗（點離開星塔後彈出）。含『Space』提示故會被
    # npc_dialogue_space_continue（15）誤判為點空白畫面 → 點空白=取消 → 退回 SHOP_CHOICE
    # 迴圈（session 20260614_004610）。priority 12 < 15,優先判為離塔確認 → 點「確認」。
    ScreenSignature(
        name="leave_tower_confirm",
        state="STATE_LEAVE_TOWER_CONFIRM",
        priority=12,
        keywords_any=LEAVE_TOWER_CONFIRM_TOKENS,
    ),
    # 「是否確定解散?」解散確認彈窗（結算畫面點垃圾桶後彈出,取消/確認；GAME_MECHANICS F2b）。
    # 彈窗蓋在結算畫面上,背景「儲存紀錄/評分」仍會被 OCR 讀到 → priority 須優先於
    # result_keywords(110);彈窗含『Space』提示亦須優先於 npc_dialogue(15)。priority 13。
    ScreenSignature(
        name="discard_confirm",
        state="STATE_DISCARD_CONFIRM",
        priority=13,
        keywords_any=DISCARD_CONFIRM_TOKENS,
    ),
    # NPC 對話 / 純點空白推進畫面（視覺小說式，右下『Space』提示）。
    # 此對話可能蓋在商店之上 → priority 須比 shop 全頁類（shop_screen=20、
    # shop_keywords=140）優先，故置於購買彈窗(10/11)之後、shop_screen 之前。
    # keywords_all 要求『Space』提示為必要字；negative_keywords 排除所有已知
    # 畫面的內容字 → 只在「無任何已知畫面內容」的純對話/推進畫面命中。
    ScreenSignature(
        name="npc_dialogue_space_continue",
        state="STATE_TAP_CONTINUE",
        priority=15,
        keywords_all=(SPACE_CONTINUE_HINT_TOKENS,),
        negative_keywords=NPC_DIALOGUE_NEGATIVE_TOKENS,
    ),
    # 離塔後「默契提升」(好感度/羈絆)獎勵畫面 → 點空白推進（STATE_TAP_CONTINUE）。
    # priority 16 < potential_select_visual(50)/result(110)/shop(140) → 優先於弱色錨誤判
    # （L3 20260614_173024:此畫面無簽名 → UNKNOWN 卡死）。「默契提升」字夠專一,單字錨即可。
    ScreenSignature(
        name="rapport_boost",
        state="STATE_TAP_CONTINUE",
        priority=16,
        keywords_any=RAPPORT_BOOST_TOKENS,
    ),
    ScreenSignature(
        name="shop_screen",
        state="STATE_SHOP",
        priority=20,
        keywords_all=(SHOP_ITEM_TOKENS, SHOP_CONTROL_TOKENS + SHOP_NOTE_GOODS_TOKENS),
    ),
    ScreenSignature(
        name="event_choice",
        state="STATE_EVENT",
        priority=30,
        keywords_any=EVENT_CHOICE_HINTS,
    ),
    ScreenSignature(
        name="potential_select_text",
        state="STATE_POTENTIAL_SELECT",
        priority=40,
        keywords_any=POTENTIAL_SELECT_HINTS,
    ),
    ScreenSignature(
        name="potential_select_visual",
        state="STATE_POTENTIAL_SELECT",
        priority=50,
        keywords_any=POTENTIAL_CARD_HINTS,
        color_anchor=BOTTOM_TEAL_ACTION_BUTTON,
        # 此簽名先天偏弱（通用卡片詞 + 底部青色按鈕色錨），編隊/秘紋畫面
        # 同樣有底部青色行動按鈕（「下一步」「開始戰鬥」），且畫面常含
        # 「潛能預設」等帶「潛能」的雜訊字 → 以編隊/準備畫面的高鑑別度
        # 按鈕字直接否決（session 20260612_233510:編隊畫面被誤判 1.05）。
        # 「編隊」同時涵蓋「自動編隊」；7 張 potential_select 語料皆不含
        # 這些字（已驗證），不影響其判定。
        # 另否決結算/紀錄畫面：其「潛能收集/收藏」分頁含「潛能」+ 底部彩色格偶命中青色色錨
        # → 被此簽名(50)搶在 result_keywords(110)前誤判卡死（L3 20260614_220443）。結算標記字
        # 僅結算畫面有、真選卡畫面絕無 → 一律否決,讓結算畫面確定落到 result_keywords。
        negative_keywords=("編隊", "下一步", "開始戰鬥", "开始战斗") + RESULT_SCREEN_MARKER_TOKENS,
    ),
    # ── 全頁關鍵字（v1 STATE_KEYWORDS 平表，dict 順序 = priority 順序）──
    ScreenSignature(
        name="reconnect_keywords",
        state="STATE_RECONNECT",
        priority=100,
        keywords_any=("重新連線", "連接中斷", "網路異常", "重連", "reconnect"),
    ),
    ScreenSignature(
        name="result_keywords",
        state="STATE_RESULT",
        priority=110,
        # 單一來源 RESULT_SCREEN_MARKER_TOKENS（= 舊「儲存紀錄/未命名紀錄/探索結束/評分」
        # +「祕紋技能」分頁,提升結算偵測 recall;同一組同時作 potential_select_visual 的否決字）。
        keywords_any=RESULT_SCREEN_MARKER_TOKENS,
    ),
    ScreenSignature(
        name="explore_complete_keywords",
        state="STATE_EXPLORE_COMPLETE",
        priority=120,
        keywords_any=("探索完成", "第20層", "收集潛能"),
    ),
    ScreenSignature(
        name="shop_choice_keywords",
        state="STATE_SHOP_CHOICE",
        priority=130,
        keywords_any=("遇到了星塔商店", "去商店購物", "強化", "直接上樓"),
    ),
    # 「啟動協奏技能!」音符畫面 →點空白推進；priority 135 < shop_keywords(140)，
    # 故贏過 shop_keywords 的通用「協奏技能」誤判（session 20260613_225933）。
    ScreenSignature(
        name="concert_skill_activated",
        state="STATE_TAP_CONTINUE",
        priority=135,
        keywords_any=CONCERT_SKILL_ACTIVATE_TOKENS,
    ),
    ScreenSignature(
        name="shop_keywords",
        state="STATE_SHOP",
        priority=140,
        keywords_any=(
            "星塔商店", "重置商店", "離開商店", "離開星塔",
            "相關協奏技能", "協奏技能",
            "優惠", "刷新次數",
        ) + SHOP_ITEM_TOKENS[:1],   # 「潛能特飲」
    ),
    ScreenSignature(
        name="event_keywords",
        state="STATE_EVENT",
        priority=150,
        keywords_any=("隨機事件", "選擇一個選項", "事件發生", "發生變化"),
    ),
    ScreenSignature(
        name="note_acquired_keywords",
        state="STATE_NOTE_ACQUIRED",
        priority=160,
        keywords_any=("獲得音符", "音符數量"),
    ),
    ScreenSignature(
        name="tap_continue_keywords",
        state="STATE_TAP_CONTINUE",
        priority=170,
        keywords_any=("點選空白處繼續", "點擊空白處繼續"),
    ),
    ScreenSignature(
        name="potential_select_keywords",
        state="STATE_POTENTIAL_SELECT",
        priority=180,
        keywords_any=POTENTIAL_SELECT_KEYWORDS,
    ),
    ScreenSignature(
        name="fast_battle_keywords",
        state="STATE_FAST_BATTLE",
        priority=190,
        keywords_any=("戰鬥中", "自動戰鬥", "跳過戰鬥"),
    ),
    ScreenSignature(
        name="prepare_keywords",
        state="STATE_PREPARE",
        priority=200,
        keywords_any=("秘紋組合", "啟動條件", "主位秘紋", "副位秘紋", "總計"),
    ),
    ScreenSignature(
        name="formation_keywords",
        state="STATE_FORMATION",
        priority=210,
        keywords_any=("編隊", "下一步", "自動編隊"),
    ),
    ScreenSignature(
        name="lobby_keywords",
        state="STATE_LOBBY",
        priority=220,
        keywords_any=("星塔探索", "難度選擇", "開始探索", "快速戰鬥"),
        # 「快速戰鬥」是跨畫面通用詞（unknown__20260531_195529 的 IDE 雜訊圖
        # 也含此字），單一命中（1.05）不足以判定大廳；要求至少命中兩個
        # 關鍵字（1.0 + 2*0.05 = 1.10）。語料庫全部大廳圖皆含
        # 「星塔探索」+「快速戰鬥」兩字以上。
        min_score=1.10,
    ),
)


# ─────────────────────────────────────────────────────────────
# 評分
# ─────────────────────────────────────────────────────────────


def _iter_item_texts(ocr_items, roi, frame_size):
    """展開 OCR 項目為文字；若 signature 有 roi 且項目帶 bbox 則以中心點過濾。

    支援三種項目格式：
      - str
      - (text, confidence, bbox)
      - {"text": ..., "bbox": ...}
    bbox 缺席或 frame_size 未知時不做 ROI 過濾（寬鬆降級）。
    """
    for item in ocr_items or ():
        bbox = None
        if isinstance(item, str):
            text = item
        elif isinstance(item, dict):
            text = item.get("text", "")
            bbox = item.get("bbox")
        else:
            text = item[0]
            if len(item) > 2:
                bbox = item[2]
        if roi is not None and bbox and frame_size:
            fw, fh = frame_size
            xs = [p[0] for p in bbox]
            ys = [p[1] for p in bbox]
            cx = sum(xs) / len(xs)
            cy = sum(ys) / len(ys)
            x0, y0, x1, y1 = roi
            if not (x0 * fw <= cx <= x1 * fw and y0 * fh <= cy <= y1 * fh):
                continue
        yield text


def score_signature(
    signature: ScreenSignature,
    ocr_items,
    frame: np.ndarray | None = None,
    frame_size: tuple[int, int] | None = None,
) -> float:
    """對單一 signature 評分。0.0 = 未命中（含被否決）。"""
    if frame_size is None and frame is not None and hasattr(frame, "shape"):
        frame_size = (frame.shape[1], frame.shape[0])

    texts = list(_iter_item_texts(ocr_items, signature.roi, frame_size))
    joined = normalize_text(" ".join(texts))

    # 否決字
    for neg in signature.negative_keywords:
        if normalize_text(neg) in joined:
            return 0.0

    # AND-of-OR 必要字群
    for group in signature.keywords_all:
        if not any(normalize_text(kw) in joined for kw in group):
            return 0.0

    # 顏色錨點（設定即必要）
    if signature.color_anchor is not None and not color_anchor_hit(frame, signature.color_anchor):
        return 0.0

    # 模板錨點（設定即必要；目前資料未使用，保留欄位供 1.2+）
    # template_anchor 需要 matcher 實例，評分層不持有 matcher，故僅在
    # signature 未設定時視為通過。
    if signature.template_anchor is not None:
        return 0.0

    any_hits = sum(1 for kw in signature.keywords_any if normalize_text(kw) in joined)
    if signature.keywords_any and not signature.keywords_all and any_hits == 0:
        return 0.0
    if not signature.keywords_all and not signature.keywords_any and signature.color_anchor is None:
        return 0.0  # 空 signature 不得命中

    return 1.0 + min(any_hits, 4) * 0.05


def signature_evidence(
    signature: ScreenSignature,
    ocr_items,
    frame: np.ndarray | None = None,
    frame_size: tuple[int, int] | None = None,
) -> tuple[str, ...]:
    """回傳此 signature 命中的關鍵字 / 錨點清單（Phase 1.2 DetectionResult.evidence）。

    只列舉「有命中」的證據，全部為可直接 JSON 序列化的字串，
    供 state_trace 與 failure bundle 使用。
    """
    if frame_size is None and frame is not None and hasattr(frame, "shape"):
        frame_size = (frame.shape[1], frame.shape[0])

    texts = list(_iter_item_texts(ocr_items, signature.roi, frame_size))
    joined = normalize_text(" ".join(texts))

    evidence: list[str] = []
    seen: set[str] = set()
    for group in signature.keywords_all:
        for kw in group:
            if kw not in seen and normalize_text(kw) in joined:
                seen.add(kw)
                evidence.append(f"keyword:{kw}")
    for kw in signature.keywords_any:
        if kw not in seen and normalize_text(kw) in joined:
            seen.add(kw)
            evidence.append(f"keyword:{kw}")
    if signature.color_anchor is not None and color_anchor_hit(frame, signature.color_anchor):
        evidence.append(f"color_anchor:{signature.name or signature.state}")
    return tuple(evidence)


def score_all(
    ocr_items,
    frame: np.ndarray | None = None,
    frame_size: tuple[int, int] | None = None,
    signatures: tuple[ScreenSignature, ...] | None = None,
) -> list[tuple[ScreenSignature, float]]:
    """回傳全部 signature 的得分（含 0 分者），供除錯與 1.2 evidence 使用。"""
    sigs = SIGNATURES if signatures is None else signatures
    return [
        (sig, score_signature(sig, ocr_items, frame=frame, frame_size=frame_size))
        for sig in sigs
    ]


def classify(
    ocr_items,
    frame: np.ndarray | None = None,
    frame_size: tuple[int, int] | None = None,
    signatures: tuple[ScreenSignature, ...] | None = None,
) -> tuple[str | None, float, ScreenSignature | None]:
    """取最高優先（priority 小者先；同 priority 比得分高者）的命中 signature。

    Returns:
        (state, score, signature)；無命中時 (None, 0.0, None)。
    """
    hits = [
        (sig, score)
        for sig, score in score_all(ocr_items, frame=frame, frame_size=frame_size, signatures=signatures)
        if score >= sig.min_score
    ]
    if not hits:
        return None, 0.0, None
    sig, score = min(hits, key=lambda pair: (pair[0].priority, -pair[1]))
    return sig.state, score, sig
