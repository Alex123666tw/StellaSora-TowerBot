import sys
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# 回放測試語料(遊戲畫面截圖、OCR 快取)因著作權已移出版控(見 .gitignore 與
# CONTRIBUTING 的「自備語料」)。當本機沒有這些語料(例如 fresh clone)時,因找不到
# 語料檔而失敗的測試會被「自動轉為 skip」,使 pytest 仍能乾淨通過;本機有語料時則
# 一律照常執行,完全不介入。只精準略過真正讀不到語料的測試,不影響純邏輯測試。
_FRAMES_DIR = PROJECT_ROOT / "tests" / "replays" / "frames"
_CORPUS_PRESENT = _FRAMES_DIR.is_dir() and any(_FRAMES_DIR.glob("*.png"))
_CORPUS_TOKENS = ("frames", "ocr_cache", "notes_calib", ".jsonl")


def _missing_corpus_path(exc) -> str | None:
    """例外若提及外置語料路徑(讀檔失敗或斷言找不到),回傳該路徑,否則 None。
    本函式僅在 _CORPUS_PRESENT 為 False(語料確實不在)時被呼叫,故提及語料路徑的
    失敗必屬語料缺失,可安全轉為 skip;不會遮蔽本機有語料時的真實失敗。"""
    name = (str(getattr(exc, "filename", "") or "") + " " + str(exc)).replace("\\", "/")
    if "replays" in name and any(t in name for t in _CORPUS_TOKENS):
        return name.strip()
    return None


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    if _CORPUS_PRESENT or call.excinfo is None:
        return
    missing = _missing_corpus_path(call.excinfo.value)
    if missing is None:
        return
    report = outcome.get_result()
    report.outcome = "skipped"
    report.longrepr = (str(item.fspath), item.location[1] or 0,
                       f"Skipped: 回放語料未提供 ({missing});見 CONTRIBUTING 的「自備語料」")
