"""
macOS 26.3+ Accessibility API 通知监听器

在通知数据库被权限保护时的替代方案。
通过 AXObserver 监听 UserNotificationCenter 进程的窗口创建事件，
提取通知横幅中的 app 名、标题、正文。

架构：
  主线程  → NSRunLoop（AX 事件循环，处于睡眠状态）
  C 回调  → 在主线程触发，向队列发送信号
  处理线程 → 收到信号后，用 pyobjc 读取 AX 窗口内容，调用 callback

前提：系统设置 → 隐私与安全性 → 辅助功能 → 授权终端或 Python
"""

import ctypes
import ctypes.util
import logging
import queue
import threading
import time
from typing import Callable, Optional

import AppKit
import ApplicationServices as AX
import CoreFoundation

logger = logging.getLogger(__name__)

# ── C 库句柄 ──────────────────────────────────────────────────────────────────

_ax = ctypes.CDLL(ctypes.util.find_library("ApplicationServices"))
_cf = ctypes.CDLL(ctypes.util.find_library("CoreFoundation"))

# ── C 函数签名声明 ─────────────────────────────────────────────────────────────

# AXObserver callback: (observer, element, notification, userInfo, refcon) -> void
_AXCallbackType = ctypes.CFUNCTYPE(
    None,
    ctypes.c_void_p,  # AXObserverRef
    ctypes.c_void_p,  # AXUIElementRef (app element)
    ctypes.c_void_p,  # CFStringRef notification name
    ctypes.c_void_p,  # CFDictionaryRef userInfo
    ctypes.c_void_p,  # void* refcon
)

_ax.AXObserverCreateWithInfoCallback.restype = ctypes.c_int
_ax.AXObserverCreateWithInfoCallback.argtypes = [
    ctypes.c_int, _AXCallbackType, ctypes.POINTER(ctypes.c_void_p)
]

_ax.AXObserverAddNotification.restype = ctypes.c_int
_ax.AXObserverAddNotification.argtypes = [
    ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p
]

_ax.AXObserverGetRunLoopSource.restype = ctypes.c_void_p
_ax.AXObserverGetRunLoopSource.argtypes = [ctypes.c_void_p]

_ax.AXUIElementCreateApplication.restype = ctypes.c_void_p
_ax.AXUIElementCreateApplication.argtypes = [ctypes.c_int]

_cf.CFStringCreateWithCString.restype = ctypes.c_void_p
_cf.CFStringCreateWithCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
_CF_ENCODING_UTF8 = 0x08000100

# ── AX 属性读取（pyobjc，在处理线程中使用）─────────────────────────────────────

def _ax_get(element, attr: str):
    err, value = AX.AXUIElementCopyAttributeValue(element, attr, None)
    return value if err == 0 else None


def _extract_texts(element, depth: int = 0, seen: set | None = None) -> list[str]:
    """递归提取 AX 元素树的所有文本，去重去空。"""
    if seen is None:
        seen = set()
    if depth > 6:
        return []
    texts = []
    for attr in ("AXValue", "AXTitle", "AXDescription"):
        val = _ax_get(element, attr)
        if val and isinstance(val, str):
            val = val.strip()
            if val and val not in seen and len(val) < 300:
                seen.add(val)
                texts.append(val)
    children = _ax_get(element, "AXChildren")
    if children:
        for child in children:
            texts.extend(_extract_texts(child, depth + 1, seen))
    return texts


_BUTTON_LABELS = frozenset(("关闭", "Close", "Options", "选项", "Reply", "回复", "查看", "View"))

def _parse_banner(window) -> Optional[dict]:
    """
    从通知横幅的 AX 窗口树解析内容。
    通知横幅文字顺序通常：App 名 → 标题/发件人 → 正文。
    """
    texts = [t for t in _extract_texts(window) if t not in _BUTTON_LABELS]
    if not texts:
        return None
    return {
        "app":      texts[0] if len(texts) > 0 else "",
        "title":    texts[1] if len(texts) > 1 else texts[0],
        "body":     texts[2] if len(texts) > 2 else "",
        "subtitle": "",
    }

# ── 主类 ──────────────────────────────────────────────────────────────────────

class AccessibilityWatcher:
    """
    事件驱动的通知监听器（macOS 26.3+）。

    callback 签名：callback(app: str, title: str, body: str, subtitle: str)
    """

    _BANNER_PROCESSES = ("UserNotificationCenter", "NotificationCenter")

    def __init__(self, callback: Callable[[str, str, str, str], None]):
        self.callback = callback
        self._evt_queue: queue.Queue = queue.Queue()
        self._observer_ptr: ctypes.c_void_p | None = None
        self._ax_callback_ref = None  # 防止 GC 回收 ctypes 回调

    # ── 工具方法 ──────────────────────────────────────────────────────────────

    def _find_banner_pid(self) -> Optional[int]:
        for name in self._BANNER_PROCESSES:
            for app in AppKit.NSWorkspace.sharedWorkspace().runningApplications():
                if name in (app.localizedName() or ""):
                    return int(app.processIdentifier())
        return None

    # ── C 回调（主线程，仅发信号，不做耗时操作）──────────────────────────────

    def _make_c_callback(self):
        evt_queue = self._evt_queue

        def _cb(observer, element, notification, user_info, refcon):
            evt_queue.put_nowait(time.monotonic())

        return _AXCallbackType(_cb)

    # ── 处理线程（pyobjc 读取 AX 窗口）──────────────────────────────────────

    def _process_loop(self) -> None:
        while True:
            self._evt_queue.get()
            # 等待窗口渲染完成
            time.sleep(0.15)

            pid = self._find_banner_pid()
            if not pid:
                continue

            try:
                app_elem = AX.AXUIElementCreateApplication(pid)
                err, windows = AX.AXUIElementCopyAttributeValue(app_elem, "AXWindows", None)
                if err != 0 or not windows:
                    continue
                for window in windows:
                    info = _parse_banner(window)
                    if info:
                        logger.debug("AX 通知: %s", info)
                        self.callback(
                            info["app"],
                            info["title"],
                            info["body"],
                            info["subtitle"],
                        )
            except Exception as e:
                logger.error("AX 读取异常: %s", e)

    # ── 权限检查 ──────────────────────────────────────────────────────────────

    def check_permission(self) -> bool:
        return bool(AX.AXIsProcessTrustedWithOptions({"AXTrustedCheckOptionPrompt": False}))

    def request_permission(self) -> None:
        AX.AXIsProcessTrustedWithOptions({"AXTrustedCheckOptionPrompt": True})

    # ── 初始化 AXObserver ─────────────────────────────────────────────────────

    def setup(self) -> bool:
        if not self.check_permission():
            logger.warning(
                "⚠️  未授予辅助功能权限\n"
                "请前往：系统设置 → 隐私与安全性 → 辅助功能 → 开启终端（或 Python）"
            )
            self.request_permission()
            return False

        pid = self._find_banner_pid()
        if not pid:
            logger.error("找不到通知横幅进程（UserNotificationCenter）")
            return False

        logger.info("绑定 AXObserver → 进程 PID=%d", pid)

        # 创建 C 回调并防止 GC 回收
        cb = self._make_c_callback()
        self._ax_callback_ref = cb

        # 创建 observer
        obs = ctypes.c_void_p()
        err = _ax.AXObserverCreateWithInfoCallback(pid, cb, ctypes.byref(obs))
        if err != 0:
            logger.error("AXObserverCreate 失败 err=%d", err)
            return False

        # 创建 CFString "AXWindowCreated"
        notif_cfstr = _cf.CFStringCreateWithCString(
            None, b"AXWindowCreated", _CF_ENCODING_UTF8
        )
        # 创建 app AX element
        app_elem_ptr = _ax.AXUIElementCreateApplication(pid)

        err2 = _ax.AXObserverAddNotification(obs.value, app_elem_ptr, notif_cfstr, None)
        if err2 != 0:
            logger.warning("AXObserverAddNotification err=%d（可能仍可用）", err2)

        # 注册到当前线程 RunLoop
        src = _ax.AXObserverGetRunLoopSource(obs.value)
        CoreFoundation.CFRunLoopAddSource(
            CoreFoundation.CFRunLoopGetCurrent(),
            src,
            CoreFoundation.kCFRunLoopDefaultMode,
        )

        self._observer_ptr = obs
        logger.info("✅ AXObserver 就绪，等待通知横幅...")
        return True

    def run_forever(self) -> None:
        """启动处理线程 + 运行 RunLoop（阻塞，进程休眠直到有 AX 事件）。"""
        worker = threading.Thread(target=self._process_loop, daemon=True)
        worker.start()
        AppKit.NSRunLoop.currentRunLoop().run()
