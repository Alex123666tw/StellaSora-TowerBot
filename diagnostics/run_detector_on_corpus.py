"""
L2 離線實圖批跑腳本 (diagnostics/run_detector_on_corpus.py)

對 tests/replays/frames/*.png 跑真 EasyOCR，把每張的 OCR 結果（文字 + bbox + 信心值）
落地存 tests/replays/ocr_cache/<檔名>.json（UTF-8），供 pytest 離線使用
（pytest 內不得 init EasyOCR）。

OCR 流程刻意複製 vision/state_detector.py 的 detect() 前處理：
  - 寬 > 1280 時縮小 50%（與實機辨識相同的 EasyOCR 輸入）
  - bbox 換算回原圖絕對座標後存檔

用法：
    .\\.venv\\Scripts\\python.exe diagnostics\\run_detector_on_corpus.py [--only <substring>]

首次執行 EasyOCR 模型載入可能要數十秒。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np

import _bootstrap  # noqa: F401  (sys.path -> project root)

FRAMES_DIR = _bootstrap.PROJECT_ROOT / "tests" / "replays" / "frames"
CACHE_DIR = _bootstrap.PROJECT_ROOT / "tests" / "replays" / "ocr_cache"


def _imread_unicode(path: Path) -> np.ndarray | None:
    """cv2.imread 在 Windows 上對非 ASCII 路徑會失敗，改用 fromfile + imdecode。"""
    import cv2
    try:
        data = np.fromfile(str(path), dtype=np.uint8)
        return cv2.imdecode(data, cv2.IMREAD_COLOR)
    except Exception:
        return None


def _detector_scale(width: int) -> float:
    # 與 vision/state_detector.py detect() 的縮放策略一致
    return 0.5 if width > 1280 else 1.0


def main() -> int:
    parser = argparse.ArgumentParser(description="批跑語料庫 OCR 並落地 cache")
    parser.add_argument("--only", default=None, help="只處理檔名包含此子字串的圖片")
    parser.add_argument("--force", action="store_true", help="覆蓋已存在的 cache 檔")
    args = parser.parse_args()

    import cv2

    frames = sorted(FRAMES_DIR.glob("*.png"))
    if args.only:
        frames = [p for p in frames if args.only in p.name]
    if not frames:
        print(f"[corpus] no frames found under {FRAMES_DIR}")
        return 1

    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    print(f"[corpus] initializing EasyOCR (ch_tra+en, gpu=True with CPU fallback)...")
    from vision.ocr_engine import OcrEngine
    try:
        ocr = OcrEngine(languages=["ch_tra", "en"], gpu=True)
    except Exception as e:
        print(f"[corpus] gpu init failed ({e}); falling back to cpu")
        ocr = OcrEngine(languages=["ch_tra", "en"], gpu=False)

    total = len(frames)
    for idx, path in enumerate(frames, start=1):
        out_path = CACHE_DIR / f"{path.name}.json"
        if out_path.exists() and not args.force:
            print(f"[corpus] ({idx}/{total}) skip (cached): {path.name}")
            continue

        frame = _imread_unicode(path)
        if frame is None:
            print(f"[corpus] ({idx}/{total}) FAILED to read image: {path.name}")
            continue

        h, w = frame.shape[:2]
        scale = _detector_scale(w)
        region = frame
        if scale != 1.0:
            region = cv2.resize(region, (0, 0), fx=scale, fy=scale, interpolation=cv2.INTER_LINEAR)

        t0 = time.time()
        results = ocr.read_text(region)
        elapsed = time.time() - t0

        items = []
        for text, conf, bbox in results:
            abs_bbox = [[int(round(x / scale)), int(round(y / scale))] for x, y in bbox]
            items.append({
                "text": text,
                "confidence": round(float(conf), 4),
                "bbox": abs_bbox,
            })

        payload = {
            "file": path.name,
            "image_w": int(w),
            "image_h": int(h),
            "ocr_scale": scale,
            "items": items,
        }
        out_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"[corpus] ({idx}/{total}) {path.name}: {len(items)} texts in {elapsed:.1f}s")

    print(f"[corpus] done -> {CACHE_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
