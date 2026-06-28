"""音符圖示辨識(NoteIconReader)

讀「準備頁啟動條件列」與其他畫面中的小型音符圖示(potion + ♪ 尾巴 + 白色內符號)。

歷史(GAME_MECHANICS D1/D2):
- 整圖 template matching 在 16~24px 失效 —— 13 種共用 potion+尾巴形狀,中央小符號
  訊號量在低解析被淹沒(D1 實證 conf 0.48~0.62 且贏家是錯的)。
- 純顏色分類也不行 —— 相鄰音符色相太近(強攻8°/幸運22°/暗26°,元素撞固定色)。

可行解(本模組,PoC 對 prepare_current_20260614_192942.png 10/10):
  ① 偵測:white(亮且低飽和)∧ 鄰近飽和色 = glyph layer → 連通元件 + disk-ring 測試
     濾掉數字/背景雜訊(數字在暗底、不在彩色圓盤上)。
  ② 分類:乾淨抽 glyph(圓盤中央最大白色連通元件,去 ♪ 尾巴/數字,保長寬比 → 24x24)
     → 與 assets/templates/note_*.png 的 glyph 做位移容忍 IoU(主,0.6)
     + 色相親和度(輔,0.4)。
  ③ 元素懲罰:7 固定音符每塔皆有、6 元素每塔僅 2 種 → 元素候選 ×0.90,破近似同色
     的「固定 vs 未現身元素」撞色(如 幸運 vs 暗)。已知元素集(known_elements)時改為
     直接排除未現身元素(更準)。

座標相依參數以 720p 為基準,依 frame_h 線性縮放。CJK 路徑用 imdecode(fromfile) 載入。
"""
from __future__ import annotations

import json
from pathlib import Path

import cv2
import numpy as np

# ── 分類權重與門檻(720p 基準)──
GLYPH_SIZE = 24
HUE_SIGMA = 18.0
GLYPH_WEIGHT = 0.60
HUE_WEIGHT = 0.40
ELEMENT_PENALTY = 0.90        # 元素音符近似同色撞時的弱化(7 固定 > 2/塔 元素)
CONFIDENCE_FLOOR = 0.34       # 低於此視為非音符圖示(避免在非啟動條件 ROI 誤判)

_ROOT = Path(__file__).resolve().parents[1]
_ASSETS = _ROOT / 'assets' / 'templates'
_NOTES_MAP = _ROOT / 'data' / 'notes_map.json'


def _imread_cjk(path: Path) -> np.ndarray | None:
    try:
        data = np.fromfile(str(path), dtype=np.uint8)
        if data.size == 0:
            return None
        return cv2.imdecode(data, cv2.IMREAD_COLOR)
    except Exception:
        return None


def _central_circle(shape, frac: float) -> np.ndarray:
    h, w = shape[:2]
    cy, cx = h / 2.0, w / 2.0
    yy, xx = np.ogrid[:h, :w]
    r = min(h, w) / 2.0 * frac
    return (yy - cy) ** 2 + (xx - cx) ** 2 <= r * r


def _pad_square_resize(binimg: np.ndarray, size: int = GLYPH_SIZE) -> np.ndarray | None:
    if binimg is None:
        return None
    ys, xs = np.where(binimg > 0)
    if len(ys) == 0:
        return None
    y0, y1, x0, x1 = ys.min(), ys.max() + 1, xs.min(), xs.max() + 1
    crop = binimg[y0:y1, x0:x1]
    h, w = crop.shape
    s = max(h, w)
    sq = np.zeros((s, s), np.uint8)
    sq[(s - h) // 2:(s - h) // 2 + h, (s - w) // 2:(s - w) // 2 + w] = crop
    return cv2.resize(sq, (size, size), interpolation=cv2.INTER_AREA)


def _largest_white_cc(white: np.ndarray, cx: float, cy: float) -> np.ndarray | None:
    """白色像素中,面積大且靠近中心的連通元件(=符號本體;自然丟掉數字/尾巴碎塊)。"""
    wb = white.astype(np.uint8)
    n, lab, stats, cent = cv2.connectedComponentsWithStats(wb, 8)
    best, bi = -1.0, -1
    for i in range(1, n):
        area = stats[i][4]
        if area < 6:
            continue
        d = (cent[i][0] - cx) ** 2 + (cent[i][1] - cy) ** 2
        score = area - 0.4 * d
        if score > best:
            best, bi = score, i
    if bi > 0:
        return (lab == bi).astype(np.uint8) * 255
    return (wb * 255) if wb.sum() >= 6 else None


def _hue_dist(a: float, b: float) -> float:
    d = abs(a - b) % 360.0
    return min(d, 360.0 - d)


def _glyph_iou(a: np.ndarray, b: np.ndarray) -> float:
    """位移容忍二值 IoU(±2px),容忍輕微對位誤差。"""
    best = 0.0
    for dx in (-2, -1, 0, 1, 2):
        for dy in (-2, -1, 0, 1, 2):
            pb = np.roll(np.roll(a, dx, 1), dy, 0)
            inter = np.logical_and(pb > 0, b > 0).sum()
            union = np.logical_or(pb > 0, b > 0).sum()
            if union:
                iou = inter / union
                if iou > best:
                    best = iou
    return best


class NoteIconReader:
    """從 assets/templates/note_*.png + notes_map.json 建立 (glyph, hue, element) 模板庫。"""

    def __init__(self, assets_dir: Path = _ASSETS, notes_map: Path = _NOTES_MAP) -> None:
        self.catalog: dict[str, dict] = {}
        self._build(assets_dir, notes_map)

    def _build(self, assets_dir: Path, notes_map: Path) -> None:
        try:
            data = json.loads(notes_map.read_text(encoding='utf-8'))
            notes = data.get('notes', [])
        except Exception:
            notes = [{'id': f'note_{i}', 'name': f'note_{i}',
                      'element': i >= 8} for i in range(1, 14)]
        for entry in notes:
            nid = entry.get('id')
            img = _imread_cjk(assets_dir / f'{nid}.png')
            if img is None:
                continue
            glyph, hue = self._asset_glyph_hue(img)
            if glyph is None:
                continue
            self.catalog[nid] = {
                'name': entry.get('name', nid),
                'element': bool(entry.get('element', False)),
                'glyph': glyph,
                'hue': hue,
            }

    @staticmethod
    def _asset_glyph_hue(img: np.ndarray):
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        Hh, S, V = hsv[..., 0], hsv[..., 1], hsv[..., 2]
        color = ((S > 60) & (V > 50)).astype(np.uint8) * 255
        cnts, _ = cv2.findContours(color, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if not cnts:
            return None, -1.0
        big = max(cnts, key=cv2.contourArea)
        M = cv2.moments(big)
        if M['m00'] == 0:
            return None, -1.0
        bcx, bcy = M['m10'] / M['m00'], M['m01'] / M['m00']
        filled = np.zeros_like(color)
        cv2.drawContours(filled, [big], -1, 255, -1)
        white = (V > 150) & (S < 90) & (filled > 0)
        glyph = _largest_white_cc(white, bcx, bcy)
        hue = float(np.median(Hh[color > 0])) * 2.0
        return _pad_square_resize(glyph), hue

    # ── 偵測 ──
    def detect_icons(self, roi_bgr: np.ndarray, scale: float = 1.0) -> list[tuple[int, int, int]]:
        """回傳 [(cx, cy, est_size)],座標相對 roi。scale = frame_h/720。"""
        h, w = roi_bgr.shape[:2]
        if h < 8 or w < 8:
            return []
        hsv = cv2.cvtColor(roi_bgr, cv2.COLOR_BGR2HSV)
        S, V = hsv[..., 1], hsv[..., 2]
        white = ((V > 175) & (S < 65)).astype(np.uint8)
        color = ((S > 100) & (V > 75)).astype(np.uint8)
        dk = max(3, int(round(7 * scale)) | 1)
        ck = max(3, int(round(5 * scale)) | 1)
        color_d = cv2.dilate(color, np.ones((dk, dk), np.uint8))
        gl = cv2.morphologyEx((white & color_d).astype(np.uint8) * 255,
                              cv2.MORPH_CLOSE, np.ones((ck, ck), np.uint8))
        n, lab, stats, cent = cv2.connectedComponentsWithStats(gl, 8)
        min_area = 40 * scale * scale
        min_wh = max(4, int(round(7 * scale)))
        ring_r = max(5, int(round(11 * scale)))
        est = max(10, int(round(26 * scale)))
        cands = []
        for i in range(1, n):
            x, y, bw, bh, area = stats[i]
            if area < min_area or bw < min_wh or bh < min_wh:
                continue
            cx, cy = int(cent[i][0]), int(cent[i][1])
            ok = tot = 0
            for ang in range(0, 360, 30):
                rx = int(cx + ring_r * np.cos(np.radians(ang)))
                ry = int(cy + ring_r * np.sin(np.radians(ang)))
                if 0 <= ry < h and 0 <= rx < w:
                    tot += 1
                    ok += int(color[ry, rx])
            if (ok / tot if tot else 0) < 0.5:
                continue
            cands.append((cx, cy, int(area)))
        cands.sort(key=lambda c: -c[2])
        gap = max(8, int(round(18 * scale)))
        kept: list[tuple[int, int, int]] = []
        for cx, cy, area in cands:
            if all(abs(cx - kx) > gap for kx, _, _ in kept):
                kept.append((cx, cy, est))
        kept.sort(key=lambda c: c[0])
        return kept

    # ── 分類 ──
    def _probe_glyph_hue(self, window_bgr: np.ndarray):
        hsv = cv2.cvtColor(window_bgr, cv2.COLOR_BGR2HSV)
        Hh, S, V = hsv[..., 0], hsv[..., 1], hsv[..., 2]
        cm = _central_circle(window_bgr.shape, 0.85)
        color = (S > 80) & (V > 60) & cm
        if int(color.sum()) < 12:
            return None, -1.0
        hue = float(np.median(Hh[color])) * 2.0
        white = (V > 140) & (S < 100) & cm
        glyph = _largest_white_cc(white, window_bgr.shape[1] / 2.0, window_bgr.shape[0] / 2.0)
        return _pad_square_resize(glyph), hue

    def classify(self, window_bgr: np.ndarray, known_elements: set[str] | None = None):
        """回傳 (note_id, confidence, hue) 或 None。"""
        probe, hue = self._probe_glyph_hue(window_bgr)
        if hue < 0:
            return None
        best = None
        for nid, info in self.catalog.items():
            if info['element'] and known_elements is not None and nid not in known_elements:
                continue
            ha = float(np.exp(-(_hue_dist(hue, info['hue']) ** 2) / (2 * HUE_SIGMA ** 2)))
            gs = _glyph_iou(probe, info['glyph']) if probe is not None else 0.0
            total = GLYPH_WEIGHT * gs + HUE_WEIGHT * ha
            if info['element'] and (known_elements is None or nid not in known_elements):
                total *= ELEMENT_PENALTY
            if best is None or total > best[1]:
                best = (nid, total, hue)
        return best

    # ── 對外 API:相容舊 _match_note_templates 回傳格式 ──
    def find_icons(self, scene_bgr: np.ndarray, roi, frame_h: int = 720,
                   known_elements: set[str] | None = None) -> list[dict]:
        x, y, w, h = roi
        if w <= 0 or h <= 0 or scene_bgr is None:
            return []
        sub = scene_bgr[y:y + h, x:x + w]
        if sub.size == 0 or not self.catalog:
            return []
        scale = max(0.4, frame_h / 720.0)
        win_half = max(8, int(round(14 * scale)))
        out: list[dict] = []
        for cx, cy, est in self.detect_icons(sub, scale):
            win = sub[max(0, cy - win_half):cy + win_half, max(0, cx - win_half):cx + win_half]
            if win.size == 0:
                continue
            res = self.classify(win, known_elements)
            if res is None:
                continue
            nid, conf, _hue = res
            if conf < CONFIDENCE_FLOOR:
                continue
            info = self.catalog[nid]
            acx, acy = x + cx, y + cy
            half = est // 2
            rect = (acx - half, acy - half, acx + half, acy + half)
            out.append({
                'note_id': nid,
                'note_name': info['name'],
                'center_x': acx,
                'center_y': acy,
                'rect': rect,
                'width': est,
                'height': est,
                'confidence': round(float(conf), 4),
            })
        out.sort(key=lambda d: (d['center_y'], d['center_x']))
        return out


_reader: NoteIconReader | None = None


def get_reader() -> NoteIconReader:
    global _reader
    if _reader is None:
        _reader = NoteIconReader()
    return _reader
