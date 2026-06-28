# gui/settings_schema.py
# §10 步2 ── GUI 設定面板單一來源旋鈕登記表
#
# 規格依據：GUI_DESIGN_SPEC §2（旋鈕表）、§6（help 介面）、§7（tier/安全鎖語意）
#           DECISION_REGISTRY.md（語意帳本）、config.yaml（現值/路徑錨點）
#
# 使用方式：
#   from gui.settings_schema import ALL_SETTINGS, Setting
#   # 渲染器依 s.type 決定控件（SpinBox/SwitchButton/ComboBox/...）
#   # help 文字 = s.help；tier 決定進階頁安全鎖（danger 需勾風險確認）
#
# tier 語意（§7）：
#   normal   — 一般使用者可調，主設定頁常用區
#   test     — 測試版功能（藍標），開了改決策，啟用前需 L3 校準
#   advanced — 進階頁可調（無需風險確認）
#   danger   — 進階頁，需勾「我了解風險」才解鎖
#
# type 對應渲染：
#   int/float   → SpinBox
#   bool        → SwitchButton
#   enum        → ComboBox（options 中文顯示、存英文值）
#   list        → Tag 編輯區 / LineEdit 清單
#   dict        → 自訂小表（key-value）
#   dict-list   → 自訂表格（list of dict）
#   list-of-list→ 群組清單
#   hotkey      → 熱鍵錄製控件
#   editor      → 獨立對話框入口（如事件編輯器）
#   group       → 多子欄群組（本行為入口列，子欄另外展開）

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Setting:
    """單一旋鈕的完整描述。

    Attributes:
        key:     config 巢狀路徑（dot 分隔），如 ``shop.buy.strategy``。
                 editor/group/hotkey 型允許非 config 路徑（如 ``event_rules``）。
        label:   中文顯示名稱（UI 呈現）。
        type:    控件型別（見模組說明）。
        options: 僅 enum 型使用。list of (中文顯示, 英文值) 對照，英文值與 config 一致。
        default: 預設值（英文/原始值，與 config.yaml 現值 byte-identical）。
                 editor/group 型允許 None。
        module:  所屬模組（'選卡'/'商店'/'事件'/'結算'/'執行'/'進階'）。
        tier:    安全等級（'normal'/'test'/'advanced'/'danger'）。
        help:    中文一句說明（help 介面 & tooltip 來源）。
    """
    key: str
    label: str
    type: str
    options: tuple[tuple[str, Any], ...]  # enum 用；非 enum 填空 tuple
    default: Any
    module: str
    tier: str
    help: str


# ─── 模組：選卡（14 條）─────────────────────────────────────────────────────

_CARD_SETTINGS: list[Setting] = [
    Setting(
        key="decision.mode",
        label="決策模式",
        type="enum",
        options=(
            ("推薦徽章", "recommendation_badge"),
            ("累計模式", "legacy"),
        ),
        default="recommendation_badge",
        module="選卡",
        tier="normal",
        help="推薦徽章 = 讀卡片左上推薦N級做升等判斷（主模式）；累計模式 = 舊版依累計升等量選卡。",
    ),
    Setting(
        key="card_counter.target_total",
        label="卡片總等級目標",
        type="int",
        options=(),
        default=78,
        module="選卡",
        tier="normal",
        help="全隊卡片等級合計目標值。未達標前優先買卡升等；達標後改買協奏缺口音符。",
    ),
    Setting(
        key="decision.upgrade_strategy",
        label="升等策略",
        type="enum",
        options=(
            ("溢出最小", "minimize_overflow"),
            ("最接近目標", "nearest_target"),
            ("補最缺", "farthest_target"),
        ),
        default="minimize_overflow",
        module="選卡",
        tier="normal",
        help="目標等級不同的候選卡之間如何排優先序。溢出最小＝不浪費升等點；需啟用推薦N級才生效。",
    ),
    Setting(
        key="decision.recommendation_target.enabled",
        label="啟用推薦N級",
        type="bool",
        options=(),
        default=True,
        module="選卡",
        tier="test",
        help="開啟後讀卡片左上「推薦N級」作為升等目標（E-3/E-4）。首次啟用需 L3 截圖校準多卡推薦級，預設關。",
    ),
    Setting(
        key="decision.required_target_level",
        label="required 滿級",
        type="int",
        options=(),
        default=6,
        module="選卡",
        tier="normal",
        help="必選清單的潛能要升到第幾級才算「滿」。預設 6＝升滿；可調 1–6（如想提早停 required 可改小）。",
    ),
    Setting(
        key="decision.max_reroll_before_backup",
        label="reroll 上限後降備選",
        type="int",
        options=(),
        default=3,
        module="選卡",
        tier="normal",
        help="選卡畫面 reroll（重抽）幾次後仍無 required 時，降為選 backup 備選清單。",
    ),
    Setting(
        key="decision.prefer_never_picked",
        label="優先沒拿過的",
        type="bool",
        options=(),
        default=True,
        module="選卡",
        tier="advanced",
        help="同階候選平手時，優先選本輪還沒拿過的潛能（增多樣性，鋪開不同潛能）。",
    ),
    Setting(
        key="decision.prefer_higher_gain",
        label="優先升等量大",
        type="bool",
        options=(),
        default=True,
        module="選卡",
        tier="advanced",
        help="同階候選平手時，優先選能升等量最多（+3>+2>+1）的卡。",
    ),
    Setting(
        key="decision.min_level_threshold",
        label="弱卡升等門檻（僅 legacy）",
        type="int",
        options=(),
        default=0,
        module="選卡",
        tier="advanced",
        help="legacy 模式下排除升等量低於此值的弱卡（0＝不過濾）。guaranteed 保底卡不受限制。",
    ),
    Setting(
        key="decision.guaranteed",
        label="保底潛能（粉色最優先）",
        type="list",
        options=(),
        default=["貓貓拳", "多發彈", "火雨傾盆", "特別裝藥", "明日·薪火", "薪火的再燃"],
        module="選卡",
        tier="normal",
        help="粉色保底潛能清單，出現即無條件優先選取，不受 reroll 或門檻影響。",
    ),
    Setting(
        key="decision.required",
        label="必選潛能",
        type="list",
        options=(),
        default=["爆裂追擊", "快拳連打", "終結打擊", "童話法則", "慶典再啟", "伏筆驗證", "客串回"],
        module="選卡",
        tier="normal",
        help="必選潛能清單，目標升到 required_target_level 級。清單優先於 backup 備選。",
    ),
    Setting(
        key="decision.backup",
        label="備選潛能",
        type="list",
        options=(),
        default=["勇猛挑戰", "回旋反擊", "巔峰狀態", "禮炮雙響", "振奮炮擊", "完美接戲", "視覺衝擊"],
        module="選卡",
        tier="normal",
        help="備選潛能清單，required 全部滿足後從此清單繼續選。受 backup_groups 互斥群組限制。",
    ),
    Setting(
        key="decision.level_required",
        label="限量必選（名稱+目標等級）",
        type="dict-list",
        options=(),
        default=[{"name": "與刃共舞", "target_level": 5}, {"name": "疾速拔刀", "target_level": 1}],
        module="選卡",
        tier="advanced",
        help="指定特定潛能的目標等級（非預設滿級）。例如「與刃共舞升到5級即停」。",
    ),
    Setting(
        key="decision.backup_groups",
        label="備選互斥群組",
        type="list-of-list",
        options=(),
        default=[
            ["勇猛挑戰", "回旋反擊", "巔峰狀態"],
            ["禮炮雙響", "振奮炮擊"],
            ["完美接戲", "視覺衝擊"],
        ],
        module="選卡",
        tier="advanced",
        help="備選互斥群組：同組只選一個（已選其中一個後，同組其餘跳過）。",
    ),
]

# ─── 模組：商店（12 條）─────────────────────────────────────────────────────

_SHOP_SETTINGS: list[Setting] = [
    Setting(
        key="shop.buy.strategy",
        label="買法策略",
        type="enum",
        options=(
            ("先卡後音符", "cards_then_notes"),
            ("全買", "all"),
            ("只買卡", "cards_only"),
            ("只買音符", "notes_only"),
        ),
        default="cards_then_notes",
        module="商店",
        tier="normal",
        help="商店購買優先序。先卡後音符＝卡片未達標買卡，達標後換買缺口音符（推薦）。",
    ),
    Setting(
        key="shop.buy.affordability",
        label="買得起才點",
        type="bool",
        options=(),
        default=True,
        module="商店",
        tier="normal",
        help="點卡前先讀卡片單價與餘額，買不起的不點（避免空點浪費時間）。",
    ),
    Setting(
        key="shop.buy.prefer_discount",
        label="買卡優惠優先",
        type="bool",
        options=(),
        default=True,
        module="商店",
        tier="test",
        help="優先買「有優惠標記（紅圈/原價劃掉）」的商品省錢。啟用前需 L3 驗證折扣識別，預設關。",
    ),
    Setting(
        key="shop.buy.discount_scope",
        label="優惠優先範圍",
        type="enum",
        options=(
            ("只音符", "notes_only"),
            ("含買卡", "cards"),
            ("全部", "all"),
        ),
        default="notes_only",
        module="商店",
        tier="advanced",
        help="buy.prefer_discount 的作用範圍。只音符＝只在音符購買階段掃優惠；含買卡＝買卡也優先優惠。",
    ),
    Setting(
        key="shop.buy.buy_non_discounted",
        label="買非特價商品",
        type="bool",
        options=(),
        default=True,
        module="商店",
        tier="normal",
        help="開＝特價/原價都買（現行）;關＝只買有優惠標記的卡,跳過原價（全無優惠則不買）。",
    ),
    Setting(
        key="shop.refresh.trigger",
        label="刷新時機",
        type="enum",
        options=(
            ("買完才刷", "exhausted"),
            ("從不", "never"),
            ("一律", "always"),
            ("有缺口", "when_gap"),
            ("未達標", "before_target"),
        ),
        default="when_gap",
        module="商店",
        tier="normal",
        help="何時刷新貨架（按 Q）。買完才刷＝現行（買無可買才刷）；從不＝不刷；有缺口＝仍有協奏音符缺口才刷。",
    ),
    Setting(
        key="shop.refresh.start_from_visit",
        label="刷新從第幾次商店啟用",
        type="int",
        options=(),
        default=4,
        module="商店",
        tier="normal",
        help="造訪商店次數未達此值時不刷（優先於 trigger）。1＝第一次就可刷（預設，現行）。",
    ),
    Setting(
        key="bot.max_shop_refresh",
        label="刷新次數上限",
        type="int",
        options=(),
        default=1,
        module="商店",
        tier="normal",
        help="每家商店最多刷新貨架幾次（Q 鍵）。0＝不刷；達上限後直接上樓。",
    ),
    Setting(
        key="shop.upgrade.enabled",
        label="強化總開關",
        type="bool",
        options=(),
        default=True,
        module="商店",
        tier="normal",
        help="控制是否使用升級機（強化）。關閉後完全不強化，直接略過升級機。",
    ),
    Setting(
        key="shop.upgrade.times_by_visit",
        label="第N次造訪強化幾次",
        type="dict",
        options=(),
        default={1: 2, 2: 3},
        module="商店",
        tier="normal",
        help="依造訪次數決定強化次數。例如 {1:2, 2:3} = 第1次商店強化2次、第2次強化3次；未列出的次數不強化。",
    ),
    Setting(
        key="shop.upgrade.price_ceiling",
        label="強化價上限",
        type="int",
        options=(),
        default=547,
        module="商店",
        tier="normal",
        help="單次強化費用超過此值就略過（0＝不限）。免費強化一律執行不受限。",
    ),
    Setting(
        key="shop.order_by_visit",
        label="第幾次先升級機/先商店",
        type="dict",
        options=(),
        default={},
        module="商店",
        tier="normal",
        help="依造訪次數決定順序（upgrade_first/shop_first）。未列出的次數退全域 shop.order（預設先強化）。",
    ),
    Setting(
        key="shop.buy.note_priority",
        label="音符購買優先序",
        type="list",
        options=(),
        default=[],
        module="商店",
        tier="advanced",
        help="買音符時優先買清單排前的音符（如風＞絕招＞強攻）。空（預設）= 只按缺口順序買。",
    ),
    Setting(
        key="shop.post_target.note_spree",
        label="達標後狂買音符",
        type="group",
        options=(),
        default=None,
        module="商店",
        tier="advanced",
        help="卡片達標後若還有金錢，依清單狂買指定音符（超出缺口也買）。開關+音符清單+金錢上限三個子欄。",
    ),
]

# ─── 模組：事件（5 條）──────────────────────────────────────────────────────

_EVENT_SETTINGS: list[Setting] = [
    Setting(
        key="event.strategy",
        label="事件策略",
        type="enum",
        options=(
            ("激進", "aggressive"),
            ("中間", "balanced"),
            ("保守", "conservative"),
        ),
        default="aggressive",
        module="事件",
        tier="normal",
        help="激進＝追最高報酬、接受風險；中間＝拒機率損失但接受確定消耗；保守＝絕不損失（只選無風險選項）。",
    ),
    Setting(
        key="event.refuse_note_cost",
        label="拒消耗音符",
        type="bool",
        options=(),
        default=True,
        module="事件",
        tier="normal",
        help="無論策略為何，一律拒絕「消耗音符」的選項（音符要留給協奏）。",
    ),
    Setting(
        key="event.aggressive_gamble_mode",
        label="賭博選錢多",
        type="bool",
        options=(),
        default=True,
        module="事件",
        tier="advanced",
        help="激進模式下純金錢機率賭注選「獲得金額最高」的選項（否則退 generic 報酬評分）。",
    ),
    Setting(
        key="event.same_option_repeat_limit",
        label="連點放棄門檻",
        type="int",
        options=(),
        default=3,
        module="事件",
        tier="advanced",
        help="同一事件選項點幾次仍不推進就放棄（防事件畫面卡死）。預設 3＝現行。",
    ),
    Setting(
        key="event_rules",
        label="編輯事件規則",
        type="editor",
        options=(),
        default=None,
        module="事件",
        tier="normal",
        help="開啟事件規則編輯器，設定「特定事件→固定選某選項」的覆蓋規則（data/event_rules.yaml）。",
    ),
]

# ─── 模組：結算（3 條）──────────────────────────────────────────────────────

_RESULT_SETTINGS: list[Setting] = [
    Setting(
        key="result.rating_threshold",
        label="評分達標門檻",
        type="int",
        options=(),
        default=30,
        module="結算",
        tier="normal",
        help="結算評分（左上六角徽章）≥ 此值才算達標並保存紀錄。0＝停用評分門檻。",
    ),
    Setting(
        key="result.require_all_secrets",
        label="祕聞全解才達標",
        type="bool",
        options=(),
        default=False,
        module="結算",
        tier="normal",
        help="開啟後在評分達標基礎上再要求所有協奏音符全達標（current ≥ target），未全解即丟棄。預設關＝現行。",
    ),
    Setting(
        key="result.potential_total_threshold",
        label="角色潛能加總門檻",
        type="int_toggle",
        options=(),
        default=0,
        module="結算",
        tier="normal",
        help="開關啟用後,角色潛能等級合計 ≥ 此數值也算達標（與評分取 OR）。關（0）＝停用。",
    ),
]

# ─── 模組：執行（2 條）──────────────────────────────────────────────────────

_RUN_SETTINGS: list[Setting] = [
    Setting(
        key="run.max_runs",
        label="最大輪數",
        type="int",
        options=(),
        default=1,
        module="執行",
        tier="normal",
        help="bot 自動執行的最大輪數。1＝跑完一輪即停；0＝無限輪（直到手動停止）。",
    ),
    Setting(
        key="bot.hotkey_stop",
        label="緊急中止快捷鍵",
        type="hotkey",
        options=(),
        default="ctrl+q",
        module="執行",
        tier="normal",
        help="全域緊急中止 bot 的熱鍵。可在 GUI 錄製改鍵；預設 Ctrl+Q（現行綁定）。",
    ),
]

# ─── 模組：進階（11 條）─────────────────────────────────────────────────────

_ADVANCED_SETTINGS: list[Setting] = [
    Setting(
        key="bot.poll_interval",
        label="輪詢間隔（秒）",
        type="float",
        options=(),
        default=1.0,
        module="進階",
        tier="advanced",
        help="主迴圈每拍間隔（秒）。原 2.0，加速後 1.0；過低可能讓動畫未完成就判斷。",
    ),
    Setting(
        key="bot.click_settle",
        label="點擊沉澱（秒）",
        type="float",
        options=(),
        default=1.0,
        module="進階",
        tier="advanced",
        help="點擊後在「重拍驗證」前的等待時間。原 2.0，加速後 1.0；過低可能驗證未反應的畫面。",
    ),
    Setting(
        key="bot.take_settle_delay",
        label="拿走沉澱（秒）",
        type="float",
        options=(),
        default=0.5,
        module="進階",
        tier="advanced",
        help="選卡→點「拿走」前的牌面 highlight 沉澱。量測支持 0.5 省時（下限 0.3，越低越快）。",
    ),
    Setting(
        key="bot.ocr_cache.enabled",
        label="OCR 快取",
        type="bool",
        options=(),
        default=True,
        module="進階",
        tier="advanced",
        help="同一幀多次 OCR 時復用結果（省 EasyOCR 時間）。僅作用於 handler 鏈，偵測鏈絕不快取。",
    ),
    Setting(
        key="bot.adaptive_settle.enabled",
        label="自適應沉澱",
        type="bool",
        options=(),
        default=False,
        module="進階",
        tier="danger",
        help="【危險】畫面提早穩定即返回（不空等）。已知會誤判遊戲過場動畫，預設關；啟用需充分 L3 測試。",
    ),
    Setting(
        key="vision.detector",
        label="偵測器版本",
        type="enum",
        options=(
            ("v2（信心值+STATE_UNKNOWN）", "v2"),
            ("v1（舊行為）", "v1"),
        ),
        default="v2",
        module="進階",
        tier="danger",
        help="【危險】v2＝現行主模式（信心值＋STATE_UNKNOWN 保護）；v1＝舊行為（未命中維持原狀），僅供回退對照。",
    ),
    Setting(
        key="ocr.languages",
        label="OCR 語言",
        type="list",
        options=(),
        default=["ch_tra", "en"],
        module="進階",
        tier="danger",
        help="【危險】EasyOCR 識別語言清單。改動可能導致中文/英文識別失效，不確定勿改。",
    ),
    Setting(
        key="ocr.gpu",
        label="OCR GPU 加速",
        type="bool",
        options=(),
        default=True,
        module="進階",
        tier="danger",
        help="【危險】EasyOCR 是否使用 GPU。關閉改用 CPU（慢很多）；無 CUDA 環境需關閉。",
    ),
    Setting(
        key="window.capture_mode",
        label="擷取模式",
        type="enum",
        options=(
            ("自動降級", "auto"),
            ("PrintWindow", "printwindow"),
            ("mss 螢幕截圖", "mss"),
            ("DXcam", "dxcam"),
        ),
        default="auto",
        module="進階",
        tier="danger",
        help="【危險】遊戲視窗擷取方式。auto＝自動依環境降級（PrintWindow→mss→DXcam）；手動指定僅在特殊情況。",
    ),
    Setting(
        key="input.mode",
        label="輸入模式",
        type="enum",
        options=(
            ("前景點擊", "foreground"),
            ("後景點擊", "background"),
        ),
        default="foreground",
        module="進階",
        tier="danger",
        help="【危險】滑鼠/鍵盤事件注入方式。foreground＝需遊戲前景（現行）；background＝後景注入（實測不穩）。",
    ),
    Setting(
        key="run.stop_on_target_level",
        label="達標後停止（未實作）",
        type="int",
        options=(),
        default=0,
        module="進階",
        tier="danger",
        help="【危險】【假旋鈕】bot.py 後端是 pass 的 TODO，調了沒有任何效果。待後端實作前請勿依賴。",
    ),
]

# ─── 完整清單 ─────────────────────────────────────────────────────────────────

ALL_SETTINGS: list[Setting] = (
    _CARD_SETTINGS
    + _SHOP_SETTINGS
    + _EVENT_SETTINGS
    + _RESULT_SETTINGS
    + _RUN_SETTINGS
    + _ADVANCED_SETTINGS
)

# ─── 查詢輔助 ─────────────────────────────────────────────────────────────────

def by_key(key: str) -> Setting | None:
    """用 key 查詢 Setting（找不到回傳 None）。"""
    for s in ALL_SETTINGS:
        if s.key == key:
            return s
    return None


def by_module(module: str) -> list[Setting]:
    """取得指定模組的所有 Setting。"""
    return [s for s in ALL_SETTINGS if s.module == module]


def by_tier(tier: str) -> list[Setting]:
    """取得指定 tier 的所有 Setting。"""
    return [s for s in ALL_SETTINGS if s.tier == tier]
