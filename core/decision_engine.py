"""
潛能選擇決策引擎 (DecisionEngine)

根據使用者制定的 required / level_required / backup 潛能清單，
對 OCR 辨識後的三個選項做出最佳決策。

【遊戲機制說明】
  - 每次選擇潛能卡，提供 +1/+2/+3 等的升等量，累計到目標等級為止。
  - 例：選了「攻擊力提升(+2)」→ 累計 Lv.2；再選(+1)→ Lv.3；直到 Lv.6 為止。
  - required 潛能目標預設為 Lv.6（攻略沒寫等級 = 要升滿）。
  - level_required 潛能有自訂目標等級（例如「5級」= 只要升到 Lv.5 就停止）。
  - 卡牌的升等量只要能讓累計等級「達到或超過」目標，就算滿足條件。

【決策規則】
  規則 1-3: 出現 required 且累計未到 Lv.6 → 選（多個時比較等級/是否選過）
  規則 4:   無必選也無備選 → Reroll
  規則 5:   Reroll 達上限且有備選 → 降級選備選（logic 同 1-3）
  規則 5a:  Reroll 達上限也無備選 → 繼續 Reroll
  規則 5b:  降級模式中出現必選 → 立刻回到規則 1-3
  規則 6:   level_required 累計未到 target_level → 選（優先序介於 required 與 backup 之間）
"""
import yaml
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable


# ─────────────────────────────────────────────────────────────
# 資料結構定義
# ─────────────────────────────────────────────────────────────

@dataclass
class ScreenOption:
    """
    代表 OCR 辨識後，畫面上單一潛能選項的標準化資料。

    Attributes:
        name:     OCR 辨識出的潛能名稱。
        level:    此卡提供的升等量（+1 / +2 / +3）。
        position: 在畫面上的點擊座標 (x, y)。
        category: 分類結果 → "required" / "level_req" / "backup" / "unknown"
    """
    name: str
    level: int = 1
    position: tuple = (0, 0)
    category: str = "unknown"  # "guaranteed" / "required" / "level_req" / "backup" / "unknown"
    recommended: bool = False
    is_pink: bool = False
    recommendation_text: str = ""
    recommendation_target_level: int = 0


@dataclass
class DecisionState:
    """
    執行期間的決策狀態追蹤，每次新局開始時應重置。

    Attributes:
        reroll_count:       當前連續 Reroll 次數（選擇後歸零）。
        accumulated_levels: 每個潛能目前已累計到幾等 {名稱: 當前等級}。
        selected_history:   選取次數追蹤 {名稱: 選取次數}（用於規則 3 判斷）。
        accept_backup:      是否已切換為「接受備選」模式。
    """
    reroll_count: int = 0
    accumulated_levels: dict = field(default_factory=dict)   # {名稱: 累計等級}
    selected_history: dict = field(default_factory=dict)     # {名稱: 選取次數}
    accept_backup: bool = False
    selected_per_group: dict = field(default_factory=dict)   # {group_idx: 已選名稱}

    def reset_reroll(self):
        """成功選擇後，重置連續 Reroll 計數。"""
        self.reroll_count = 0
        self.accept_backup = False

    def record_selection(self, option: "ScreenOption"):
        """
        記錄選取結果：累計等級 += 卡牌升等量，並更新選取次數。
        """
        name = option.name
        gain = option.level
        self.accumulated_levels[name] = self.accumulated_levels.get(name, 0) + gain
        self.selected_history[name] = self.selected_history.get(name, 0) + 1

    def increment_reroll(self, max_before_backup: int):
        """增加 Reroll 計數，達上限後自動切換備選模式。"""
        self.reroll_count += 1
        if self.reroll_count >= max_before_backup:
            self.accept_backup = True

    def current_level(self, name: str) -> int:
        """查詢指定潛能目前的累計等級。"""
        return self.accumulated_levels.get(name, 0)


# ─────────────────────────────────────────────────────────────
# 決策引擎
# ─────────────────────────────────────────────────────────────

class DecisionEngine:
    """
    潛能三選一的核心決策大腦。

    設定來源（config.yaml → decision 區塊）:
      required:       必選清單，目標升級到 Lv.6
      level_required: 有等級要求的潛能清單，目標到指定等級即可
      backup:         備選清單，Reroll 達上限才考慮
    """

    # required 潛能的預設目標等級（升到 Lv.6 為滿）
    REQUIRED_TARGET_LEVEL = 6
    _PUNCT_TRANSLATION = str.maketrans({
        "：": ":",
        "﹕": ":",
        "·": ":",
        "•": ":",
        "・": ":",
        "‧": ":",
        "－": "-",
        "—": "-",
        "–": "-",
    })

    def __init__(self, config_path: str = "config.yaml"):
        self._config_path = Path(config_path)
        self._guaranteed: list[str] = []
        self._required: list[str] = []
        self._backup: list[str] = []
        self._max_reroll: int = 3
        self._mode: str = "legacy"
        # required 潛能目標滿級（預設 = class attr REQUIRED_TARGET_LEVEL=6，單一來源）
        self._required_target_level: int = self.REQUIRED_TARGET_LEVEL
        # _pick_best 排序旗標：未選過優先（多樣性）／升等量大優先
        self._prefer_never_picked: bool = True
        self._prefer_higher_gain: bool = True
        # legacy _pick_best 單卡升等量門檻：低於此值的弱卡不選。0=不過濾（現行）。
        # guaranteed（規則0 保底）不受此限；過濾後候選全空 → 退全集（保證推進）。
        self._min_level_threshold: int = 0
        # E-4 升等策略（recommendation_badge 模式排序）：minimize_overflow（預設，使用者拍板）
        # / nearest_target / farthest_target。僅在 _recommendation_target_enabled=True 且所有
        # 候選都讀到推薦N級(>0) 時生效；否則退現行排序（升後等級最高、平手最左）byte-identical。
        self._upgrade_strategy: str = "minimize_overflow"
        # 總開關：預設 False = 退現行排序，逐位元同現版。
        self._recommendation_target_enabled: bool = False
        self._alias_map: dict[str, str] = {}
        # {潛能名稱: target_level}，達到或超過此等級後停止選取
        self._level_required: dict[str, int] = {}
        # backup_groups：[[A,B,C], [D,E]]
        self._backup_groups: list[list[str]] = []
        # 快速反查：潛能名稱 → 所屬 group idx
        self._backup_group_index: dict[str, int] = {}

        self.state = DecisionState()
        self._load_config()
        self._load_priority_list()

    # ── 設定載入 ──────────────────────────────────────────────

    def _load_config(self) -> None:
        """從 config.yaml 讀取 decision 區塊。"""
        if not self._config_path.exists():
            logging.warning(f"找不到設定檔 {self._config_path}，使用預設空清單。")
            return

        try:
            with open(self._config_path, 'r', encoding='utf-8') as f:
                cfg = yaml.safe_load(f)
            decision = cfg.get("decision", {})

            self._mode = str(decision.get("mode", "legacy") or "legacy")
            self._guaranteed = [self._canonicalize_name(str(s)) for s in decision.get("guaranteed", [])]
            self._required = [self._canonicalize_name(str(s)) for s in decision.get("required", [])]
            self._backup = [self._canonicalize_name(str(s)) for s in decision.get("backup", [])]
            self._max_reroll = int(decision.get("max_reroll_before_backup", 3))

            # required 潛能目標滿級（可調，clamp 1~6；壞型別/越界 → 退預設 6）
            try:
                lvl = int(decision.get("required_target_level", self.REQUIRED_TARGET_LEVEL))
                self._required_target_level = lvl if 1 <= lvl <= 6 else (
                    1 if lvl < 1 else self.REQUIRED_TARGET_LEVEL
                )
            except (TypeError, ValueError):
                self._required_target_level = self.REQUIRED_TARGET_LEVEL
            # _pick_best 排序旗標
            self._prefer_never_picked = bool(decision.get("prefer_never_picked", True))
            self._prefer_higher_gain = bool(decision.get("prefer_higher_gain", True))

            # 單卡升等量門檻（legacy _pick_best 用；負數/壞型別/None → 退 0=不過濾）
            try:
                threshold = int(decision.get("min_level_threshold", 0))
                self._min_level_threshold = threshold if threshold > 0 else 0
            except (TypeError, ValueError):
                self._min_level_threshold = 0

            # E-4 升等策略：白名單驗證，否則退 minimize_overflow（壞值/未知值不靜默生效）。
            self._upgrade_strategy = str(decision.get("upgrade_strategy", "minimize_overflow"))
            if self._upgrade_strategy not in (
                "minimize_overflow", "nearest_target", "farthest_target"
            ):
                self._upgrade_strategy = "minimize_overflow"
            # recommendation_target 是巢狀 dict；三層防呆（缺/None/非 dict → enabled False）。
            rt = decision.get("recommendation_target", {})
            self._recommendation_target_enabled = (
                bool(rt.get("enabled", False)) if isinstance(rt, dict) else False
            )

            # level_required 格式: [{name: "...", target_level: N}, ...]
            for entry in decision.get("level_required", []):
                name = self._canonicalize_name(str(entry.get("name", "")))
                target = int(entry.get("target_level", 1))
                if name and 1 <= target <= 6:
                    self._level_required[name] = target

            # backup_groups：[[A,B,C], [D,E], ...]
            raw_groups = decision.get("backup_groups", [])
            self._backup_groups = [
                [self._canonicalize_name(str(n)) for n in g]
                for g in raw_groups if len(g) >= 2
            ]
            self._backup_group_index = {}
            for gidx, grp in enumerate(self._backup_groups):
                for n in grp:
                    self._backup_group_index[n] = gidx

            logging.info(
                f"載入設定：模式={self._mode}, "
                f"保底 {len(self._guaranteed)} 筆, "
                f"必選 {len(self._required)} 筆, "
                f"限量必選 {len(self._level_required)} 筆, "
                f"備選 {len(self._backup)} 筆, "
                f"最大 Reroll = {self._max_reroll}"
            )
        except Exception as e:
            logging.error(f"讀取 config.yaml 失敗: {e}")

    def _load_priority_list(self) -> None:
        """從 priority_list.json 載入別名對照表。"""
        plist_path = Path("data/priority_list.json")
        try:
            with open(self._config_path, 'r', encoding='utf-8') as f:
                cfg = yaml.safe_load(f)
            plist_path = Path(cfg.get("ocr", {}).get("priority_list_path", str(plist_path)))
        except Exception:
            pass

        if not plist_path.exists():
            logging.warning(f"找不到潛能對照表 {plist_path}，別名比對停用。")
            return

        try:
            with open(plist_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            for item in data.get("potentials", []):
                std_name = self._canonicalize_name(str(item.get("name", "")))
                for alias in item.get("aliases", [std_name]):
                    clean_alias = self._canonicalize_name(str(alias))
                    self._alias_map[clean_alias] = std_name
            logging.info(f"載入別名對照表，共 {len(self._alias_map)} 筆。")
        except Exception as e:
            logging.error(f"讀取 priority_list.json 失敗: {e}")

    # ── 名稱正規化 ────────────────────────────────────────────

    def _canonicalize_name(self, raw: str) -> str:
        clean = raw.strip().replace(" ", "").translate(self._PUNCT_TRANSLATION)
        return clean

    def _normalize(self, raw: str) -> str:
        """將 OCR 辨識的潛能名稱正規化為標準名稱。"""
        clean = self._canonicalize_name(raw)
        if clean in self._alias_map:
            return self._alias_map[clean]
        for alias, std in self._alias_map.items():
            if alias in clean:
                return std
        return clean

    # ── 分類 ─────────────────────────────────────────────────

    def categorize(self, options: list[ScreenOption]) -> list[ScreenOption]:
        """
        依照必選 / 限量必選 / 備選清單分類，同時考慮已累計的等級。

        分類優先序：
          required  → 名稱在必選清單，且還未達 Lv.6
          level_req → 名稱在限量必選清單，且還未達 target_level
          backup    → 名稱在備選清單
          unknown   → 其他（含已達目標等級的潛能）
        """
        for opt in options:
            opt.name = self._normalize(opt.name)
            name = opt.name
            current = self.state.current_level(name)

            if name in self._guaranteed:
                opt.category = "guaranteed"
            elif name in self._required:
                # required：還未升到 Lv.6 就繼續選
                opt.category = "required" if current < self._required_target_level else "unknown"

            elif name in self._level_required:
                target = self._level_required[name]
                opt.category = "level_req" if current < target else "unknown"

            elif name in self._backup:
                # 備選群組互斥判斷：
                # 如果同群組已有別人被採用，且該潛能不是那個已選的 → 降級為 unknown
                gidx = self._backup_group_index.get(name)
                if gidx is not None:
                    selected_in_group = self.state.selected_per_group.get(gidx)
                    if selected_in_group is not None and selected_in_group != name:
                        opt.category = "unknown"   # 同群其他成員，不再考慮
                    else:
                        opt.category = "backup"
                else:
                    opt.category = "backup"         # 不屬於任何群組，正常備選
            else:
                opt.category = "unknown"

        return options

    # ── 最佳選擇輔助 ─────────────────────────────────────────

    def _pick_best(
        self,
        candidates: list[ScreenOption],
        apply_min_threshold: bool = True,
    ) -> ScreenOption:
        """
        從候選清單依以下順序選出最佳選項（同階級內）：
          1. 從未選過的 > 已選過的（多樣性優先）
          2. 相同選取狀態下，升等量較大者優先（+3 > +2 > +1）
          3. 全部相同則取清單第一個

        apply_min_threshold=True（預設）時，先排除升等量 < _min_level_threshold 的弱卡；
        門檻=0 則 pool==candidates，逐位元同現行。過濾後全空退全集（保證選得出一張）。
        guaranteed（規則0 保底）呼叫端傳 False 繞過此過濾。
        """
        if len(candidates) == 1:
            return candidates[0]                       # 單一候選一律拿（保證推進），不被門檻擋

        pool = candidates
        if apply_min_threshold and self._min_level_threshold > 0:
            filtered = [o for o in candidates if o.level >= self._min_level_threshold]
            if filtered:
                pool = filtered                        # 過濾後非空才用；全空退全集

        def sort_key(opt: ScreenOption):
            # primary：未選過優先（多樣性）；secondary：升等量大優先（+3>+2>+1）。
            # 旗標關閉時該維度退化為常數 0，不影響排序（sorted 穩定 → 退回清單順序）。
            primary = (not (self.state.selected_history.get(opt.name, 0) == 0)) if self._prefer_never_picked else 0
            secondary = (-opt.level) if self._prefer_higher_gain else 0
            return (primary, secondary)  # 預設 True/True ⇒ (not never_picked, -level)，逐位元同現行

        return sorted(pool, key=sort_key)[0]

    def _choose_from_candidates(
        self,
        candidates: list[ScreenOption],
        log_message: str,
        on_commit: Callable[[ScreenOption], None] | None = None,
        apply: bool = True,
        apply_min_threshold: bool = True,
    ) -> ScreenOption:
        chosen = self._pick_best(candidates, apply_min_threshold=apply_min_threshold)
        logging.info(log_message.format(chosen=chosen))
        if apply:
            self.state.record_selection(chosen)
            if on_commit is not None:
                on_commit(chosen)
            self.state.reset_reroll()
        return chosen

    def preview_decision(self, options: list[ScreenOption]) -> ScreenOption | None:
        """預覽本輪決策，但不修改 reroll/累計等任何狀態。"""
        return self._decide(options, apply=False)

    # ── 核心決策 ─────────────────────────────────────────────

    def decide(self, options: list[ScreenOption]) -> ScreenOption | None:
        return self._decide(options, apply=True)

    def _decide(self, options: list[ScreenOption], apply: bool) -> ScreenOption | None:
        """
        對畫面上的三個潛能選項做出決策。

        Returns:
            ScreenOption → 選定的選項（應執行點擊）
            None         → 應執行 Reroll
        """
        if self._mode == "recommendation_badge":
            return self._decide_recommendation_badge(options, apply=apply)

        options = self.categorize(options)

        guaranteed_opts = [opt for opt in options if opt.category == "guaranteed"]
        required_opts   = [opt for opt in options if opt.category == "required"]
        level_req_opts  = [opt for opt in options if opt.category == "level_req"]
        backup_opts     = [opt for opt in options if opt.category == "backup"]

        # 規則 0：粉色保底（最高優先級，無條件選取以免錯過 → 不受 min_level_threshold 過濾）
        if guaranteed_opts:
            return self._choose_from_candidates(
                guaranteed_opts,
                log_message="[決策] 規則0：選粉色保底「{chosen.name}」",
                apply=apply,
                apply_min_threshold=False,
            )

        # 規則 1-3 / 5b：必選優先（即使處於降級備選模式也立刻回頭）
        if required_opts:
            return self._choose_required(required_opts, apply)

        # 規則 6：限量必選（已達目標的不會出現在此清單）
        if level_req_opts:
            return self._choose_level_required(level_req_opts, apply)

        # 規則 5：Reroll 達上限且有備選 → 降級
        if self.state.accept_backup and backup_opts:
            return self._choose_backup(backup_opts, apply)

        # 規則 4/5a：執行 Reroll
        logging.info(f"[決策] 規則4/5a：Reroll（連續第 {self.state.reroll_count + 1} 次）")
        if apply:
            self.state.increment_reroll(self._max_reroll)
        return None

    def _decide_recommendation_badge(
        self,
        options: list[ScreenOption],
        apply: bool,
    ) -> ScreenOption | None:
        """
        半周年推薦標籤模式。

        遊戲會在仍需選取的卡片上顯示紅色推薦標籤；達標後標籤消失。
        因此本模式不追蹤每張卡的目標等級，也不使用 legacy 的必選/備選/保底規則。
        """
        for opt in options:
            opt.name = self._normalize(opt.name)
            opt.category = "recommended" if getattr(opt, "recommended", False) else "unknown"

        candidates = [opt for opt in options if getattr(opt, "recommended", False)]
        fallback = False
        if not candidates:
            # 無紅字推薦：在 reroll 上限內續抽找推薦卡；達上限仍無推薦 → 改取最佳卡
            # 保證推進（永不卡死）。session 20260613_223142：本模式原本無上限且無
            # fallback，配上 reroll 鈕為 icon 文字點不到 → 無限 reroll 卡死。
            if self.state.reroll_count < self._max_reroll:
                logging.info(
                    "[決策] 推薦標籤模式：未偵測到紅字推薦，Reroll（連續第 %d/%d 次）",
                    self.state.reroll_count + 1,
                    self._max_reroll,
                )
                if apply:
                    self.state.increment_reroll(self._max_reroll)
                return None
            logging.info(
                "[決策] 推薦標籤模式：Reroll 達上限 %d 仍無推薦 → 取最佳卡（fallback 保證推進）",
                self._max_reroll,
            )
            candidates = list(options)
            fallback = True

        if not candidates:
            return None

        # E-4 升等策略：在「有候選後的排序」這一步決定 sort key。
        #   pos_x  = 平手 tiebreak（最左優先，同現行）
        #   after  = opt.level = 升後等級 M（不是升等量）
        #   tgt    = 推薦N級目標等級（E-3 填；讀不到=0）
        # 僅在總開關開 + 全部候選都讀到推薦級(>0) 時套策略；任一讀不到 → 退現行
        # （byte-identical）。minimize_overflow 在無卡溢出時 key=(0,-after,pos_x)，
        # 排序等同現行 (-after, pos_x)；只有某些卡升過頭(after>target)才把它們排後。
        pos_x = lambda o: int(getattr(o, "position", (0, 0))[0])
        after = lambda o: int(getattr(o, "level", 1) or 1)
        tgt = lambda o: int(getattr(o, "recommendation_target_level", 0) or 0)
        use_strategy = (
            self._recommendation_target_enabled
            and all(tgt(o) > 0 for o in candidates)
        )
        if use_strategy and self._upgrade_strategy == "minimize_overflow":
            key = lambda o: (max(0, after(o) - tgt(o)), -after(o), pos_x(o))
        elif use_strategy and self._upgrade_strategy == "nearest_target":
            key = lambda o: (abs(after(o) - tgt(o)), pos_x(o))
        elif use_strategy and self._upgrade_strategy == "farthest_target":
            key = lambda o: (after(o) - tgt(o), pos_x(o))
        else:
            key = lambda o: (-after(o), pos_x(o))   # 現行：升後等級最高、平手最左
        chosen = sorted(candidates, key=key)[0]
        logging.info(
            "[決策] 推薦標籤模式：選%s「%s」(+%s, pink=%s, badge=%s)",
            "fallback 卡" if fallback else "紅字推薦",
            chosen.name,
            chosen.level,
            getattr(chosen, "is_pink", False),
            getattr(chosen, "recommendation_text", ""),
        )
        if apply:
            self.state.reset_reroll()
        return chosen

    def _choose_required(self, candidates: list[ScreenOption], apply: bool) -> ScreenOption:
        chosen = self._pick_best(candidates)
        new_level = self.state.current_level(chosen.name) + chosen.level
        logging.info(
            f"[決策] 規則1-3：選必選「{chosen.name}」"
            f"(+{chosen.level}, 累計→Lv.{new_level}/{self._required_target_level})"
        )
        if apply:
            self.state.record_selection(chosen)
            self.state.reset_reroll()
        return chosen

    def _choose_level_required(self, candidates: list[ScreenOption], apply: bool) -> ScreenOption:
        chosen = self._pick_best(candidates)
        target = self._level_required[chosen.name]
        new_level = self.state.current_level(chosen.name) + chosen.level
        logging.info(
            f"[決策] 規則6：選限量必選「{chosen.name}」"
            f"(+{chosen.level}, 累計→Lv.{new_level}/{target})"
        )
        if apply:
            self.state.record_selection(chosen)
            self.state.reset_reroll()
        return chosen

    def _choose_backup(self, candidates: list[ScreenOption], apply: bool) -> ScreenOption:
        chosen = self._pick_best(candidates)
        new_level = self.state.current_level(chosen.name) + chosen.level
        logging.info(f"[決策] 規則5：降級選備選「{chosen.name}」(+{chosen.level}, 累計→Lv.{new_level})")
        if apply:
            self.state.record_selection(chosen)
            gidx = self._backup_group_index.get(chosen.name)
            if gidx is not None and gidx not in self.state.selected_per_group:
                self.state.selected_per_group[gidx] = chosen.name
                logging.info(f"[決策] 群組#{gidx} 已鎖定「{chosen.name}」，同群其他選項排除。")
            self.state.reset_reroll()
        return chosen

    def reset_state(self) -> None:
        """重置整個決策狀態，新局開始時呼叫。"""
        self.state = DecisionState()
        logging.info("[決策] 狀態已重置（新局開始）。")
