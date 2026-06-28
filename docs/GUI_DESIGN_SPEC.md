# GUI 設計定稿(GUI_DESIGN_SPEC)

> 產出:2026-06-16｜整合使用者拍板的 10 點需求 + 原 `GUI_ARCHITECTURE.md` 三層架構。
> 定位:**Phase 3④ GUI 的實作單一依據**。先設計再實作,避免返工。
> 技術棧:PyQt5 + qfluentwidgets(沿用現有 `gui/`)。
> 原則:**schema 驅動**(加旋鈕=登記表加一條,不改 GUI 程式)、**GUI 只讀寫 config + 顯示 signal**(決策永遠在 `core/`)、**中文呈現英文存底**。
> **視覺 mockup**:`docs/gui_mockups/index.html`(瀏覽器直接開;設定頁 + 監控頁,已含本文所有新拍板調整)。實作前照此視覺,別重畫跑掉。

---

## 0. 使用者拍板記錄(2026-06-16)

| # | 需求 | 拍板 |
|---|---|---|
| 1 | 控件選項中文化 | label/選項顯示中文,config 仍存英文 |
| 2 | 每選項說明 | 做 help 介面(第 10 點) |
| 3 | 結算加項 | 祕聞全解開關(=音符全達標)+ 潛能加總門檻開關 |
| 4 | 事件 | 策略放事件模組 + **事件編輯器(本波一起做)**,事件規則 config 化 |
| 5 | 商店細項 | 第幾次先升級機/商店(per-visit)+ 刷新從第 N 次商店啟用 |
| 6 | 緊急中止快捷鍵 | 現有 Ctrl+Q,做成 GUI 可設定 |
| 7 | 決策方案區 | 改成「當前生效設定總覽」 |
| 8 | 對照登記表補漏 | 已補(見 §2) |
| 9 | 設定檔管理 | 保存/載入/導出/匯入 + 命名(多 profile) |
| 10 | help 介面 | 每選項功能說明 |
| — | 樓層死碼 | 監控頁「樓層」改「商店造訪次數」(shop_visit_count 真值) |
| — | 祕聞全解判定 | =所有協奏音符達標(current_notes ≥ target_notes),不靠 OCR 祕聞數 |
| — | 進階頁 | 獨立「進階」頁,非安全旋鈕需勾選「我了解風險」才解鎖 |

## 0b. 第二輪調整(2026-06-16,對著 mockup 細修)

1. **事件規則卡**:每條改成上「**問題**(比對的畫面問句)」、下「**選項**(要選的)」兩層,比單行清楚。
2. **結算潛能加總門檻**:做成**開關 + 數值框**(開關啟用、框填角色潛能加總最低數量),非純數字。
3. **拿掉頂部常用區**,旋鈕全下放細項兩欄;頂部工具列改「**設定檔與啟動**」—— **開始鈕只在設定頁、停止鈕只在監控頁**,兩者互斥(跑起來→開始鎖、停止亮;停了→反過來);最大輪數放工具列。
4. **整體流暢動畫**(qfluentwidgets Fluent 轉場 + QPropertyAnimation),避免「軟體卡住」感 —— **列入 GUI DoD**(頁籤切換/控件/載入/聚光燈都要順)。
5. **商店補旋鈕**(⭐ 需新後端,加進 §3):
   - `shop.buy.buy_non_discounted`(買非特價商品,bool,預設 true=現行也買原價)
   - `shop.buy.note_priority`(音符購買優先序,list[音符名],預設空=現行只補缺口順序)
   - 逐次強化(`times_by_visit`)+ 順序(`order_by_visit`)在商店模組以「逐次」UI 列清楚;強化價上限、達標後音符策略(`note_spree`)同列。

> §2/§3 的商店、結算欄位依本節為準(buy_non_discounted、note_priority 為新增;潛能門檻開關化)。

---

## 1. 整體架構

```
┌─ 主視窗 StellaSoraApp ───────────────────────────────────┐
│ 頂部工具列:[設定檔 ▾ 命名/保存/載入/導出/匯入]  [▶ 開始][■ 停止(Ctrl+Q)]  [? help] │
├─ 頁籤 ───────────────────────────────────────────────────│
│ [設定]            [監控]              [進階]               │
├──────────────────────────────────────────────────────────│
│ 設定頁:常用區 + 細項兩欄(選卡/商店/事件/結算)             │
│   事件模組內含「編輯事件」入口 → 事件編輯器對話框          │
│ 監控頁:LIVE 狀態 + 進度 metrics + FSM 聚光燈 + 音符進度    │
│         + 當前生效設定總覽                                 │
│ 進階頁:速度/感知/硬體;危險項鎖定,勾「我了解風險」解鎖    │
└──────────────────────────────────────────────────────────┘
```

- **保留現有**:攻略圖解析卡(Parser)、log console、跨執行緒 signal 管線、`load_config`/`save_config`(保留未知 key)。
- **三大頁籤**:設定 / 監控 / 進階。設定檔管理 + 開始/停止 + help 放頂部工具列(跨頁常駐)。

---

## 2. 完整 settings_schema(層 1 單一來源)

> 檔案:`gui/settings_schema.py`。每條一個 `Setting`:`key`(config 巢狀路徑)/`label`(中文)/`type`/`options`(中文顯示↔英文值對照)/`default`/`module`/`tier`/`help`。
> `tier`:`normal`(一般)/`test`(測試版,藍標,開了改決策)/`advanced`(進階)/`danger`(進階-危險,需風險勾選)。
> render:`int/float`→SpinBox、`bool`→SwitchButton、`enum`→ComboBox(中文)、`list`→Tag/LineEdit、`dict`→自訂小表。

### 模組:選卡
| key | label | type | 選項(中文=英文) | 預設 | tier |
|---|---|---|---|---|---|
| `decision.mode` | 決策模式 | enum | 推薦徽章=recommendation_badge / 累計模式=legacy | 推薦徽章 | normal |
| `card_counter.target_total` | 卡片總等級目標 | int | — | 78 | normal |
| `decision.upgrade_strategy` | 升等策略 | enum | 溢出最小=minimize_overflow / 最接近目標=nearest_target / 補最缺=farthest_target | 溢出最小 | normal |
| `decision.recommendation_target.enabled` | 啟用推薦N級 | bool | — | 關 | **test** |
| `decision.required_target_level` | required 滿級 | int(1-6) | — | 6 | normal |
| `decision.max_reroll_before_backup` | reroll 上限後降備選 | int | — | 3 | normal |
| `decision.prefer_never_picked` | 優先沒拿過的 | bool | — | 開 | advanced |
| `decision.prefer_higher_gain` | 優先升等量大 | bool | — | 開 | advanced |
| `decision.min_level_threshold` | 弱卡升等門檻(僅 legacy) | int | — | 0 | advanced |
| `decision.guaranteed` | 保底潛能(粉色最優先) | list | — | 6 項 | normal |
| `decision.required` | 必選潛能 | list | — | 7 項 | normal |
| `decision.backup` | 備選潛能 | list | — | 7 項 | normal |
| `decision.level_required` | 限量必選(名稱+目標等級) | dict-list | — | 2 項 | advanced |
| `decision.backup_groups` | 備選互斥群組 | list-of-list | — | 3 組 | advanced |

> 潛能清單(保底/必選/備選)整合現有「攻略圖解析卡」的編輯區 —— 攻略解析自動填、面板也能手調。

### 模組:商店
| key | label | type | 選項 | 預設 | tier |
|---|---|---|---|---|---|
| `shop.buy.strategy` | 買法策略 | enum | 先卡後音符=cards_then_notes / 全買=all / 只買卡=cards_only / 只買音符=notes_only | 先卡後音符 | normal |
| `shop.buy.affordability` | 買得起才點 | bool | — | 開 | normal |
| `shop.buy.prefer_discount` | 買卡優惠優先 | bool | — | 關 | **test** |
| `shop.buy.discount_scope` | 優惠優先範圍 | enum | 只音符=notes_only / 含買卡=cards / 全部=all | 只音符 | advanced |
| `shop.refresh.trigger` | 刷新時機 | enum | 買完才刷=exhausted / 從不=never / 一律=always / 有缺口=when_gap / 未達標=before_target | 買完才刷 | normal |
| `shop.refresh.start_from_visit` ⭐ | 刷新從第幾次商店啟用 | int | — | 1 | normal |
| `bot.max_shop_refresh` | 刷新次數上限 | int | — | 1 | normal |
| `shop.upgrade.enabled` | 強化總開關 | bool | — | 開 | normal |
| `shop.upgrade.times_by_visit` | 第N次造訪強化幾次 | dict | — | {1:2,2:3} | normal |
| `shop.upgrade.price_ceiling` | 強化價上限 | int | — | 540 | normal |
| `shop.order_by_visit` ⭐ | 第幾次先升級機/先商店 | dict | 每次:先強化=upgrade_first / 先商店=shop_first | 全 upgrade_first | normal |
| `shop.post_target.note_spree` | 達標後狂買音符 | group | enabled/notes/max_spend | 關 | advanced |

⭐ = 需新增後端(見 §3)。

### 模組:事件
| key | label | type | 選項 | 預設 | tier |
|---|---|---|---|---|---|
| `event.strategy` | 事件策略 | enum | 激進=aggressive / 中間=balanced / 保守=conservative | 激進 | normal |
| `event.refuse_note_cost` | 拒消耗音符 | bool | — | 開 | normal |
| `event.aggressive_gamble_mode` | 賭博選錢多 | bool | — | 開 | advanced |
| `event.same_option_repeat_limit` | 連點放棄門檻 | int | — | 3 | advanced |
| `event_rules`(編輯器) ⭐ | 編輯事件規則 | editor | — | — | normal |

### 模組:結算
| key | label | type | 選項 | 預設 | tier |
|---|---|---|---|---|---|
| `result.rating_threshold` | 評分達標門檻 | int | — | 30 | normal |
| `result.require_all_secrets` ⭐ | 祕聞全解才達標 | bool | — | 關 | normal |
| `result.potential_total_threshold` | 角色潛能加總門檻 | int+開關 | — | 關(0) | normal |

### 模組:執行(放頂部工具列或執行區)
| key | label | type | 預設 | tier |
|---|---|---|---|---|
| `run.max_runs` | 最大輪數 | int | 1 | normal |
| `bot.hotkey_stop` ⭐ | 緊急中止快捷鍵 | hotkey | ctrl+q | normal |

### 進階頁(獨立 tab,危險項需風險勾選)
| key | label | tier |
|---|---|---|
| `bot.poll_interval` | 輪詢間隔 | advanced |
| `bot.click_settle` / `take_settle_delay` | 點擊/拿走沉澱 | advanced |
| `bot.ocr_cache.enabled` | OCR 快取 | advanced |
| `bot.adaptive_settle.enabled` | 自適應沉澱 | **danger**(已知會誤判過場) |
| `vision.detector` | 偵測器版本 v1/v2 | **danger** |
| `ocr.languages` / `ocr.gpu` | OCR 語言/GPU | **danger** |
| `window.capture_mode` / `input.mode` | 擷取/輸入模式 | **danger** |
| ~~`run.stop_on_target_level`~~ | (假旋鈕,後端 TODO) | 不列,或標「無效」 |

---

## 3. 後端前置(⭐ 項,GUI 前先做;一子項一 commit,先紅後綠,byte-identical 預設)

1. **`result.require_all_secrets`**(祕聞全解):`handle_result` 達標判定加分支 —— 開啟時,達標條件 = 所有 `target_notes` 都 `current_notes ≥ target`(音符全達標=6 祕聞全解)。預設關=現行。回寫 GAME_MECHANICS(祕聞=音符達標)。
2. **`result.potential_total_threshold` 語意+開關**:現已存在(輔依據 OR),GUI 包成「開關 + 數值」。確認語意=角色潛能加總 ≥ N。
3. **`shop.refresh.start_from_visit`**:`_refresh_trigger_allows` 加條件 —— `shop_visit_count < start_from_visit` 時不刷。預設 1=現行(第一次就可刷)。
4. **`shop.order_by_visit`**:`_shop_order` 從全域字串改成「查 visit_count → 該次的 order」,缺則退全域 `shop.order`。預設全 upgrade_first=現行。
5. **`event_rules` 後端**(事件 config 化,見 §4):事件偵測先查使用者規則,命中照規則選,否則走現有 strategy 評分。預設空規則=現行。

---

## 4. 事件 config 化(編輯事件,§D 最大項)

**目標**:使用者能新增/編輯「特定事件 → 固定選某選項」規則,防遊戲更新後新事件 bot 不認。

**設計:`data/event_rules.yaml`(quiz_answers.json 的泛化,使用者層,優先於 generic 評分)**
```yaml
# 使用者自訂事件覆蓋規則。比對命中 → 直接選指定選項;未命中 → 落回現有 strategy 評分。
overrides:
  - id: quiz_favorite_number
    match_any: ["最喜歡哪個數字", "喜歡哪個數字"]   # 畫面文字含任一 → 命中此事件
    pick_any: ["總是如此", "3"]                      # 選含任一字的選項
    note: "猜數字 quiz,正解 3"                       # 給使用者看的說明
  - id: ...
```

**後端**(`states.py` `_select_event_option` 開頭):
- 載入 `event_rules.yaml`(同 quiz_answers 機制)。
- 對畫面 OCR 文字比對 `match_any` → 命中則在選項中找 `pick_any` → `_pick_event_click_target` 點它。
- 未命中 → 現有 quiz 題庫 / 升級事件 / strategy 評分(全不動)。

**GUI 事件編輯器**(對話框):表格列出 `overrides`,每列 = id/match/pick/note,可新增/編輯/刪除/上下移(優先序)。存回 `event_rules.yaml`。

**分期**:① 後端 event_rules 載入+比對(先紅後綠)→ ② GUI 編輯器。現有 quiz_answers.json 可併入或並存。

---

## 5. 設定檔管理(多 profile)

- 設定檔存 `configs/<名稱>.yaml`(或 config.yaml + `profiles/`)。
- 頂部工具列下拉:目前 profile 名 + [新建命名][另存][載入][導出檔案][從檔案匯入]。
- 載入 = 覆寫 `config.yaml`(runtime 讀的);切換 profile 即切設定。
- 命名/導出/匯入讓使用者分享設定檔(開源後社群可交流套組)。

---

## 6. help 介面

- **每控件**:label 旁 `?` 小圖示,hover/點出 tooltip = schema 的 `help` 欄。
- **獨立 help 頁/對話框**:按頂部 `? help` → 列出所有選項 + 完整說明(schema 衍生,加旋鈕自動進 help)。
- 涵蓋第 2 點(選卡選項等每項是什麼)。

---

## 7. 進階頁安全鎖

- 進階 tab 頂部:`☐ 我了解調整這些可能讓 bot 失常`,未勾 → `danger` 旋鈕灰鎖不可調(`advanced` 可調)。
- 勾選 → 解鎖 `danger`。每次重開 GUI 重置為鎖定(防誤觸)。

---

## 8. 緊急中止快捷鍵

- 現有 `gui/app.py` 已綁 `keyboard.add_hotkey('ctrl+q')` 全域中止。
- 做成 `bot.hotkey_stop` 旋鈕:GUI 顯示目前鍵、可錄製改鍵;重綁 hotkey。預設 ctrl+q。

---

## 9. 監控頁(層 3,接 signal 只讀)

- **LIVE 狀態列**:當前狀態(中文+英文)、辨識信心 bar、LIVE pulse 燈。
- **進度 metrics**:商店造訪次數(代樓層)、卡片總等級 N/78、金錢、本輪 N/max、紀錄保留目標。
- **FSM 流程聚光燈**:大廳→編隊→準備→快速戰鬥→[選卡/音符/事件/商店循環]→結算;走過打勾、當前高亮 pulse、未到灰。階梯登場動畫。
- **協奏音符進度**(開合):各音符 current/target 進度條。
- **當前生效設定總覽**(開合,取代原「決策方案」):列出所有旋鈕當前值,邊跑邊確認生效設定。
- 資料管線:`gui/signals.py` 加 `monitor_update`,`gui/workers.py` 既有 `patched_detect` emit(讀 `ctx` 既有 trace);**不動 core**。

---

## 10. 實作順序(一層一 commit,先紅後綠;後端前置 byte-identical 預設)

| 步 | 內容 | 檔案 |
|---|---|---|
| 1 | **後端前置**(§3 五項,各一 commit) | `core/states.py` `core/bot.py` `config.yaml` `data/event_rules.yaml` + tests |
| 2 | 層1 `settings_schema`(完整旋鈕 + 中文 + help + tier) | `gui/settings_schema.py` + test(每 key 對得上 config 路徑) |
| 3 | 層2 設定頁(常用區+細項兩欄,泛型 render/load/save,中文化) | `gui/app.py` |
| 4 | 進階頁 + 安全鎖 | `gui/app.py` |
| 5 | 設定檔管理(多 profile) | `gui/app.py` + `gui/profiles.py` |
| 6 | help 介面 | `gui/app.py` |
| 7 | 緊急中止快捷鍵可設定 | `gui/app.py` |
| 8 | 事件編輯器(對話框,接 §4 後端) | `gui/event_editor.py` |
| 9 | 層3 監控頁(FSM 聚光燈 + 進度 + 音符 + 設定總覽) | `gui/monitor_view.py` `gui/signals.py` `gui/workers.py` |

**驗收**(GUI DoD):改任一旋鈕→存→重啟 bot→行為變(log 佐證);GUI↔config 雙向一致;加旋鈕只動 schema;監控頁聚光燈即時跟狀態;既有三卡不退化;無 mojibake。

---

## 附:與既有文件關係
- 取代 `GUI_ARCHITECTURE.md` 的附錄 A(舊種子清單)→ 本文 §2 為完整版。
- `DECISION_REGISTRY.md` 仍是決策旋鈕的語意帳本;本文 §2 是其 GUI 化的 schema。
- 後端前置(§3)+ 事件 config(§4)完成後回寫 `DECISION_REGISTRY.md` / `GAME_MECHANICS.md`。
