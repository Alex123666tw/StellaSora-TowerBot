"""
點擊安全化 (core/actions.py) — REPAIR_PLAN Phase 1.3，解 R3。

R3：舊版 `states._click_text_or_fallback()` 找不到目標時**無條件**點備援座標，
把辨識失敗放大成錯誤動作（典型軌跡 20260602_215757：商店購買成功 → 誤判選卡
→ OCR 找不到 Reroll → 盲點 (1216, 648) → 卡死）。

本模組提供唯一的安全點擊入口：

    click_verified(ctx, target, *, expect, timeout=3.0, source=...) -> bool

核心契約：
  1. **找不到 target → 不點**，回傳 False，由 handler 決定重拍或放棄。
  2. 點擊後**重拍**驗證 expect（下一狀態 signature 命中、或 ROI hash 改變）；
     未滿足 → 重新解析 target 後重試一次 → 仍失敗回 False，
     並寫入 click_trace（source=click_verified_verify_failed）與
     state_trace（verify=failed），failure bundle 可直接看出 verify 失敗。
  3. 明確座標 target 僅允許白名單（SAFE_POINT_WHITELIST，tap_continue 類
     安全空白點）；其餘一律 ValueError（程式錯誤，不得靜默吞掉）。

target 支援形式：
  - TextTarget(keywords, roi)   : OCR 文字（正規化比對，roi 為像素座標）。
  - TemplateTarget(name)        : assets/templates 模板。
  - OcrPoint(x, y, matched_text): 呼叫端已在「當前 frame」用 OCR 命中的座標
                                  （帶證據，非盲點；matched_text 必須非空）。
  - PointTarget(name)           : 白名單安全空白點（比例座標）。

expect 支援形式：
  - ExpectRoiChange(roi)        : 點擊後指定比例 ROI（None=全畫面）的像素
                                  hash 必須改變（畫面有反應）。
  - ExpectStateIn(states, signature_names)
                                : 點擊後重拍 + OCR + vision.signatures.classify，
                                  判定狀態（或 signature 名）必須命中指定集合。
  - EXPECT_NONE                 : 過渡用——target 仍必須命中才點（R3 核心不變），
                                  但不做點後驗證。僅限「下一畫面機制未驗證
                                  （見 docs/GAME_MECHANICS.md）」或重複點擊有害、
                                  已有跨輪詢驗證機制（如商店 pending slot）的點位。

降級行為：ctx 無 wm / 重拍失敗 / ExpectStateIn 無 ocr 時**無法驗證**，
記 trace（verify=skipped_*）後視為成功（單次點擊、不重試）——
等同舊行為的「點了就走」，但仍保有「找不到不點」的安全性。
"""
from __future__ import annotations

import logging
import time
import zlib
from dataclasses import dataclass
from typing import TYPE_CHECKING

from vision import signatures

if TYPE_CHECKING:
    from core.bot import BotContext

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────
# 白名單安全空白點（比例座標）。明確座標 target 僅允許此清單（R3）。
# 與 states._safe_blank_point 保持一致（tap_continue / note_acquired 變體）。
# ─────────────────────────────────────────────────────────────

SAFE_POINT_WHITELIST: dict[str, tuple[float, float]] = {
    "tap_continue": (0.88, 0.78),
    "tap_continue_note": (0.88, 0.72),
    # 結算畫面「垃圾桶🗑️」丟棄鈕：純 icon 無文字（同 reroll icon,無法以 OCR 文字定位）。
    # 僅在 handle_result 已正向判定 STATE_RESULT + 已解鎖 + 決定丟棄後點擊,且
    # click_verified 以 ExpectStateIn(STATE_DISCARD_CONFIRM) 驗證(座標錯則快速失敗,
    # 非盲點迴圈)。座標 L1 取自 result__20260614_005348(垃圾桶暗色圓 icon 中心 448,654
    # → 1280x720 相對 0.350,0.908),待 L3 校準。
    "result_trash": (0.350, 0.908),
}


# ─────────────────────────────────────────────────────────────
# target / expect 資料結構
# ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class TextTarget:
    """OCR 文字目標：keywords 任一（正規化後）命中即解析為該文字中心點。"""

    keywords: tuple[str, ...]
    roi: tuple[int, int, int, int] | None = None  # 像素座標 (x, y, w, h)


@dataclass(frozen=True)
class TemplateTarget:
    """模板目標：assets/templates 內的模板名（經 ctx.matcher）。"""

    name: str


@dataclass(frozen=True)
class OcrPoint:
    """已由 OCR 在當前 frame 命中所得的座標（帶證據，非盲點）。

    matched_text 必須非空 —— 沒有證據的座標不是合法 target。
    """

    x: int
    y: int
    matched_text: str


@dataclass(frozen=True)
class PointTarget:
    """白名單安全空白點。name 必須在 SAFE_POINT_WHITELIST。"""

    name: str


@dataclass(frozen=True)
class ExpectRoiChange:
    """點擊後指定比例 ROI 的像素 hash 必須改變。roi=None 代表全畫面。"""

    roi: tuple[float, float, float, float] | None = None  # (x0, y0, x1, y1) 比例


@dataclass(frozen=True)
class ExpectStateIn:
    """點擊後重拍 + classify，狀態（或 signature 名）須命中集合。"""

    states: tuple[str, ...] = ()
    signature_names: tuple[str, ...] = ()


@dataclass(frozen=True)
class ExpectNone:
    """明確的「不驗證」哨兵（見模組 docstring 的使用限制）。"""


EXPECT_NONE = ExpectNone()


# ─────────────────────────────────────────────────────────────
# trace / frame 工具（不可 import core.states，避免循環依賴）
# ─────────────────────────────────────────────────────────────


def _record_click(ctx: "BotContext", **kw) -> None:
    recorder = getattr(ctx, "record_click", None)
    if callable(recorder):
        recorder(**kw)


def _record_ocr(ctx: "BotContext", **kw) -> None:
    recorder = getattr(ctx, "record_ocr_hit", None)
    if callable(recorder):
        recorder(**kw)


def _record_state(ctx: "BotContext", **kw) -> None:
    recorder = getattr(ctx, "record_state_transition", None)
    if callable(recorder):
        recorder(**kw)


def _can_recapture(ctx: "BotContext") -> bool:
    wm = getattr(ctx, "wm", None)
    return wm is not None and hasattr(wm, "capture")


def _refresh_frame(ctx: "BotContext") -> bool:
    if not _can_recapture(ctx):
        return False
    try:
        frame, _method = ctx.wm.capture()
    except Exception:
        return False
    ctx.last_frame = frame
    if frame is not None:
        h, w = frame.shape[:2]
        ctx.frame_w = w
        ctx.frame_h = h
    return True


def _ocr_center(bbox: tuple) -> tuple[int, int]:
    xs = [p[0] for p in bbox]
    ys = [p[1] for p in bbox]
    return int(sum(xs) / 4), int(sum(ys) / 4)


def _frame_roi_hash(frame, roi: tuple[float, float, float, float] | None) -> int | None:
    if frame is None or getattr(frame, "size", 0) == 0:
        return None
    if roi is not None:
        h, w = frame.shape[:2]
        x0, y0, x1, y1 = roi
        crop = frame[int(h * y0):int(h * y1), int(w * x0):int(w * x1)]
        if crop.size == 0:
            return None
    else:
        crop = frame
    return zlib.crc32(crop.tobytes())


def roi_hash_of(frame) -> int | None:
    """整圖 roi_hash 的語義化公開入口（None = 算不出，見 _frame_roi_hash）。

    薄封裝 _frame_roi_hash(frame, None)，零行為改變、不影響 ExpectRoiChange。
    讓 states.py adaptive settle 用清楚的公開入口判畫面是否靜止，
    而不直接呼私有 _frame_roi_hash。
    """
    return _frame_roi_hash(frame, None)


# ─────────────────────────────────────────────────────────────
# target 解析 / expect 驗證
# ─────────────────────────────────────────────────────────────


def describe_target(target) -> str:
    if isinstance(target, TextTarget):
        return "text:" + "/".join(target.keywords)
    if isinstance(target, TemplateTarget):
        return f"template:{target.name}"
    if isinstance(target, OcrPoint):
        return f"ocr_point:({target.x},{target.y}):{(target.matched_text or '')[:24]}"
    if isinstance(target, PointTarget):
        return f"point:{target.name}"
    return f"invalid:{target!r}"


def describe_expect(expect) -> str:
    if isinstance(expect, ExpectRoiChange):
        return f"roi_change:{expect.roi or 'full_frame'}"
    if isinstance(expect, ExpectStateIn):
        return f"state_in:{list(expect.states)}|sig:{list(expect.signature_names)}"
    if isinstance(expect, ExpectNone):
        return "none"
    return f"invalid:{expect!r}"


def _resolve_target(ctx: "BotContext", target) -> dict | None:
    """解析 target → {'x','y',...證據}；找不到回 None（呼叫端**不得**點擊）。"""
    if isinstance(target, TextTarget):
        ocr = getattr(ctx, "ocr", None)
        frame = getattr(ctx, "last_frame", None)
        if ocr is None or frame is None:
            return None
        try:
            results = ocr.read_text(frame, roi=target.roi)
        except Exception:
            return None
        keywords = tuple(target.keywords)
        for text, conf, bbox in results:
            if signatures.text_has_any(text, keywords):
                cx, cy = _ocr_center(bbox)
                return {
                    "x": cx,
                    "y": cy,
                    "matched_text": text,
                    "confidence": round(float(conf), 4),
                }
        return None

    if isinstance(target, TemplateTarget):
        matcher = getattr(ctx, "matcher", None)
        frame = getattr(ctx, "last_frame", None)
        if matcher is None or frame is None:
            return None
        try:
            res = matcher.match(frame, target.name)
        except KeyError:
            return None
        if not getattr(res, "found", False):
            return None
        return {
            "x": int(res.center_x),
            "y": int(res.center_y),
            "template_name": target.name,
            "confidence": round(float(getattr(res, "confidence", 0.0)), 4),
        }

    if isinstance(target, OcrPoint):
        if not (target.matched_text or "").strip():
            raise ValueError(
                "OcrPoint 需要非空 matched_text 作為證據；"
                "沒有證據的座標不是合法 click_verified target（R3）"
            )
        return {"x": int(target.x), "y": int(target.y), "matched_text": target.matched_text}

    if isinstance(target, PointTarget):
        if target.name not in SAFE_POINT_WHITELIST:
            raise ValueError(
                f"PointTarget '{target.name}' 不在白名單 {sorted(SAFE_POINT_WHITELIST)}；"
                "明確座標僅允許 tap_continue 類安全空白點（R3）"
            )
        rx, ry = SAFE_POINT_WHITELIST[target.name]
        fw = int(getattr(ctx, "frame_w", 0) or 0)
        fh = int(getattr(ctx, "frame_h", 0) or 0)
        if fw <= 0 or fh <= 0:
            return None
        return {"x": int(fw * rx), "y": int(fh * ry), "safe_point": target.name}

    raise ValueError(f"未知的 click_verified target 型別: {target!r}")


def _verify_expect(ctx: "BotContext", expect, baseline_hash: int | None, settle_delay: float):
    """點擊後驗證 expect。

    Returns:
        True  → 滿足；
        False → 不滿足（呼叫端可重試）；
        None  → 無法驗證（無 wm / 重拍失敗 / 無 ocr），降級視為成功。
    """
    if isinstance(expect, ExpectNone):
        return True

    if not _can_recapture(ctx):
        return None
    time.sleep(max(0.0, settle_delay))
    if not _refresh_frame(ctx):
        return None

    if isinstance(expect, ExpectRoiChange):
        new_hash = _frame_roi_hash(getattr(ctx, "last_frame", None), expect.roi)
        if new_hash is None:
            return None
        if baseline_hash is None:
            # 點擊前沒有可比對的基準 → 無法驗證，降級
            return None
        return new_hash != baseline_hash

    if isinstance(expect, ExpectStateIn):
        ocr = getattr(ctx, "ocr", None)
        frame = getattr(ctx, "last_frame", None)
        if ocr is None or frame is None:
            return None
        try:
            items = ocr.read_text(frame)
        except Exception:
            return None
        state, _score, sig = signatures.classify(items, frame=frame)
        if expect.signature_names:
            return sig is not None and getattr(sig, "name", None) in expect.signature_names
        return state is not None and state in expect.states

    raise ValueError(f"未知的 click_verified expect 型別: {expect!r}")


# ─────────────────────────────────────────────────────────────
# click_verified 本體
# ─────────────────────────────────────────────────────────────


def click_verified(
    ctx: "BotContext",
    target,
    *,
    expect,
    timeout: float = 3.0,
    source: str = "click_verified",
) -> bool:
    """安全點擊：看到才點、點完驗證（REPAIR_PLAN Phase 1.3，解 R3）。

    Args:
        ctx:     BotContext（或測試用 SimpleNamespace）。
        target:  TextTarget / TemplateTarget / OcrPoint / PointTarget（白名單）。
        expect:  ExpectRoiChange / ExpectStateIn / EXPECT_NONE（見模組 docstring）。
        timeout: 驗證等待總預算（秒）；每次驗證重拍前 settle min(0.9, timeout/2)。
        source:  click_trace 的 source 標籤（保留各 handler 的語意名稱）。

    Returns:
        True  → 已點擊且 expect 滿足（或明確降級 / EXPECT_NONE）。
        False → 找不到 target（零點擊）或 expect 重試一次後仍不滿足。
    """
    target_desc = describe_target(target)
    expect_desc = describe_expect(expect)

    resolved = _resolve_target(ctx, target)
    if resolved is None:
        logger.info("[click_verified] target 未命中，跳過點擊（不盲點）：%s", target_desc)
        _record_ocr(
            ctx,
            purpose="click_verified",
            matched_text="",
            result="target_not_found",
            target=target_desc,
            expect=expect_desc,
            source=source,
        )
        return False

    # 點擊後在重拍驗證前的沉澱等待。優先讀 config bot.click_settle(使用者拍板 2 秒,
    # 讓點擊觸發的動畫/轉場跑完才驗證,避免動畫中 verify 失敗→重複點擊);未設則退回
    # 舊公式 min(0.9, timeout/2)。測試 ctx 多半無 config → 走舊公式,行為不變。
    _click_settle = (getattr(ctx, "config", {}) or {}).get("bot", {}).get("click_settle", None)
    if _click_settle is not None:
        settle_delay = max(0.05, float(_click_settle))
    else:
        settle_delay = min(0.9, max(0.05, float(timeout) / 2.0))
    baseline_hash = None
    if isinstance(expect, ExpectRoiChange):
        baseline_hash = _frame_roi_hash(getattr(ctx, "last_frame", None), expect.roi)

    max_attempts = 2
    last_xy = (int(resolved["x"]), int(resolved["y"]))
    for attempt in range(1, max_attempts + 1):
        x, y = int(resolved["x"]), int(resolved["y"])
        last_xy = (x, y)
        details = {k: v for k, v in resolved.items() if k not in ("x", "y")}
        result = ctx.input.click(x, y, delay=0.05)
        success = result is not False
        _record_click(
            ctx,
            source=source,
            x=x,
            y=y,
            success=success,
            target=target_desc,
            expect=expect_desc,
            attempt=attempt,
            **details,
        )
        if not success:
            raise RuntimeError(f"input click failed: source={source}, x={x}, y={y}")
        logger.info(
            "[click_verified] %s 點擊 (%s, %s)（attempt %s/%s, target=%s）",
            source, x, y, attempt, max_attempts, target_desc,
        )

        verdict = _verify_expect(ctx, expect, baseline_hash, settle_delay)
        if verdict is True:
            return True
        if verdict is None:
            logger.info(
                "[click_verified] %s 無法重拍驗證（降級視為成功）：expect=%s",
                source, expect_desc,
            )
            _record_ocr(
                ctx,
                purpose="click_verified",
                matched_text=str(details.get("matched_text", "")),
                result="verify_skipped_unverifiable",
                target=target_desc,
                expect=expect_desc,
                source=source,
            )
            return True

        # verdict is False → expect 未滿足
        if attempt < max_attempts:
            logger.warning(
                "[click_verified] %s expect 未滿足，重新解析 target 後重試一次：%s",
                source, expect_desc,
            )
            resolved = _resolve_target(ctx, target)
            if resolved is None:
                logger.warning(
                    "[click_verified] %s 重試時 target 已消失，停止重試：%s",
                    source, target_desc,
                )
                break

    # 重試一次後仍失敗 → 記 trace（click_trace / state_trace 都看得出 verify 失敗）
    current_state = getattr(ctx, "current_state", "?")
    _record_click(
        ctx,
        source="click_verified_verify_failed",
        x=last_xy[0],
        y=last_xy[1],
        success=False,
        original_source=source,
        target=target_desc,
        expect=expect_desc,
        attempts=max_attempts,
    )
    _record_state(
        ctx,
        source="click_verified",
        previous=current_state,
        current=current_state,
        verify="failed",
        click_source=source,
        target=target_desc,
        expect=expect_desc,
    )
    logger.warning(
        "[click_verified] %s verify 失敗（重試 %s 次後放棄）：target=%s expect=%s",
        source, max_attempts - 1, target_desc, expect_desc,
    )
    return False
