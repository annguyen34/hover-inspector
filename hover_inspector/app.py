"""Hover Inspector: pick a Windows UI element by resting the mouse on it.

The app polls the cursor cheaply, waits for a configurable dwell (1 second by
default), then asks UIA for the element under the pointer in a background
thread. A click-through highlight window marks the captured rectangle and a
panel shows the locator fields plus a ready-to-paste locator.

No Appium, no WinAppDriver, no full-tree walk.
"""

from __future__ import annotations

import argparse
import queue
import sys
import threading
import time
import traceback

sys.coinit_flags = 2  # STA, before pywinauto/comtypes/pythoncom are imported.

try:
    import tkinter as tk
    from tkinter import font as tkfont
    from tkinter import ttk
except ImportError as exc:  # pragma: no cover - Windows Python ships Tk
    raise SystemExit("tkinter is required to run the Hover Inspector UI.") from exc

try:
    import pythoncom
except ImportError:  # pragma: no cover - pywin32 is Windows-only
    pythoncom = None  # type: ignore[assignment]

from . import native
from .dwell import DwellTracker
from .model import LOCATOR_FORMATS, ElementInfo, format_locator, process_matches

APP_TITLE = "Hover Inspector"
VERSION = "1.0.0"
TICK_MS = 40
PROBE_TIMEOUT_S = 10.0
COLOR_KEY = "#ff00ff"
UIA_COLOR = "#00d4ff"
WIN32_COLOR = "#ffb300"
SELF_ROW_COLOR = "#e8f4ff"

STATUS_COLORS = {
    "idle": "#666666",
    "busy": "#0b5cad",
    "ok": "#0a7a3d",
    "warn": "#b26a00",
    "err": "#b00020",
    "paused": "#6a1b9a",
}
TOAST_COLORS = {
    "info": "#444444",
    "ok": "#0a7a3d",
    "warn": "#b26a00",
    "err": "#b00020",
}


class ProbeWorker:
    """One background thread that owns the UIA COM objects."""

    def __init__(self, *, chain_depth: int, debug: bool = False):
        self.chain_depth = chain_depth
        self.debug = debug
        self.requests: queue.Queue[tuple[int, int] | None] = queue.Queue(maxsize=1)
        self.results: queue.Queue[tuple[int, int, ElementInfo, float]] = queue.Queue(maxsize=8)
        self._thread = threading.Thread(target=self._run, name="hover-inspector-uia", daemon=True)

    @property
    def is_alive(self) -> bool:
        return self._thread.is_alive()

    def start(self) -> None:
        self._thread.start()

    def request(self, x: int, y: int) -> bool:
        """Queue one probe. Returns False when the worker is still busy."""
        try:
            self.requests.put_nowait((int(x), int(y)))
            return True
        except queue.Full:
            return False

    def stop(self) -> None:
        try:
            self.requests.put_nowait(None)
        except queue.Full:
            try:
                self.requests.get_nowait()
            except queue.Empty:
                pass
            try:
                self.requests.put_nowait(None)
            except queue.Full:
                pass
        if self._thread.is_alive():
            self._thread.join(timeout=0.5)

    def _run(self) -> None:
        if pythoncom is not None:
            try:
                pythoncom.CoInitialize()
            except Exception as exc:
                if self.debug:
                    print(f"[hover] COM init failed: {exc}", file=sys.stderr)

        try:
            probe = native.ElementProbe(chain_depth=self.chain_depth)
            while True:
                item = self.requests.get()
                if item is None:
                    break
                x, y = item
                started = time.perf_counter()
                try:
                    info = probe.probe(x, y)
                except Exception as exc:
                    info = ElementInfo(source="error", error=f"{type(exc).__name__}: {exc}")
                elapsed = (time.perf_counter() - started) * 1000
                info.probe_ms = elapsed
                if self.debug:
                    print(
                        f"[hover] ({x},{y}) {elapsed:.1f} ms source={info.source} "
                        f"type={info.control_type!r} name={info.name!r}",
                        file=sys.stderr,
                    )
                try:
                    self.results.put_nowait((x, y, info, elapsed))
                except queue.Full:
                    try:
                        self.results.get_nowait()
                    except queue.Empty:
                        pass
                    try:
                        self.results.put_nowait((x, y, info, elapsed))
                    except queue.Full:
                        pass
        except Exception as exc:
            if self.debug:
                traceback.print_exc()
            try:
                self.results.put_nowait((0, 0, ElementInfo(source="error", error=str(exc)), 0.0))
            except queue.Full:
                pass
        finally:
            if pythoncom is not None:
                try:
                    pythoncom.CoUninitialize()
                except Exception:
                    pass


class HighlightOverlay:
    """A click-through, topmost rectangle drawn around the captured element."""

    def __init__(self, root: tk.Misc):
        self.root = root
        self.window = tk.Toplevel(root)
        self.window.withdraw()
        self.window.overrideredirect(True)
        self.window.configure(bg=COLOR_KEY)
        self.window.attributes("-topmost", True)
        try:
            self.window.attributes("-transparentcolor", COLOR_KEY)
        except tk.TclError as exc:
            raise RuntimeError(f"Tk transparentcolor is unavailable: {exc}") from exc

        self.canvas = tk.Canvas(self.window, bg=COLOR_KEY, highlightthickness=0, bd=0)
        self.canvas.pack(fill="both", expand=True)
        self._hwnd = 0
        self._geometry = native.virtual_screen()
        self.canvas.configure(width=self._geometry[2], height=self._geometry[3])
        family = (
            "Segoe UI" if "Segoe UI" in tkfont.families(root) else tkfont.nametofont("TkDefaultFont").actual("family")
        )
        self._font = tkfont.Font(root=root, family=family, size=9, weight="bold")
        self._enabled = True
        self._mapped = False

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = bool(enabled)
        if not self._enabled:
            self.hide()

    def show(self, info: ElementInfo) -> None:
        """Draw the highlight for an element; hides itself for empty bounds."""
        if not self._enabled or info.bounds is None or info.bounds.is_empty:
            self.hide()
            return

        self._reposition_if_needed()
        if not self._mapped:
            self.window.deiconify()
            self.window.update_idletasks()
            # Tk creates the real top-level wrapper only after mapping.
            self._hwnd = native.toplevel_hwnd(self.window)
            native.make_click_through(self._hwnd)
            native.make_child_click_through(int(self.window.winfo_id()))
            self._mapped = True
        self.window.update_idletasks()
        native.place_topmost(self._hwnd, *self._geometry)

        self.canvas.delete("all")
        bounds = info.bounds
        vx, vy, _, _ = self._geometry
        x0 = bounds.left - vx
        y0 = bounds.top - vy
        x1 = bounds.right - vx
        y1 = bounds.bottom - vy
        color = UIA_COLOR if info.source == "uia" else WIN32_COLOR
        self.canvas.create_rectangle(x0 - 1, y0 - 1, x1 + 1, y1 + 1, outline="#001018", width=3)
        self.canvas.create_rectangle(x0, y0, x1, y1, outline=color, width=2)

        label = f"{info.control_type}: {info.display_name}"
        if info.source != "uia":
            label += "  [Win32]"
        self._draw_label(label, x0=x0, y0=y0, x1=x1, y1=y1, color=color)

    def hide(self) -> None:
        self.canvas.delete("all")
        if self._mapped:
            self.window.withdraw()
            self._mapped = False

    def destroy(self) -> None:
        try:
            self.window.destroy()
        except tk.TclError:
            pass

    def _reposition_if_needed(self) -> None:
        screen = native.virtual_screen()
        if screen == self._geometry:
            return
        self._geometry = screen
        self.canvas.configure(width=screen[2], height=screen[3])

    def _draw_label(self, text: str, *, x0: int, y0: int, x1: int, y1: int, color: str) -> None:
        width, height = self._geometry[2], self._geometry[3]
        max_text_width = max(80, width - 20)
        while self._font.measure(text) > max_text_width and len(text) > 12:
            text = text[:-2] + "…"

        pad_x, pad_y = 8, 4
        text_width = self._font.measure(text)
        line_height = self._font.metrics("linespace")
        box_width = text_width + pad_x * 2
        box_height = line_height + pad_y * 2
        left = max(0, min(x0, width - box_width))
        top = y0 - box_height - 4
        if top < 0:
            top = y1 + 4
        top = max(0, min(top, height - box_height))

        self.canvas.create_rectangle(
            left, top, left + box_width, top + box_height, fill="#0b1a20", outline=color, width=1
        )
        self.canvas.create_text(left + pad_x, top + pad_y, anchor="nw", text=text, fill="#ffffff", font=self._font)


class HoverInspectorApp:
    """Tk panel plus the polling loop that drives the dwell tracker."""

    def __init__(self, root: tk.Tk, args: argparse.Namespace):
        self.root = root
        self.args = args
        self.ui_scale = max(1.0, native.system_dpi() / 96.0)
        self._closed = False
        self._paused = False
        self._in_flight = False
        self._request_started: float | None = None
        self._pending_point: tuple[int, int] | None = None
        self._last_info: ElementInfo | None = None
        self._last_point: tuple[int, int] | None = None
        self._status_kind = "idle"
        self._toast_after: str | None = None
        self._tick_after: str | None = None
        self._was_over_panel = False
        self._tree_info: dict[str, ElementInfo] = {}
        self._context_entry: ttk.Entry | None = None
        self._overlay_error = ""

        self.dwell = DwellTracker(delay=args.delay, tolerance=args.tolerance)
        self.worker = ProbeWorker(chain_depth=args.chain, debug=args.debug)

        self._configure_window()
        self.root.update_idletasks()
        try:
            self._panel_hwnd = native.toplevel_hwnd(self.root)
        except Exception:
            self._panel_hwnd = 0

        self.overlay: HighlightOverlay | None = None
        if not args.no_overlay:
            try:
                self.overlay = HighlightOverlay(root)
            except Exception as exc:
                self._overlay_error = str(exc)

        self._build()
        self._update_hint()
        self.root.report_callback_exception = self._report_exception
        self._bind_keys()
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.worker.start()
        self._status(self._idle_text(), "idle")
        if self._overlay_error:
            self._toast(f"Overlay disabled: {self._overlay_error}", "warn", 6000)
        self._tick_after = self.root.after(TICK_MS, self._tick)

    # --- window / layout -------------------------------------------------

    def _configure_window(self) -> None:
        self.root.title(f"{APP_TITLE} {VERSION}")
        try:
            self.root.tk.call("tk", "scaling", max(1.0, native.system_dpi() / 72.0))
        except tk.TclError:
            pass

        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        width = min(int(560 * self.ui_scale), max(460, screen_w - 80))
        height = min(int(700 * self.ui_scale), max(560, screen_h - 120))
        x = max(0, screen_w - width - int(48 * self.ui_scale))
        y = max(0, (screen_h - height) // 2)
        self.root.geometry(f"{width}x{height}+{x}+{y}")
        self.root.minsize(
            min(int(500 * self.ui_scale), max(400, screen_w - 80)),
            min(int(560 * self.ui_scale), max(420, screen_h - 120)),
        )
        if not self.args.no_topmost:
            self.root.attributes("-topmost", True)

    def _build(self) -> None:
        scale = self.ui_scale
        pad = max(8, int(12 * scale))
        family = (
            "Segoe UI"
            if "Segoe UI" in tkfont.families(self.root)
            else tkfont.nametofont("TkDefaultFont").actual("family")
        )
        mono = (
            "Consolas"
            if "Consolas" in tkfont.families(self.root)
            else tkfont.nametofont("TkFixedFont").actual("family")
        )
        self._mono_font = (mono, 9)

        style = ttk.Style(self.root)
        for theme in ("vista", "winnative", "clam"):
            if theme in style.theme_names():
                style.theme_use(theme)
                break
        style.configure("Title.TLabel", font=(family, 15, "bold"))
        style.configure("Headline.TLabel", font=(family, 11, "bold"), foreground="#0b5cad")
        style.configure("Hint.TLabel", font=(family, 9), foreground="#666666")
        style.configure("Status.TLabel", font=(family, 9))
        style.configure("Field.TLabel", font=(family, 9), foreground="#444444")
        style.configure("Treeview", font=(family, 9), rowheight=int(22 * scale))
        style.configure("Treeview.Heading", font=(family, 9, "bold"))

        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)
        outer = ttk.Frame(self.root, padding=(pad, pad, pad, pad))
        outer.grid(row=0, column=0, sticky="nsew")
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(6, weight=1)

        header = ttk.Frame(outer)
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(0, weight=1)
        ttk.Label(header, text=APP_TITLE, style="Title.TLabel").grid(row=0, column=0, sticky="w")
        self.status_var = tk.StringVar(value="")
        self.status_label = ttk.Label(
            header,
            textvariable=self.status_var,
            style="Status.TLabel",
            anchor="e",
            justify="right",
            wraplength=int(260 * scale),
        )
        self.status_label.grid(row=0, column=1, sticky="e")

        self.headline_var = tk.StringVar(value="No element captured")
        ttk.Label(
            outer,
            textvariable=self.headline_var,
            style="Headline.TLabel",
            anchor="w",
            justify="left",
            wraplength=int(520 * scale),
        ).grid(row=1, column=0, sticky="ew", pady=(6, 0))

        self.hint_var = tk.StringVar(value="")
        ttk.Label(
            outer,
            textvariable=self.hint_var,
            style="Hint.TLabel",
            anchor="w",
            justify="left",
            wraplength=int(520 * scale),
        ).grid(row=2, column=0, sticky="ew", pady=(2, 0))

        ttk.Separator(outer, orient="horizontal").grid(row=3, column=0, sticky="ew", pady=(8, 4))

        fields = ttk.Frame(outer)
        fields.grid(row=4, column=0, sticky="ew", pady=(2, 2))
        fields.columnconfigure(1, weight=1)
        rows = (
            ("control_type", "Control type"),
            ("name", "Name"),
            ("automation_id", "Automation ID"),
            ("class_name", "Class"),
            ("bounds", "Bounds"),
            ("process", "Process"),
            ("window_title", "Window"),
            ("hwnd", "HWND"),
            ("framework_id", "Framework"),
            ("state", "State"),
        )
        self.vars: dict[str, tk.StringVar] = {}
        self._entry_menu = tk.Menu(self.root, tearoff=0)
        self._entry_menu.add_command(label="Copy value", command=self._copy_context_value)
        self._entry_menu.add_command(label="Copy locator", command=self.copy_locator)
        self._entry_menu.add_command(label="Copy JSON", command=self.copy_json)
        for index, (key, label) in enumerate(rows):
            ttk.Label(fields, text=label, style="Field.TLabel", width=13, anchor="w").grid(
                row=index, column=0, sticky="w", pady=1
            )
            var = tk.StringVar()
            entry = ttk.Entry(fields, textvariable=var, state="readonly", font=self._mono_font)
            entry.grid(row=index, column=1, sticky="ew", pady=1)
            self.vars[key] = var
            self._bind_entry_menu(entry)

        buttons = ttk.Frame(outer)
        buttons.grid(row=5, column=0, sticky="ew", pady=(6, 4))
        buttons.columnconfigure(5, weight=1)
        ttk.Button(buttons, text="Copy locator", command=self.copy_locator).grid(row=0, column=0, padx=(0, 6))
        ttk.Button(buttons, text="Copy JSON", command=self.copy_json).grid(row=0, column=1, padx=(0, 6))
        ttk.Button(buttons, text="Refresh (F5)", command=self.refresh).grid(row=0, column=2, padx=(0, 6))
        ttk.Label(buttons, text="Locator", style="Field.TLabel").grid(row=0, column=3, padx=(8, 4))
        self.locator_format_var = tk.StringVar(value=self.args.locator_format)
        locator_format = ttk.Combobox(
            buttons,
            textvariable=self.locator_format_var,
            values=LOCATOR_FORMATS,
            state="readonly",
            width=10,
        )
        locator_format.grid(row=0, column=4, sticky="w")

        ancestors = ttk.LabelFrame(outer, text="Ancestors", padding=(max(6, int(8 * scale)), max(4, int(6 * scale))))
        ancestors.grid(row=6, column=0, sticky="nsew", pady=(4, 4))
        ancestors.columnconfigure(0, weight=1)
        ancestors.rowconfigure(0, weight=1)
        columns = ("level", "type", "name", "automation_id", "class")
        self.tree = ttk.Treeview(ancestors, columns=columns, show="headings", height=4)
        headings = {
            "level": "#",
            "type": "Type",
            "name": "Name",
            "automation_id": "Automation ID",
            "class": "Class",
        }
        widths = {
            "level": int(32 * scale),
            "type": int(82 * scale),
            "name": int(150 * scale),
            "automation_id": int(115 * scale),
            "class": int(90 * scale),
        }
        for column in columns:
            self.tree.heading(column, text=headings[column])
            self.tree.column(
                column,
                width=widths[column],
                minwidth=max(28, int(30 * scale)),
                stretch=column == "name",
            )
        self.tree.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(ancestors, orient="vertical", command=self.tree.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.tag_configure("self", background=SELF_ROW_COLOR)
        self.tree.bind("<Double-1>", self._copy_tree_row)
        ttk.Label(
            ancestors,
            text="Double-click a row to copy its locator.",
            style="Hint.TLabel",
            anchor="w",
        ).grid(row=1, column=0, sticky="w", pady=(4, 0))

        options = ttk.Frame(outer)
        options.grid(row=7, column=0, sticky="ew", pady=(4, 0))
        ttk.Label(options, text="Hover delay", style="Field.TLabel").grid(row=0, column=0, sticky="w")
        self.delay_var = tk.StringVar(value=f"{self.dwell.delay:g}")
        self.delay_spin = ttk.Spinbox(options, from_=0.1, to=30.0, increment=0.1, width=6, textvariable=self.delay_var)
        self.delay_spin.grid(row=0, column=1, sticky="w", padx=(6, 4))
        self.delay_spin.bind("<Return>", lambda _event: self._normalize_delay())
        self.delay_spin.bind("<FocusOut>", lambda _event: self._normalize_delay())
        self.delay_var.trace_add("write", self._on_delay_change)
        ttk.Label(options, text="s", style="Field.TLabel").grid(row=0, column=2, sticky="w")

        self.overlay_var = tk.BooleanVar(value=self.overlay is not None)
        ttk.Checkbutton(options, text="Overlay", variable=self.overlay_var, command=self._toggle_overlay).grid(
            row=0, column=3, padx=(16, 0)
        )
        self.topmost_var = tk.BooleanVar(value=not self.args.no_topmost)
        ttk.Checkbutton(options, text="On top", variable=self.topmost_var, command=self._toggle_topmost).grid(
            row=0, column=4, padx=(10, 0)
        )
        self.pause_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(options, text="Pause", variable=self.pause_var, command=self._toggle_pause).grid(
            row=0, column=5, padx=(10, 0)
        )
        options.columnconfigure(6, weight=1)
        ttk.Button(options, text="Clear (Esc)", command=self.clear).grid(row=0, column=7, padx=(6, 0))
        ttk.Button(options, text="Quit", command=self.close).grid(row=0, column=8, padx=(6, 0))

        self.toast_var = tk.StringVar(value="")
        self.toast_label = ttk.Label(
            outer,
            textvariable=self.toast_var,
            style="Hint.TLabel",
            anchor="w",
            wraplength=int(520 * scale),
        )
        self.toast_label.grid(row=8, column=0, sticky="ew", pady=(6, 0))

    def _bind_entry_menu(self, entry: ttk.Entry) -> None:
        entry.bind("<Button-3>", lambda event, widget=entry: self._show_entry_menu(event, widget))
        entry.bind("<Control-a>", lambda _event, widget=entry: self._select_all(widget))

    def _bind_keys(self) -> None:
        self.root.bind_all("<F5>", lambda _event: self.refresh())
        self.root.bind_all("<F8>", lambda _event: self._toggle_pause_key())
        self.root.bind_all("<Escape>", lambda _event: self.clear())
        self.root.bind_all("<Control-q>", lambda _event: self.close())
        self.root.bind_all("<Control-l>", lambda _event: self.copy_locator())

    # --- status / toast --------------------------------------------------

    def _idle_text(self) -> str:
        return f"Hover an element for {self.dwell.delay:g}s…"

    def _update_hint(self) -> None:
        self.hint_var.set(
            f"Hover an element for {self.dwell.delay:g} s. Move the mouse away to re-arm. Right-click a field to copy."
        )

    def _status(self, text: str, kind: str = "idle") -> None:
        if self._closed:
            return
        self._status_kind = kind
        self.status_var.set(text)
        try:
            self.status_label.configure(foreground=STATUS_COLORS.get(kind, "#333333"))
        except tk.TclError:
            pass

    def _toast(self, text: str, kind: str = "info", ms: int = 2500) -> None:
        if self._closed:
            return
        self.toast_var.set(text)
        try:
            self.toast_label.configure(foreground=TOAST_COLORS.get(kind, TOAST_COLORS["info"]))
        except tk.TclError:
            pass
        if self._toast_after is not None:
            try:
                self.root.after_cancel(self._toast_after)
            except tk.TclError:
                pass
        self._toast_after = self.root.after(ms, self._clear_toast)

    def _clear_toast(self) -> None:
        self._toast_after = None
        if not self._closed:
            self.toast_var.set("")

    def _report_exception(self, exc_type, exc_value, exc_tb) -> None:
        if self.args.debug:
            traceback.print_exception(exc_type, exc_value, exc_tb)
        self._status(f"UI error: {exc_type.__name__}: {exc_value}", "err")

    # --- polling loop ----------------------------------------------------

    def _tick(self) -> None:
        if self._closed:
            return
        self._poll_results()
        self._check_probe_health()
        self._run_pending()

        if not self._paused:
            try:
                x, y = native.cursor_pos()
            except Exception as exc:
                self._status(f"Win32 error: {exc}", "err")
            else:
                over_panel = self._over_panel(x, y)
                if over_panel != self._was_over_panel:
                    self._was_over_panel = over_panel
                    if over_panel:
                        self._status("Over the inspector — move onto a target", "idle")
                    elif self._status_kind == "idle":
                        self._status(self._idle_text(), "idle")
                if over_panel:
                    self.dwell.reset()
                elif self.dwell.update(x, y):
                    if self._in_flight:
                        self._pending_point = (x, y)
                    else:
                        self._request(x, y)
        self._tick_after = self.root.after(TICK_MS, self._tick)

    def _run_pending(self) -> None:
        """Run a dwell that fired while the previous probe was still busy."""
        if self._paused or self._pending_point is None or self._in_flight:
            return
        x, y = self._pending_point
        self._pending_point = None
        try:
            cursor_x, cursor_y = native.cursor_pos()
        except Exception:
            return
        if abs(cursor_x - x) > self.dwell.tolerance or abs(cursor_y - y) > self.dwell.tolerance:
            return
        if self._request(x, y):
            self.dwell.mark_fired(x, y)

    def _over_panel(self, x: int, y: int) -> bool:
        if self._panel_hwnd:
            try:
                return native.point_in_window(x, y, self._panel_hwnd)
            except Exception:
                return False
        rx, ry = self.root.winfo_rootx(), self.root.winfo_rooty()
        return rx <= x < rx + self.root.winfo_width() and ry <= y < ry + self.root.winfo_height()

    def _poll_results(self) -> None:
        while True:
            try:
                x, y, info, elapsed = self.worker.results.get_nowait()
            except queue.Empty:
                break
            self._handle_result(x, y, info, elapsed)

    def _check_probe_health(self) -> None:
        if not self._in_flight:
            return
        if not self.worker.is_alive:
            self._in_flight = False
            self._request_started = None
            self._pending_point = None
            self._status("Probe worker stopped unexpectedly", "err")
            return
        if self._request_started is None:
            return
        if time.monotonic() - self._request_started > PROBE_TIMEOUT_S:
            self._in_flight = False
            self._request_started = None
            self._pending_point = None
            self.dwell.reset()
            self._status(f"Probe timed out after {PROBE_TIMEOUT_S:g}s", "err")
            self._restore_overlay()

    def _request(self, x: int, y: int) -> bool:
        if self.overlay is not None:
            self.overlay.hide()
        if not self.worker.request(x, y):
            self._in_flight = False
            self._request_started = None
            self.dwell.reset()
            self._toast("Probe worker is busy; move the mouse and try again", "warn")
            self._restore_overlay()
            return False
        self._in_flight = True
        self._request_started = time.monotonic()
        self._status("Inspecting…", "busy")
        return True

    def _restore_overlay(self) -> None:
        if self._last_info is None or self.overlay is None or not self.overlay_var.get():
            return
        try:
            self.overlay.show(self._last_info)
        except Exception:
            pass

    def _handle_result(self, x: int, y: int, info: ElementInfo, elapsed: float) -> None:
        self._in_flight = False
        self._request_started = None
        self._last_point = (x, y)

        if info.source == "error" or not self._has_content(info):
            self._status(f"Probe failed: {info.error or 'no element at the cursor'}", "err")
            if self.overlay is not None:
                self.overlay.hide()
            return

        if self.args.process and info.process and not process_matches(info.process, self.args.process):
            self._status(f"Filtered {info.process} (wanted {self.args.process})", "warn")
            if self.overlay is not None:
                self.overlay.hide()
            return

        if self._last_info is not None and info.signature == self._last_info.signature:
            self._status(f"Same element · {elapsed:.0f} ms", "ok")
            self._restore_overlay()
            return

        self._show_info(info)
        if info.source == "win32":
            detail = f" · {info.error[:90]}" if info.error else ""
            self._status(f"Win32 fallback · {elapsed:.0f} ms{detail}", "warn")
        elif self.args.process and not info.process:
            self._status(f"{info.control_type} · process unknown · {elapsed:.0f} ms", "warn")
        else:
            self._status(f"{info.control_type} · {info.process or '?'} · {elapsed:.0f} ms", "ok")

    @staticmethod
    def _has_content(info: ElementInfo) -> bool:
        return bool(
            (info.bounds is not None and not info.bounds.is_empty)
            or info.hwnd
            or info.name
            or info.automation_id
            or info.class_name
        )

    # --- rendering -------------------------------------------------------

    def _show_info(self, info: ElementInfo) -> None:
        self._last_info = info
        self.headline_var.set(f"{info.control_type}  ·  {info.display_name}")
        self.vars["control_type"].set(info.control_type)
        self.vars["name"].set(self._one_line(info.name))
        self.vars["automation_id"].set(self._one_line(info.automation_id))
        self.vars["class_name"].set(self._one_line(info.class_name))
        self.vars["bounds"].set(str(info.bounds) if info.bounds else "")
        self.vars["process"].set(info.process_text())
        self.vars["window_title"].set(self._one_line(info.window_title))
        self.vars["hwnd"].set(info.hwnd_text)
        self.vars["framework_id"].set(self._one_line(info.framework_id))
        self.vars["state"].set(info.state_text())
        self._fill_tree(info)
        if self.overlay is not None and self.overlay_var.get():
            try:
                self.overlay.show(info)
            except Exception as exc:
                self._toast(f"Overlay error: {exc}", "warn")

    def _clear_fields(self) -> None:
        for var in self.vars.values():
            var.set("")
        self.headline_var.set("No element captured")

    @staticmethod
    def _one_line(text: str) -> str:
        return " ".join((text or "").split())

    def _fill_tree(self, info: ElementInfo | None) -> None:
        for item in self.tree.get_children():
            self.tree.delete(item)
        self._tree_info.clear()
        if info is None:
            return

        def add(level: int, element: ElementInfo, tag: str | None = None) -> None:
            values = (
                str(level),
                element.control_type,
                element.display_name,
                element.automation_id,
                element.class_name,
            )
            item = self.tree.insert("", "end", values=values, tags=(tag,) if tag else ())
            self._tree_info[item] = element

        add(0, info, "self")
        for level, ancestor in enumerate(info.ancestors, start=1):
            add(level, ancestor)

    # --- actions ---------------------------------------------------------

    def _copy(self, text: str, message: str) -> None:
        if not text:
            self._toast("Nothing to copy", "warn")
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.root.update_idletasks()
        self._toast(message, "ok")

    def copy_locator(self, info: ElementInfo | None = None) -> None:
        target = info or self._last_info
        if target is None:
            self._toast("Hover an element first", "warn")
            return
        fmt = self.locator_format_var.get()
        try:
            text = format_locator(target, fmt)
        except ValueError as exc:
            self._toast(str(exc), "err")
            return
        if not text:
            self._toast("No Name / AutomationId / ControlType / ClassName to build a locator", "warn")
            return
        self._copy(text, f"{fmt} locator copied")

    def copy_json(self) -> None:
        if self._last_info is None:
            self._toast("Hover an element first", "warn")
            return
        self._copy(self._last_info.to_json(), "Element JSON copied")

    def _copy_context_value(self) -> None:
        entry = self._context_entry
        if entry is None:
            return
        self._copy(entry.get(), "Field copied")

    def _copy_tree_row(self, _event=None) -> None:
        selection = self.tree.selection()
        if not selection:
            return
        info = self._tree_info.get(selection[0])
        if info is not None:
            self.copy_locator(info)

    def _show_entry_menu(self, event, entry: ttk.Entry) -> None:
        self._context_entry = entry
        try:
            self._entry_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self._entry_menu.grab_release()

    @staticmethod
    def _select_all(entry: ttk.Entry) -> str:
        entry.selection_range(0, "end")
        entry.icursor("end")
        return "break"

    def refresh(self) -> None:
        if self._in_flight:
            self._toast("A probe is already running", "warn")
            return
        try:
            x, y = native.cursor_pos()
        except Exception as exc:
            self._toast(f"Win32 error: {exc}", "err")
            return
        if self._over_panel(x, y):
            if self._last_point is None:
                self._toast("Move the cursor over a target first", "warn")
                return
            x, y = self._last_point
        if self._request(x, y):
            self.dwell.mark_fired(x, y)

    def clear(self) -> None:
        self._last_info = None
        self._last_point = None
        self._pending_point = None
        self._clear_fields()
        self._fill_tree(None)
        if self.overlay is not None:
            self.overlay.hide()
        self.dwell.reset()
        self._status("Cleared", "paused" if self._paused else "idle")

    def _toggle_pause(self) -> None:
        self._paused = bool(self.pause_var.get())
        self.dwell.reset()
        if self._paused:
            self._pending_point = None
            self._status("Paused — last capture kept", "paused")
        else:
            self._status(self._idle_text(), "idle")

    def _toggle_pause_key(self) -> None:
        self.pause_var.set(not self.pause_var.get())
        self._toggle_pause()

    def _toggle_overlay(self) -> None:
        enabled = bool(self.overlay_var.get())
        if self.overlay is None:
            self.overlay_var.set(False)
            self._toast("Overlay is unavailable in this session", "warn")
            return
        self.overlay.set_enabled(enabled)
        if enabled and self._last_info is not None:
            try:
                self.overlay.show(self._last_info)
            except Exception as exc:
                self._toast(f"Overlay error: {exc}", "warn")

    def _toggle_topmost(self) -> None:
        try:
            self.root.attributes("-topmost", bool(self.topmost_var.get()))
        except tk.TclError:
            pass

    def _on_delay_change(self, *_args) -> None:
        try:
            value = float(self.delay_var.get())
        except (TypeError, ValueError):
            return
        value = max(0.1, min(30.0, value))
        self.dwell.delay = value
        self.dwell.reset()
        self._update_hint()
        if self._status_kind == "idle":
            self._status(self._idle_text(), "idle")

    def _normalize_delay(self) -> None:
        try:
            value = float(self.delay_var.get())
        except (TypeError, ValueError):
            value = self.dwell.delay
        value = max(0.1, min(30.0, value))
        self.delay_var.set(f"{value:g}")
        self.dwell.delay = value
        self.dwell.reset()
        self._update_hint()

    # --- shutdown --------------------------------------------------------

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._toast_after is not None:
            try:
                self.root.after_cancel(self._toast_after)
            except tk.TclError:
                pass
            self._toast_after = None
        if self._tick_after is not None:
            try:
                self.root.after_cancel(self._tick_after)
            except tk.TclError:
                pass
            self._tick_after = None
        try:
            self.worker.stop()
        except Exception:
            pass
        if self.overlay is not None:
            try:
                self.overlay.destroy()
            except Exception:
                pass
        try:
            self.root.destroy()
        except tk.TclError:
            pass


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hover-inspector",
        description="Inspect the Windows UIA element under the cursor after a short hover.",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=1.0,
        help="seconds the cursor must rest before inspecting (default 1.0)",
    )
    parser.add_argument(
        "--tolerance",
        type=int,
        default=3,
        help="pixels the cursor may move without re-arming the dwell (default 3)",
    )
    parser.add_argument(
        "--chain",
        type=int,
        default=3,
        help="ancestor levels to read for the tree (default 3, 0 disables)",
    )
    parser.add_argument("--process", help="only capture elements from this process, e.g. revit.exe")
    parser.add_argument(
        "--locator-format",
        choices=LOCATOR_FORMATS,
        default="appium",
        help="locator style for Copy locator (default: appium)",
    )
    parser.add_argument("--no-overlay", action="store_true", help="do not draw the highlight rectangle")
    parser.add_argument("--no-topmost", action="store_true", help="do not keep the panel above other windows")
    parser.add_argument("--debug", action="store_true", help="print probe timings and errors to stderr")
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.delay < 0.1:
        parser.error("--delay must be at least 0.1 seconds")
    if args.tolerance < 0:
        parser.error("--tolerance must be zero or greater")
    if args.chain < 0:
        parser.error("--chain must be zero or greater")
    if not native.IS_WINDOWS:
        raise SystemExit("The hover inspector is Windows-only; run it from Windows, not WSL.")
    if not native.HAS_WIN32:
        raise SystemExit('pywin32 is not installed. Run `pip install -e ".[dev]"` or `pip install pywin32`.')

    native.enable_dpi_awareness()
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        raise SystemExit(f"Could not start the Tk UI: {exc}") from exc

    app = HoverInspectorApp(root, args)
    try:
        root.mainloop()
    except KeyboardInterrupt:
        app.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
