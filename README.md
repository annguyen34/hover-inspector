# Hover Inspector

A native Windows UI inspector that captures the element under the mouse after a
short dwell (1 second by default), draws a click-through highlight around it,
and shows the locator fields in a small always-on-top panel.

It uses **UI Automation through `pywinauto` plus `win32gui`/`win32api`**. There
is no Appium session, no WinAppDriver, and no full-tree walk, so a capture is one
point lookup plus a handful of property reads instead of a multi-second driver
round trip.

This directory is self-contained and is intended to be the root of its own
repository. Copy its contents (not a parent `.venv`) into a new folder, then
initialise git there.

## Project layout

```text
hover-inspector/
├── hover_inspector/
│   ├── __init__.py
│   ├── app.py          # Tk UI, dwell loop, worker thread, overlay
│   ├── native.py       # Win32 + UIA probing and overlay helpers
│   ├── model.py        # ElementInfo, Rect, locator/JSON formatting (pure)
│   └── dwell.py        # Cursor-dwell state machine (pure)
├── tests/
│   └── test_pure.py    # Tests that do not need a window
├── pyproject.toml
├── ruff.toml
├── .gitignore
└── README.md
```

## Requirements

- Windows 10 or 11
- Python 3.11 or newer, with Tk 8.6 (the standard python.org installer includes it)
- Access to the target application at the same integrity level

## Install

From this directory:

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install --upgrade pip
.venv\Scripts\python -m pip install -e ".[dev]"
```

Run the app:

```powershell
.venv\Scripts\hover-inspector
```

Or run the module directly:

```powershell
.venv\Scripts\python -m hover_inspector.app
```

During development, `python -m hover_inspector.app` from the project root is
enough once the dependencies are installed.

## Common variants

```powershell
# Only capture Revit controls.
hover-inspector --process revit.exe

# Wait 1.5 s, read 5 ancestor levels.
hover-inspector --delay 1.5 --chain 5

# No highlight overlay (panel only).
hover-inspector --no-overlay

# Start without keeping the panel above other windows.
hover-inspector --no-topmost

# Print probe timings and provider errors.
hover-inspector --debug
```

Smoke-tested on Windows with Python 3.13 and pywinauto 0.6.9, including a
vertical multi-monitor layout whose virtual screen has a negative origin.

## What you do

1. Start the app. The panel opens near the right edge of the screen.
2. Move the mouse over a control and leave it still for the hover delay.
3. The panel fills in and a cyan rectangle appears around the element.
4. Choose a locator style in the **Locator** combo box, then click **Copy
   locator** for a ready-to-paste locator. **Copy JSON** copies every captured
   field.
5. Move the mouse more than the tolerance (3 px by default) to re-arm.

The panel area itself is ignored, so you can move the mouse onto the panel to
click buttons without starting a new capture. Use **Pause** (F8) to freeze the
last capture while you work, **Refresh** (F5) to re-read the same point, and
**Clear** (Esc) to drop the highlight.

Keyboard shortcuts while the panel has focus:

| Key        | Action                    |
| ---------- | ------------------------- |
| `F5`       | Re-probe the last point   |
| `F8`       | Pause / resume            |
| `Esc`      | Clear the capture         |
| `Ctrl+L`   | Copy the locator          |
| `Ctrl+Q`   | Quit                      |

Right-click any field for **Copy value**, **Copy locator** or **Copy JSON**.
Double-click an ancestor row to copy that ancestor's locator.

## What the panel shows

| Field         | Source                                                        |
| ------------- | ------------------------------------------------------------- |
| Control type  | UIA `ControlType` (or the Win32 class mapping on fallback)    |
| Name          | UIA `Name` (or `GetWindowText` on fallback)                   |
| Automation ID | UIA `AutomationId`                                            |
| Class         | UIA `ClassName`                                               |
| Bounds        | UIA `BoundingRectangle`, `left,top,width x height`            |
| Process       | PID owner resolved through `psutil`, e.g. `revit.exe (pid …)` |
| Window        | Top-level window title for the element's HWND                 |
| HWND          | Native window handle, when the element has one                |
| Framework     | UIA `FrameworkId` (`Win32`, `WPF`, `WinForms`, …)             |
| State         | visible/offscreen, enabled/disabled, focus, password, source  |

`HelpText`, `AccessKey` and `AcceleratorKey` are included in the copied JSON
but are not shown in the compact panel.

The **Ancestors** table shows the captured element plus its parent chain
(innermost first) using the same fields, so you can see which container to
anchor a locator on.

## Locator output

`Copy locator` picks the strongest identifier available:

1. `AutomationId`
2. `Name`
3. `ControlType`
4. `ClassName`

Then it formats the locator in one of three common styles. Pick the style in the
**Locator** combo box or with `--locator-format`.

### `appium` (default)

The Appium/WinAppDriver wire shape. This is only a portable JSON representation;
the inspector does not start an Appium session.

```json
{
  "using": "accessibility id",
  "value": "btnOK"
}
```

For a control with only a `ControlType`, the strategy becomes `xpath`:

```json
{
  "using": "xpath",
  "value": "//Button"
}
```

### `pywinauto`

A kwargs dict for `child_window(**locator)`:

```python
{"auto_id": "btnOK", "control_type": "Button", "class_name": "Button"}
```

Use it as:

```python
dlg.child_window(
    **{
        "auto_id": "btnOK",
        "control_type": "Button",
        "class_name": "Button",
    }
)
```

### `tuple`

A `(strategy, value)` pair for `driver.find_element(*locator)`:

```python
("accessibility id", "btnOK")
```

If your locator schema is different, edit `format_locator()` in
`hover_inspector/model.py`.

## Flags

| Flag             | Default | Meaning                                                               |
| ---------------- | ------- | --------------------------------------------------------------------- |
| `--delay S`      | `1.0`   | Seconds the cursor must rest before a capture.                        |
| `--tolerance N`  | `3`     | Pixels the cursor may jitter without re-arming.                       |
| `--chain N`      | `3`     | Ancestor levels to read; `0` disables the ancestor walk.              |
| `--process NAME` | (any)   | Only accept elements owned by this process; `revit` matches `revit.exe`. |
| `--locator-format STYLE` | `appium` | Locator style: `appium`, `pywinauto` or `tuple`.              |
| `--no-overlay`   | off     | Do not create the highlight window.                                   |
| `--no-topmost`   | off     | Do not keep the panel above other windows.                            |
| `--debug`        | off     | Print probe timings and provider errors to stderr.                    |
| `--version`      |         | Print the version.                                                    |

## How it works (and why it is fast)

1. **Poll the cursor cheaply.** Every 40 ms the Tk loop calls
   `win32api.GetCursorPos()` and feeds a `DwellTracker`. No UIA call happens
   while the mouse moves.
2. **Dwell fires once.** After the cursor stays within the tolerance for
   `--delay`, the tracker returns `True` exactly once. Moving re-arms it. This
   is deterministic, unlike `EVENT_OBJECT_HOVER`, which depends on each app's
   provider and the OS hover timer.
3. **Probe off the UI thread.** A single worker thread owns the COM/UIA objects
   (STA via `pythoncom.CoInitialize`) and calls
   `Desktop(backend="uia").from_point(x, y)`. That is the pywinauto 0.6.9 API;
   the older `element_from_point` name is not part of that `Desktop` class.
4. **Read only the fields the panel needs.** `UIAElementInfo` exposes Name,
   AutomationId, ClassName, ControlType, Bounds, Enabled, Visible, HWND, PID and
   FrameworkId. The raw `IUIAutomationElement` behind it supplies focus, help
   text, access keys and password state. A missing property becomes blank, not
   an exception.
5. **Walk a few ancestors.** `ControlViewWalker`/`UIAElementInfo.parent` is used
   for `--chain N`, default 3, so the locator author can see containers.
6. **Fall back to Win32.** If UIA has no provider or the element vanished
   mid-read, `WindowFromPoint` + `GetClassName` + `GetWindowText` +
   `GetWindowRect` still produce a usable rectangle and class. The panel marks
   this as **Win32 fallback**.
7. **Hide the overlay while probing.** The highlight window is withdrawn before
   the UIA point lookup and redrawn after the result, so the inspector never
   captures its own rectangle.
8. **Be DPI correct.** `SetProcessDpiAwarenessContext(PER_MONITOR_AWARE_V2)` is
   called before Tk starts, so cursor coordinates, UIA bounds and overlay pixels
   all use the same physical-pixel space on scaled and multi-monitor setups.

### Cost

- Idle: one `GetCursorPos` every 40 ms; effectively free.
- Per capture: one UIA `ElementFromPoint` plus roughly a dozen property reads,
  plus up to `--chain` parent reads. The first capture after launch pays a
  one-time COM/UIA initialization cost (about 0.2 s on the test machine); later
  captures are typically 10–25 ms with `--chain 3`. Heavy provider trees (large
  Revit panes, some Chromium hosts) can take longer. The UI stays responsive
  because the probe is on a worker.
- No Appium HTTP round trips, no WinAppDriver session, no full descendant walk.

If a provider hangs, the panel reports a timeout after 10 seconds. The worker
thread is daemonized so quitting the app does not hang.

## Troubleshooting

**`pywin32 is not installed` / `pywinauto` import errors.**
Install the project dependencies with `pip install -e ".[dev]"`. The app needs
`pywin32`, `pywinauto`, `comtypes` and `psutil`.

**`tkinter is required`.** Use the standard python.org Windows build or a Python
installation that includes Tk. Some minimal distributions omit Tk.

**Nothing is captured over an elevated app.** UIA across integrity levels is
restricted. Run the inspector from an elevated shell as well.

**The panel says `Win32 fallback`.** The app or control does not expose a UIA
provider at that point. You still get class/title/rectangle, but not
AutomationId or the UIA control type. Custom-drawn, DirectX and some
canvas-based surfaces are the usual cause.

**The overlay is disabled at startup.** Tk's `-transparentcolor` was not
available in this Tcl/Tk build. The panel still works; use `--no-overlay` to
avoid the warning, or run a standard Python build with Tk 8.6.

**Coordinates look offset on a high-DPI monitor.** The app requests per-monitor
v2 awareness. If the executable has a Windows compatibility override
("Override high DPI scaling behavior"), clear it for `python.exe`.

**A probe timed out.** A UIA provider blocked. Restart the app; the stuck
provider is usually the target app, not the inspector. Try `--chain 0` and a
longer `--delay` to reduce pressure.

**The highlight covers the target I want to hover.** Move the panel or uncheck
**On top**; the overlay itself is click-through and is withdrawn during each
probe.

## Tests

The pure modules are testable without Windows:

```powershell
python -m pytest -q
```

The tests cover dwell timing/re-arming, locator preference, JSON shape,
process filtering, and the `UIAElementInfo` property mapping using fake objects.
