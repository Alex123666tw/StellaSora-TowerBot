# 遊戲機制假設清單(GAME_MECHANICS)

> 用途:程式碼中所有「對遊戲行為的假設」集中在此逐條管理。
> 規則:涉及機制的改動,動工前查這裡;實機驗證後**回寫狀態欄**。
> 狀態:✅ 已驗證|❓ 未驗證|⚠️ 程式與假設不符(死碼/半實作)|🔧 實作中/待實機驗證|🗑️ 已決定移除
> 驗證方法縮寫:`L1` = 回放截圖(tests/replays/frames/)、`L4` = Claude 電腦控制實機截圖確認(見 REPAIR_PLAN §6)。

## A. 探索主流程

| # | 假設 | 程式碼依據 | 狀態 | 驗證方法 |
|---|------|-----------|------|---------|
| A1 | 流程:大廳 →(快速戰鬥)→ 編隊(下一步)→ 準備頁(開始戰鬥)→ 快速戰鬥 → 中途各互動畫面 → 第 20 層探索完成 → 結算 → 返回大廳 | `core/states.py` handler 鏈 | ❓(單段已實測通過:大廳→準備頁) | L3 完整一輪 |
| A2 | 快速戰鬥期間畫面無需操作,等待狀態自動切換 | `handle_fast_battle` 回傳 None | ❓ | L4 觀察 |
| A3 | 「點選空白處繼續」畫面點擊安全空白點即可推進 | `handle_tap_continue`、`_safe_blank_point` | ❓ | L1 + L4 |
| A4 | 探索固定 20 層;第 20 層有「探索完成」獨立畫面 | `_is_last_shop_floor(>=20)`、`STATE_EXPLORE_COMPLETE` 關鍵字「第20層」 | ⚠️ 樓層恆 0,20 層邏輯死碼(REPAIR_PLAN 2.2) | L4 截圖終層 |

## B. 潛能選卡

| # | 假設 | 程式碼依據 | 狀態 | 驗證方法 |
|---|------|-----------|------|---------|
| B1 | 三選一;卡片提供 +1~+3 升等;required 目標 Lv.6 | `decision_engine.py` 註解、`REQUIRED_TARGET_LEVEL=6` | ❓(legacy 模式) | L4 |
| B2 | 半周年模式:需要的卡有紅色「推薦」標籤,達標後標籤消失;無標籤 → reroll | `_decide_recommendation_badge`、`signatures.recommendation_badge_color_hit`(HSV 紅色偵測,1.1 起單一來源) | ❓(實機打中過一次 +5,未完整驗證) | L1 紅標截圖 + L4 |
| B3 | 粉卡(保底)畫面上**沒有**等級標記,以此判定 `is_pink`,不計入卡片總等級 | `_extract_slot_options` `is_pink = not has_level_marker` | ❓ | L4 截圖粉卡 |
| B4 | 選卡後需點「拿走」按鈕確認 | `signatures.TAKE_BUTTON_TOKENS` + `_click_take_button`(1.3 起 `EXPECT_NONE`:點後畫面未驗證,B4 驗證前不加 expect) | ❓ | L1 |
| B5 | Reroll 在右下角是**圓形 🔄 icon(無文字、旁標花費「40」)**,熱鍵 **Q**(底部列「Q 更新」);取卡熱鍵 Space(「Space 推取」) | `_reroll_potential_cards` 改送 **Q 鍵**(`input.press_key('q')`);`REROLL_BUTTON_TOKENS` 降為無鍵盤能力時的後備文字點擊(找不到 icon 文字仍 R3 不盲點) | ✅ **L1 已驗(last_frame 20260613_223142):reroll 鈕非文字按鈕,是 icon+Q 熱鍵 → 原文字點擊永遠 target_not_found 卡死** | Q 真能 reroll 待 L3 觀察(畫面變新卡組) |
| B8 | Reroll 重抽後畫面內容必變(ROI hash 改變) | 主路徑改 Q 鍵後不驗 ROI;`ExpectRoiChange` 僅留在無鍵盤後備文字點擊。連抽無上限改由 `_decide_recommendation_badge` 的 reroll 上限(`max_reroll_before_backup`)+ 達上限 fallback 取最佳卡兜底(永不卡死) | ❓ Q 是否真重抽待 L3 觀察 | L3 觀察 |
| B6 | 卡片總等級目標 78(半周年);達標 = 本輪 required 完成 | `config card_counter.target_total: 78` | ❓(GUI 預設 31 與 config 78 不一致,REPAIR_PLAN 2.6) | 使用者確認攻略 |
| B7 | 隊伍升級畫面是二選一(非三選一),標題含「強化/升級」 | `_detect_expected_card_count` → 2 | ❓ | L1 |

## C. 商店(SHOP_CHOICE / SHOP)

| # | 假設 | 程式碼依據 | 狀態 | 驗證方法 |
|---|------|-----------|------|---------|
| C1 | 遇商店三選項:強化(免費)/ 去商店購物 / 直接上樓 | `handle_shop_choice` keywords | ❓ | L1(已有截圖語料) |
| C2 | 第 N 次遇商店強化幾次可調(預設第 1 次 2 次、第 2 次 3 次、之後 0) | `_shop_upgrade_times(ctx, visit_count)` 讀 `config shop.upgrade.times_by_visit`(缺退 `{1:2,2:3}` 預設;`enabled:false` → 一律 0);`shop.upgrade.order = upgrade_first`(預設,先強化)/`shop_first`(先進商店買完回來才強化) | 🔧 **config 化(2026-06-14 使用者拍板「全做」)**:次數/開關/順序皆 config 可調,寫死表改 fallback 預設。次數本身仍 ❓ 來源不明 | 使用者確認次數依據 + L3 觀察 order |
| C3 | 強化「單一價格」>= 上限就不強化(可調,免費一律強化) | `_parse_upgrade_price`(從「強化」選項 OCR 文字解析真實價:「強化 (120C)」→120、「強化（免費…）」→0、純「強化」→None 視為未知照常強化)+ `_try_shop_upgrade` 比 `config shop.upgrade.price_ceiling`(預設 540;0=不限) | 🔧 **接上真實價(2026-06-14)**:不再是死碼。進店與否與強化價脫鉤(強化在強化分支用真實價判);`_should_enter_shop` 的 `upgrade_price` 死參數已移除(repair 2.6,呼叫端恆傳 0、語意錯亂);`current_floor` 終層死分支亦移除(repair 2.2);價格解析 token「免費」單一來源於 `signatures.SHOP_UPGRADE_FREE_TOKENS` | L3 觀察強化選項真實文字格式(價格寫法待校準) |
| C4 | 商品「潛能特飲」可買卡;購買彈窗含「購買」+「單價」 | `signatures.shop_purchase_modal_text`(1.1 起單一來源) | ✅(20260602_215757 實機打中) | — |
| C5 | 優惠商品有「優惠/折扣」字樣 | `_has_discount_keyword` | ❓ | L4 |
| C6 | 商店刷新貨架 = 快捷鍵 **Q**(使用者實機確認 2026-06-14,與選卡 reroll 同熱鍵;刷新鈕多半無文字 icon) | `handle_shop` → `_refresh_shop`(按 Q,`input.press_key`)+ `config bot.max_shop_refresh`(預設 1,可調;達上限改上樓);無鍵盤能力退 `signatures.SHOP_REFRESH_TOKENS` 文字後備 | 🔧 **改 Q 鍵 + 補 config key(2026-06-14,repair 2.4)**:原走「刷新」文字點擊永遠抓不到 + config 缺 key(恆 0)→ 刷新形同死碼。改按 Q、config 補 `bot.max_shop_refresh:1` | L3 觀察一次成功刷新(刷新是否花費/有免費次數 → 校準 max_shop_refresh) |
| C7 | 金錢餘額顯示於右上角 HUD,旁有金幣圖示;餘額位置固定(語料相對 x≈0.94、y≈0.04) | `_read_money_via_icon`(Phase 2.1 已實作:固定右上 HUD ROI 讀數;icon_money 模板命中時改取圖示右側,作加分非必要)、`handle_shop`/`handle_shop_choice` 更新 `ctx.current_money` | 🔧 實作中/待 L3 驗證 — L1 語料 4/4 商店 HUD 圖讀出正確餘額(900/930/930/930);購買彈窗圖無 HUD 故回 0(視為未知)。註:icon_money 在 production 單尺度 `matcher.match()` 上 conf 僅 0.69–0.765(<0.80 門檻)且與非商店畫面重疊,無法當定位門檻,故餘額改讀固定 ROI(`tests/test_money_reading.py`) | L3 實機 log `current_money` 出現非零合理值 |
| C8 | 同一商店格位買過一次後不可再買(去重) | `shop_purchased_slots` 格位分桶 | ❓(實機買 1 次成功,未驗證多卡) | L3 |
| C9 | 點「購買」後彈窗關閉、畫面 hash 必變 | `handle_shop` 購買的 `ExpectRoiChange` | ❓(1.3 新引入;C4 只驗了彈窗文字) | L3 觀察 |
| C10 | 商店三選項點擊後對話框關閉;「去商店購物」下一畫面 = STATE_SHOP 簽名 | `handle_shop_choice` 的 `ExpectStateIn(('STATE_SHOP',))` | ❓(1.3 新引入) | L3 觀察 |
| C11 | 優惠/缺口音符購買後會出現含「確認」文字的彈窗 | `handle_shop`:settle+重拍後 `TextTarget(('確認',))`,找不到不點 | ❓(1.3 新引入;舊碼為無條件補點) | L3 觀察 |
| C12 | 商店內點商店卡(潛能特飲)後,可能插入「商店 NPC 招呼對話」(視覺小說式:名牌「珀蘿娜」+ 對白氣泡 + 右下『Space』提示);點空白處/Space 推進(映射 STATE_TAP_CONTINUE,使用者證實舊版行為)。對白與 NPC 名牌隨場合變化,簽名僅靠右下『Space』+ 否決所有已知畫面內容字 | `signatures.npc_dialogue_space_continue`(SPACE_CONTINUE_HINT_TOKENS + NPC_DIALOGUE_NEGATIVE_TOKENS,priority=15) | ❓ 使用者證實舊版點空白推進;簽名待 L3 實機驗證(L1 語料 `tap_continue__20260612_234815__last.png` 佐證) | L3 |
| C13 | **商店買法「真經濟」可調(2026-06-14 使用者拍板,預設 `cards_then_notes`)**:`config shop.buy.strategy`。`cards_then_notes`(預設)= 卡片總等級 `card_counter_current_total < target_total`(78)前買卡片(特飲),達標後改買協奏缺口音符(`_compute_note_gaps` 的音符);`all` = 買全部各一次(舊 buy-all 隔離路徑,`_handle_shop_buy_all`);`cards_only`/`notes_only` = 對應子集。沿用既有去重 `shop_purchased_slots`;音符總量靠 STATE_NOTE_ACQUIRED 覆蓋(D3),shop 不累加。**continue-run 改吃真經濟**(card_counter 開啟驅動 cards→notes 切換,不再強制 shop_buy_all) | `handle_shop` 依 `_shop_buy_strategy(ctx)` 分派、`_strategy_wants_cards`/`_strategy_wants_notes` 切換;`BotContext.shop_buy_strategy`(由 `_build_context` 從 config 帶上,舊 `shop_buy_all=True` 旗標等價 `all`);`diagnostics/safe_single_round_test.py` continue-run | 🔧 **新實作(2026-06-14)**:單元覆蓋(cards_then_notes 未達/達標切換、cards_only/notes_only、strategy=all 仍走 buy-all、舊旗標相容);永不卡死(達 78 只切買音符,停止仍只在 max_runs/回大廳)。**2 blocker 修復(L3 20260614_162359:真經濟買卡片整段失效+商店進進出出無限迴圈)**:① **emptied 信號**——真經濟 `handle_shop` 走 `_leave_shop` 卻從不設 `shop_done`/`shop_emptied_streak`(buy-all 早有),致 SHOP_CHOICE 永遠重進空商店;修為「本拍沒買到任何東西→設 shop_done=True+emptied+=1、買到貨→reset」。② **affordability 過濾**(`config shop.buy.affordability` 預設 True)——`_select_shop_card_to_buy` 點卡前讀卡片價格(per-slot 取最小可信價,`[30,5000]` 過濾等級/糊字)vs `current_money`,買不起的不點(原本錢剩 130 仍空點 200/400 卡);價格不可信/讀不到餘額時不過濾,改由購買 modal 把關(零回歸)。先紅後綠 243→249 passed。**L3 驗證通過(20260614_171544)**:商店買完→`shop_done=True emptied_streak=1`→上樓不重進(「去商店購物」只 1 次 vs 162359 的 12+,visit_count 最大 5 vs 16);affordability 本輪未觸發(滿錢,0 skip/0 unaffordable),待破產商店較長 L3 補驗 | ✅ emptied/不重進已 L3 驗;cards→notes 切換 + affordability 過濾待破產商店 L3 |

## D. 音符 / 協奏

| # | 假設 | 程式碼依據 | 狀態 | 驗證方法 |
|---|------|-----------|------|---------|
| D1 | 共 13 種音符,圖示模板 note_1~note_13 | `data/notes_map.json`、`assets/templates/` | ⚠️ **模板比對對當前版本畫面行不通(2026-06-14 實證)**:模板 111×117px、實機 PREPARE frame 1280×720 內圖示僅 ~16px(啟動條件列)/~32px(總計列)。多尺度比對(0.18~0.42)同期 frame `prepare__20260308`:① 原模板最高 conf 0.53;② 去白底中心裁後最高 0.62,但**畫面實際有的 4 種(note_2橙眼/note_6藍劍/note_10水滴/note_12紅焰)只 0.48~0.57、且全輸給不在畫面的 note_9/5/8**=贏家是錯的。**根因**:13 種共用同一「音符尾巴」形狀,僅中央小符號+顏色不同 → 16~32px 下 pixel 比對訊號量不足以分辨。**結論:單尺度/多尺度/去白底 template matching 都不可行,不是調門檻能救**。可行方向僅剩「顏色(HSV hue)分類」(同色系 2~3 種仍會撞,需配每塔僅 7 固定+2 元素的子集縮小空間)或更高解析擷取,皆為 best-effort 且工程量大。**✅ 已解(2026-06-14,`23415c9`):glyph(白色內符號)+ 色相辨識器(`vision/note_reader.py`)。整圖比對死路、純色撞色,但「中央白色符號形狀 + 色相 + 元素懲罰」對啟動條件列語料 10/10。關鍵:`assets/note_1..13.png` 既有 glyph 即正確(免重切模板);只需乾淨抽「圓盤中央最大白色連通元件」(去 ♪ 尾巴/數字、保長寬比)再做位移容忍 IoU。** | ✅ glyph+色相(L1 語料 10/10);L3 待 |
| D2 | 準備頁 3 張主位秘紋,各標示啟動所需音符與數量;右側欄為目前持有 | `_prepare_card_note_rois`、`_prepare_total_note_roi`、`vision/note_reader.py` | 🔧 **target_notes 已實作(2026-06-14,`23415c9`,glyph+色相 L1 10/10,L3 待);以下為原始診斷** ⚠️ **ROI 大致對(layout 未變,prepare__20260308 與當前截圖一致)但讀不到值**:卡在 D1 的模板比對失敗 → `_load_prepare_target_notes`/`_load_prepare_current_notes` 恆回 {}(L3 log `[PREPARE] target_notes={} current_notes={}`)。**本身非 ROI 問題,是音符圖示辨識問題(見 D1)。** ROI:啟動條件列 ~16px 圖示(難);總計列 ~32px 彩色可分(較可行,建議先攻)。**ROI 修正(使用者 2026-06-14)**:沒觸發協奏/秘紋 → 傷害掉一大截 → 影響清塔/評分,**讀音符有實質價值**。**顏色分類 PoC(2026-06-14,prepare__20260308 frame 實證)**:① 算各 note 模板去白底主色 hue;② 偵測總計列彩色圖示主色比最近 hue。**結果**:固定 7 種可分(橙眼 hue25.2→**固定 7 子集** note_2 幸運 ✓;藍劍 hue219.5→note_6 技巧 ✓,Δhue 3)。**兩個真難點**:(a) **元素音符(每塔 2 種)撞色** —— 橙眼在全 13 種裡更近 note_9(暗,25.8)而非 note_2(22.2),紅焰/水滴(元素)與同色固定音符撞 → **須先知道本塔 2 種元素**才能準分(可從 STATE_NOTE_ACQUIRED 取得音符的文字名反推);(b) **偵測切割**:sat 門檻會被面板藍底干擾、碎裂 → 用「按 y 列分群、每列取最左彩色簇=圖示、數字在右」解。**實作計畫**:hue 參考從 `ctx.matcher` 記憶體模板算(避 CJK 路徑 cv2.imread None);重寫 `_match_note_templates`→色彩偵測(同回傳格式,3 處共用);先攻 current_notes(總計列 32px);測試用 prepare__20260308 frame(CJK-safe imdecode)+ ocr_cache。**啟動條件列 ~16px 更難,放第二階段**。**方向修正(使用者 2026-06-14)**:① 每個音符**符號各異** → 改用**符號比對**(別只靠顏色,撞色不是問題);② **current_notes 不讀 PREPARE 總計,改靠 STATE_NOTE_ACQUIRED 面板讀「名字(文字)+數量」**(中間面板「○○之音 4→23」,文字可靠,D3 已在做)→ **只需專注 target_notes(圖二啟動條件,讀每祕紋需要哪些音符圖示)**。**資料已收集(corpus PNG 修復 d903561 生效,session 20260614_192942)**:當前版 PREPARE frame + **9 個有標註 NOTE_ACQUIRED frame(中間面板大圖示~35px + 音符名 ground truth)** 已存 `tests/replays/notes_calib/`。**下一步(第二階段建)**:從 NOTE_ACQUIRED 標註圖示切正確尺度符號模板 → 比對 PREPARE 啟動條件列(16px,多尺度)→ 寫進 `_load_prepare_target_notes` 先紅後綠。**✅ 已實作(2026-06-14,`23415c9`)— 但用更佳路徑(免重切模板)**:`vision/note_reader.py` glyph+色相辨識器。`_match_note_templates` 改呼叫之(保留簽名+回傳格式)。`_prepare_card_note_rois` 收緊到啟動條件列窄帶(y 0.620~0.700,三卡 x 0.020/0.255/0.500 各寬 0.215)。數量讀取:小白字 OCR 不可靠(實證只讀末位),改退預設 15(數量不驅動決策,只需 need>current)。**對語料 `prepare_current_20260614_192942` 三卡識別 10/10**:card1 空與花與詩=風/絕招/強攻/專注;card2 鹿鳴=風/幸運/強攻;card3 春日紀事=風/絕招/幸運 → `target_notes={風45,絕招30,強攻30,幸運30,專注15}`(跨祕紋累加)。**已知限**:(a) 真用 `暗`(元素)的塔在 `暗(26°)/幸運(22°)` 撞色會被懲罰判成幸運(色相 Δ4° + glyph 環vs三角難分;需 NOTE_ACQUIRED 元素史才根治,已留 `known_elements` hook);(b) 低解析(<720p)圖示更小恐降準。先紅後綠 `test_prepare_target_notes_from_real_frame`,250→251 passed。**✅✅ L3 端到端(20260614_213030):** `[PREPARE] target_notes` 識別 5/5 正確、live 非空;卡組首破 80/78 → 用 target_notes 買到缺口音符 `強攻之音`。順帶修 2 下游 bug:數量爆量 clamp(`b88b27a`,強攻=430→退預設,最右圖示把 Lv90 讀進來)+ 買音符格位去重(`cf28a4c`,修連買同張 state_stuck) | ✅ L1 10/10 + L3 端到端;note-buying 去重待長 L3 再確認(單元已驗) |
| D3 | 「獲得音符」畫面變化列(例「幸運之音 6→9」)的數字 = **本次變動後的持有總量**(非增量);音符總數一律以本畫面**覆蓋**讀取,不累加 | `handle_note_acquired`:變化列 `_extract_note_updates`(權威覆蓋)+ 頂列 `_read_note_totals_via_icons`/`_note_totals_hud_roi`(best-effort 重新同步,變化列優先);`handle_shop` 買音符已**停止 +=** (靠買後必出現的 STATE_NOTE_ACQUIRED 覆蓋) | ✅ **已解決:全部覆蓋,shop 不累加**(2026-06-14 使用者拍板)。依據:L3 log「買音符→STATE_NOTE_ACQUIRED→[NOTE] updated current_notes={…:5}」+ 變化列覆蓋設定總量,shop 再 += 必重複計。頂列圖示總量為 best-effort:語料(20+ 張 NOTE_ACQUIRED corpus JSON)頂列那排總量數字 OCR 從未浮現、亦無 PNG 可測模板命中率 → 不可靠時自動退回變化列(已驗證不弄壞既有路徑) | L3 已佐證覆蓋語意;頂列重新同步待有 PNG 語料後 L2/L4 校準 |
| D4 | 音符集齊觸發協奏 → 影響結算達標 | `current_notes_satisfied()` | ⚠️ 無人呼叫(`concert_triggered` 旗標已於 2.6 刪除);函式本體去留待使用者確認協奏機制 | 使用者確認機制後接上或刪 |

## E. 隨機事件

| # | 假設 | 程式碼依據 | 狀態 | 驗證方法 |
|---|------|-----------|------|---------|
| E1 | **事件選項策略改 config 可切(2026-06-14 拍板,預設激進)**:`config event.strategy = aggressive`(預設)/`conservative`。**激進**=追最高報酬接受風險:在「非消耗音符」選項中挑報酬最好(接受消耗金錢、接受機率損失),機率純金錢賭注沿用「選錢多的」;**保守**=絕不冒損失:排除「機率損失/失去金錢」「機率損失/失去生命」「消耗金錢」「消耗音符」,在無下行選項中挑報酬最好。報酬序:**稀有潛能 > 潛能 > 普通潛能 > 音符 > 金錢**。舊 key `event.gamble_prefer: max_money` 讀成 aggressive 別名(向後相容)。注意:遊戲economy多用裸數字(「消耗100」「失去100」不寫幣別),成本/損失偵測據此判 | `_event_strategy`、`_event_option_groups`、`_event_reward_rank`、`_event_has_money_cost/_money_loss/_hp_loss/_note_cost`、`_select_event_option` | ✅ 單元覆蓋(test_event_strategy.py:升級機/quiz/傾聽/賭博 各策略);❓ 報酬序與激進/保守取捨待使用者實機認可 | 使用者確認 + L3 遇到時記錄 |
| E2 | 命運之鏡 / 魔鏡等事件為二選一對話畫面 | `signatures.EVENT_CHOICE_HINTS`(1.1 起單一來源) | ✅(20260602_210622 實機誤判修復時驗證;另有 4 張事件語料 L1 佐證) | — |
| E2b | **升級機 NPC 事件(花錢換潛能)**:問句「想用你的運氣獲得一些好處嗎?」三選一:謹慎出手(消耗100→普通潛能)/積極出手(消耗120→潛能)/還是算了(獲得30,不消耗)。激進→積極出手;保守→還是算了 | `signatures.EVENT_CHOICE_HINTS`(「想用你的運氣」「獲得一些好處」+簡體)、`_select_event_option` strategy 分支 | ✅ 單元覆蓋(classify→STATE_EVENT + 兩策略各挑對);❓ 真圖 L1 語料未取(僅 FakeOCR 合成) | L3 遇到時補 last_frame |
| E3 | 答題型事件:題庫 `data/quiz_answers.json`(dict 格式 {題目關鍵字: 答案關鍵字}),比對題目關鍵字選答案;quiz 命中不受 strategy 影響(永遠答對) | `_iter_quiz_answers` | ✅ 已填數學題庫:**二的N次方 N=1..16**(「二的十次方」→「024」,因 OCR 把 1024 讀成「1,024」故答案關鍵字取可 substring 命中的子字串)。❌ 主觀題(如「什麼才算是有遠大抱負?」)正確答案未知,**刻意不填**,留待使用者實機驗證後補 | L3 遇新題型記錄題目+正解後加進 json |
| E4 | 事件選項點擊後畫面必變(ROI hash 改變) | `_select_event_option` 的 `ExpectRoiChange` | ❓(1.3 新引入) | L3 觀察 |

## F. 結算 / 重連

| # | 假設 | 程式碼依據 | 狀態 | 驗證方法 |
|---|------|-----------|------|---------|
| F1 | **結算畫面實機已抵達(L3 20260614_005347,史上首次完整一輪)**:離開星塔→確認彈窗→確認→TAP_CONTINUE→**STATE_RESULT**。**補(L3 20260614_173024/180705)**:離塔流程中間會插入**「默契提升」(好感度/羈絆)獎勵畫面**(角色+獎勵圖示),點空白推進 → `rapport_boost` 簽名(priority 16)導向 STATE_TAP_CONTINUE,過此即到 RESULT。畫面元素(使用者實機截圖確認):右下**「儲存紀錄」**鈕、中下**垃圾桶🗑️**(刪除/丟棄該紀錄)、左下「已鎖定」「偏好」;左上六角徽章數字=**評分**(目前版本最高約 **33**,截圖為 27);「😊 評分 7965」=分數;標題「未命名紀錄」;tabs「潛能收集/祕紋技能」 | `STATE_RESULT` keywords + 待新增 ROI | ✅ 畫面已抵達+元素經使用者確認;按鈕點擊行為待 Phase 2.3 接 | 已達 L3 |
| F1b | **結算留存判斷依據**(使用者解說 2026-06-14):① 每個角色頭像下方數字=該角色潛能總等級(截圖 +29/+14/+13;對標 [[B6]] 卡片總等級 78);② 圖鑑分組標題旁⊕數(風影⊕29/夏花⊕14)同義;③ 評分(六角徽章,max~33)、樓層(**目前版本 max 20**,與評分不同物,別混淆)亦可當依據 | `_read_result_rating`/`_read_result_potential_total`(states.py,Phase 2.3) | ✅ **L3 驗證(9b6fdf8;實機 20260614_140140/141824 讀出 rating=27 conf 高、potential_total=26 best-effort)**:評分六角徽章(左上)**最可靠 → 主依據**;每角色 ⊕ 數字直讀不可靠 → 角色潛能總等級改 **best-effort** 合計右側清單;樓層仍死碼(Phase 2.2)。門檻 = config `result.rating_threshold`(預設 30,可調),GUI 留 Phase 3 | ✅ 評分已驗 |
| F2 | **達標→停在結算畫面(或儲存);不達標→丟棄該紀錄**(使用者拍板 2026-06-14,取代舊「無條件儲存」) | `handle_result` + `_result_meets_target`(e68b97e) | ✅ **L3 驗證(20260614_140140+141824)**:評分 >= `rating_threshold`(預設30,或潛能總等級 >= `potential_total_threshold`,OR)→ 達標儲存;否則丟棄。讀數失敗 → 退回 `required_potentials_satisfied()`(保守不誤丟)。實機:rating=27<30 → 不達標→丟棄。計數延後到回大廳(d28fc25,整輪含丟棄跑完才計+停)。**達標儲存路徑**:已 unit 測,L3 待一筆達標紀錄(兩次實機皆 27<30 走丟棄) | L3(不達標已驗) |
| F2b | **丟棄(解散)完整流程**(使用者解說 2026-06-14,含鎖定陷阱):① **先檢查鎖定狀態** —— 左下「已鎖定」=鎖定中(評分高自動上鎖),要先**點它解鎖**才能丟;② 點**垃圾桶🗑️** → **「是否確定解散?」確認彈窗**(取消/確認)→ 點**確認**;③ → **「獲得道具!」**(點空白)④ → 回**大廳**。 | `handle_result` 解鎖+垃圾桶 / `handle_discard_confirm`(STATE_DISCARD_CONFIRM,fd58736) | ✅✅ **L3 全鏈驗證**:① 解鎖(20260614_140140:`result_unlock` 點「已鎖定」(132,662));② 垃圾桶(141824:`result_trash` (448,653));③ **解散確認彈窗偵測成功**(discard_confirm 簽名 score 1.15,問句「是否確定解散」)→ 點「確認」(780,535);④ 獲得道具→STATE_TAP_CONTINUE→點空白;⑤ 回 STATE_LOBBY→延後計數 run1/success0→停。**端到端跑通,無卡死。** | ✅ 已驗 |
| F3 | 斷線畫面含「重新連線/連接中斷/網路異常」 | `STATE_RECONNECT` keywords | ❓ 從未實測 | 遇到時記錄 |

---

## 維護規則

1. 新增任何畫面假設(關鍵字、ROI、顏色、流程)→ 先在此登記一條 ❓。
2. L1/L4 驗證後改 ✅,附證據路徑(語料檔名或 session id)。
3. 決定移除的機制改 🗑️ 並在同 commit 刪除對應程式碼。
4. 本清單與 `vision/signatures.py`(Phase 1 後)是兩份互補文件:這裡管「遊戲長怎樣」,signatures 管「程式怎麼認」。
