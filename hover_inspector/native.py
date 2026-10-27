"""Win32 + UI Automation access for the hover inspector.

The fast path is one ``Desktop(backend="uia").from_point(x, y)`` call
followed by a handful of property reads. There is no Appium session, no
WinAppDriver, and no tree walk. If UIA has no provider for the control, the
module falls back to ``WindowFromPoint`` plus the classic Win32 getters so the
app still shows a class, title and rectangle.

Everything in this module is Windows-only. Imports are guarded so ``--help``
and the pure tests still work on a non-Windows checkout.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import sys
import time
import warnings
from typing import Any

from .model import ElementInfo, Rect

sys.coinit_flags = 2  # STA, before pywinauto/comtypes are imported.
warnings.filterwarnings("ignore", category=UserWarning, module=r"pywinauto(\..*)?")

IS_WINDOWS = sys.platform == "win32"
HAS_WIN32 = False

win32api = win32gui = win32process = None
if IS_WINDOWS:
    try:
        import win32api
        import win32gui
        import win32process

        HAS_WIN32 = True
    except ImportError:
        HAS_WIN32 = False

if IS_WINDOWS:
    _user32 = ctypes.windll.user32
    _user32.GetAncestor.restype = wintypes.HWND
    _user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
else:
    _user32 = None

# --- Win32 constants -------------------------------------------------------
GWL_EXSTYLE = -20
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000
HWND_TOPMOST = -1
SWP_NOACTIVATE = 0x0010
SWP_SHOWWINDOW = 0x0040
GA_ROOT = 2

SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79

_MISSING = object()

_CONTROL_TYPES = {
    50000: "Button",
    50001: "Calendar",
    50002: "CheckBox",
    50003: "ComboBox",
    50004: "Edit",
    50005: "Hyperlink",
    50006: "Image",
    50007: "ListItem",
    50008: "List",
    50009: "Menu",
    50010: "MenuBar",
    50011: "MenuItem",
    50012: "ProgressBar",
    50013: "RadioButton",
    50014: "ScrollBar",
    50015: "Slider",
    50016: "Spinner",
    50017: "StatusBar",
    50018: "Tab",
    50019: "TabItem",
    50020: "Text",
    50021: "ToolBar",
    50022: "ToolTip",
    50023: "Tree",
    50024: "TreeItem",
    50025: "Custom",
    50026: "Group",
    50027: "Thumb",
    50028: "DataGrid",
    50029: "DataItem",
    50030: "Document",
    50031: "SplitButton",
    50032: "Window",
    50033: "Pane",
    50034: "Header",
    50035: "HeaderItem",
    50036: "Table",
    50037: "TitleBar",
    50038: "Separator",
    50039: "SemanticZoom",
    50040: "AppBar",
}

_WIN32_TYPES = {
    "Button": "Button",
    "ComboBox": "ComboBox",
    "ComboLBox": "List",
    "Edit": "Edit",
    "MDIClient": "Pane",
    "ScrollBar": "ScrollBar",
    "Static": "Text",
    "SysDateTimePick32": "Calendar",
    "SysHeader32": "Header",
    "SysListView32": "List",
    "SysMonthCal32": "Calendar",
    "SysTabControl32": "Tab",
    "SysTreeView32": "Tree",
    "ToolbarWindow32": "ToolBar",
    "Chrome_RenderWidgetHostHWND": "Pane",
    "Chrome_WidgetWin_1": "Window",
    "CASCADIA_HOSTING_WINDOW_CLASS": "Window",
    "DirectUIHWND": "Pane",
    "Internet Explorer_Server": "Document",
    "MozillaWindowClass": "Window",
    "msctls_hotkey32": "Custom",
    "msctls_progress32": "ProgressBar",
    "msctls_statusbar32": "StatusBar",
    "msctls_trackbar32": "Slider",
    "msctls_updown32": "Spinner",
    "TermControl": "Document",
    "TkChild": "Pane",
    "TkTopLevel": "Window",
    "#32770": "Window",
    "ApplicationFrameWindow": "Window",
    "CabinetWClass": "Window",
    "ExploreWClass": "Window",
    "Windows.UI.Core.CoreWindow": "Window",
}


# --- Small helpers ---------------------------------------------------------


def _require_win32() -> None:
    """Raise a clear error when the app is not running on Windows."""
    if not IS_WINDOWS:
        raise RuntimeError("The hover inspector is Windows-only; run it from Windows, not WSL.")
    if not HAS_WIN32:
        raise RuntimeError('pywin32 is not installed. Run `pip install -e ".[dev]"` or `pip install pywin32`.')


def _get(obj: Any, name: str, default: Any = None) -> Any:
    """Read an attribute, swallowing provider errors and unsupported properties."""
    try:
        return getattr(obj, name)
    except Exception:
        return default


def _first(obj: Any, names: tuple[str, ...], default: Any = None) -> Any:
    """Return the first readable, non-None attribute from ``names``."""
    for name in names:
        value = _get(obj, name, _MISSING)
        if value is not _MISSING and value is not None:
            return value
    return default


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    try:
        return str(value)
    except Exception:
        return ""


def _as_bool(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    try:
        return bool(value)
    except Exception:
        return None


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _maybe_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _control_type(value: Any) -> str:
    if isinstance(value, int):
        return _CONTROL_TYPES.get(value, f"ControlType({value})")
    text = _as_text(value).strip()
    return text or "Unknown"


def _win32_control_type(class_name: str) -> str:
    if not class_name:
        return "Unknown"
    if class_name in _WIN32_TYPES:
        return _WIN32_TYPES[class_name]
    if class_name.startswith(("RichEdit", "RICHEDIT")):
        return "Document"
    return "Unknown"


def _rect_from(value: Any) -> Rect | None:
    """Convert a UIA RECT / pywinauto rectangle to our immutable Rect."""
    if value is None:
        return None
    if isinstance(value, (tuple, list)) and len(value) == 4:
        left, top, right, bottom = (_maybe_int(part) for part in value)
    else:
        left = _maybe_int(_get(value, "left"))
        top = _maybe_int(_get(value, "top"))
        right = _maybe_int(_get(value, "right"))
        bottom = _maybe_int(_get(value, "bottom"))
    if None not in (left, top, right, bottom):
        return Rect(left, top, right, bottom)

    # Some wrappers expose left/top plus width()/height() instead of right/bottom.
    left = _maybe_int(_get(value, "left"))
    top = _maybe_int(_get(value, "top"))
    try:
        width = int(value.width())
        height = int(value.height())
    except Exception:
        return None
    if left is None or top is None:
        return None
    return Rect(left, top, left + width, top + height)


# --- Display / DPI / cursor -------------------------------------------------


def enable_dpi_awareness() -> None:
    """Make cursor, UIA bounds and Tk coordinates use physical pixels."""
    if not IS_WINDOWS:
        return
    user32 = ctypes.windll.user32
    try:
        user32.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        user32.SetProcessDpiAwarenessContext.restype = wintypes.BOOL
        if user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):  # PER_MONITOR_AWARE_V2
            return
    except (AttributeError, OSError):
        pass
    try:
        shcore = ctypes.windll.shcore
        shcore.SetProcessDpiAwareness.argtypes = [ctypes.c_int]
        shcore.SetProcessDpiAwareness.restype = ctypes.c_long
        if shcore.SetProcessDpiAwareness(2) == 0:  # PROCESS_PER_MONITOR_DPI_AWARE
            return
    except (AttributeError, OSError):
        pass
    try:
        user32.SetProcessDPIAware()
    except (AttributeError, OSError):
        pass


def system_dpi() -> int:
    """System DPI (96 = 100%, 144 = 150%), used to scale Tk fonts."""
    if not IS_WINDOWS:
        return 96
    try:
        user32 = ctypes.windll.user32
        user32.GetDpiForSystem.restype = wintypes.UINT
        return int(user32.GetDpiForSystem()) or 96
    except (AttributeError, OSError):
        pass
    try:
        user32 = ctypes.windll.user32
        gdi32 = ctypes.windll.gdi32
        user32.GetDC.restype = wintypes.HDC
        user32.GetDC.argtypes = [wintypes.HWND]
        user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
        gdi32.GetDeviceCaps.argtypes = [wintypes.HDC, ctypes.c_int]
        gdi32.GetDeviceCaps.restype = ctypes.c_int
        dc = user32.GetDC(0)
        dpi = int(gdi32.GetDeviceCaps(dc, 88))  # LOGPIXELSX
        user32.ReleaseDC(0, dc)
        return dpi or 96
    except Exception:
        return 96


def cursor_pos() -> tuple[int, int]:
    """Current mouse position in physical screen pixels."""
    _require_win32()
    return win32api.GetCursorPos()


def virtual_screen() -> tuple[int, int, int, int]:
    """``(left, top, width, height)`` covering every monitor."""
    _require_win32()
    return (
        int(win32api.GetSystemMetrics(SM_XVIRTUALSCREEN)),
        int(win32api.GetSystemMetrics(SM_YVIRTUALSCREEN)),
        int(win32api.GetSystemMetrics(SM_CXVIRTUALSCREEN)),
        int(win32api.GetSystemMetrics(SM_CYVIRTUALSCREEN)),
    )


def window_rect(hwnd: int) -> Rect | None:
    """Screen rectangle of a window handle, including its non-client area."""
    if not hwnd:
        return None
    _require_win32()
    try:
        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        return Rect(int(left), int(top), int(right), int(bottom))
    except Exception:
        return None


def point_in_window(x: int, y: int, hwnd: int) -> bool:
    """True when a screen point is inside a window's outer rectangle."""
    rect = window_rect(hwnd)
    if rect is None:
        return False
    return rect.left <= x < rect.right and rect.top <= y < rect.bottom


def window_at_point(x: int, y: int) -> int:
    """HWND under a screen point, or 0."""
    _require_win32()
    try:
        return int(win32gui.WindowFromPoint((int(x), int(y))) or 0)
    except Exception:
        return 0


def _pid_from_hwnd(hwnd: int) -> int:
    if not hwnd:
        return 0
    try:
        return int(win32process.GetWindowThreadProcessId(hwnd)[1])
    except Exception:
        return 0


def _root_hwnd(hwnd: int) -> int:
    if not hwnd or _user32 is None:
        return hwnd
    try:
        return int(_user32.GetAncestor(hwnd, GA_ROOT) or hwnd)
    except Exception:
        return hwnd


def window_title(hwnd: int) -> str:
    """Title of the top-level window that owns an HWND."""
    if not hwnd:
        return ""
    root = _root_hwnd(hwnd)
    try:
        return win32gui.GetWindowText(root)
    except Exception:
        return ""


def _rect_from_hwnd(hwnd: int) -> Rect | None:
    return window_rect(hwnd)


# --- Overlay helpers -------------------------------------------------------


def toplevel_hwnd(window: Any) -> int:
    """The real top-level HWND behind a Tk Toplevel."""
    _require_win32()
    hwnd = int(window.winfo_id())
    try:
        return int(_user32.GetAncestor(hwnd, GA_ROOT) or hwnd)
    except Exception:
        return hwnd


def make_click_through(hwnd: int) -> None:
    """Keep the highlight from intercepting clicks or focus."""
    _require_win32()
    try:
        style = win32gui.GetWindowLong(hwnd, GWL_EXSTYLE)
        win32gui.SetWindowLong(
            hwnd,
            GWL_EXSTYLE,
            style | WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE,
        )
    except Exception:
        pass


def make_child_click_through(hwnd: int) -> None:
    """Make Tk's client child transparent without layering it a second time."""
    _require_win32()
    try:
        style = win32gui.GetWindowLong(hwnd, GWL_EXSTYLE)
        win32gui.SetWindowLong(hwnd, GWL_EXSTYLE, style | WS_EX_TRANSPARENT | WS_EX_NOACTIVATE)
    except Exception:
        pass


def place_topmost(hwnd: int, x: int, y: int, width: int, height: int) -> None:
    """Move/size a topmost window without activating it."""
    _require_win32()
    try:
        win32gui.SetWindowPos(
            hwnd,
            HWND_TOPMOST,
            int(x),
            int(y),
            int(width),
            int(height),
            SWP_NOACTIVATE | SWP_SHOWWINDOW,
        )
    except Exception:
        pass


# --- UIA / Win32 probing ---------------------------------------------------


def _parent_element(element: Any) -> Any | None:
    """Parent of a pywinauto wrapper, UIAElementInfo, or raw UIA element."""
    if getattr(element, "element_info", None) is not None:
        try:
            return element.parent() or None
        except Exception:
            pass
    attr = _get(element, "parent", None)
    if attr is None:
        return _get(element, "CurrentParent", None)
    if callable(attr):
        try:
            return attr() or None
        except Exception:
            return None
    return attr


def _parent_hwnd(hwnd: int) -> int:
    try:
        return int(win32gui.GetParent(hwnd) or 0)
    except Exception:
        return 0


class ElementProbe:
    """Runs UIA queries. Must be used from a COM-initialized worker thread."""

    def __init__(self, *, chain_depth: int = 3):
        self.chain_depth = max(0, int(chain_depth))
        self._desktop: Any | None = None
        self._process_cache: dict[int, str] = {}

    def _ensure_desktop(self) -> None:
        if self._desktop is not None:
            return
        _require_win32()
        try:
            import pythoncom  # noqa: PLC0415 - Windows-only, imported in the COM worker

            pythoncom.CoInitialize()
        except Exception:
            pass
        from pywinauto import Desktop  # noqa: PLC0415 - Windows-only, imported in the COM worker

        self._desktop = Desktop(backend="uia")

    def probe(self, x: int, y: int) -> ElementInfo:
        """Return the element at a screen point, falling back to Win32."""
        started = time.perf_counter()
        error = ""
        element = None
        try:
            self._ensure_desktop()
            from_point = getattr(self._desktop, "from_point", None)
            if callable(from_point):
                element = from_point(int(x), int(y))
            else:  # Compatibility with forks that expose element_from_point.
                element = self._desktop.element_from_point(int(x), int(y))
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"

        if element is not None:
            try:
                info = self._from_uia(element)
                if self._has_content(info):
                    info.probe_ms = (time.perf_counter() - started) * 1000
                    return info
            except Exception as exc:
                error = f"UIA read failed: {type(exc).__name__}: {exc}"

        info = self._from_win32(x, y)
        info.error = error or info.error
        info.probe_ms = (time.perf_counter() - started) * 1000
        return info

    @staticmethod
    def _has_content(info: ElementInfo) -> bool:
        return bool(
            (info.bounds is not None and not info.bounds.is_empty)
            or info.hwnd
            or info.name
            or info.automation_id
            or info.class_name
        )

    def _read_facts(self, obj: Any) -> ElementInfo:
        """Read the panel fields from a UIAElementInfo, wrapper, or raw element."""
        # UIAElementInfo exposes a smaller property set than the raw
        # IUIAutomationElement. Keep a handle on the raw element for the
        # Current* properties it does not wrap (focus, help text, password...).
        raw = _get(obj, "element", None)

        def read(names: tuple[str, ...], default: Any = None) -> Any:
            value = _first(obj, names, _MISSING)
            if value is not _MISSING:
                return value
            if raw is not None:
                return _first(raw, names, default)
            return default

        offscreen = read(("is_offscreen", "CurrentIsOffscreen"), _MISSING)
        if offscreen is _MISSING or offscreen is None:
            visible = read(("visible",), _MISSING)
            offscreen = None if visible is _MISSING or visible is None else not bool(visible)

        return ElementInfo(
            control_type=_control_type(read(("control_type", "CurrentControlType"), "Unknown")),
            name=_as_text(read(("name", "CurrentName"), "")),
            automation_id=_as_text(read(("automation_id", "CurrentAutomationId"), "")),
            class_name=_as_text(read(("class_name", "CurrentClassName"), "")),
            framework_id=_as_text(read(("framework_id", "CurrentFrameworkId"), "")),
            bounds=_rect_from(read(("rectangle", "CurrentBoundingRectangle"), None)),
            enabled=_as_bool(read(("enabled", "is_enabled", "CurrentIsEnabled"), None)),
            offscreen=_as_bool(offscreen),
            keyboard_focusable=_as_bool(read(("is_keyboard_focusable", "CurrentIsKeyboardFocusable"), None)),
            has_focus=_as_bool(read(("has_focus", "CurrentHasKeyboardFocus"), None)),
            is_password=_as_bool(read(("is_password", "CurrentIsPassword"), None)),
            help_text=_as_text(read(("help_text", "CurrentHelpText"), "")),
            access_key=_as_text(read(("access_key", "CurrentAccessKey"), "")),
            accelerator_key=_as_text(read(("accelerator_key", "CurrentAcceleratorKey"), "")),
            hwnd=_as_int(read(("handle", "CurrentNativeWindowHandle"), 0)),
            pid=_as_int(read(("process_id", "CurrentProcessId"), 0)),
            source="uia",
        )

    def _from_uia(self, element: Any) -> ElementInfo:
        info_obj = getattr(element, "element_info", None) or element
        info = self._read_facts(info_obj)
        info.source = "uia"

        node = element
        seen = {info.signature}
        for _ in range(self.chain_depth):
            node = _parent_element(node)
            if node is None:
                break
            ancestor_obj = getattr(node, "element_info", None) or node
            ancestor = self._read_facts(ancestor_obj)
            if ancestor.signature in seen:
                break
            seen.add(ancestor.signature)
            info.ancestors.append(ancestor)
            if not info.hwnd and ancestor.hwnd:
                info.hwnd = ancestor.hwnd
            if not info.pid and ancestor.pid:
                info.pid = ancestor.pid

        if not info.hwnd:
            info.hwnd = next((item.hwnd for item in info.ancestors if item.hwnd), 0)
        if not info.pid:
            info.pid = next((item.pid for item in info.ancestors if item.pid), 0)
        if not info.pid and info.hwnd:
            info.pid = _pid_from_hwnd(info.hwnd)

        info.process = self._process_name(info.pid)
        info.window_title = window_title(info.hwnd)
        for ancestor in info.ancestors:
            if not ancestor.pid and ancestor.hwnd:
                ancestor.pid = _pid_from_hwnd(ancestor.hwnd)
            if not ancestor.process:
                ancestor.process = self._process_name(ancestor.pid)
            if not ancestor.window_title:
                ancestor.window_title = window_title(ancestor.hwnd)
        return info

    def _from_win32(self, x: int, y: int) -> ElementInfo:
        hwnd = window_at_point(x, y)
        if not hwnd:
            return ElementInfo(source="win32", error="no window at point")

        info = self._facts_from_hwnd(hwnd)
        parent = _parent_hwnd(hwnd)
        for _ in range(self.chain_depth):
            if not parent:
                break
            info.ancestors.append(self._facts_from_hwnd(parent))
            parent = _parent_hwnd(parent)
        return info

    def _facts_from_hwnd(self, hwnd: int) -> ElementInfo:
        class_name = ""
        title = ""
        try:
            class_name = win32gui.GetClassName(hwnd)
        except Exception:
            pass
        try:
            title = win32gui.GetWindowText(hwnd)
        except Exception:
            pass
        pid = _pid_from_hwnd(hwnd)
        visible = None
        enabled = None
        try:
            visible = bool(win32gui.IsWindowVisible(hwnd))
            enabled = bool(win32gui.IsWindowEnabled(hwnd))
        except Exception:
            pass
        return ElementInfo(
            control_type=_win32_control_type(class_name),
            name=title,
            class_name=class_name,
            framework_id="Win32",
            bounds=_rect_from_hwnd(hwnd),
            enabled=enabled,
            offscreen=None if visible is None else not visible,
            hwnd=hwnd,
            pid=pid,
            process=self._process_name(pid),
            window_title=window_title(hwnd),
            source="win32",
        )

    def _process_name(self, pid: int) -> str:
        if not pid:
            return ""
        cached = self._process_cache.get(pid)
        if cached is not None:
            return cached
        name = ""
        try:
            import psutil  # noqa: PLC0415 - optional at import time; cached after first use

            name = psutil.Process(pid).name() or ""
        except Exception:
            name = ""
        name = name.casefold()
        if len(self._process_cache) >= 512:
            self._process_cache.clear()
        self._process_cache[pid] = name
        return name
