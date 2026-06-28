"""
parse_guide_image.py — 攻略圖自動解析工具

用途：
  解析社群攻略截圖，自動提取「必拿」「備選」潛能清單，
  並直接覆寫 config.yaml 的 decision 區塊。

使用方法：
  python tools/parse_guide_image.py                          # 互動式輸入路徑
  python tools/parse_guide_image.py assets/guide_images/input.png
  python tools/parse_guide_image.py --dry-run               # 僅顯示，不寫入

演算法摘要：
  1. 圖像前處理：放大 → 灰階 → CLAHE 強化對比 → 自適應二值化
  2. 全畫面 OCR：取得所有文字區塊與其 bbox
  3. 區塊分類：找到「必拿/抓滿」「盡量抓/二選一/三選一」等標籤區塊
  4. 1-to-N 空間配對：以標籤 bbox 的「X 軸涵蓋範圍」向上延伸，
     抓出所有 X 軸重疊且 Y 軸在其上方的潛能名稱文字，全歸屬於該標籤
  5. 模糊比對：將 OCR 文字與 priority_list.json aliases 進行相似度比對
  6. 互動確認 → 寫入 config.yaml
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import cv2
import numpy as np
import yaml

# ─────────────────────────────────────────────────────────────────────────────
# 路徑設定
# ─────────────────────────────────────────────────────────────────────────────
_ROOT = Path(__file__).resolve().parents[1]
_PRIORITY_LIST = _ROOT / "data" / "priority_list.json"
_CONFIG_YAML   = _ROOT / "config.yaml"
_GUIDE_DIR     = _ROOT / "assets" / "guide_images"

# ─────────────────────────────────────────────────────────────────────────────
# 標籤關鍵字字典（含任務擴充）
# ─────────────────────────────────────────────────────────────────────────────
LABEL_KEYWORDS: dict[str, list[str]] = {
    "required": ["必拿", "抓滿", "必選", "必要", "核心", "S級", "SS級", "必等", "必享",
                "拣满", "必选", "5级", "1级", "5級", "1級", "一級", "五級", "一级", "五级"],
    "backup":   ["盡量抓", "二選一", "三選一", "備選", "替代", "可選", "次選", "A級", "B級", "三選", "二選", "盡量",
                "尽量抓", "尽量", "备选", "可选", "次选", "备选潜能", "備選潛能"],
    "skip":     ["不選", "避免", "地雷", "垃圾", "不选"],
}

# ─────────────────────────────────────────────────────────────────────────────
# 資料結構
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class TextBlock:
    """OCR 識別出的單一文字區塊。"""
    text:    str
    x:       int   # bbox 左邊 X
    y:       int   # bbox 上邊 Y
    w:       int   # 寬度
    h:       int   # 高度
    block_type:      str        = "unknown"    # "label" / "potential" / "unknown"
    matched_label:   str | None = None         # "required" / "backup" / "skip"
    matched_name:    str | None = None         # 模糊比對後的正規化名稱
    fuzzy_score:     float      = 0.0

    @property
    def cx(self) -> int:
        return self.x + self.w // 2

    @property
    def cy(self) -> int:
        return self.y + self.h // 2

    @property
    def right(self) -> int:
        return self.x + self.w

    @property
    def bottom(self) -> int:
        return self.y + self.h


# ─────────────────────────────────────────────────────────────────────────────
# OCR 封裝（優先用 EasyOCR，fallback 到 pytesseract）
# ─────────────────────────────────────────────────────────────────────────────

def _run_ocr(image: np.ndarray) -> list[TextBlock]:
    """
    對圖片執行 OCR，回傳 TextBlock 列表。
    自動偵測可用引擎：EasyOCR > pytesseract。
    """
    blocks: list[TextBlock] = []

    # ── 嘗試 EasyOCR ─────────────────────────────────
    try:
        import easyocr  # type: ignore
        # 改用簡體中文為主模型，因為大多數攻略圖來源為簡體且其泛化能力較佳
        reader = easyocr.Reader(["ch_sim", "en"], gpu=False, verbose=False)
        # 啟用對比度調整、放大倍率以應付小字體與漸層背景
        results = reader.readtext(
            image, 
            detail=1, 
            paragraph=False,
            adjust_contrast=True,
            mag_ratio=1.5,
            width_ths=0.7,
            text_threshold=0.6,
            low_text=0.3
        )
        for bbox_pts, text, conf in results:
            # 降低信心度門檻，由後續模糊比對來做可靠度過濾
            if not text.strip() or conf < 0.01:
                continue
            xs = [int(p[0]) for p in bbox_pts]
            ys = [int(p[1]) for p in bbox_pts]
            blocks.append(TextBlock(
                text=text.strip(),
                x=min(xs), y=min(ys),
                w=max(xs) - min(xs),
                h=max(ys) - min(ys),
            ))
        return blocks
    except ImportError:
        pass

    # ── fallback: pytesseract ────────────────────────
    try:
        import pytesseract  # type: ignore
        data = pytesseract.image_to_data(
            image,
            lang="chi_tra+eng",
            config="--psm 11",
            output_type=pytesseract.Output.DICT,
        )
        n = len(data["text"])
        for i in range(n):
            text = data["text"][i].strip()
            conf = float(data["conf"][i])
            if not text or conf < 20:
                continue
            blocks.append(TextBlock(
                text=text,
                x=data["left"][i], y=data["top"][i],
                w=data["width"][i], h=data["height"][i],
            ))
        return blocks
    except ImportError:
        pass

    print("[ERROR] 需要 EasyOCR 或 pytesseract。請安裝其一後重試。")
    sys.exit(1)


# ─────────────────────────────────────────────────────────────────────────────
# 圖像前處理
# ─────────────────────────────────────────────────────────────────────────────

def preprocess(image: np.ndarray, scale: float = 2.0) -> tuple[np.ndarray, float]:
    """
    因為 EasyOCR 是建構於深度學習，過度的自適應二值化反而會破壞反鋸齒資訊導致辨識率大幅下降。
    這裡我們僅進行適度的放大，或直接回傳原圖讓 EasyOCR 內部處理。
    針對使用者低解析度的截圖，適當倍率的放大 (預設 2.0 倍) 有助於提升辨識率。
    """
    h, w = image.shape[:2]
    # 若圖片寬度小於 1000，代表是低解析度截圖，強制放大
    target_scale = scale if w < 1000 else 1.0
    
    if target_scale != 1.0:
        image = cv2.resize(image, (int(w * target_scale), int(h * target_scale)), interpolation=cv2.INTER_CUBIC)
    
    return image, target_scale


# ─────────────────────────────────────────────────────────────────────────────
# 模糊比對
# ─────────────────────────────────────────────────────────────────────────────

def _char_overlap_ratio(a: str, b: str) -> float:
    """計算兩字串共享字元比例（簡易中文模糊比對）。"""
    if not a or not b:
        return 0.0
    set_a, set_b = set(a), set(b)
    return len(set_a & set_b) / max(len(set_a), len(set_b))


def _levenshtein(a: str, b: str) -> int:
    m, n = len(a), len(b)
    if m < n:
        a, b, m, n = b, a, n, m
    dp = list(range(n + 1))
    for i in range(1, m + 1):
        prev = dp[:]
        dp[0] = i
        for j in range(1, n + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            dp[j] = min(dp[j] + 1, dp[j - 1] + 1, prev[j - 1] + cost)
    return dp[n]


def fuzzy_match_potential(
    ocr_text: str,
    potentials: list[dict],
    threshold: float = 0.4,
) -> tuple[str | None, float]:
    """
    將 OCR 文字與 priority_list.json 中所有 aliases 進行模糊比對。
    回傳 (正規化名稱, 分數)；若低於閾值回傳 (None, 0.0)。
    """
    best_name, best_score = None, 0.0

    for pot in potentials:
        canonical = pot["name"]
        aliases: list[str] = pot.get("aliases", [canonical])
        for alias in aliases:
            # 字元重疊比率
            overlap = _char_overlap_ratio(ocr_text, alias)
            # 標準化 Levenshtein 相似度
            lev_dist = _levenshtein(ocr_text, alias)
            max_len = max(len(ocr_text), len(alias), 1)
            lev_sim = 1.0 - lev_dist / max_len
            # 加權平均
            score = 0.6 * overlap + 0.4 * lev_sim
            if score > best_score:
                best_score = score
                best_name = canonical

    if best_score >= threshold:
        return best_name, best_score
    return None, 0.0


# ─────────────────────────────────────────────────────────────────────────────
# 區塊分類
# ─────────────────────────────────────────────────────────────────────────────

def classify_blocks(blocks: list[TextBlock], potentials: list[dict]) -> list[TextBlock]:
    """
    對每個 OCR 區塊：
    1. 偵測是否為標籤（必拿/備選等關鍵字）包含確切比對與 1 個字元的容錯比對
    2. 否則嘗試模糊比對為潛能名稱
    """
    label_dict = LABEL_KEYWORDS
    # 動態補充常見 OCR 錯別字到字典
    if "三迭" not in label_dict["backup"]: label_dict["backup"].append("三迭")
    if "儘量抓" not in label_dict["backup"]: label_dict["backup"].append("儘量抓")
    if "儘量" not in label_dict["backup"]: label_dict["backup"].append("儘量")

    for blk in blocks:
        try:
            import zhconv
            txt = zhconv.convert(blk.text, 'zh-tw')
        except ImportError:
            txt = blk.text
        # 去除冒號等標點符號以利比對
        clean_txt = re.sub(r'[:：]', '', txt)
        best_label_type = None
        
        # 1. 確切子字串比對
        for label_type, keywords in label_dict.items():
            if any(kw in clean_txt for kw in keywords):
                best_label_type = label_type
                break
                
        # 2. 泛型 N選一 (如 二選一, 3选一)
        if not best_label_type and re.search(r'[一二三四五六1-6][選选]一', clean_txt):
            best_label_type = "backup"
            
        # 3. Levenshtein 距離容錯比對 (容許 1 個字元差異)
        # 用於拯救 "三迭", "盡量抓" 被誤辨識時的問題
        if not best_label_type:
            for label_type, keywords in label_dict.items():
                for kw in keywords:
                    if len(kw) <= 1: continue
                    dist = _levenshtein(clean_txt, kw)
                    if len(kw) >= 2 and dist <= 1:
                        best_label_type = label_type
                        break
                if best_label_type:
                    break

        if best_label_type:
            blk.block_type = "label"
            blk.matched_label = best_label_type
            continue

        # 嘗試模糊比對潛能名稱
        name, score = fuzzy_match_potential(txt, potentials)
        if name:
            blk.block_type = "potential"
            blk.matched_name = name
            blk.fuzzy_score = score

    return blocks


# ─────────────────────────────────────────────────────────────────────────────
# 1-to-N 空間配對（核心演算法）
# ─────────────────────────────────────────────────────────────────────────────

def _is_pink_card(
    img: np.ndarray,
    x: int, y: int, w: int, h: int,
    digit_blocks: list | None = None,
) -> bool:
    """
    判斷潛能名稱對應的卡片是否為粉色（保底必拿）卡。
    
    核心：攻略圖中，粉色卡背景為鮮蔭色/粉紫色，金色卡背景為橘金色。
    使用 HSV 色相空間對卡片上方區域進行大面積取樣，袁卻單點取色的不可靠性。
    
    卡片背景在潛能名稱文字區塊的正上方：
      top_y  = max(0, y - w)             # 卡片頂部 (w 大約等於卡片寬度)
      bot_y  = max(0, y - int(w * 0.35)) # 取上半段 (65%~100%)
    """
    if img is None:
        return False

    if _has_digit_block_above(x, y, w, h, digit_blocks, img.shape):
        return False

    roi = _extract_card_color_roi(img, x, y, w, h)
    if roi is None or roi.size == 0:
        return False

    pink_ratio, gold_ratio = _measure_card_color_ratios(roi)
    return pink_ratio >= 0.50 and pink_ratio > gold_ratio


def _estimate_card_bounds(img_shape: tuple[int, ...], x: int, y: int, w: int, h: int) -> tuple[int, int, int, int]:
    """估算潛能名稱所對應卡面的整體邊界。"""
    card_h = max(80, w * 2)
    top = max(0, y - card_h)
    bottom = min(img_shape[0], y)
    left = max(0, x)
    right = min(img_shape[1], x + w)
    return left, top, right, bottom


def _has_digit_block_above(
    x: int,
    y: int,
    w: int,
    h: int,
    digit_blocks: list | None,
    img_shape: tuple[int, ...],
) -> bool:
    """卡面上方若存在等級數字 block，視為金色卡而非粉卡。"""
    if not digit_blocks:
        return False

    left, top, right, bottom = _estimate_card_bounds(img_shape, x, y, w, h)
    margin_x = max(10, w // 2)
    for block in digit_blocks:
        bx1 = getattr(block, "x", 0)
        by1 = getattr(block, "y", 0)
        bx2 = bx1 + getattr(block, "w", 0)
        by2 = by1 + getattr(block, "h", 0)

        horizontally_aligned = bx2 >= left - margin_x and bx1 <= right + margin_x
        vertically_above = by1 >= top and by2 <= bottom
        if horizontally_aligned and vertically_above:
            return True
    return False


def _extract_card_color_roi(img: np.ndarray, x: int, y: int, w: int, h: int) -> np.ndarray | None:
    """擷取卡面上方用於顏色判定的 ROI。"""
    left, top, right, bottom = _estimate_card_bounds(img.shape, x, y, w, h)
    card_h = max(80, w * 2)
    color_bottom = max(top + 1, bottom - int(card_h * 0.6))
    if top >= color_bottom or left >= right:
        return None
    return img[top:color_bottom, left:right]


def _measure_card_color_ratios(roi: np.ndarray) -> tuple[float, float]:
    """回傳卡面顏色中的粉卡比例與金卡比例。"""
    hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
    h_ch = hsv[:, :, 0]
    s_ch = hsv[:, :, 1]
    v_ch = hsv[:, :, 2]
    b_ch = roi[:, :, 0]

    valid = (s_ch > 50) & (v_ch > 60) & (b_ch > 80)
    n_valid = int(np.sum(valid))
    if n_valid < 10:
        return 0.0, 0.0

    valid_h = h_ch[valid]
    pink_ratio = float(np.sum((valid_h >= 130) & (valid_h <= 175))) / n_valid
    gold_ratio = float(np.sum((valid_h >= 10) & (valid_h <= 35))) / n_valid
    return pink_ratio, gold_ratio


def spatial_pair_1_to_n(blocks: list[TextBlock], img_height: int, y_offset_ratio: float = 0.3, potentials_db: list[dict] = None, raw_img: np.ndarray = None) -> dict[str, list[str]]:
    """
    透過 Y 軸分群來配對：
    將圖片視為由上而下數個橫向條狀區塊（例如每一列為一個推薦組合）。
    同一個 Y 軸區間內的標籤，將套用於該區間內的所有潛能。
    
    [新增] 若該潛能在 DB 被標記為 type='guaranteed'，將獨立拉出至 guaranteed 區塊，不參與一般群聚。
    """
    res = {"required": [], "backup": [], "skip": [], "guaranteed": []}
    
    # 建立 DB 快取以反查潛能屬性
    db_map = {}
    if potentials_db:
        for p in potentials_db:
            db_map[p["name"]] = p

    labels = [b for b in blocks if b.block_type == "label" and b.matched_label]
    potentials = [b for b in blocks if b.block_type == "potential" and b.matched_name]

    # ── Fallback：把 DB 裡沒有收錄但看起來像潛能名稱的純漢字也加入空間配對 ──
    # 這解決了 priority_list.json 未窮舉所有潛能時導致的漏判問題。
    label_kws = {kw for kws in LABEL_KEYWORDS.values() for kw in kws}

    def _is_cjk_card_name(text: str) -> bool:
        """純 CJK 漢字、2-8字、非標籤關鍵字、非 N選一。"""
        if not (2 <= len(text) <= 8):
            return False
        if any(kw in text for kw in label_kws):
            return False
        # 排除 N選一 樣式的純數字標籤
        if re.search(r'[一二三四五六1-6]選一', text) or re.search(r'[一二三四五六1-6]选一', text):
            return False
        return all('\u4e00' <= ch <= '\u9fff' or '\u3400' <= ch <= '\u4dbf' or ch in '·・' for ch in text)

    known_names = {p.matched_name for p in potentials}
    for blk in blocks:
        if blk.block_type != "unknown":
            continue
        try:
            import zhconv
            txt = zhconv.convert(blk.text, 'zh-tw')
        except ImportError:
            txt = blk.text
        if not _is_cjk_card_name(txt) or txt in known_names:
            continue
        # 當作原生 OCR 文字直接作為潛能名稱使用
        blk.block_type = "potential"
        blk.matched_name = txt.strip()
        blk.fuzzy_score  = 0.0            # 標示為 fallback，分數為 0
        potentials.append(blk)
        known_names.add(txt)

    if not potentials:
        return res
    if not labels and not res["guaranteed"]:
        return res

    # 以 Label 為基準，定義每一個 Label 的 Y 軸管轄範圍（例如 +- 15% 的圖片高度）
    # digit_blocks：純數字文字（等級數 1/5/6...），用來判斷金色卡（有數字 → 非粉色）
    digit_blocks = [b for b in blocks if re.fullmatch(r'\d+', b.text.strip())]
    y_margin = img_height * 0.15
    _backup_group_map: dict[int, list[str]] = {}   # id(lbl) → [潛能名稱]

    # 判斷是否為「區塊模式」(Block Mode)，例如包含冒號或潛能文字等標籤
    is_block_mode = any('潜能' in l.text or '潛能' in l.text or ':' in l.text or '：' in l.text or '核心' in l.text or '一級' in l.text for l in labels)

    for pot in potentials:
        name = pot.matched_name
        
        # [粉色保底] 若為保底給予的特殊潛能，直接放入 guaranteed 不再參加空間配對
        # 條件1：寫死在 .json 裡的標記
        pot_info = db_map.get(name, {})
        is_guaranteed = (pot_info.get("type") == "guaranteed")
        # 條件2：HSV 色彩判定：粉紫色背景 = 保底粉卡
        if not is_guaranteed and raw_img is not None:
            is_guaranteed = _is_pink_card(raw_img, pot.x, pot.y, pot.w, pot.h,
                                          digit_blocks=digit_blocks)
            
        if is_guaranteed:
            if name not in res["guaranteed"]:
                res["guaranteed"].append(name)
            continue

        best_label = None
        best_lbl   = None
        min_dist   = float('inf')

        for lbl in labels:
            # y大為下, x大為右
            dy = lbl.cy - pot.cy
            dx = abs(lbl.cx - pot.cx)
            
            if is_block_mode:
                # 區塊模式：放寬 Y 軸，允許標籤在卡片上方 (dy < 0) 或同行
                dist = (dx ** 2) + (dy * 1.5) ** 2
                max_dx = img_height * 0.5
                if dy > -img_height * 0.4 and dx < max_dx:
                    if dist < min_dist:
                        min_dist   = dist
                        best_label = lbl.matched_label
                        best_lbl   = lbl
            else:
                # 排版管轄範圍 (Bounding constraints)：
                # 1. Y軸：標籤在卡片正下方。不可高於卡片很多 (dy > -10%)，不可低於卡片太遠 (dy < 35%)
                # 2. X軸：樣銀對齊ま。容許一個標籤涵蓋 3 張卡片的標籤最外側卡片距離：
                #    卡片間距約 1 卡片寬 (pot.w*3)，最外側卡片到中心標籤距離就是 1.5 卡片寬 = pot.w*3*0.5 = pot.w*4.5/3
                #    實用上 max_dx = max(img_height*0.2, pot.w*3.5) 碎為安全樓次
                max_dx = max(img_height * 0.2, pot.w * 3.5)
                if -img_height * 0.1 < dy < img_height * 0.35 and dx < max_dx:
                    # 綜合距離評分：找出管轄區內最近的標籤
                    total_dist = dy + dx * 1.5
                    
                    if total_dist < min_dist:
                        min_dist   = total_dist
                        best_label = lbl.matched_label
                        best_lbl   = lbl

        if best_label and best_label in res:
            res[best_label].append(pot.matched_name)
            # 若為備選，記錄到同標籤的群組中（用於互斥判斷）
            if best_lbl is not None:
                # 擴充 _backup_group_map 記錄完整的配對資訊：[name, dy]
                # 以便事後過濾跨列卡片
                _backup_group_map.setdefault(id(best_lbl), []).append((pot.matched_name, best_label, min_dist, (best_lbl.cy - pot.cy)))
        else:
            # 自然掉落機制 (Natural Fallback)
            if name not in res["guaranteed"]:
                res["guaranteed"].append(name)

    # 進階過濾：排除跨列誤抓 (Cross-row false positive filter)
    # 對於每個標籤，我們只保留「距離標籤最近的那一行卡片」
    # 但若是 is_block_mode (區塊面板模式)，同一個面板內本來就會有多行卡片，不套用同行過濾！
    res["backup_groups"] = []
    
    if not is_block_mode:
        for lbl_id, members in _backup_group_map.items():
            if not members: continue
            # members 元素格式: (name, label_type, total_dist, dy)
            min_dy = min(m[3] for m in members)
            
            # 篩選保留條件：dy 與最小 dy 差距小於圖片高度 8% (同一列)
            threshold = img_height * 0.08
            valid_members = []
            for name, lbl_type, dist, dy in members:
                if abs(dy - min_dy) <= threshold:
                    valid_members.append(name)
                else:
                    # 剔除跨列卡片，將其從原分類中移除，並歸類至 guaranteed
                    if name in res[lbl_type]:
                        res[lbl_type].remove(name)
                    if name not in res["guaranteed"]:
                        res["guaranteed"].append(name)
                        
            # 更新 backup_groups (僅備選標籤才建立群組)
            if len(valid_members) >= 2 and members[0][1] == "backup":
                res["backup_groups"].append(valid_members)
    else:
        # 若為 Block Mode，所有備選均視為自由選，不硬性群組化 (或可視為同一個大群組)
        pass

    # 去重複並維持順序
    res["required"]   = list(dict.fromkeys(res["required"]))
    res["backup"]     = list(dict.fromkeys(res["backup"]))
    res["guaranteed"] = list(dict.fromkeys(res["guaranteed"]))
    
    return res


# ─────────────────────────────────────────────────────────────────────────────
# fallback：若無標籤，列出所有潛能供手動分類
# ─────────────────────────────────────────────────────────────────────────────

def manual_classify(blocks: list[TextBlock]) -> dict[str, list[str]]:
    """無法自動配對時，互動式讓使用者分類。"""
    potentials = [b for b in blocks if b.block_type == "potential" and b.matched_name]
    names = list({b.matched_name for b in potentials})

    if not names:
        print("[警告] 未偵測到任何潛能名稱，請確認圖片品質或 OCR 環境。")
        return {"required": [], "backup": []}

    print("\n[降級模式] 未找到「必拿/備選」標籤，以下是偵測到的潛能名稱：")
    for i, n in enumerate(names):
        print(f"  {i+1:2d}. {n}")

    required, backup, guaranteed = [], [], []
    for name in names:
        resp = input(f"「{name}」 → [r]必拿 / [b]備選 / [g]保底 / [s]跳過: ").strip().lower()
        if resp.startswith("r"):
            required.append(name)
        elif resp.startswith("b"):
            backup.append(name)
        elif resp.startswith("g"):
            guaranteed.append(name)

    return {"required": required, "backup": backup, "guaranteed": guaranteed}


# ─────────────────────────────────────────────────────────────────────────────
# 寫入 config.yaml
# ─────────────────────────────────────────────────────────────────────────────

def write_to_config(parsed: dict[str, list], config_path: Path = _CONFIG_YAML) -> None:
    """覆寫 config.yaml 的 decision 區塊（含 required, backup, backup_groups, guaranteed）。"""
    with open(config_path, "r", encoding="utf-8") as f:
        cfg: dict[str, Any] = yaml.safe_load(f) or {}

    if "decision" not in cfg:
        cfg["decision"] = {}

    cfg["decision"]["required"]      = parsed.get("required", [])
    cfg["decision"]["backup"]        = parsed.get("backup", [])
    cfg["decision"]["backup_groups"] = parsed.get("backup_groups", [])
    cfg["decision"]["guaranteed"]    = parsed.get("guaranteed", [])

    with open(config_path, "w", encoding="utf-8") as f:
        yaml.dump(cfg, f, allow_unicode=True, default_flow_style=False, sort_keys=False)

    print(f"\n[✅ 已寫入] {config_path}")
    print(f"  必拿   (required)  : {cfg['decision']['required']}")
    print(f"  備選   (backup)    : {cfg['decision']['backup']}")
    print(f"  備選群組(groups)   : {cfg['decision']['backup_groups']}")
    print(f"  粉色保底(guaranteed): {cfg['decision']['guaranteed']}")


# ─────────────────────────────────────────────────────────────────────────────
# 主程式
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    # 強制輸出 UTF-8，進行跨平台相容
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    parser = argparse.ArgumentParser(description="星塔旅人攻略圖潛能解析工具")
    parser.add_argument("image", nargs="?", help="攻略圖路徑（省略時互動輸入）")
    parser.add_argument("--dry-run", action="store_true", help="僅顯示結果，不寫入 config.yaml")
    parser.add_argument("--scale",   type=float, default=2.0, help="圖像放大倍率（預設 2.0）")
    parser.add_argument("--y-offset", type=float, default=0.30,
                        help="向上搜尋標籤的高度比例（預設 0.30 = 30%%）")
    args = parser.parse_args()

    # ── 取得圖片路徑 ────────────────────────────────────
    if args.image:
        img_path = Path(args.image)
    else:
        default = _GUIDE_DIR / "input.png"
        raw = input(f"請輸入攻略圖路徑 [Enter = {default}]: ").strip()
        img_path = Path(raw) if raw else default

    if not img_path.exists():
        print(f"[ERROR] 圖片不存在：{img_path}")
        sys.exit(1)

    # ── 載入優先級資料庫 ─────────────────────────────────
    if not _PRIORITY_LIST.exists():
        print(f"[ERROR] 找不到 {_PRIORITY_LIST}")
        sys.exit(1)
    with open(_PRIORITY_LIST, "r", encoding="utf-8") as f:
        priority_db: dict = json.load(f)
    potentials_db: list[dict] = priority_db.get("potentials", [])

    # ── 載入並前處理圖片 ─────────────────────────────────
    print(f"\n[步驟 1] 載入並前處理圖片：{img_path}")
    img_array = np.fromfile(str(img_path), dtype=np.uint8)
    raw_img = cv2.imdecode(img_array, cv2.IMREAD_COLOR)
    if raw_img is None:
        print("[ERROR] 無法開啟圖片，檢查路徑或格式（支援 PNG/JPG/WebP）。")
        sys.exit(1)

    h_orig, w_orig = raw_img.shape[:2]
    processed, actual_scale = preprocess(raw_img, scale=args.scale)
    h_proc, _  = processed.shape[:2]
    print(f"  原始解析度：{w_orig}×{h_orig}，前處理後 OCR 圖像：{processed.shape[1]}×{h_proc}")

    # ── OCR ──────────────────────────────────────────────
    print("\n[步驟 2] 執行 OCR（可能需要數秒）...")
    blocks = _run_ocr(processed)
    # 座標縮回原始比例（OCR 在放大圖上跑，座標要除以 scale）
    for b in blocks:
        b.x = int(b.x / actual_scale)
        b.y = int(b.y / actual_scale)
        b.w = int(b.w / actual_scale)
        b.h = int(b.h / actual_scale)
    print(f"  共識別到 {len(blocks)} 個文字區塊。")

    # ── 區塊分類 ─────────────────────────────────────────
    print("\n[步驟 3] 分類：標籤 vs 潛能名稱...")
    blocks = classify_blocks(blocks, potentials_db)
    labels     = [b for b in blocks if b.block_type == "label"]
    potentials_found = [b for b in blocks if b.block_type == "potential"]
    print(f"  找到標籤 {len(labels)} 個，潛能 {len(potentials_found)} 個。")

    for lbl in labels:
        print(f"  　標籤：[{lbl.text}] -> {lbl.matched_label} @ ({lbl.x},{lbl.y},{lbl.w},{lbl.h})")
    for pot in potentials_found:
        print(f"  　潛能：[{pot.text}] -> [{pot.matched_name}] (分={pot.fuzzy_score:.2f}) @ ({pot.cx},{pot.cy})")

    # ── 空間配對 1-to-N ──────────────────────────────────
    print("\n[步驟 4] 1-to-N 空間配對...")
    # 4. 根據標籤的 Y 軸分佈，對潛能進行管轄區間映射 (1對N)
    if labels:
        parsed = spatial_pair_1_to_n(blocks, img_height=raw_img.shape[0], y_offset_ratio=0.30, potentials_db=potentials_db, raw_img=raw_img)
    else:
        print("  未找到標籤，進入降級互動模式...")
        parsed = manual_classify(blocks)

    # ── 預覽結果 ─────────────────────────────────────────
    print("\n" + "=" * 50)
    print("[解析結果預覽]")
    print(f"  必拿 (required): {parsed.get('required', [])}")
    print(f"  備選 (backup):   {parsed.get('backup', [])}")
    print(f"  粉色保底 (guaranteed): {parsed.get('guaranteed', [])}")
    print("=" * 50)

    if not parsed["required"] and not parsed["backup"]:
        print("[警告] 解析結果為空，請確認圖片包含「必拿」「備選」標籤與潛能名稱。")

    # ── 使用者確認 ───────────────────────────────────────
    if not args.dry_run:
        print()
        confirm = input("是否寫入 config.yaml？[Y/n] ").strip().lower()
        if confirm in ("", "y", "yes"):
            # 支援手動修改
            edit = input("是否手動修改後再寫入？[y/N] ").strip().lower()
            if edit in ("y", "yes"):
                raw_req = input("  必拿 (用逗號分隔): ").strip()
                raw_bkp = input("  備選 (用逗號分隔): ").strip()
                if raw_req:
                    parsed["required"] = [x.strip() for x in raw_req.split(",") if x.strip()]
                if raw_bkp:
                    parsed["backup"]   = [x.strip() for x in raw_bkp.split(",") if x.strip()]

            write_to_config(parsed)
        else:
            print("[跳過] 未寫入 config.yaml。")
    else:
        print("[Dry-run] 未寫入 config.yaml。")


if __name__ == "__main__":
    main()
