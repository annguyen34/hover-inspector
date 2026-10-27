"""Pure data model and formatting helpers for the hover inspector.

Nothing in this module imports Win32 or pywinauto, so the locator output and
JSON shape can be unit-tested on any platform.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Rect:
    """A screen rectangle in physical pixels."""

    left: int
    top: int
    right: int
    bottom: int

    @property
    def width(self) -> int:
        """Rectangle width in pixels, clamped at zero."""
        return max(0, self.right - self.left)

    @property
    def height(self) -> int:
        """Rectangle height in pixels, clamped at zero."""
        return max(0, self.bottom - self.top)

    @property
    def is_empty(self) -> bool:
        """True when the rectangle has no drawable area."""
        return self.width <= 0 or self.height <= 0

    def as_dict(self) -> dict[str, int]:
        """JSON-friendly representation, including derived width/height."""
        return {
            "left": self.left,
            "top": self.top,
            "right": self.right,
            "bottom": self.bottom,
            "width": self.width,
            "height": self.height,
        }

    def __str__(self) -> str:
        return f"{self.left},{self.top},{self.width}x{self.height}"


@dataclass
class ElementInfo:
    """The subset of UIA properties the inspector shows and copies."""

    control_type: str = "Unknown"
    name: str = ""
    automation_id: str = ""
    class_name: str = ""
    framework_id: str = ""
    bounds: Rect | None = None
    enabled: bool | None = None
    offscreen: bool | None = None
    keyboard_focusable: bool | None = None
    has_focus: bool | None = None
    is_password: bool | None = None
    help_text: str = ""
    access_key: str = ""
    accelerator_key: str = ""
    hwnd: int = 0
    pid: int = 0
    process: str = ""
    window_title: str = ""
    source: str = "uia"
    error: str = ""
    probe_ms: float = 0.0
    ancestors: list[ElementInfo] = field(default_factory=list)

    @property
    def display_name(self) -> str:
        """A one-line name for the overlay and headlines."""
        text = " ".join((self.name or self.automation_id or self.class_name or "(unnamed)").split())
        return text or "(unnamed)"

    @property
    def hwnd_text(self) -> str:
        """HWND formatted as an uppercase ``0x``-prefixed hex string."""
        return f"0x{self.hwnd:08x}" if self.hwnd else ""

    @property
    def signature(self) -> tuple[object, ...]:
        """Identity used to avoid redrawing the same element after a re-probe."""
        bounds = None
        if self.bounds is not None:
            bounds = (self.bounds.left, self.bounds.top, self.bounds.right, self.bounds.bottom)
        return (
            self.source,
            self.control_type,
            self.automation_id,
            self.name,
            self.class_name,
            bounds,
            self.hwnd,
            self.enabled,
            self.offscreen,
            self.has_focus,
            self.error,
        )

    def process_text(self) -> str:
        """``revit.exe (pid 1234)`` or an empty string when unknown."""
        if self.process and self.pid:
            return f"{self.process} (pid {self.pid})"
        return self.process or (f"pid {self.pid}" if self.pid else "")

    def state_text(self) -> str:
        """Human-readable state flags plus the data source (UIA or Win32)."""
        parts: list[str] = []
        if self.offscreen is False:
            parts.append("visible")
        elif self.offscreen is True:
            parts.append("offscreen")
        if self.enabled is True:
            parts.append("enabled")
        elif self.enabled is False:
            parts.append("disabled")
        if self.has_focus:
            parts.append("focused")
        if self.keyboard_focusable:
            parts.append("focusable")
        if self.is_password:
            parts.append("password")
        if self.source == "uia":
            parts.append("UIA")
        elif self.source == "win32":
            parts.append("Win32 fallback")
        else:
            parts.append(self.source)
        return " · ".join(parts)

    def as_dict(self, *, include_ancestors: bool = True) -> dict[str, Any]:
        """A JSON-friendly dict, optionally including the ancestor chain."""
        data: dict[str, Any] = {
            "source": self.source,
            "control_type": self.control_type,
            "name": self.name,
            "automation_id": self.automation_id,
            "class_name": self.class_name,
            "framework_id": self.framework_id,
            "bounds": self.bounds.as_dict() if self.bounds else None,
            "bounds_text": str(self.bounds) if self.bounds else "",
            "enabled": self.enabled,
            "offscreen": self.offscreen,
            "keyboard_focusable": self.keyboard_focusable,
            "has_focus": self.has_focus,
            "is_password": self.is_password,
            "help_text": self.help_text,
            "access_key": self.access_key,
            "accelerator_key": self.accelerator_key,
            "hwnd": self.hwnd_text,
            "pid": self.pid,
            "process": self.process,
            "window_title": self.window_title,
            "probe_ms": round(self.probe_ms, 2),
        }
        if self.error:
            data["error"] = self.error
        if include_ancestors:
            data["ancestors"] = [item.as_dict(include_ancestors=False) for item in self.ancestors]
        return data

    def to_json(self, *, include_ancestors: bool = True) -> str:
        """Pretty-printed JSON for the Copy JSON button."""
        return json.dumps(self.as_dict(include_ancestors=include_ancestors), indent=2, ensure_ascii=False)


LOCATOR_FORMATS = ("appium", "pywinauto", "tuple")


def locator_parts(info: ElementInfo) -> tuple[str, str, dict[str, str]] | None:
    """Pick the strongest locator strategy for an element.

    AutomationId is the most stable, then Name, then ControlType, then
    ClassName. Returns ``(by, locator, attributes)`` or ``None``.
    """
    if info.automation_id:
        by, locator = "accessibility id", info.automation_id
    elif info.name:
        by, locator = "name", info.name
    elif info.control_type and info.control_type != "Unknown":
        by, locator = "control type", info.control_type
    elif info.class_name:
        by, locator = "class name", info.class_name
    else:
        return None

    attributes: dict[str, str] = {}
    if by != "control type" and info.control_type and info.control_type != "Unknown":
        attributes["ControlType"] = info.control_type
    if info.class_name:
        attributes["ClassName"] = info.class_name
    return by, locator, attributes


def _appium_locator(by: str, locator: str) -> tuple[str, str]:
    """Map a native strategy to the Appium/WinAppDriver wire vocabulary."""
    if by == "control type":
        return "xpath", f"//{locator.replace(' ', '')}"
    return by, locator


def format_locator(info: ElementInfo, fmt: str = "appium") -> str:
    """Format a ready-to-paste locator in a common automation shape.

    ``appium`` is the Appium/WinAppDriver wire shape (``using``/``value``),
    ``pywinauto`` is a kwargs dict for ``child_window(**locator)``, and
    ``tuple`` is a ``(strategy, value)`` pair for ``find_element(*locator)``.
    """
    if fmt not in LOCATOR_FORMATS:
        raise ValueError(f"unknown locator format: {fmt!r}")
    parts = locator_parts(info)
    if parts is None:
        return ""
    by, locator, _attributes = parts

    if fmt == "appium":
        using, value = _appium_locator(by, locator)
        return json.dumps({"using": using, "value": value}, indent=2, ensure_ascii=False)

    if fmt == "tuple":
        using, value = _appium_locator(by, locator)
        return repr((using, value))

    # pywinauto: pass this dict to child_window(**locator).
    data: dict[str, str] = {}
    if info.automation_id:
        data["auto_id"] = info.automation_id
    elif info.name:
        data["title"] = info.name
    elif info.class_name:
        data["class_name"] = info.class_name
    if info.control_type and info.control_type != "Unknown":
        data["control_type"] = info.control_type
    if info.class_name and "class_name" not in data:
        data["class_name"] = info.class_name
    return json.dumps(data, indent=2, ensure_ascii=False)


def process_matches(actual: str, wanted: str | None) -> bool:
    """Case-insensitive process filter; ``revit`` also matches ``revit.exe``."""
    if not wanted:
        return True
    actual_name = (actual or "").casefold()
    wanted_name = wanted.casefold()
    if actual_name == wanted_name:
        return True
    if not wanted_name.endswith(".exe"):
        return actual_name.removesuffix(".exe") == wanted_name
    return False
