"""
astrbot_plugin_napcat_screenshot
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

NapCat 智能截图插件 — LLM 自主决定何时截图、截哪个窗口，
像真人一样自然地展示进度。

核心设计：
  1. on_llm_request 阶段注入截图能力提示词，告诉 LLM 它可以截图
  2. LLM 在回复中自然使用 [SCREENSHOT:目标窗口] 标记来请求截图
  3. on_decorating_result 阶段检测标记 → 执行截图 → 发送图片
  4. 支持窗口名模糊匹配（如"Claude Code""VS Code""终端"等）
  5. 多重截图后端：Win32 API 窗口捕获 > NapCat API > PIL 全屏

标记格式：
  [SCREENSHOT:Claude Code]   — 截图包含"Claude Code"的窗口
  [SCREENSHOT:VS Code]       — 截图 VS Code 窗口
  [SCREENSHOT:终端]          — 截图终端窗口
  [SCREENSHOT]               — 默认截图（优先活动窗口）
"""

from __future__ import annotations

import asyncio
import base64
import ctypes
import ctypes.wintypes
import io
import re
import struct
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

# LLM 截图标记正则 — 兼容中英文冒号，支持各种空格，支持指定截图区域
SCREENSHOT_MARKER_RE = re.compile(
    r'\[SCREENSHOT(?:[:：]\s*([^\]\|]*?))?(?:\|\s*([^\]]*?))?\s*\]',
    re.IGNORECASE
)
# 中文自然语言截图意图检测
SCREENSHOT_INTENT_RE = re.compile(
    r'(截图|截个图|屏幕截图|screenshot|看看.{0,4}(进度|状态|情况|界面|窗口|桌面|屏幕))',
    re.IGNORECASE
)

# ---------------------------------------------------------------------------
# 注入 LLM 的系统提示词 — 告诉 LLM 它拥有截图能力
# ---------------------------------------------------------------------------

SCREENSHOT_SYSTEM_PROMPT = """## 截图工具

每次截图**必须指定窗口名**，格式：`[SCREENSHOT:关键词]` 或 `[SCREENSHOT:关键词|x,y,宽度,高度]`

规则（严格遵循）：
- 用户问什么软件/窗口就截什么：Claude Code→[SCREENSHOT:Claude Code]，VS Code→[SCREENSHOT:VS Code]，终端→[SCREENSHOT:终端]，浏览器→[SCREENSHOT:Chrome]
- 如果你需要截取特定区域，可以加上位置和大小（相对于目标窗口的左上角，或全屏时的屏幕左上角），例如：`[SCREENSHOT:VS Code|100,100,800,600]`
- 从用户消息中提取窗口关键词，不要自己编
- 用户没说具体软件时，猜最可能的：写代码→VS Code或Claude Code，上网→浏览器，文件→资源管理器
- **禁止**用 [SCREENSHOT] 不写窗口名，除非用户明确说"全屏""整个屏幕""桌面"

示例——
用户："Claude Code开发到哪了" → `[SCREENSHOT:Claude Code]`
用户："看看VS Code有没有报错" → `[SCREENSHOT:VS Code]`
用户："截个全屏" → `[SCREENSHOT]`
用户："截取浏览器右上角的区域" → `[SCREENSHOT:Chrome|800,0,400,300]`

每次回复最多一个截图标记。"""

# ---------------------------------------------------------------------------
# Win32 API 常量与结构体
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Win32 API 类型定义（必须在函数签名之前）
# ---------------------------------------------------------------------------

# Window enumeration callback type
EnumWindowsProc = ctypes.WINFUNCTYPE(
    ctypes.c_bool, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM
)
# Keep a reference to prevent GC of the callback during EnumWindows
_enum_callback_ref = None

# GDI / PrintWindow constants
SRCCOPY = 0x00CC0020
CAPTUREBLT = 0x40000000
PW_CLIENTONLY = 0x00000001       # PrintWindow: only client area
PW_RENDERFULLCONTENT = 0x00000002  # PrintWindow: render full content (Win 8.1+)


class RECT(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", ctypes.wintypes.DWORD),
        ("biWidth", ctypes.c_long),
        ("biHeight", ctypes.c_long),
        ("biPlanes", ctypes.wintypes.WORD),
        ("biBitCount", ctypes.wintypes.WORD),
        ("biCompression", ctypes.wintypes.DWORD),
        ("biSizeImage", ctypes.wintypes.DWORD),
        ("biXPelsPerMeter", ctypes.c_long),
        ("biYPelsPerMeter", ctypes.c_long),
        ("biClrUsed", ctypes.wintypes.DWORD),
        ("biClrImportant", ctypes.wintypes.DWORD),
    ]


# ---------------------------------------------------------------------------
# Win32 API 函数声明与签名
# ---------------------------------------------------------------------------

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
kernel32 = ctypes.windll.kernel32

# -- user32.dll --

user32.PrintWindow.argtypes = [
    ctypes.wintypes.HWND, ctypes.wintypes.HDC, ctypes.wintypes.UINT
]
user32.PrintWindow.restype = ctypes.wintypes.BOOL

user32.GetWindowRect.argtypes = [
    ctypes.wintypes.HWND, ctypes.POINTER(RECT)
]
user32.GetWindowRect.restype = ctypes.wintypes.BOOL

user32.IsWindowVisible.argtypes = [ctypes.wintypes.HWND]
user32.IsWindowVisible.restype = ctypes.wintypes.BOOL

user32.IsIconic.argtypes = [ctypes.wintypes.HWND]
user32.IsIconic.restype = ctypes.wintypes.BOOL

user32.SetForegroundWindow.argtypes = [ctypes.wintypes.HWND]
user32.SetForegroundWindow.restype = ctypes.wintypes.BOOL

user32.BringWindowToTop.argtypes = [ctypes.wintypes.HWND]
user32.BringWindowToTop.restype = ctypes.wintypes.BOOL

user32.ShowWindow.argtypes = [ctypes.wintypes.HWND, ctypes.c_int]
user32.ShowWindow.restype = ctypes.wintypes.BOOL

user32.EnumWindows.argtypes = [EnumWindowsProc, ctypes.wintypes.LPARAM]
user32.EnumWindows.restype = ctypes.wintypes.BOOL

# Foreground window management
user32.GetForegroundWindow.argtypes = []
user32.GetForegroundWindow.restype = ctypes.wintypes.HWND

user32.GetWindowThreadProcessId.argtypes = [
    ctypes.wintypes.HWND, ctypes.POINTER(ctypes.wintypes.DWORD)
]
user32.GetWindowThreadProcessId.restype = ctypes.wintypes.DWORD

kernel32.GetCurrentThreadId.argtypes = []
kernel32.GetCurrentThreadId.restype = ctypes.wintypes.DWORD

user32.AttachThreadInput.argtypes = [
    ctypes.wintypes.DWORD, ctypes.wintypes.DWORD, ctypes.wintypes.BOOL
]
user32.AttachThreadInput.restype = ctypes.wintypes.BOOL

# SwitchToThisWindow (undocumented but works on all Windows versions)
user32.SwitchToThisWindow.argtypes = [ctypes.wintypes.HWND, ctypes.wintypes.BOOL]
user32.SwitchToThisWindow.restype = None

# AllowSetForegroundWindow: BOOL AllowSetForegroundWindow(DWORD dwProcessId)
# ASFW_ANY = 0xFFFFFFFF = allow any process to set foreground
user32.AllowSetForegroundWindow.argtypes = [ctypes.wintypes.DWORD]
user32.AllowSetForegroundWindow.restype = ctypes.wintypes.BOOL

user32.GetDC.argtypes = [ctypes.wintypes.HWND]
user32.GetDC.restype = ctypes.wintypes.HDC

user32.ReleaseDC.argtypes = [ctypes.wintypes.HWND, ctypes.wintypes.HDC]
user32.ReleaseDC.restype = ctypes.c_int

user32.GetSystemMetrics.argtypes = [ctypes.c_int]
user32.GetSystemMetrics.restype = ctypes.c_int

# keybd_event for foreground window permission
user32.keybd_event.argtypes = [
    ctypes.wintypes.BYTE, ctypes.wintypes.BYTE, ctypes.wintypes.DWORD, ctypes.c_void_p
]
user32.keybd_event.restype = None

# -- gdi32.dll --

gdi32.CreateCompatibleDC.argtypes = [ctypes.wintypes.HDC]
gdi32.CreateCompatibleDC.restype = ctypes.wintypes.HDC

gdi32.CreateCompatibleBitmap.argtypes = [
    ctypes.wintypes.HDC, ctypes.c_int, ctypes.c_int
]
gdi32.CreateCompatibleBitmap.restype = ctypes.wintypes.HBITMAP

gdi32.SelectObject.argtypes = [ctypes.wintypes.HDC, ctypes.wintypes.HGDIOBJ]
gdi32.SelectObject.restype = ctypes.wintypes.HGDIOBJ

gdi32.DeleteObject.argtypes = [ctypes.wintypes.HGDIOBJ]
gdi32.DeleteObject.restype = ctypes.wintypes.BOOL

gdi32.DeleteDC.argtypes = [ctypes.wintypes.HDC]
gdi32.DeleteDC.restype = ctypes.wintypes.BOOL

gdi32.BitBlt.argtypes = [
    ctypes.wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    ctypes.wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.wintypes.DWORD,
]
gdi32.BitBlt.restype = ctypes.wintypes.BOOL

gdi32.GetDIBits.argtypes = [
    ctypes.wintypes.HDC, ctypes.wintypes.HBITMAP, ctypes.wintypes.UINT,
    ctypes.wintypes.UINT, ctypes.c_void_p, ctypes.POINTER(BITMAPINFOHEADER),
    ctypes.wintypes.UINT,
]
gdi32.GetDIBits.restype = ctypes.c_int


# Try importing PIL (Pillow) — required for screenshots
try:
    from PIL import Image, ImageGrab
    HAS_PIL = True
except ImportError:
    HAS_PIL = False
    logger.warning(
        "[NapcatScreenshot] ⚠ Pillow 未安装！截图功能将不可用。\n"
        "    请执行: pip install Pillow"
    )

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

@dataclass
class PluginConfig:
    """插件配置"""
    enable: bool = True
    inject_system_prompt: bool = True
    screenshot_mode: str = "window_first"  # napcat_first | window_first | fullscreen | napcat_only
    cooldown: int = 5
    max_screenshots_per_session: int = 3
    send_as_separate_message: bool = True
    auto_delete_after: int = 0  # 秒，0=不自动删除
    napcat_http_url: str = "http://localhost:6099"
    napcat_token: str = ""
    screenshot_delay_ms: int = 300
    image_quality: int = 85
    max_image_width: int = 1920
    flip_correction: str = "none"  # none | v | h | both — 截图朝向修正
    allowed_users: str = ""  # 逗号分隔的QQ号列表

    @classmethod
    def from_context(cls, context: Context) -> "PluginConfig":
        try:
            cfg = getattr(context, "_config", None)
        except Exception:
            cfg = None
        if not isinstance(cfg, dict) or not cfg:
            return cls()
        return cls(
            enable=bool(cfg.get("enable", True)),
            inject_system_prompt=bool(cfg.get("inject_system_prompt", True)),
            screenshot_mode=str(cfg.get("screenshot_mode", "window_first")),
            cooldown=_clamp(int(cfg.get("cooldown", 5)), 0, 120),
            max_screenshots_per_session=_clamp(int(cfg.get("max_screenshots_per_session", 3)), 0, 20),
            send_as_separate_message=bool(cfg.get("send_as_separate_message", True)),
            auto_delete_after=_clamp(int(cfg.get("auto_delete_after", 0)), 0, 300),
            napcat_http_url=str(cfg.get("napcat_http_url", "http://localhost:6099")).rstrip("/"),
            napcat_token=str(cfg.get("napcat_token", "")),
            screenshot_delay_ms=_clamp(int(cfg.get("screenshot_delay_ms", 300)), 0, 2000),
            image_quality=_clamp(int(cfg.get("image_quality", 85)), 10, 100),
            max_image_width=_clamp(int(cfg.get("max_image_width", 1920)), 0, 7680),
            flip_correction=str(cfg.get("flip_correction", "none")),
            allowed_users=str(cfg.get("allowed_users", "")),
        )


def _clamp(val: int, lo: int, hi: int) -> int:
    return max(lo, min(val, hi))


# ---------------------------------------------------------------------------
# Win32 窗口截图引擎 — 无需额外依赖，直接调用 Windows API
# ---------------------------------------------------------------------------

class Win32ScreenCapture:
    """
    Windows 原生截图引擎。

    功能：
      - 根据窗口标题模糊搜索窗口
      - 将目标窗口置于前台后截取该窗口区域
      - 不指定窗口时截取整个屏幕
    """

    @staticmethod
    def find_windows_by_title(title_keyword: str) -> List[Tuple[int, str]]:
        """
        根据标题关键词查找可见窗口。
        返回 [(hwnd, window_title), ...]，按匹配度降序排列。

        匹配策略：完全匹配 > 前缀匹配 > 包含匹配 > 分词匹配。
        """
        if not title_keyword:
            return []

        global _enum_callback_ref
        results: List[Tuple[int, str, int]] = []  # (hwnd, title, score)
        keyword_lower = title_keyword.lower()

        def enum_callback(hwnd: int, _lparam: int) -> bool:
            if not user32.IsWindowVisible(hwnd):
                return True

            length = user32.GetWindowTextLengthW(hwnd)
            if length == 0:
                return True

            buffer = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buffer, length + 1)
            title = buffer.value
            if not title:
                return True

            title_lower = title.lower()

            # 跳过无意义的系统窗口
            skip_titles = {
                "", "program manager", "microsoft text input application",
                "windows input experience", "msctfmonitor",
                "task switching", "task view",
            }
            if title_lower in skip_titles:
                return True

            # 计算匹配分数
            if title_lower == keyword_lower:
                score = 100
            elif title_lower.startswith(keyword_lower):
                score = 80
            elif keyword_lower in title_lower:
                score = 60
            else:
                # 每个词分别匹配（支持 "Claude Code" → "Claude" + "Code"）
                words = keyword_lower.split()
                if len(words) > 1 and all(w in title_lower for w in words):
                    score = 40
                elif any(w in title_lower for w in words):
                    score = 20
                else:
                    return True  # 不匹配

            results.append((hwnd, title, score))
            return True

        # 创建回调并保持引用以防 GC
        callback = EnumWindowsProc(enum_callback)
        _enum_callback_ref = callback
        user32.EnumWindows(callback, 0)
        _enum_callback_ref = None

        # 按分数降序排列
        results.sort(key=lambda x: x[2], reverse=True)
        return [(hwnd, title) for hwnd, title, _score in results]

    @staticmethod
    def get_window_rect(hwnd: int) -> Optional[RECT]:
        """获取窗口的屏幕坐标矩形"""
        rect = RECT()
        if user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            # 验证矩形有效性
            if rect.right > rect.left and rect.bottom > rect.top:
                return rect
        return None

    @staticmethod
    def bring_window_to_front(hwnd: int) -> bool:
        """
        将目标窗口强行拉到前台。

        依次尝试 4 种方法突破 Windows 前台锁定：
          1. AttachThreadInput — 挂载到前台线程获取权限
          2. 模拟 Alt 键 — 让系统认为进程收到了用户输入
          3. AllowSetForegroundWindow — 显式授权自身
          4. SwitchToThisWindow — 不验证前台权限（最后手段）
        """
        try:
            # 恢复最小化的窗口
            if user32.IsIconic(hwnd):
                user32.ShowWindow(hwnd, 9)  # SW_RESTORE

            success = False

            # ── 方法 1: AttachThreadInput ──
            try:
                current_thread = kernel32.GetCurrentThreadId()
                fg_hwnd = user32.GetForegroundWindow()
                fg_thread = user32.GetWindowThreadProcessId(fg_hwnd, None)

                attached = False
                if current_thread != fg_thread and fg_thread:
                    attached = bool(user32.AttachThreadInput(
                        current_thread, fg_thread, True
                    ))

                success = bool(user32.SetForegroundWindow(hwnd))

                if attached:
                    user32.AttachThreadInput(current_thread, fg_thread, False)
            except Exception:
                pass

            # ── 方法 2: 模拟 Alt 键获取前台权限 ──
            if not success:
                try:
                    # Alt down → up，让系统认为进程收到了输入
                    user32.keybd_event(0x12, 0, 0, 0)  # VK_MENU
                    user32.keybd_event(0x12, 0, 2, 0)
                    success = bool(user32.SetForegroundWindow(hwnd))
                except Exception:
                    pass

            # ── 方法 3: AllowSetForegroundWindow ──
            if not success:
                try:
                    # ASFW_ANY = 0xFFFFFFFF → 允许任何进程设置前台
                    user32.AllowSetForegroundWindow(0xFFFFFFFF)
                    success = bool(user32.SetForegroundWindow(hwnd))
                except Exception:
                    pass

            # ── 方法 4: SwitchToThisWindow（激进，绕过所有检查）──
            if not success:
                try:
                    user32.SwitchToThisWindow(hwnd, True)
                    success = True
                except Exception:
                    pass

            # 提至最顶层
            time.sleep(0.05)
            user32.BringWindowToTop(hwnd)

            if success:
                logger.info(f"[NapcatScreenshot] Window brought to front: hwnd={hwnd}")
            else:
                logger.warning(
                    f"[NapcatScreenshot] Failed to bring window to front: hwnd={hwnd}"
                )

            return True  # 即使前台失败也继续截图（窗口可能部分可见）
        except Exception as e:
            logger.warning(f"[NapcatScreenshot] bring_window_to_front error: {e}")
            return False

    @staticmethod
    def capture_window(hwnd: int, crop_rect: Optional[Tuple[int, int, int, int]] = None) -> Optional[bytes]:
        """
        使用 PIL ImageGrab 截取指定窗口的屏幕区域。

        PIL 内部调用 Windows API 并正确处理像素格式和 DPI 缩放。
        """
        if not HAS_PIL:
            logger.error("[NapcatScreenshot] PIL 未安装，无法截图窗口")
            return None

        rect = Win32ScreenCapture.get_window_rect(hwnd)
        if not rect:
            return None

        left, top, right, bottom = rect.left, rect.top, rect.right, rect.bottom
        if crop_rect:
            x, y, w, h = crop_rect
            # Crop relative to window position
            new_left = left + x
            new_top = top + y
            new_right = new_left + w
            new_bottom = new_top + h
            # Ensure it's within the window
            bbox = (new_left, new_top, min(new_right, right), min(new_bottom, bottom))
        else:
            bbox = (left, top, right, bottom)
            
        try:
            img = ImageGrab.grab(bbox=bbox, all_screens=True)
            return Win32ScreenCapture._pil_to_jpeg(img)
        except Exception as e:
            logger.error(f"[NapcatScreenshot] PIL window capture failed: {e}")
            return None

    @staticmethod
    def capture_full_screen(crop_rect: Optional[Tuple[int, int, int, int]] = None) -> Optional[bytes]:
        """使用 PIL ImageGrab 截取整个屏幕"""
        if not HAS_PIL:
            logger.error("[NapcatScreenshot] PIL 未安装，无法截图全屏")
            return None
        try:
            if crop_rect:
                x, y, w, h = crop_rect
                bbox = (x, y, x + w, y + h)
                img = ImageGrab.grab(bbox=bbox, all_screens=True)
            else:
                img = ImageGrab.grab(all_screens=True)
            return Win32ScreenCapture._pil_to_jpeg(img)
        except Exception as e:
            logger.error(f"[NapcatScreenshot] PIL fullscreen capture failed: {e}")
            return None

    @staticmethod
    def _pil_to_jpeg(img: "Image.Image") -> bytes:
        """PIL Image → PNG bytes"""
        buf = io.BytesIO()
        img.convert("RGB").save(buf, format="PNG")
        return buf.getvalue()

    @staticmethod
    def capture_target(target: str, delay_ms: int = 300, crop_rect: Optional[Tuple[int, int, int, int]] = None) -> Tuple[Optional[bytes], str]:
        """
        根据目标字符串截图。

        策略：
          1. target 非空 → 搜窗口 → 前台 → PIL 截图
          2. target 为空 → 全屏截图
          3. 窗口未找到 → 返回 None + 可用窗口列表（不回退全屏！）

        返回: (image_bytes_or_None, description)
        """
        if delay_ms > 0:
            time.sleep(delay_ms / 1000.0)

        target = target.strip() if target else ""

        if target:
            windows = Win32ScreenCapture.find_windows_by_title(target)
            if windows:
                hwnd, title = windows[0]
                logger.info(
                    f"[NapcatScreenshot] Matched window: '{title}' "
                    f"for target='{target}'"
                )

                Win32ScreenCapture.bring_window_to_front(hwnd)
                wait_ms = max(delay_ms, 200)
                time.sleep(wait_ms / 1000.0)

                img_bytes = Win32ScreenCapture.capture_window(hwnd, crop_rect)
                if img_bytes:
                    return img_bytes, f"窗口「{title}」"
                else:
                    logger.warning(
                        f"[NapcatScreenshot] PIL capture failed for '{title}'"
                    )
                    return None, f"截图失败：无法捕获窗口「{title}」"
            else:
                # 窗口未找到 — 列出当前可见窗口帮助 LLM 下次命中
                logger.warning(
                    f"[NapcatScreenshot] No window found for target='{target}'"
                )
                return None, f"未找到包含「{target}」的窗口"

        # target 为空 → 全屏
        logger.info("[NapcatScreenshot] Capturing full screen (no target specified)")
        img_bytes = Win32ScreenCapture.capture_full_screen(crop_rect)
        if img_bytes:
            return img_bytes, "全屏"
        return None, "截图失败"


# ---------------------------------------------------------------------------
# NapCat HTTP API 引擎 (备用)
# ---------------------------------------------------------------------------

class NapcatAPICapture:
    """通过 NapCat HTTP API 截图"""

    @staticmethod
    async def capture(http_session, base_url: str, token: str) -> Optional[bytes]:
        import aiohttp
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"

        # 可能的截图端点
        endpoints = [
            "/api/screenshot",
            "/screenshot",
            "/api/get_screen_shot",
            "/api/system/screenshot",
            "/api/capture_screen",
        ]

        for endpoint in endpoints:
            url = f"{base_url}{endpoint}"
            try:
                async with http_session.post(url, headers=headers, json={}, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status == 200:
                        ct = resp.headers.get("Content-Type", "")
                        if "image" in ct:
                            return await resp.read()
                        data = await resp.json()
                        img = (
                            data.get("data", {}).get("image")
                            or data.get("image")
                            or data.get("data")
                        )
                        if img and isinstance(img, str):
                            return base64.b64decode(img)
                        elif img:
                            return img
            except Exception:
                continue
        return None


# ---------------------------------------------------------------------------
# 事件文本提取工具
# ---------------------------------------------------------------------------

def _get_event_text(event: AstrMessageEvent) -> str:
    """从事件中提取用户消息文本"""
    try:
        msg = event.message_str
        if msg:
            return msg.strip()
    except Exception:
        pass
    try:
        for seg in event.message_obj.message:
            text = getattr(seg, "text", None)
            if text:
                return text.strip()
    except Exception:
        pass
    return ""


def _get_result_text(event: AstrMessageEvent) -> str:
    """获取当前 result 的纯文本内容"""
    try:
        result = event.get_result()
    except Exception:
        return ""

    if result is None:
        return ""

    # AstrBot MessageChain or similar
    if hasattr(result, "get_plain_text"):
        try:
            return result.get_plain_text() or ""
        except Exception:
            pass

    # Try string conversion
    try:
        return str(result)
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# 主插件
# ---------------------------------------------------------------------------

@register(
    "napcat_screenshot",
    "bentianjia",
    "LLM自主决定截图时机与目标窗口，调用QQNT截图展示进度，像真人一样自然",
    "1.0.0",
)
class NapcatScreenshot(Star):
    """
    NapCat 智能截图插件。

    让 LLM 像真人一样，在需要时自主决定截图并指定截哪个窗口。
    支持窗口名模糊匹配：LLM 说 [SCREENSHOT:Claude Code] 即截取对应窗口。

    架构：
      - Win32ScreenCapture: 主力引擎，Win32 找窗口 + PIL ImageGrab 截图
      - NapcatAPICapture:   备选，NapCat HTTP API
      - Bot Action API:     备选，OneBot v11 action
    """

    def __init__(self, context: Context) -> None:
        super().__init__(context)
        self._config: Optional[PluginConfig] = None
        self._http: Any = None  # aiohttp.ClientSession
        self._last_screenshot_time: float = 0.0
        self._session_screenshot_count: int = 0
        self._session_id: str = ""

        # 启动时明确告知 PIL 状态
        if HAS_PIL:
            logger.info("[NapcatScreenshot] ✓ PIL/Pillow 已就绪，截图引擎正常")
        else:
            logger.error(
                "[NapcatScreenshot] ✗ PIL/Pillow 未安装！\n"
                "    请执行: pip install Pillow\n"
                "    没有 PIL 将无法截图！"
            )

    # ── 配置 ────────────────────────────────────────────────

    def _get_config(self) -> PluginConfig:
        if self._config is None:
            self._config = PluginConfig.from_context(self.context)
        return self._config

    async def _get_http(self):
        if self._http is None:
            import aiohttp
            self._http = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=30)
            )
        return self._http

    # ── 窗口名推断 ──────────────────────────────────────────

    @staticmethod
    def _guess_window(user_msg: str) -> str:
        """
        LLM 没指定窗口时，从用户消息中提取窗口关键词。

        常见软件→窗口标题关键词映射。
        """
        if not user_msg:
            return ""

        msg = user_msg.lower()
        # 按优先级匹配
        keywords = [
            # (消息关键词, 窗口关键词)
            ("claude code", "Claude Code"),
            ("claude", "Claude Code"),
            ("vs code", "VS Code"),
            ("vscode", "VS Code"),
            ("终端", "终端"),
            ("terminal", "终端"),
            ("浏览器", "Chrome"),
            ("chrome", "Chrome"),
            ("edge", "Edge"),
            ("记事本", "记事本"),
            ("notepad", "记事本"),
            ("资源管理器", "资源管理器"),
            ("explorer", "资源管理器"),
            ("下载", "下载"),
            ("音乐", "音乐"),
            ("视频", "视频"),
            ("设置", "设置"),
            ("控制面板", "控制面板"),
            ("任务管理器", "任务管理器"),
            ("桌面", ""),  # 桌面=全屏
            ("全屏", ""),
            ("整个屏幕", ""),
        ]

        for kw, window_title in keywords:
            if kw in msg:
                return window_title

        return ""

    # ── 会话追踪 ────────────────────────────────────────────

    def _get_session_key(self, event: AstrMessageEvent) -> str:
        """生成当前会话标识（同一群聊/私聊 + 同一用户 = 同一会话）"""
        gid = ""
        uid = ""
        try:
            gid = str(event.get_group_id())
        except Exception:
            pass
        try:
            uid = str(event.get_sender_id())
        except Exception:
            pass
        return f"{gid}:{uid}"

    def _reset_session_if_new(self, event: AstrMessageEvent) -> None:
        """检测新会话，重置截图计数"""
        key = self._get_session_key(event)
        if key != self._session_id:
            self._session_id = key
            self._session_screenshot_count = 0

    # ── 生命周期 ────────────────────────────────────────────

    async def terminate(self) -> None:
        if self._http:
            await self._http.close()
            self._http = None
        self._config = None
        logger.info("[NapcatScreenshot] Plugin unloaded")

    # ── 阶段 1: 注入截图提示词到 LLM ─────────────────────────

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req):
        """
        在 LLM 请求前，将截图能力说明注入 system prompt。
        LLM 会被告知它可以随时通过 [SCREENSHOT:窗口名] 来截图。
        """
        cfg = self._get_config()
        if not cfg.enable or not cfg.inject_system_prompt:
            return

        # 恢复会话计数
        self._reset_session_if_new(event)

        # 注入截图提示词
        if req.system_prompt:
            req.system_prompt = SCREENSHOT_SYSTEM_PROMPT + "\n\n" + req.system_prompt
        else:
            req.system_prompt = SCREENSHOT_SYSTEM_PROMPT

    # ── 阶段 2: 检测截图标记并执行 ───────────────────────────

    @filter.on_decorating_result()
    async def on_decorating_result(self, event: AstrMessageEvent):
        """
        检测 LLM 响应中的 [SCREENSHOT:目标] 标记。
        如果存在且未超过限制，执行截图并发送。
        """
        cfg = self._get_config()
        if not cfg.enable:
            return

        # 获取 LLM 生成的文本
        text = _get_result_text(event)
        if not text:
            return

        # 搜索截图标记
        match = SCREENSHOT_MARKER_RE.search(text)
        if not match:
            return

        target = (match.group(1) or "").strip()
        crop_str = (match.group(2) or "").strip()

        crop_rect = None
        if crop_str:
            try:
                # 解析 x,y,w,h
                parts = [int(p.strip()) for p in re.split(r'[,，\s]+', crop_str) if p.strip()]
                if len(parts) >= 4:
                    crop_rect = (parts[0], parts[1], parts[2], parts[3])
            except Exception as e:
                logger.warning(f"[NapcatScreenshot] Failed to parse crop rect: {crop_str}, {e}")

        # 如果 LLM 没指定窗口，从用户消息中提取
        if not target:
            user_msg = _get_event_text(event)
            target = self._guess_window(user_msg)
            if target:
                logger.info(
                    f"[NapcatScreenshot] LLM没指定窗口，从用户消息推断: '{target}'"
                )

        logger.info(f"[NapcatScreenshot] Screenshot target='{target or '(全屏)'}'")

        # 检查白名单权限
        if cfg.allowed_users:
            allowed_list = [u.strip() for u in cfg.allowed_users.split(",") if u.strip()]
            if allowed_list:
                uid = ""
                try:
                    uid = str(event.get_sender_id())
                except Exception:
                    pass
                if uid not in allowed_list:
                    logger.warning(f"[NapcatScreenshot] 拦截: 用户 {uid} 尝试截图，但不在白名单内。")
                    clean_text = SCREENSHOT_MARKER_RE.sub("", text).strip()
                    event.set_result(event.plain_result(clean_text + "\n\n[系统提示：您没有截图权限]"))
                    return

        # 检查冷却时间
        now = time.time()
        elapsed = now - self._last_screenshot_time
        if elapsed < cfg.cooldown:
            logger.info(f"[NapcatScreenshot] Cooldown active ({elapsed:.1f}s < {cfg.cooldown}s), skipping")
            clean_text = SCREENSHOT_MARKER_RE.sub("", text).strip()
            event.set_result(event.plain_result(clean_text))
            return

        # 检查每轮截图上限
        if self._session_screenshot_count >= cfg.max_screenshots_per_session:
            logger.info(f"[NapcatScreenshot] Session limit reached ({cfg.max_screenshots_per_session}), skipping")
            clean_text = SCREENSHOT_MARKER_RE.sub("", text).strip()
            event.set_result(event.plain_result(clean_text + "\n\n[本轮截图次数已达上限]"))
            return

        # ── 执行截图 ──
        img_bytes, captured_desc = await self._execute_screenshot(target, crop_rect, cfg, event)

        # 移除标记，清理文本
        clean_text = SCREENSHOT_MARKER_RE.sub("", text).strip()
        # 清理多余空行
        clean_text = re.sub(r'\n{3,}', '\n\n', clean_text)

        if img_bytes:
            self._last_screenshot_time = now
            self._session_screenshot_count += 1
            logger.info(
                f"[NapcatScreenshot] Screenshot OK ({self._session_screenshot_count}/{cfg.max_screenshots_per_session}): "
                f"{captured_desc}"
            )

            if cfg.send_as_separate_message:
                # 先发文字，再发截图（更拟人）
                event.set_result(event.plain_result(clean_text))
                await self._send_image_message(event, img_bytes)
            else:
                # 尝试合并（部分平台支持）
                event.set_result(event.plain_result(clean_text))
                await self._send_image_message(event, img_bytes)
        else:
            # 截图失败，附带具体原因
            logger.error(f"[NapcatScreenshot] Screenshot failed: {captured_desc}")
            event.set_result(event.plain_result(
                clean_text + f"\n\n[⚠ 截图失败：{captured_desc}]"
            ))

    # ── 截图执行器 ────────────────────────────────────────────

    async def _execute_screenshot(
        self, target: str, crop_rect: Optional[Tuple[int, int, int, int]], cfg: PluginConfig, event: AstrMessageEvent
    ) -> Tuple[Optional[bytes], str]:
        """
        截图优先级（从高到低）：
          1. NapCat bot action (QQ自带截图，零朝向问题)  ← 主力
          2. Win32窗口定位 + PIL截图（指定窗口时）
          3. NapCat HTTP API
          4. PIL 全屏兜底
        """
        img_bytes: Optional[bytes] = None
        captured_desc = ""

        mode = cfg.screenshot_mode

        # ════════════════════════════════════════════════════════
        # 方式 1: NapCat bot action — QQ 自带截图，最可靠
        #          napcat_first / napcat_only 模式下优先
        # ════════════════════════════════════════════════════════
        if mode in ("napcat_first", "napcat_only"):
            # 即使使用 NapCat，也尝试把目标窗口置于前台
            if target:
                windows = Win32ScreenCapture.find_windows_by_title(target)
                if windows:
                    hwnd, title = windows[0]
                    Win32ScreenCapture.bring_window_to_front(hwnd)
                    if cfg.screenshot_delay_ms > 0:
                        await asyncio.sleep(cfg.screenshot_delay_ms / 1000.0)
            try:
                img_bytes, captured_desc = await self._screenshot_via_bot_client(event)
            except Exception as e:
                logger.warning(f"[NapcatScreenshot] Bot action failed: {e}")

        # ════════════════════════════════════════════════════════
        # 方式 2: Win32窗口定位 + PIL截图（指定窗口时）
        # ════════════════════════════════════════════════════════
        if not img_bytes and mode in ("window_first", "fullscreen"):
            try:
                if mode == "window_first":
                    img_bytes, captured_desc = await asyncio.to_thread(
                        Win32ScreenCapture.capture_target,
                        target,
                        cfg.screenshot_delay_ms,
                        crop_rect
                    )
                else:
                    img_bytes = await asyncio.to_thread(
                        Win32ScreenCapture.capture_full_screen,
                        crop_rect
                    )
                    captured_desc = "全屏"
            except Exception as e:
                logger.warning(f"[NapcatScreenshot] Win32 capture failed: {e}")

        # ════════════════════════════════════════════════════════
        # 方式 3: NapCat HTTP API
        # ════════════════════════════════════════════════════════
        if not img_bytes:
            try:
                http = await self._get_http()
                img_bytes = await NapcatAPICapture.capture(
                    http, cfg.napcat_http_url, cfg.napcat_token
                )
                if img_bytes:
                    captured_desc = "NapCat HTTP API"
            except Exception as e:
                logger.warning(f"[NapcatScreenshot] NapCat HTTP failed: {e}")

        # ════════════════════════════════════════════════════════
        # 后处理：翻转修正 + 缩放
        # ════════════════════════════════════════════════════════
        if img_bytes:
            try:
                img_bytes = await asyncio.to_thread(
                    self._post_process, img_bytes,
                    cfg.max_image_width, cfg.image_quality, cfg.flip_correction,
                )
            except Exception:
                pass

        return img_bytes, captured_desc

    @staticmethod
    def _post_process(img_bytes: bytes, max_width: int, quality: int,
                      flip: str = "both") -> bytes:
        """
        后处理：numpy 翻转 + 缩放。

        flip 参数:
          "both"  — np.flipud + np.fliplr (180°旋转, 默认)
          "v"     — 仅 np.flipud (上下翻转)
          "h"     — 仅 np.fliplr (左右镜像)
          "none"  — 不翻转仅缩放

        numpy 直接操作像素数组，不存在 PIL transpose 实现差异。
        """
        if not HAS_PIL:
            return img_bytes

        try:
            import numpy as np

            img = Image.open(io.BytesIO(img_bytes))
            arr = np.array(img.convert("RGB"))

            # 按配置翻转
            if flip == "both" or flip == "v":
                arr = np.flipud(arr)
            if flip == "both" or flip == "h":
                arr = np.fliplr(arr)

            img = Image.fromarray(arr)

            # 缩放
            if max_width > 0 and img.width > max_width:
                ratio = max_width / img.width
                new_height = int(img.height * ratio)
                img = img.resize((max_width, new_height), Image.LANCZOS)

            buf = io.BytesIO()
            img.save(buf, format="PNG")
            return buf.getvalue()
        except Exception:
            return img_bytes

    async def _screenshot_via_bot_client(self, event: AstrMessageEvent) -> Tuple[Optional[bytes], str]:
        """
        通过 OneBot/NapCat 的 bot client call_action 截图。
        尝试多种可能的 action 名称。
        """
        bot = None
        try:
            bot = getattr(event, "bot", None)
        except Exception:
            pass
        if not bot:
            try:
                bot = event.get_bot()
            except Exception:
                pass
        if not bot or not hasattr(bot, "call_action"):
            return None, ""

        # 尝试不同的 action 名称
        action_names = [
            "get_screen_shot",
            "napcat_get_screen_shot",
            "get_screenshot",
            "capture_screen",
        ]

        for action_name in action_names:
            try:
                result = await bot.call_action(action_name)
                if result:
                    img = self._extract_image_from_result(result)
                    if img:
                        return img, f"Bot:{action_name}"
            except Exception:
                continue

        return None, ""

    @staticmethod
    def _extract_image_from_result(result) -> Optional[bytes]:
        """从 bot call_action 返回结果中提取图片数据"""
        if isinstance(result, bytes):
            return result
        if isinstance(result, dict):
            data = result.get("data", result)
            if isinstance(data, dict):
                # 文件路径
                file_path = data.get("file") or data.get("path")
                if file_path and isinstance(file_path, str):
                    if file_path.startswith("file://"):
                        file_path = file_path[7:]
                    try:
                        with open(file_path, "rb") as f:
                            return f.read()
                    except Exception:
                        pass
                # base64
                for key in ("base64", "image_base64", "image"):
                    b64 = data.get(key)
                    if b64 and isinstance(b64, str):
                        try:
                            return base64.b64decode(b64)
                        except Exception:
                            pass
            # 直接值可能是 base64 字符串
            if isinstance(data, str):
                try:
                    return base64.b64decode(data)
                except Exception:
                    # 可能是文件路径
                    if data.startswith("file://"):
                        try:
                            with open(data[7:], "rb") as f:
                                return f.read()
                        except Exception:
                            pass
        return None

    # ── 发送图片消息 ──────────────────────────────────────────

    async def _send_image_message(self, event: AstrMessageEvent, img_bytes: bytes) -> bool:
        """
        发送截图。先用 file:// 路径发（避开 base64 编码层），
        失败再回退 base64。
        """
        bot = None
        try:
            bot = getattr(event, "bot", None)
        except Exception:
            pass
        if not bot:
            try:
                bot = event.get_bot()
            except Exception:
                pass

        if not bot or not hasattr(bot, "call_action"):
            logger.error("[NapcatScreenshot] No bot client available to send image")
            return False

        gid = None
        uid = None
        try:
            gid = event.get_group_id()
        except Exception:
            pass
        try:
            uid = event.get_sender_id()
        except Exception:
            pass

        # ── 方法 1: 写临时文件，发 file:// 路径 ──
        import tempfile, os
        tmp_path = ""
        try:
            fd, tmp_path = tempfile.mkstemp(suffix=".png", prefix="napcat_ss_")
            os.close(fd)
            with open(tmp_path, "wb") as f:
                f.write(img_bytes)

            file_url = f"file:///{tmp_path.replace(chr(92), '/')}"
            cq_image = f"[CQ:image,file={file_url}]"

            if gid:
                resp = await bot.call_action(
                    "send_group_msg", group_id=int(gid), message=cq_image,
                )
            elif uid:
                resp = await bot.call_action(
                    "send_private_msg", user_id=int(uid), message=cq_image,
                )
            else:
                logger.error("[NapcatScreenshot] Cannot determine target")
                return False

            msg_id = None
            if isinstance(resp, dict):
                msg_id = resp.get("message_id") or resp.get("data", {}).get("message_id")

            logger.info(f"[NapcatScreenshot] Image sent via file://, msg_id={msg_id}")

            # 阅后即焚
            cfg = self._get_config()
            if cfg.auto_delete_after > 0 and msg_id:
                asyncio.create_task(
                    self._auto_delete_image(bot, msg_id, cfg.auto_delete_after)
                )

            # 延迟删临时文件
            asyncio.create_task(self._cleanup_tmp(tmp_path, delay=30))
            return True

        except Exception as e:
            logger.warning(f"[NapcatScreenshot] file:// send failed: {e}, falling back to base64")
            # 清理临时文件
            if tmp_path:
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass

        # ── 方法 2: base64 兜底 ──
        try:
            img_b64 = base64.b64encode(img_bytes).decode("ascii")
            cq_image = f"[CQ:image,file=base64://{img_b64}]"

            if gid:
                resp = await bot.call_action(
                    "send_group_msg", group_id=int(gid), message=cq_image,
                )
            elif uid:
                resp = await bot.call_action(
                    "send_private_msg", user_id=int(uid), message=cq_image,
                )
            else:
                return False

            msg_id = None
            if isinstance(resp, dict):
                msg_id = resp.get("message_id") or resp.get("data", {}).get("message_id")

            cfg = self._get_config()
            if cfg.auto_delete_after > 0 and msg_id:
                asyncio.create_task(
                    self._auto_delete_image(bot, msg_id, cfg.auto_delete_after)
                )

            logger.info(f"[NapcatScreenshot] Image sent via base64, msg_id={msg_id}")
            return True
        except Exception as e:
            logger.error(f"[NapcatScreenshot] base64 send also failed: {e}")
            return False

    async def _cleanup_tmp(self, path: str, delay: int = 30) -> None:
        """延迟删除临时文件"""
        await asyncio.sleep(delay)
        try:
            import os
            os.remove(path)
        except Exception:
            pass

    async def _auto_delete_image(self, bot, msg_id: int, delay_seconds: int) -> None:
        """
        在 delay_seconds 秒后自动撤回截图消息。

        独立 task，不阻塞主流程。撤回失败忽略（可能是被管理员删了或超时）。
        """
        await asyncio.sleep(delay_seconds)
        try:
            await bot.call_action("delete_msg", message_id=int(msg_id))
            logger.info(f"[NapcatScreenshot] Auto-deleted image msg_id={msg_id} after {delay_seconds}s")
        except Exception as e:
            logger.debug(f"[NapcatScreenshot] Failed to auto-delete msg_id={msg_id}: {e}")


