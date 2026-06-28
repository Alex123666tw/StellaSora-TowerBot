from __future__ import annotations

from dataclasses import dataclass


class FakeInput:
    def __init__(self) -> None:
        self.clicks: list[tuple[int, int]] = []
        self.keys: list[str] = []

    def click(self, x: int, y: int, delay: float = 0.05) -> None:
        self.clicks.append((x, y))

    def press_key(self, key: str, delay: float = 0.05) -> bool:
        self.keys.append(key)
        return True

    def press_esc(self, delay: float = 0.05) -> bool:
        self.keys.append('esc')
        return True


class FakeOCR:
    def __init__(
        self,
        slot_results: list[list[tuple[str, float, tuple]]] | None = None,
        global_results: list[tuple[str, float, tuple]] | None = None,
        simple_results: dict[tuple[int, int, int, int] | None, list[str]] | None = None,
        simple_sequence: list[list[str]] | None = None,
    ) -> None:
        self.slot_results = list(slot_results or [])
        self.global_results = list(global_results or [])
        self.simple_results = dict(simple_results or {})
        # 依呼叫順序回傳的 read_text_simple 結果（模擬連續輪詢畫面變化）。
        # 佇列耗盡後回到 simple_results 查表行為。
        self.simple_sequence = [list(items) for items in (simple_sequence or [])]

    def read_text(self, img, roi=None):
        if roi is None:
            return list(self.global_results)
        rx, ry, _rw, _rh = roi
        if self.slot_results:
            absolute = []
            for text, conf, bbox in self.slot_results.pop(0):
                absolute.append((
                    text,
                    conf,
                    tuple((point[0] + rx, point[1] + ry) for point in bbox),
                ))
            return absolute
        filtered = []
        for text, conf, bbox in self.global_results:
            xs = [point[0] for point in bbox]
            ys = [point[1] for point in bbox]
            cx = int(sum(xs) / 4)
            cy = int(sum(ys) / 4)
            if rx <= cx <= rx + _rw and ry <= cy <= ry + _rh:
                filtered.append((text, conf, bbox))
        return filtered

    def read_text_simple(self, img, roi=None):
        if self.simple_sequence:
            return list(self.simple_sequence.pop(0))
        key = tuple(roi) if roi is not None else None
        return list(self.simple_results.get(key, []))


@dataclass
class FakeMatchResult:
    found: bool
    confidence: float = 1.0
    center_x: int = 0
    center_y: int = 0
    rect: tuple = ()


class FakeMatcher:
    def __init__(self, matches: dict[str, FakeMatchResult] | None = None) -> None:
        self.matches = dict(matches or {})

    def match(self, img, template_name, threshold=None):
        if template_name not in self.matches:
            raise KeyError(template_name)
        return self.matches[template_name]


class FakeDecisionState:
    def __init__(self) -> None:
        self.recorded: list[object] = []

    def record_selection(self, option) -> None:
        self.recorded.append(option)


class FakeDecisionEngine:
    def __init__(self, choose_index: int = 0) -> None:
        self.choose_index = choose_index
        self.state = FakeDecisionState()
        self.last_options = []

    def decide(self, options):
        self.last_options = list(options)
        if not options:
            return None
        return options[self.choose_index]

    def preview_decision(self, options):
        self.last_options = list(options)
        if not options:
            return None
        return options[self.choose_index]

    def categorize(self, options):
        self.last_options = list(options)
        return options

    def _pick_best(self, options):
        return options[self.choose_index]


class FakeWindowManager:
    def __init__(self, frame) -> None:
        self.frame = frame
        self.capture_calls = 0
        # 模擬已鎖定的視窗 handle（core/bot.py 主迴圈會檢查 truthy）
        self.hwnd = 1

    def screenshot(self):
        return self.frame.copy()

    def capture(self):
        self.capture_calls += 1
        return self.frame.copy(), "fake"


class FakeMatcherSequence:
    def __init__(self, matches_by_template: dict[str, list[FakeMatchResult]] | None = None) -> None:
        self.matches_by_template = {k: list(v) for k, v in (matches_by_template or {}).items()}

    def match(self, img, template_name, threshold=None):
        queue = self.matches_by_template.get(template_name)
        if queue:
            return queue.pop(0)
        raise KeyError(template_name)


class FakeWindowManagerSequence:
    """依呼叫順序回傳不同 frame 的 WindowManager（模擬點擊後畫面變化）。

    佇列剩最後一張時不再 pop（之後每次 capture 都回同一張）。
    Phase 1.3 click_verified 的 expect 驗證測試使用。
    """

    def __init__(self, frames) -> None:
        self.frames = list(frames)
        if not self.frames:
            raise ValueError("FakeWindowManagerSequence needs at least one frame")
        self.capture_calls = 0
        self.hwnd = 1

    def screenshot(self):
        return self.frames[0].copy()

    def capture(self):
        self.capture_calls += 1
        frame = self.frames.pop(0) if len(self.frames) > 1 else self.frames[0]
        return frame.copy(), "fake"
