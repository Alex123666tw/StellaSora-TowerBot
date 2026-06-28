# 可客製化決策項登記表(DECISION_REGISTRY)

> 產出:2026-06-15｜定位:**所有決策邏輯 = GUI 可調旋鈕**(使用者客製化、應付遊戲版本變更),bot 不寫死「最佳」。
> 用途:GUI schema 驅動設定面板(GUI_ARCHITECTURE.md 區段 D1/D2)的**完整 schema 藍圖**。本表是 GUI 附錄 A 的超集。
> 來源:`config.yaml`、`core/decision_engine.py`、`core/states.py`、`core/bot.py` 全讀盤點。
> 狀態:✅ 已 config 可調 ｜ 🔧 寫死待提取成旋鈕 ｜ ⬜ 缺、待新增後端 + 旋鈕

## 實作進度(2026-06-16 更新)

**決策 config 化第一批(DECISION_CONFIG_PLAN 10 步)完成**(repair/phase-3,PR #3)。每項預設 = 現有行為 byte-identical,新值才改行為。已完成:E-1~E-4(卡片選擇)、A-1/A-2(事件)、c(商店優惠)、B(刷新時機)、D(達標後狂買音符)。**全套 423 passed**。
**延後項**(技術理由,非阻塞):reward_rank 提示字外部化(順序拍板固定、test_signatures 不強制)、clear_category 刷新清類(需格位類型追蹤)、B 分樓層(需樓層 OCR)。
**啟用待 L3 校準**:E-3/E-4 推薦N級(`recommendation_target.enabled` 預設 false,翻 true 需 L3 貼圖驗多卡)、c `prefer_discount`(預設 false,L3 驗後開)。

---

## A. 事件(高風險高回報 / 低風險低回報 / 無虧損)

| 決策項 | 現況 | 型別/選項 | 預設 | 狀態 |
|---|---|---|---|---|
| 事件總策略 | `event.strategy` | enum: aggressive / **balanced** / conservative | aggressive | ✅ |
| **風險三檔(高/中/無虧損)** | A-1 加 balanced 中間檔(拒機率損失、接受確定消耗) | enum 3 檔 | aggressive | ✅ |
| 報酬偏好排序 | 寫死 `_event_reward_rank`(稀有潛能>潛能>普通>音符>金錢) | list 排序 | 固定序 | 🔧(延後:token 待移 screen_tokens;順序拍板固定) |
| 賭博「選錢多的」 | `event.aggressive_gamble_mode`(A-2) | bool | true | ✅ |
| 拒消耗音符硬規則 | `event.refuse_note_cost`(A-2) | bool | true | ✅ |
| 同選項連點放棄門檻 | `event.same_option_repeat_limit`(A-2) | int | 3 | ✅ |

> conservative ≈「無虧損」(排除機率損失/消耗金錢/消耗音符);balanced = 中間檔(只拒機率損失下行、接受確定消耗金錢換報酬)。

## B. 商店購買(刷新時機 / 購買邏輯 / 分樓層)

| 決策項 | 現況 | 型別/選項 | 預設 | 狀態 |
|---|---|---|---|---|
| 買法策略 | `shop.buy.strategy` | enum: cards_then_notes / all / cards_only / notes_only | cards_then_notes | ✅ |
| 買得起才點(affordability) | `shop.buy.affordability` | bool | true | ✅ |
| 刷新次數上限 | `bot.max_shop_refresh` | int | 1 | ✅ |
| **刷新時機/條件** | `shop.refresh.trigger`(B) | enum: exhausted/never/always/when_gap/before_target | exhausted | ✅ |
| 是否進商店把關 | 寫死 `_should_enter_shop`(餘額>0) | — | 固定 | 🔧 |
| 優惠/折扣自動點(買卡) | `shop.buy.prefer_discount`+`discount_scope`(c) | bool+enum | false / notes_only | ✅ |
| **分樓層策略** | **無**。current_floor 恆 0 死碼,全樓層同策略 | per-floor | — | ⬜(需先補樓層 OCR) |

## C. 升級機/強化(用幾次 / 順序)— 五類中最完整

| 決策項 | 現況 | 型別/選項 | 預設 | 狀態 |
|---|---|---|---|---|
| 強化總開關 | `shop.upgrade.enabled` | bool | true | ✅ |
| 第 N 次造訪強化幾次 | `shop.upgrade.times_by_visit` | dict {visit:int} | {1:2, 2:3} | ✅ |
| 強化價格上限 | `shop.upgrade.price_ceiling` | int (0=不限) | 540 | ✅ |
| 先商店或先強化 | `shop.order` | enum: upgrade_first / shop_first | upgrade_first | ✅ |

## D. 達標後金錢功用(狂買特定音符 / 刷新清特定貨)

| 決策項 | 現況 | 型別/選項 | 預設 | 狀態 |
|---|---|---|---|---|
| 達標後買缺口音符 | 寫死(補 target_notes 缺口為止) | — | 固定 | 🔧(現行核心,保留) |
| **狂買特定音符(無上限/花光)** | `shop.post_target.note_spree`(D) | {enabled,notes,max_spend} | enabled false | ✅ |
| **刷新清空特定部分貨品** | **無**(刷新清整架) | enum 清哪類 | — | ⬜(延後:需格位類型追蹤) |
| 音符優惠優先(使用者拍板) | `shop.buy.prefer_discount`(c,scope 含 notes) | bool | false | ✅ |

## E. 卡片選擇邏輯

| 決策項 | 現況 | 型別/選項 | 預設 | 狀態 |
|---|---|---|---|---|
| 決策模式 | `decision.mode` | enum: recommendation_badge / legacy | recommendation_badge | ✅ |
| 保底清單(粉色最優先) | `decision.guaranteed` | list[str] | 6 項 | ✅ |
| 必選清單(目標 Lv.6) | `decision.required` | list[str] | 7 項 | ✅ |
| 備選清單 | `decision.backup` | list[str] | 7 項 | ✅ |
| 限量必選(自訂目標等級) | `decision.level_required` | list[{name,target_level:1-6}] | 2 項 | ✅ |
| 備選互斥群組 | `decision.backup_groups` | list[list[str]] | 3 組 | ✅ |
| Reroll 上限後降備選 | `decision.max_reroll_before_backup` | int | 3 | ✅ |
| 卡片總等級目標 | `card_counter.target_total` | int | 78 | ✅ |
| required 預設滿級 | `decision.required_target_level`(E-1,clamp 1-6) | int | 6 | ✅ |
| **「先拿沒拿過的」** | `decision.prefer_never_picked`(E-1) | bool | true | ✅ |
| 同階級「升等量大優先」 | `decision.prefer_higher_gain`(E-1) | bool | true | ✅ |
| **「低於特定等級不拿」** | `decision.min_level_threshold`(E-2,legacy 模式) | int 門檻 | 0(不過濾) | ✅(legacy) |
| **「目標等級不同時的升等判斷」** | `decision.upgrade_strategy`(E-4)+ `recommendation_target.enabled`(E-3 讀「推薦N級」) | enum: minimize_overflow/nearest_target/farthest_target | minimize_overflow(總開關預設關) | ✅(啟用待 L3 校準) |

> E-3/E-4:卡片左上「推薦N級」OCR = 該卡目標等級(`ScreenOption.recommendation_target_level`),不需維護每卡 max 庫。`recommendation_target.enabled` 預設 false → 退現行排序 byte-identical;翻 true 才啟用 minimize_overflow(溢出最小、不浪費),首次啟用需 L3 貼圖驗多卡推薦級。

## F. 結算(達標門檻)

| 決策項 | 現況 | 型別 | 預設 | 狀態 |
|---|---|---|---|---|
| 評分達標門檻(主) | `result.rating_threshold` | int (0=停用) | 30 | ✅ |
| 潛能總等級門檻(輔,OR) | `result.potential_total_threshold` | int (0=停用) | 0 | ✅ |

## G. 速度/感知/硬體(已有,版本/環境相依,值得放 GUI 進階頁)
`bot.poll_interval` / `click_settle` / `take_settle_delay` / `adaptive_settle.*`(勿開) / `ocr_cache.*` / `stuck_poll_limit` / `max_unknown_streak`；`vision.detector`(v1/v2) / `ocr.languages` / `gpu`；`window.capture_mode` / `input.mode`；`run.max_runs`。

---

## 🔴 使用者列了但 bot 還缺的(待新增後端,⬜)— 本批後剩餘

1. ~~A 事件中間風險檔~~ → ✅ **A-1 balanced 已做**。
2. ~~B 刷新時機/條件~~ → ✅ **B `shop.refresh.trigger` 已做**(「每進店先刷」需改時序,另開子項)。
3. **B 分樓層策略**(完全無,且 current_floor 死碼、需先補樓層 OCR)— 仍 ⬜。
4. ~~D 狂買特定音符~~ → ✅ **D `note_spree` 已做**。
5. **D 刷新清空特定部分貨品**(現只清整架)— 仍 ⬜(延後:需格位類型追蹤)。
6. ~~E 低於特定等級不拿~~ → ✅ **E-2 `min_level_threshold` 已做**(legacy 模式)。
7. ~~E 目標等級不同時的升等判斷~~ → ✅ **E-3/E-4 已做**(推薦N級 OCR + minimize_overflow,啟用待 L3 校準)。

## ⚠️ 兩個「假旋鈕」(列 GUI 前須先實作後端,否則調了沒反應)
- `run.stop_on_target_level`:bot.py:551 是 `pass` 的 TODO,恆無效
- `current_floor`:恆 0 死碼(分樓層策略的前提)

## 📌 本批延後項(技術債,非阻塞)
- **reward_rank 提示字外部化**:`_event_reward_rank` 的「稀有潛能/潛能/音符/金幣」token 仍在 states.py(裸字面);test_signatures 不強制(不在黑名單)、報酬序使用者拍板固定。鐵則2 完整化可後補。
- **D clear_category**(刷新清哪類):語意需「格位類型追蹤」(purchased_slots 只存 slot_key 無類型),且刷新後貨全換、清哪類記錄價值低 → 延後。
- **B 「每進店先刷」**:需改 handler 時序(非純 config),另開子項。

---

## 後端優先序建議(供拍板)— 本批後更新
做 GUI 前,⬜ 項要先補後端(否則旋鈕無效)。剩餘建議序:
1. **B 分樓層**(大,需樓層 OCR,最後)— 唯一剩的大項。
2. D clear_category / reward_rank 外部化 / B 每進店先刷(小,延後項)。
之後 D2 schema 驅動面板(Phase 3④)把登記表 ✅ 項 render 成控件(加旋鈕=登記表加一條,不改 GUI 程式)。
