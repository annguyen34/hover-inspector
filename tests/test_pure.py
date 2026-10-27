"""Pure tests for the hover inspector's dwell tracker, model and UIA mapping.

Run from the project root:

    python -m pytest -q

These do not open a window or call Win32; the probe mapping is tested with
plain fake objects that mimic pywinauto's UIAElementInfo.
"""

from __future__ import annotations

import json

from hover_inspector import native
from hover_inspector.dwell import DwellTracker
from hover_inspector.model import LOCATOR_FORMATS, ElementInfo, Rect, format_locator, process_matches


def test_dwell_fires_once_after_delay():
    tracker = DwellTracker(delay=1.0, tolerance=3)
    assert tracker.update(10, 10, now=0.0) is False
    assert tracker.update(10, 10, now=0.5) is False
    assert tracker.update(10, 10, now=1.0) is True
    assert tracker.update(10, 10, now=2.0) is False


def test_dwell_movement_rearms():
    tracker = DwellTracker(delay=1.0, tolerance=3)
    tracker.update(10, 10, now=0.0)
    assert tracker.update(10, 10, now=1.0) is True
    assert tracker.update(20, 10, now=1.1) is False
    assert tracker.update(20, 10, now=2.1) is True


def test_dwell_small_jitter_keeps_timer():
    tracker = DwellTracker(delay=1.0, tolerance=3)
    tracker.update(10, 10, now=0.0)
    assert tracker.update(12, 11, now=0.5) is False
    assert tracker.update(12, 11, now=1.0) is True


def test_dwell_mark_fired_blocks_repeat():
    tracker = DwellTracker(delay=1.0, tolerance=3)
    tracker.update(10, 10, now=0.0)
    tracker.mark_fired(10, 10, now=0.0)
    assert tracker.update(10, 10, now=2.0) is False


def test_rect_format_and_empty():
    rect = Rect(10, 20, 110, 70)
    assert str(rect) == "10,20,100x50"
    assert rect.width == 100
    assert rect.height == 50
    assert rect.is_empty is False
    assert Rect(0, 0, 0, 0).is_empty is True


def test_locator_prefers_automation_id():
    info = ElementInfo(control_type="Button", name="OK", automation_id="btnOK", class_name="Button")
    assert json.loads(format_locator(info)) == {"using": "accessibility id", "value": "btnOK"}


def test_locator_falls_back_to_name():
    info = ElementInfo(control_type="Button", name="OK", class_name="Button")
    assert json.loads(format_locator(info)) == {"using": "name", "value": "OK"}


def test_locator_falls_back_to_control_type():
    info = ElementInfo(control_type="Window")
    assert json.loads(format_locator(info)) == {"using": "xpath", "value": "//Window"}


def test_locator_falls_back_to_class_name():
    info = ElementInfo(class_name="Button")
    assert json.loads(format_locator(info)) == {"using": "class name", "value": "Button"}


def test_locator_requires_an_identifier():
    assert format_locator(ElementInfo()) == ""


def test_pywinauto_locator():
    info = ElementInfo(control_type="Button", name="OK", automation_id="btnOK", class_name="Button")
    assert json.loads(format_locator(info, "pywinauto")) == {
        "auto_id": "btnOK",
        "control_type": "Button",
        "class_name": "Button",
    }


def test_tuple_locator():
    info = ElementInfo(control_type="Button", name="OK", class_name="Button")
    assert format_locator(info, "tuple") == "('name', 'OK')"


def test_locator_formats_are_available():
    assert set(LOCATOR_FORMATS) == {"appium", "pywinauto", "tuple"}


def test_process_matches():
    assert process_matches("revit.exe", "revit.exe")
    assert process_matches("revit.exe", "revit")
    assert process_matches("notepad.exe", "REVIT") is False
    assert process_matches("", None) is True
    assert process_matches("", "revit") is False


def test_json_includes_ancestors_without_recursion():
    child = ElementInfo(name="OK", ancestors=[ElementInfo(name="Pane")])
    data = child.as_dict()
    assert data["ancestors"][0]["name"] == "Pane"
    assert "ancestors" not in data["ancestors"][0]


def test_signature_changes_with_state():
    assert ElementInfo(name="OK", enabled=True).signature != ElementInfo(name="OK", enabled=False).signature


class _FakeRawElement:
    CurrentIsOffscreen = False
    CurrentIsKeyboardFocusable = True
    CurrentHasKeyboardFocus = False
    CurrentIsPassword = False
    CurrentHelpText = "help"
    CurrentAccessKey = "A"
    CurrentAcceleratorKey = "Ctrl+A"


class _FakeUIAElementInfo:
    control_type = "Button"
    name = "OK"
    automation_id = "btnOK"
    class_name = "Button"
    framework_id = "Win32"
    rectangle = Rect(1, 2, 30, 40)
    handle = 123
    process_id = 456
    enabled = True
    visible = True
    element = _FakeRawElement()


def test_uia_element_info_properties_are_mapped():
    info = native.ElementProbe()._read_facts(_FakeUIAElementInfo())
    assert info.control_type == "Button"
    assert info.name == "OK"
    assert info.automation_id == "btnOK"
    assert info.bounds == Rect(1, 2, 30, 40)
    assert info.enabled is True
    assert info.offscreen is False
    assert info.keyboard_focusable is True
    assert info.has_focus is False
    assert info.help_text == "help"
    assert info.hwnd == 123
    assert info.pid == 456
