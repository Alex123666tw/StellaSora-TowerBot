"""
視窗管理員 (WindowManager)
負責取得遊戲視窗句柄 (HWND) 以及實作背景截圖功能。
"""
import ctypes
import win32gui
import win32ui
import win32con
from ctypes import windll
import cv2
import numpy as np
from PIL import Image
import mss

# 宣告程序支援 Per-Monitor DPI 模式，強制 win32 API 回傳實體像素座標
# 避免 Windows DPI 縮放 (125%/150% 等) 造成截圖尺寸偏小
_dpi_set = ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE

class WindowManager:
    """
    提供取得特定名稱視窗之控制代碼，並使用 PrintWindow 擷取畫面的類別。
    """
    def __init__(self, window_name: str = "StellaSora"):
        self.window_name = window_name
        self.hwnd = None

    def find_window(self) -> int:
        """
        尋找遊戲視窗控制代碼 (HWND)。
        
        Returns:
            int: 視窗控制代碼。
            
        Raises:
            Exception: 若找不到視窗則拋出例外。
        """
        self.hwnd = win32gui.FindWindow(None, self.window_name)
        if not self.hwnd:
            raise Exception(f"找不到名稱為 '{self.window_name}' 的視窗，請確認遊戲是否已啟動。")
        return self.hwnd

    def focus_window(self) -> int:
        """
        將目標遊戲視窗還原並帶到前景。

        Returns:
            int: 視窗控制代碼。
        """
        import time
        import win32api
        import win32process

        if not self.hwnd:
            self.find_window()

        placement = win32gui.GetWindowPlacement(self.hwnd)
        if placement[1] == win32con.SW_SHOWMINIMIZED:
            win32gui.ShowWindow(self.hwnd, win32con.SW_RESTORE)
            time.sleep(0.2)

        foreground_hwnd = win32gui.GetForegroundWindow()
        current_thread_id = win32api.GetCurrentThreadId()
        foreground_thread_id = 0
        if foreground_hwnd:
            foreground_thread_id, _ = win32process.GetWindowThreadProcessId(foreground_hwnd)

        attached = False
        try:
            if foreground_thread_id and foreground_thread_id != current_thread_id:
                win32process.AttachThreadInput(current_thread_id, foreground_thread_id, True)
                attached = True
            win32gui.ShowWindow(self.hwnd, win32con.SW_RESTORE)
            win32gui.BringWindowToTop(self.hwnd)
            win32gui.SetForegroundWindow(self.hwnd)
            win32gui.SetActiveWindow(self.hwnd)
        finally:
            if attached:
                win32process.AttachThreadInput(current_thread_id, foreground_thread_id, False)

        return self.hwnd

    def capture_background(self) -> np.ndarray:
        """
        擷取背景視窗畫面 (支援被遮擋時的截圖)。
        
        使用 PrintWindow API 進行截圖，若部分引擎不支援，也可改用 BitBlt。
        本實作採 `PW_RENDERFULLCONTENT (2)` 來擷取可能包含硬體加速的畫面。
        
        Returns:
            np.ndarray: OpenCV 格式的圖片矩陣 (BGR)。
            
        Raises:
            Exception: 截圖失敗時拋出。
        """
        if not self.hwnd:
            self.find_window()

        # 取得視窗客戶區 (Client Area) 實際大小
        left, top, right, bottom = win32gui.GetClientRect(self.hwnd)
        width = right - left
        height = bottom - top

        # 最小化時 GetClientRect 會是 0，改抓原始視窗大小
        if width == 0 or height == 0:
            placement = win32gui.GetWindowPlacement(self.hwnd)
            _, _, _, _, rcNormalPosition = placement
            width = rcNormalPosition[2] - rcNormalPosition[0]
            height = rcNormalPosition[3] - rcNormalPosition[1]
            if width == 0 or height == 0:
                raise Exception("無法取得視窗尺寸，視窗可能無效。")

        # 獲取 Device Contexts
        hwndDC = win32gui.GetWindowDC(self.hwnd)
        mfcDC  = win32ui.CreateDCFromHandle(hwndDC)
        saveDC = mfcDC.CreateCompatibleDC()

        # 建立 Bitmap，並選入 DC
        saveBitMap = win32ui.CreateBitmap()
        saveBitMap.CreateCompatibleBitmap(mfcDC, width, height)
        saveDC.SelectObject(saveBitMap)

        try:
            # 呼叫 PrintWindow 擷取畫面，參數 3 代表 PW_CLIENTONLY | PW_RENDERFULLCONTENT
            # 注意：如果只給 2，截圖會包含標題列，導致 Y 軸向下偏移（OCR 發現的 Y 座標加上了標題列高度）
            result = windll.user32.PrintWindow(self.hwnd, saveDC.GetSafeHdc(), 3)
            if result != 1:
                #  fallback 給沒有 DWM 的老系統
                result = windll.user32.PrintWindow(self.hwnd, saveDC.GetSafeHdc(), 1)
                if result != 1:
                    raise Exception("PrintWindow 回傳失敗 (result != 1)")

            # 使用 ctypes GetDIBits 取像素（比 GetBitmapBits 對硬體加速視窗更相容）
            import ctypes
            hdc      = saveDC.GetSafeHdc()
            hbitmap  = saveBitMap.GetHandle()

            # 建立 BITMAPINFOHEADER
            class BITMAPINFOHEADER(ctypes.Structure):
                _fields_ = [
                    ("biSize",          ctypes.c_uint32),
                    ("biWidth",         ctypes.c_int32),
                    ("biHeight",        ctypes.c_int32),
                    ("biPlanes",        ctypes.c_uint16),
                    ("biBitCount",      ctypes.c_uint16),
                    ("biCompression",   ctypes.c_uint32),
                    ("biSizeImage",     ctypes.c_uint32),
                    ("biXPelsPerMeter", ctypes.c_int32),
                    ("biYPelsPerMeter", ctypes.c_int32),
                    ("biClrUsed",       ctypes.c_uint32),
                    ("biClrImportant",  ctypes.c_uint32),
                ]

            bmi = BITMAPINFOHEADER()
            bmi.biSize      = ctypes.sizeof(BITMAPINFOHEADER)
            bmi.biWidth     = width
            bmi.biHeight    = -height  # 負值 = top-down bitmap
            bmi.biPlanes    = 1
            bmi.biBitCount  = 32
            bmi.biCompression = 0  # BI_RGB

            buf_len = width * height * 4
            buf = (ctypes.c_byte * buf_len)()
            ret_bits = ctypes.windll.gdi32.GetDIBits(
                hdc, hbitmap, 0, height,
                buf, ctypes.byref(bmi), 0  # DIB_RGB_COLORS
            )
            if ret_bits == 0:
                raise Exception("GetDIBits 呼叫失敗，回傳 0。")

            # 轉為 numpy，BGRA -> BGR
            img_arr = np.frombuffer(buf, dtype=np.uint8).reshape((height, width, 4))
            return cv2.cvtColor(img_arr, cv2.COLOR_BGRA2BGR)

        finally:
            # 無論成功或失敗均釋放 GDI 資源，防止 memory leak
            win32gui.DeleteObject(saveBitMap.GetHandle())
            saveDC.DeleteDC()
            mfcDC.DeleteDC()
            win32gui.ReleaseDC(self.hwnd, hwndDC)


    def capture_foreground(self) -> np.ndarray:
        """
        使用 mss 擷取視窗在螢幕上的畫面 (前台截圖降級方案)。
        
        截圖前會先將遊戲視窗帶至最前景，截圖完成後還原原焦點視窗，
        確保不會因他視窗遮擋而擷取到錯誤畫面。
        
        Returns:
            np.ndarray: OpenCV 格式的圖片矩陣 (BGR)。
        """
        import time
        import win32process
        import win32api
        
        if not self.hwnd:
            self.find_window()

        # 記錄當前前景視窗，截圖後還原
        prev_hwnd = win32gui.GetForegroundWindow()

        # 若視窗被最小化，先還原
        placement = win32gui.GetWindowPlacement(self.hwnd)
        if placement[1] == win32con.SW_SHOWMINIMIZED:
            win32gui.ShowWindow(self.hwnd, win32con.SW_RESTORE)
            time.sleep(0.3)  # 等待視窗還原

        # 透過 AttachThreadInput 強制奪取焦點 (Bypass Foreground Lock)
        current_thread_id = win32api.GetCurrentThreadId()
        foreground_hwnd = win32gui.GetForegroundWindow()
        
        if foreground_hwnd and foreground_hwnd != self.hwnd:
            foreground_thread_id, _ = win32process.GetWindowThreadProcessId(foreground_hwnd)
            try:
                # 附加執行緒輸入
                if current_thread_id != foreground_thread_id:
                    win32process.AttachThreadInput(current_thread_id, foreground_thread_id, True)
                
                win32gui.SetForegroundWindow(self.hwnd)
                win32gui.BringWindowToTop(self.hwnd)
                
            except Exception as e:
                pass # 盡力而為
            finally:
                # 解除附加
                if current_thread_id != foreground_thread_id:
                    win32process.AttachThreadInput(current_thread_id, foreground_thread_id, False)
        else:
            try:
                win32gui.SetForegroundWindow(self.hwnd)
            except:
                pass
            
        time.sleep(0.5)

        # 取得「純客戶區域 (Client Area)」在螢幕上的絕對實體座標，排除標題列與邊框干擾
        c_left, c_top, c_right, c_bottom = win32gui.GetClientRect(self.hwnd)
        width = c_right - c_left
        height = c_bottom - c_top
        
        # 轉成螢幕絕對座標
        cl_pt = win32gui.ClientToScreen(self.hwnd, (0, 0))
        cr_pt = win32gui.ClientToScreen(self.hwnd, (width, height))
        
        abs_left, abs_top = cl_pt[0], cl_pt[1]
        abs_right, abs_bottom = cr_pt[0], cr_pt[1]

        # 取得主螢幕解析度，將截圖範圍限制在螢幕邊界內
        with mss.mss() as sct:
            primary = sct.monitors[1]  # monitors[0] 為全虛擬桌面，[1] 為主螢幕
            screen_right  = primary["left"] + primary["width"]
            screen_bottom = primary["top"]  + primary["height"]
            
        clamped_left   = max(abs_left, primary["left"])
        clamped_top    = max(abs_top, primary["top"])
        clamped_right  = min(abs_right, screen_right)
        clamped_bottom = min(abs_bottom, screen_bottom)

        monitor = {
            "left":   clamped_left,
            "top":    clamped_top,
            "width":  clamped_right  - clamped_left,
            "height": clamped_bottom - clamped_top,
        }

        if monitor["width"] <= 0 or monitor["height"] <= 0:
            raise ValueError(f"視窗尺寸異常或完全在螢幕外: {monitor}")

        with mss.mss() as sct:
            try:
                screenshot = sct.grab(monitor)
            except Exception as e:
                raise Exception(f"MSS grab 失敗, 參數: {monitor}, 原始錯誤: {e}")
            img = np.array(screenshot)

        # 截圖完成後嘗試還原原焦點視窗
        try:
            if prev_hwnd and win32gui.IsWindow(prev_hwnd) and prev_hwnd != self.hwnd:
                current_tid = win32api.GetCurrentThreadId()
                fg_tid, _ = win32process.GetWindowThreadProcessId(win32gui.GetForegroundWindow()) if win32gui.GetForegroundWindow() else (0, 0)
                attached = False
                if fg_tid and fg_tid != current_tid:
                    win32process.AttachThreadInput(current_tid, fg_tid, True)
                    attached = True
                win32gui.SetForegroundWindow(prev_hwnd)
                if attached:
                    win32process.AttachThreadInput(current_tid, fg_tid, False)
        except Exception:
            pass  # 還原失敗不影響截圖結果

        return cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)


    def capture_dxcam(self) -> np.ndarray:
        """
        使用 dxcam (Desktop Duplication API) 擷取畫面。
        這是最強的硬體級別截圖，可無視遊戲引擎的獨佔全螢幕限制。
        """
        if not hasattr(self, '_dxcam_camera'):
            import dxcam
            self._dxcam_camera = dxcam.create(output_color="BGR")

        if not self.hwnd:
            self.find_window()

        import win32gui
        
        # 取得「純客戶區域 (Client Area)」在螢幕上的絕對實體座標，排除標題列與邊框干擾
        c_left, c_top, c_right, c_bottom = win32gui.GetClientRect(self.hwnd)
        width = c_right - c_left
        height = c_bottom - c_top
        
        # 轉成螢幕絕對座標
        cl_pt = win32gui.ClientToScreen(self.hwnd, (0, 0))
        cr_pt = win32gui.ClientToScreen(self.hwnd, (width, height))
        
        if width <= 0 or height <= 0:
            raise ValueError(f"視窗尺寸完全失效: ClientRect=({c_left},{c_top},{c_right},{c_bottom})")
            
        # dxcam grab takes a tuple (left, top, right, bottom)
        region = (
            int(cl_pt[0]), 
            int(cl_pt[1]), 
            int(cr_pt[0]), 
            int(cr_pt[1])
        )
        
        # Bring window to foreground simply
        try:
            win32gui.SetForegroundWindow(self.hwnd)
        except:
            pass
            
        frame = self._dxcam_camera.grab(region=region)
        if frame is None:
            raise Exception("DXcam grab 回傳 None (可能無新畫面或被阻擋)")
            
        return np.array(frame)

    # 空白判定門檻：std-dev 低於此值視為「全黑/單一色/無內容」。
    #
    # 背景:PrintWindow 對最小化/獨佔全螢幕/未渲染的視窗會回傳 result==1(成功)
    # 但內容全黑的 buffer(實機 session 20260613_144354 即此情況,last_frame.png
    # 為 1920x1080 純黑圖)。此時必須視同失敗、降級到下一個截圖方案。
    #
    # 門檻取值理由(實測):
    #   - 那張實機黑圖:max==0, std==0.000
    #   - 全部 38 張實機合法截圖中「最暗/最低變異」者:std==47.497
    #     (一般遊戲畫面 std≈50~79)
    # 真實遊戲 frame 即使偏暗也有大量紋理/變異,std 遠高於 3.0。
    # 取 3.0 在「黑圖 0.0」與「最暗合法圖 47.5」之間留下約 15 倍安全邊際,
    # 既能抓到全黑/近乎單一色的空白 frame,又不會把合法但偏暗的遊戲畫面誤判成空白。
    _BLANK_STD_EPS = 3.0

    def _is_blank_frame(self, img) -> bool:
        """
        判定 frame 是否為「全黑/近乎單一色(無內容)」的空白畫面。

        Returns:
            bool: 空白(無內容)為 True,有實際畫面內容為 False。
        """
        # None 或空陣列(size 0)→ 視為空白
        if img is None:
            return True
        if getattr(img, "size", 0) == 0:
            return True

        # 純黑:最大像素值為 0
        if int(img.max()) == 0:
            return True

        # 近乎單一色/無內容:整張圖變異極低(門檻保守,見 _BLANK_STD_EPS 說明)
        if float(img.std()) < self._BLANK_STD_EPS:
            return True

        return False

    def capture(self) -> tuple:
        """
        自動選擇截圖方案：優先嘗試背景截圖，接著 mss，最後 DXcam。

        每個方法回傳後會以 `_is_blank_frame` 檢查內容；若為全黑/空白(例如
        視窗最小化時 PrintWindow 回成功但全黑),視同失敗並降級到下一個方法,
        而非直接 return 一張空白 frame 讓 OCR 讀空、狀態誤判 UNKNOWN。

        Returns:
            tuple: (圖片矩陣 BGR, 使用方法說明字串)
        """
        import logging as _log
        _logger = _log.getLogger(__name__)

        # 嘗試背景截圖（PrintWindow + GetDIBits）
        try:
            img = self.capture_background()
            if self._is_blank_frame(img):
                _logger.debug("[WindowMgr] 背景截圖回傳空白/全黑 frame，視同失敗、降級。")
            else:
                return img, "背景截圖 (PrintWindow)"
        except BaseException as e:
            _logger.debug(f"[WindowMgr] 背景截圖失敗（{e}）。")

        # 降級：mss 前台截圖
        try:
            img = self.capture_foreground()
            if self._is_blank_frame(img):
                _logger.debug("[WindowMgr] mss 截圖回傳空白/全黑 frame，視同失敗、降級。")
            else:
                return img, "前台截圖 (mss 降級)"
        except BaseException as e:
            _logger.debug(f"[WindowMgr] mss 截圖失敗（{e}）。")

        # 終極方案：DXcam 硬體截圖
        try:
            img = self.capture_dxcam()
            if self._is_blank_frame(img):
                _logger.debug("[WindowMgr] DXcam 截圖回傳空白/全黑 frame，視同失敗。")
            else:
                return img, "終極前台截圖 (DXcam 硬體級)"
        except BaseException as e:
            _logger.debug(f"[WindowMgr] DXcam 截圖失敗（{e}）。")

        # 三段方案全部回傳空白或全部拋例外
        raise Exception(
            "所有擷取方案皆回傳空白/全黑(視窗可能最小化未還原、獨佔全螢幕、或未在渲染)。"
        )

