"""
模板比對辨識模組 (TemplateMatcher)

使用 OpenCV 的 cv2.matchTemplate 找出遊戲畫面中的 UI 元素位置。
支援多尺度比對（應對遊戲解析度變化），並提供視覺化除錯功能。
"""
import cv2
import numpy as np
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional

# ────────────────────────────────────────────
# 資料結構定義
# ────────────────────────────────────────────

@dataclass
class MatchResult:
    """單一模板的比對結果"""
    name: str                   # 模板名稱 (不含副檔名)
    found: bool                 # 是否達到閾值
    confidence: float           # 最佳匹配信心值 (0.0 ~ 1.0)
    center_x: int = 0           # 匹配中心 X 座標 (未找到時為 0)
    center_y: int = 0           # 匹配中心 Y 座標 (未找到時為 0)
    rect: tuple = field(default_factory=tuple)  # (left, top, right, bottom)

    def __repr__(self) -> str:
        if self.found:
            return (f"MatchResult({self.name}: ✅ 找到, "
                    f"信心={self.confidence:.3f}, 中心=({self.center_x},{self.center_y}))")
        return f"MatchResult({self.name}: ❌ 未找到, 信心={self.confidence:.3f})"


# ────────────────────────────────────────────
# 主要類別
# ────────────────────────────────────────────

class TemplateMatcher:
    """
    提供 OpenCV 模板比對功能，支援批次比對與視覺化除錯。
    
    說明：
    - 模板圖片應存放於 assets/templates/ 目錄下，支援 .png / .jpg。
    - 建議模板使用遊戲實際解析度截圖裁切，避免縮放失真。
    - 預設使用 TM_CCOEFF_NORMED 方法（對光線變化較不敏感）。
    """

    SUPPORTED_METHODS = {
        "CCOEFF_NORMED":  cv2.TM_CCOEFF_NORMED,   # 最常用，對明度變化較不敏感（推薦）
        "CCORR_NORMED":   cv2.TM_CCORR_NORMED,    # 速度快，但誤判率較高
        "SQDIFF_NORMED":  cv2.TM_SQDIFF_NORMED,   # 最小值為最佳，邏輯相反
    }

    def __init__(
        self,
        template_dir: str = "assets/templates",
        threshold:    float = 0.80,
        method:       str   = "CCOEFF_NORMED",
    ):
        """
        Args:
            template_dir: 模板圖片的資料夾路徑（相對或絕對均可）。
            threshold:    信心值閾值，超過才視為「找到」(0.0~1.0，預設 0.80)。
            method:       matchTemplate 演算法，預設 CCOEFF_NORMED。
        """
        self.template_dir = Path(template_dir)
        self.threshold    = threshold
        self.method_name  = method
        self.method       = self.SUPPORTED_METHODS.get(method, cv2.TM_CCOEFF_NORMED)

        # 模板快取：{名稱: BGR 圖片矩陣}，避免重複讀檔
        self._cache: dict[str, np.ndarray] = {}

        # 初始化時先載入所有模板
        self._load_all_templates()

    # ────────────────────────────────────────
    # 私有方法
    # ────────────────────────────────────────

    def _load_all_templates(self) -> None:
        """掃描模板資料夾，將所有圖片預載入快取"""
        if not self.template_dir.exists():
            # 資料夾不存在時只給警告，不阻斷啟動
            print(f"[TemplateMatcher] 警告：模板資料夾不存在：{self.template_dir}")
            return

        loaded = 0
        for ext in ("*.png", "*.jpg", "*.jpeg", "*.bmp"):
            for img_path in self.template_dir.glob(ext):
                name = img_path.stem  # 不含副檔名的檔名
                img = cv2.imread(str(img_path), cv2.IMREAD_COLOR)
                if img is None:
                    print(f"[TemplateMatcher] 警告：無法讀取模板：{img_path}")
                    continue
                self._cache[name] = img
                loaded += 1

        print(f"[TemplateMatcher] 已載入 {loaded} 個模板 (資料夾: {self.template_dir})")

    def _raw_match(
        self,
        scene: np.ndarray,
        template: np.ndarray,
    ) -> tuple[float, tuple[int, int, int, int]]:
        """
        執行單次模板比對，回傳最佳信心值與對應的矩形範圍。

        Returns:
            (confidence, (left, top, right, bottom))
        """
        sh, sw = scene.shape[:2]
        th, tw = template.shape[:2]
        if sh < th or sw < tw:
            return 0.0, ()
        result = cv2.matchTemplate(scene, template, self.method)

        if self.method == cv2.TM_SQDIFF_NORMED:
            # SQDIFF 方法：值越小越接近，需轉換
            min_val, _, min_loc, _ = cv2.minMaxLoc(result)
            confidence = 1.0 - min_val
            top_left   = min_loc
        else:
            _, max_val, _, max_loc = cv2.minMaxLoc(result)
            confidence = max_val
            top_left   = max_loc

        left, top = top_left
        rect = (left, top, left + tw, top + th)
        return confidence, rect

    # ────────────────────────────────────────
    # 公開方法
    # ────────────────────────────────────────

    def load_template(self, name: str, path: str) -> None:
        """手動載入單一模板至快取（可在執行期動態新增）"""
        img = cv2.imread(path, cv2.IMREAD_COLOR)
        if img is None:
            raise FileNotFoundError(f"無法讀取模板圖片：{path}")
        self._cache[name] = img

    def match(
        self,
        scene: np.ndarray,
        template_name: str,
        threshold: Optional[float] = None,
    ) -> MatchResult:
        """
        在場景圖中尋找指定模板。

        Args:
            scene:         來源畫面 (OpenCV BGR 格式)。
            template_name: 模板名稱（對應快取中的 key，即檔名不含副檔名）。
            threshold:     臨時覆蓋閾值，None 表示使用預設值。

        Returns:
            MatchResult 物件。
        """
        if template_name not in self._cache:
            raise KeyError(
                f"找不到模板「{template_name}」，"
                f"可用模板：{list(self._cache.keys())}"
            )

        thr = threshold if threshold is not None else self.threshold
        template = self._cache[template_name]
        confidence, rect = self._raw_match(scene, template)
        found = confidence >= thr

        cx = 0
        cy = 0
        if rect:
            cx = (rect[0] + rect[2]) // 2
            cy = (rect[1] + rect[3]) // 2

        return MatchResult(
            name=template_name,
            found=found,
            confidence=confidence,
            center_x=cx if found else 0,
            center_y=cy if found else 0,
            rect=rect if found else (),
        )

    def match_any(
        self,
        scene: np.ndarray,
        template_names: list[str],
        threshold: Optional[float] = None,
    ) -> Optional[MatchResult]:
        """
        批次比對多個模板，回傳「第一個找到的」結果（信心值最高者優先）。

        Args:
            scene:          來源畫面。
            template_names: 要依序比對的模板名稱列表。
            threshold:      臨時覆蓋閾值。

        Returns:
            找到的 MatchResult；若全部未找到則回傳 None。
        """
        best: Optional[MatchResult] = None
        for name in template_names:
            try:
                result = self.match(scene, name, threshold)
                if result.found:
                    if best is None or result.confidence > best.confidence:
                        best = result
            except KeyError as e:
                print(f"[TemplateMatcher] 警告：{e}")
        return best

    def match_all(
        self,
        scene: np.ndarray,
        threshold: Optional[float] = None,
    ) -> dict[str, MatchResult]:
        """
        對所有已載入的模板執行比對，回傳完整結果字典。
        適合用於「偵測當前遊戲狀態」的場景。

        Returns:
            {模板名稱: MatchResult}
        """
        results = {}
        for name in self._cache:
            results[name] = self.match(scene, name, threshold)
        return results

    def draw_result(
        self,
        scene: np.ndarray,
        result: MatchResult,
        color: tuple[int, int, int] = (0, 255, 0),
        thickness: int = 2,
    ) -> np.ndarray:
        """
        在場景圖上繪製比對結果（用於除錯視覺化）。

        Returns:
            繪製標記後的圖片（不修改原始圖片）。
        """
        output = scene.copy()
        if result.found and result.rect:
            l, t, r, b = result.rect
            # 繪製矩形框
            cv2.rectangle(output, (l, t), (r, b), color, thickness)
            # 繪製十字準心
            cx, cy = result.center_x, result.center_y
            cv2.drawMarker(output, (cx, cy), color,
                           cv2.MARKER_CROSS, 20, thickness)
            # 標記名稱與信心值
            label = f"{result.name} ({result.confidence:.2f})"
            cv2.putText(output, label, (l, t - 8),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.55, color, 2)
        else:
            # 未找到：標示紅色文字
            label = f"[未找到] {result.name} ({result.confidence:.2f})"
            cv2.putText(output, label, (10, 30),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.65,
                        (0, 0, 255), 2)
        return output

    @property
    def templates(self) -> list[str]:
        """回傳目前快取中所有已載入的模板名稱"""
        return list(self._cache.keys())

    def reload(self) -> None:
        """清空快取並重新掃描模板資料夾（熱重載用）"""
        self._cache.clear()
        self._load_all_templates()
