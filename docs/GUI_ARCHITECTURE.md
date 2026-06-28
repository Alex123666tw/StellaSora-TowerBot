# GUI 架構書(GUI_ARCHITECTURE)

> 產出:2026-06-14｜範圍:Phase 3 GUI —— 模塊化設定面板 + 即時決策監控圖。
> 定位:這是 GUI 的**設計與實作藍圖**。與 REPAIR_PLAN(整體修復架構)/REMAINING_TASKS(工單 區段 D)/GAME_MECHANICS(機制帳本)互補。
> 技術棧:**PyQt5 + qfluentwidgets**(Fluent Design,內建動畫)。
> 對應 REMAINING_TASKS 區段 D(D1 登記表 / D2 設定面板 / D3 監控圖)。

---

## 0. 一句話目標

把現有 GUI 從「手刻控制台」升級成兩件事:
1. **schema 驅動的模塊化設定面板** —— 所有策略旋鈕在這調,**加旋鈕不必改 GUI 程式碼**。
2. **即時決策監控圖** —— FSM 節點圖**階梯式登場** + **聚光燈跟著當前狀態走**,讓你邊跑邊看 bot 在判什麼、要點哪。

---

## 1. 範圍界定(先講清楚不做什麼)

- ❌ **不重寫現有 GUI**:攻略解析卡 / 核心控制卡 / log console 全保留,本架構是「**擴充**」。
- ❌ **不走 Artifacts**:Artifacts 是沙箱網頁,碰不到 win32 / OpenCV / 遊戲視窗,**無法當真 GUI**(已確認)。真 GUI 一律 PyQt 桌面;Artifacts 至多當設計原型沙箱。
- ❌ **GUI 不含遊戲邏輯**:GUI 只「讀寫 config + 顯示 signal」;決策永遠留在 `core/`。
- ✅ **做**:單一設定登記表、schema 驅動設定面板、即時決策監控圖。

---

## 2. 現況盤點

### 2a. 既有 GUI(`gui/`)— 保留並擴充
| 檔 | 現況 | 對本架構的意義 |
|---|---|---|
| `app.py` `StellaSoraApp(QWidget)` | 三張 CardWidget:攻略解析 / 核心控制 / 即時監控(state/floor/money/runs/cards label + log console) | 監控卡是層 3 的容器;新增「設定面板卡」為層 2 |
| `app.py` `load_config`/`save_config` | 讀寫 `config.yaml`,**已保留未知 key** | schema 擴充的現成地基 —— 不會洗掉手寫設定 |
| `signals.py` | `log_msg` / `status_update` / `parser_result` / `finished` / `error` | 跨執行緒管線已建好;層 3 再加一條 `monitor_update` |
| `workers.py` `BotWorker(QThread)` | 跑 `StateMachine`,monkey-patch `_detect_state` → emit `status_update({floor,runs,max_runs,success,state,money,card_counter})`;`GuiLogHandler` 把 logging 導進 `log_msg` | 監控圖的資料就從這個 patch 點 emit;**不需動 core** |

### 2b. 已完成的 config 層(Phase 3 參數化,2026-06-14)
策略已全 config 化,**承載點就緒,缺的只是 GUI 介面**:
- `event.strategy`(激進/保守)、`shop.upgrade.times_by_visit` / `price_ceiling` / `order`、`shop.buy.strategy`(真經濟 cards_then_notes)、`shop.buy.affordability`、`card_counter.target_total`(78)、`result.rating_threshold`(30)、`data/screen_tokens.yaml`(OCR 提示字外部化)。

---

## 3. 設計核心原則:單一來源、schema 驅動

專案已有兩個成功的「單一來源」模式:
- `GAME_MECHANICS.md` = 遊戲假設的單一帳本
- `vision/signatures.py` + `data/screen_tokens.yaml` = 畫面文字的單一來源

**GUI 用同一招,建第三本:可調設定登記表 →(`config.yaml` + GUI)都從它衍生。**

- **加一個旋鈕 = 在登記表加一條**(key / 型別 / 範圍 / 預設 / 模組 / 說明)→ GUI 自動 render 對應控件,**GUI 程式碼一行不改**。
- 杜絕 GUI 與 config.yaml **雙寫漂移**(B6 曾 GUI 31 ↔ config 78 不一致就是這病)。
- runtime 永遠讀 `config.yaml`(`ctx` 由 `_build_context` 帶上),GUI 改完下次跑生效。

---

## 4. 三層架構

```
┌──────────────────────────────────────────────┐
│ 層 1  可調設定登記表 (settings_schema)         │  ← 單一來源
│   每條:key 路徑 / label / type / range / default / module
└───────────────┬──────────────────────────────┘
        衍生     │
   ┌────────────┴────────────┐
   ▼                         ▼
┌─────────────────┐   ┌──────────────────────────┐
│ 層 2 設定面板    │   │ config.yaml (runtime 讀)  │
│ 照 schema 自動   │   └──────────────────────────┘
│ render 控件      │
└─────────────────┘
┌──────────────────────────────────────────────┐
│ 層 3  即時決策監控圖                            │  ← 接 signal,只讀不寫
│   FSM 節點圖 + 階梯登場 + 聚光燈 + 決策側欄      │
└──────────────────────────────────────────────┘
```

### 層 1 — 可調設定登記表(`gui/settings_schema.py`)
**做什麼**:用資料宣告所有可調項,一處定義。
**形狀**(每條一個 dataclass / dict):
```python
Setting(
    key="shop.buy.strategy",        # config.yaml 巢狀路徑
    label="商店買法",                # 中文標籤
    type="enum",                    # int|float|bool|enum|list|str
    options=["cards_then_notes","all","cards_only","notes_only"],
    default="cards_then_notes",
    module="商店",                   # 分組到哪張卡
    help="達 78 前買卡片,達標後買缺口音符",
)
```
**模組分組**:選卡 / 商店 / 事件 / 結算 / 音符 / 執行。
**影響檔案**:新增 `gui/settings_schema.py`。
**驗收**:登記表涵蓋附錄 A 全部旋鈕;每條 key 對得上 `config.yaml` 真實路徑。

### 層 2 — 模塊化設定面板(擴充 `gui/app.py`)
**做什麼**:照層 1 自動生成控件,不手刻。
**做法**:
- 每個 `module` 一張可折疊 CardWidget。
- 每條 Setting 依 `type` render:`int/float`→SpinBox、`bool`→CheckBox、`enum`→ComboBox、`list`→LineEdit(逗號分隔)、`str`→LineEdit。
- 讀:`load_config` 照 schema 的 key 路徑從 config.yaml 取值填控件(無值用 default)。
- 寫:`save_config` 把控件值寫回 config.yaml **巢狀路徑**(沿用「保留未知 key」)。
**影響檔案**:`gui/app.py`(新增 `setup_settings_cards` + 泛型 render/bind)。
**驗收**:改任一控件 → 存 → 重啟 bot → 行為改變(log 佐證);GUI ↔ config.yaml 雙向一致。

### 層 3 — 即時決策監控圖(動態節點圖)★ 本架構重點
**做什麼**:把 FSM 畫成節點圖,**階梯式登場**,聚光燈跟著 bot 當前狀態,旁邊一條即時決策流。讓實機跑時「卡在哪一階」一眼看到 —— 直接服務 L3 續跑測試。

**視覺規格**(已做 mockup 確認方向):
- 主軸(垂直,一階一階往下):待機→大廳→編隊→準備→快速戰鬥→探索完成→結算。
- 戰鬥中互動(縮排子群,可循環):點擊繼續 / 獲得音符 / 選潛能 / 隨機事件 / 商店選擇 / 商店。
- **登場動畫**:每節點一支動畫(opacity/位置),**index × 延遲錯開** → 階梯式長出來。
- **聚光燈**:當前狀態節點換色 + 發光 + pulse;走過的節點淡標。
- **決策側欄**:當前狀態(中+英)、辨識信心 bar、下一步動作、metrics(樓層/金錢/本輪/卡片總等級)、LIVE 指示燈。
- (進階,可後加)**畫面疊圖**:鏡像 `last_frame` 縮圖 + 疊偵測 ROI / 點擊紅圈 / OCR 框。

**PyQt 對應**:
| 規格 | 做法 |
|---|---|
| 節點圖 + 連線 | `QGraphicsView` + `QGraphicsScene`,節點 = 圓角 item |
| 階梯登場 | 每節點一支 `QPropertyAnimation`(opacity/pos),`QSequentialAnimationGroup` 或 index×delay 錯開 |
| 聚光燈 | 接 signal → 對應節點換 brush/pen + pulse(`QPropertyAnimation` loop) |
| 決策側欄 / metrics | 一般 widget,接同一 signal 更新 |
| (進階)疊圖 | `QGraphicsPixmapItem`(縮小 QImage)+ 疊 overlay item |

**資料管線**(關鍵,但**資料全現成**):
- 新增 `signals.monitor_update`,payload:
  ```python
  {state, confidence, evidence, next_action, click_point, metrics:{floor,money,run,card_total}}
  ```
- emit 點:`workers.py` 既有的 `patched_detect`(已有 ctx),或 handler 點擊前。資料來源:
  - `state` + `confidence` + `evidence` ← `ctx.state_trace`(StateDetector v2 已產)
  - `next_action` / `click_point` ← `ctx.click_trace`
  - metrics ← 既有 status 欄位
- (進階疊圖)另 emit 縮小過的 `QImage(ctx.last_frame)`。

**影響檔案**:新增 `gui/monitor_view.py`(QGraphicsView 子類);`gui/signals.py`(+ `monitor_update`);`gui/workers.py`(emit);`gui/app.py`(嵌入監控卡)。
**驗收**:bot 跑動時聚光燈即時跟著狀態走;登場動畫正確;側欄資料與 log 一致。

---

## 5. 技術約束(務必遵守)

1. **跨執行緒鐵律**:`BotWorker` 在 QThread,GUI 在主執行緒。**任何 GUI 更新一律走 signal**,絕不從 worker 執行緒直接碰 widget(現有程式已遵守)。
2. **監控圖推送節流**:若推 frame 疊圖,emit **縮小過的 `QImage`**、節流 **2–5 fps**、別阻塞 bot 迴圈(poll ~每 1–2s,綽綽有餘)。
3. **資料零成本**:state/信心/下一步/metrics 都已在 trace 裡,監控圖是「接線」不是「造資料」。
4. **裝飾動畫先跳過**:qfluentwidgets 的 Fluent 轉場順手就有,但監控工具要的是「即時、一眼清」,別為純裝飾花時間;登場動畫 + 聚光燈是功能性的,做;漣漪/滑入那種,略。

---

## 6. 整合接點(影響檔案總表)

| 檔案 | 動作 |
|---|---|
| `gui/settings_schema.py` | 新增(層 1 登記表單一來源) |
| `gui/app.py` | 擴充:`setup_settings_cards`(層 2)+ 嵌入監控卡(層 3);泛型 render/load/save |
| `gui/monitor_view.py` | 新增(層 3 `QGraphicsView` 節點圖 + 階梯動畫 + 聚光燈) |
| `gui/signals.py` | 新增 `monitor_update` signal |
| `gui/workers.py` | 在 patched detect 後 emit `monitor_update`(讀 ctx 既有 trace) |
| `config.yaml` | 不新增 schema 邏輯,純承載值(已就緒) |

---

## 7. 時機與相依

放在 **REMAINING_TASKS 區段 A/B/C 之後**(感知層音符已完成、機制與門檻定了再做 GUI),避免對著未定案旋鈕做控件而重工。
- 層 1 + 層 2(設定面板)可先做,因 config 旋鈕已穩定。
- 層 3(監控圖)獨立性高,**可當獨立小件提前做** —— 它讓每次珍貴的 L3 續跑資訊量翻倍(邊看聚光燈邊抓誤判),與測試流程強綁。

---

## 8. 驗收(GUI DoD)

- **設定面板**:改任一旋鈕 → 存 → 重啟 → bot 行為改變;GUI 與 config.yaml 雙向一致;新增旋鈕只動 schema 不動 GUI。
- **監控圖**:bot 跑動時聚光燈即時跟狀態;登場動畫正確;側欄與 log 一致;(進階)暫停後 bot 零點擊、可單步。
- 全程:既有三卡功能不退化;無 mojibake 殘留(動工前先掃一遍 `gui/app.py` 字串)。

---

## 附錄 A — 待登記的可調項清單(層 1 種子)

| 模組 | key (config 路徑) | 型別 | 預設 | 來源 |
|---|---|---|---|---|
| 選卡 | `decision.mode` | enum | recommendation_badge | 既有 |
| 選卡 | `decision.required` / `backup` / `guaranteed` | list | — | 既有 |
| 選卡 | `decision.max_reroll_before_backup` | int | 3 | 既有 |
| 選卡 | **`card_counter.target_total`(原 B6)** | int | 78 | **Phase 2 移入** |
| 商店 | **`shop.upgrade.times_by_visit`(原 C2)** | dict/list | {1:2,2:3} | **Phase 2 移入** |
| 商店 | `shop.upgrade.price_ceiling` | int | 540 | 既有 |
| 商店 | `shop.upgrade.order` | enum | upgrade_first | 既有 |
| 商店 | `shop.buy.strategy` | enum | cards_then_notes | 既有 |
| 商店 | `shop.buy.affordability` | bool | true | 既有 |
| 事件 | **`event.strategy`(原 E1)** | enum | aggressive | **Phase 2 移入** |
| 結算 | `result.rating_threshold` | int | 30 | 既有 |
| 結算 | `result.potential_total_threshold` | int | — | 既有(OR 條件) |
| 執行 | `run.max_runs` | int | 1 | 既有 |
| 執行 | `bot.poll_interval` | float | 1.0 | 既有 |

> 註:C2 / E1 / B6 為使用者 2026-06-14 拍板從 Phase 2 移入(本是使用者可調項)。Phase 2 期間沿用上表預設值,Phase 3 由本面板暴露調整。
