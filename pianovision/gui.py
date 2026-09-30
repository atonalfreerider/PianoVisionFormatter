"""Piano Library: a window over the score library and the headset (``python3 -m pianovision gui``).

GTK 4 with libadwaita when it is installed (plain GTK 4 otherwise); stdlib + PyGObject only.
Everything slow runs in a worker thread (one job at a time) with a progress bar, a live log and
Cancel where the work allows it; results come back to the UI through GLib.idle_add.  The logic
lives in :mod:`pianovision.guimodel` and the formatter's own modules.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import threading
import time
import traceback
from typing import Callable, List, Optional

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Graphene", "1.0")
from gi.repository import Gio, GLib, GObject, Graphene, Gtk, Pango  # noqa: E402

try:
    gi.require_version("Adw", "1")
    from gi.repository import Adw  # noqa: E402
except (ValueError, ImportError):
    Adw = None

from . import guimodel as M  # noqa: E402

APP_ID = "org.pianovision.Library"
TITLE = "Piano Library"
POLL_SECONDS = 4
DETAILS_EVERY = 20            # seconds between dumpsys/df probes while connected

FILTERS = [
    ("scores", "Scores"),
    ("all", "Everything (with excluded)"),
    ("build", "Needs a build"),
    ("pv", "Not current in PianoVision"),
    ("nw", "Not current in Note Waterfall"),
    ("problems", "Problems (failed, not a score, changed outside)"),
    ("simplified", "With a simplified part"),
    ("orchestra", "With orchestra"),
    ("renamed", "Title changed (rename)"),
    ("excluded", "Excluded"),
]

CSS = b"""
.mono { font-family: monospace; }
.summary { font-weight: bold; }
.status-accent { color: @accent_color; font-weight: bold; }
.status-success { color: @success_color; }
.status-warning { color: @warning_color; font-weight: bold; }
.status-error { color: @error_color; font-weight: bold; }
.status-dim-label { opacity: 0.55; }
.device-ok { color: @success_color; font-weight: bold; }
.device-bad { color: @warning_color; font-weight: bold; }
"""


class Item(GObject.Object):
    __gtype_name__ = "PianoLibraryItem"

    def __init__(self, row: M.Row):
        super().__init__()
        self.row = row


def _cmp(a, b) -> Gtk.Ordering:
    return Gtk.Ordering.SMALLER if a < b else Gtk.Ordering.LARGER if a > b else Gtk.Ordering.EQUAL


def _yes_no(v: Optional[bool]) -> str:
    return "?" if v is None else ("yes" if v else "")


def _filter_match(key: str, r: M.Row) -> bool:
    if key == "all":
        return True
    if key == "scores":
        return r.status != "excluded"
    if key == "build":
        return r.status in M.NEEDS_BUILD
    if key == "pv":
        return r.is_score and r.pianovision != "current"
    if key == "nw":
        return r.is_score and r.waterfall != "current"
    if key == "problems":
        return r.status in ("failed", "invalid", "conflict")
    if key == "simplified":
        return bool(r.simplified)
    if key == "orchestra":
        return bool(r.orchestra)
    if key == "renamed":
        return bool(r.new_name)
    if key == "excluded":
        return r.status == "excluded"
    return True


def _text_view(text: str = "", mono: bool = True, height: int = 220) -> Gtk.ScrolledWindow:
    tv = Gtk.TextView(editable=False, cursor_visible=False, wrap_mode=Gtk.WrapMode.WORD_CHAR,
                      top_margin=6, bottom_margin=6, left_margin=8, right_margin=8)
    if mono:
        tv.add_css_class("mono")
    tv.get_buffer().set_text(text)
    sw = Gtk.ScrolledWindow(min_content_height=height, vexpand=True, hexpand=True)
    sw.set_child(tv)
    return sw


class Window(Gtk.ApplicationWindow if Adw is None else Adw.ApplicationWindow):
    def __init__(self, app, args):
        super().__init__(application=app, title=TITLE, default_width=1280, default_height=860)
        self.args = args
        self.read_only = bool(getattr(args, "read_only", False))
        self.no_device = bool(getattr(args, "no_device", False))
        self.cfg = None
        self.cfg_error = ""
        try:
            self.cfg = M_load_config(args)
        except Exception as e:                  # shown once the window is up
            self.cfg_error = f"{type(e).__name__}: {e}"
        self.snap: Optional[M.Snapshot] = None
        self.status_cache = None                # last lib.status(), reused for device-only refreshes
        self.device = M.DeviceState("disabled" if self.no_device else "none")
        self.device_files: Optional[M.DeviceFiles] = None
        self._last_details = 0.0
        self._probing = False
        self.busy = False
        self.cancel: Optional[M.CancelToken] = None
        self.reviews = None                     # hand-edit reviews (the latest `review`)
        self.reviews_lib = None
        self._log_pending: List[str] = []
        self._log_lock = threading.Lock()
        self._fraction: Optional[float] = None
        self.mscore = M.find_musescore()
        self._tips = {}
        self._device_dirty = False

        self._build_ui()
        if self.no_device:
            self._show_device()
        GLib.timeout_add(100, self._flush_log)
        GLib.timeout_add(120, self._tick_progress)
        if not self.no_device:
            GLib.timeout_add_seconds(POLL_SECONDS, self._poll_device)
            GLib.idle_add(lambda: (self._poll_device(), False)[1])     # once now; the timeout repeats
        self._update_sensitivity()
        if self.cfg_error:
            GLib.idle_add(lambda: self.error("Cannot read the settings", self.cfg_error) and False)
        else:
            GLib.idle_add(lambda: self.refresh() and False)

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        prov = Gtk.CssProvider()
        prov.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_display(self.get_display(), prov, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        header = Adw.HeaderBar() if Adw else Gtk.HeaderBar()
        self.refresh_btn = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Refresh (F5)")
        self.refresh_btn.connect("clicked", lambda *_: self.refresh())
        header.pack_start(self.refresh_btn)

        menu = Gio.Menu()
        for label, action in (("Push PianoVision only…", "win.push-pv"), ("Push Note Waterfall only…", "win.push-nw"),
                              ("Rename songs after title changes…", "win.rename"),
                              ("Open the output folder", "win.open-output"), ("Open the settings file", "win.open-config")):
            menu.append(label, action)
        header.pack_end(Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu, tooltip_text="More"))
        for name, fn in (("push-pv", lambda *_: self.push([M.PIANOVISION_APP])),
                         ("push-nw", lambda *_: self.push([M.WATERFALL_APP])),
                         ("rename", lambda *_: self.rename()),
                         ("open-output", lambda *_: self._open_path(self.cfg.output if self.cfg else "")),
                         ("open-config", lambda *_: self._open_path(self.cfg.path if self.cfg else ""))):
            a = Gio.SimpleAction.new(name, None)
            a.connect("activate", fn)
            self.add_action(a)
        self._actions = {n: self.lookup_action(n) for n in ("push-pv", "push-nw", "rename")}
        sc = Gtk.ShortcutController()
        sc.add_shortcut(Gtk.Shortcut.new(Gtk.ShortcutTrigger.parse_string("F5"),
                                         Gtk.CallbackAction.new(lambda *_: (self.refresh(), True)[1])))
        sc.add_shortcut(Gtk.Shortcut.new(Gtk.ShortcutTrigger.parse_string("<Control>f"),
                                         Gtk.CallbackAction.new(lambda *_: (self.search.grab_focus(), True)[1])))
        self.add_controller(sc)

        # stack: Library / Orphans & strays / Hand edits
        if Adw:
            self.stack = Adw.ViewStack()
            switcher = Adw.ViewSwitcher(stack=self.stack, policy=Adw.ViewSwitcherPolicy.WIDE)
            header.set_title_widget(switcher)
        else:
            self.stack = Gtk.Stack()
            switcher = Gtk.StackSwitcher(stack=self.stack)
            header.set_title_widget(switcher)

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin_top=6, margin_bottom=6,
                       margin_start=10, margin_end=10)
        body.append(self._summary_box())
        body.append(self._actions_box())

        lib_page = self._library_page()
        self._add_page(lib_page, "library", "Library", "view-list-symbolic")
        self._add_page(self._strays_page(), "strays", "Orphans & strays", "edit-clear-all-symbolic")
        self._add_page(self._hands_page(), "hands", "Hand edits", "input-touchpad-symbolic")

        paned = Gtk.Paned(orientation=Gtk.Orientation.VERTICAL, vexpand=True, wide_handle=True)
        paned.set_start_child(self.stack)
        paned.set_end_child(self._log_box())
        paned.set_resize_start_child(True)
        paned.set_shrink_end_child(False)
        paned.set_position(520)
        body.append(paned)

        if Adw:
            tv = Adw.ToolbarView()
            tv.add_top_bar(header)
            self.ro_banner = Adw.Banner(title="Read-only: build, push, rename and hand edits are off",
                                        revealed=self.read_only)
            tv.add_top_bar(self.ro_banner)
            tv.set_content(body)
            self.set_content(tv)
        else:
            self.set_titlebar(header)
            if self.read_only:
                lbl = Gtk.Label(label="Read-only: build, push, rename and hand edits are off")
                lbl.add_css_class("dim-label")
                body.prepend(lbl)
            self.set_child(body)

    def _add_page(self, widget, name, title, icon):
        if Adw:
            self.stack.add_titled_with_icon(widget, name, title, icon)
        else:
            self.stack.add_titled(widget, name, title)

    def _summary_box(self):
        box = Gtk.Box(spacing=16)
        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True)
        self.summary1 = Gtk.Label(xalign=0, label="Reading the library…", wrap=True, max_width_chars=100)
        self.summary1.add_css_class("summary")
        self.summary2 = Gtk.Label(xalign=0, wrap=True, max_width_chars=100)
        self.summary2.add_css_class("dim-label")
        self.paths_lbl = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.MIDDLE, max_width_chars=100)
        self.paths_lbl.add_css_class("dim-label")
        self.paths_lbl.add_css_class("caption")
        for w in (self.summary1, self.summary2, self.paths_lbl):
            left.append(w)
        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, halign=Gtk.Align.END)
        dev = Gtk.Box(spacing=6, halign=Gtk.Align.END)
        self.device_icon = Gtk.Image(icon_name="network-offline-symbolic")
        self.device_lbl = Gtk.Label(xalign=1, label="Checking for the headset…")
        dev.append(self.device_icon)
        dev.append(self.device_lbl)
        self.onhs_lbl = Gtk.Label(xalign=1)
        self.onhs_lbl.add_css_class("dim-label")
        right.append(dev)
        right.append(self.onhs_lbl)
        box.append(left)
        box.append(right)
        return box

    def _button(self, label, tooltip, fn, suggested=False):
        b = Gtk.Button(label=label, tooltip_text=tooltip)
        b.connect("clicked", lambda *_: fn())
        if suggested:
            b.add_css_class("suggested-action")
        return b

    def _actions_box(self):
        box = Gtk.Box(spacing=6)
        self.build_btn = self._button("Build", "Render new and edited scores, write Note Waterfall's parts files, "
                                      "retire removed scores (to the attic)", self.build)
        self.push_btn = self._button("Push to headset…", "Show what would be copied to PianoVision and Note "
                                     "Waterfall, then push", lambda: self.push(None))
        self.sync_btn = self._button("Sync", "Build, then push to both apps (shows the push first)", self.sync,
                                     suggested=True)
        self.rename_btn = self._button("Rename…", "Rename songs whose score title changed", self.rename)
        self.scores_btn = self._button("Scores folder", "Open the MuseScore scores folder",
                                       lambda: self._open_path(self.cfg.scores if self.cfg else ""))
        for b in (self.build_btn, self.push_btn, self.sync_btn, self.rename_btn):
            box.append(b)
        box.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))
        box.append(self.scores_btn)
        spacer = Gtk.Box(hexpand=True)
        box.append(spacer)
        self.filter_dd = Gtk.DropDown.new_from_strings([t for _k, t in FILTERS])
        self.filter_dd.set_tooltip_text("Show only…")
        self.filter_dd.connect("notify::selected", lambda *_: self._refilter())
        box.append(self.filter_dd)
        self.search = Gtk.SearchEntry(placeholder_text="Search composer, title, file…", width_chars=30)
        self.search.connect("search-changed", lambda *_: self._refilter())
        box.append(self.search)
        return box

    def _library_page(self):
        self.store = Gio.ListStore(item_type=Item)
        self.filter = Gtk.CustomFilter.new(self._match, None)
        filtered = Gtk.FilterListModel(model=self.store, filter=self.filter)
        self.sorted = Gtk.SortListModel(model=filtered)
        self.selection = Gtk.SingleSelection(model=self.sorted, autoselect=False, can_unselect=True)
        self.selection.connect("notify::selected", lambda *_: self._update_sensitivity())
        self.view = Gtk.ColumnView(model=self.selection, show_row_separators=True, reorderable=True)
        self.view.add_css_class("data-table")
        self.view.connect("activate", lambda *_: self.open_in_musescore())
        self.sorted.set_sorter(self.view.get_sorter())

        def status_css(r):
            return "status-" + M.STATUS[r.status][1]

        def app_css(v):
            return {"current": "status-success", "old": "status-warning", "missing": "status-warning"}.get(v, "status-dim-label")

        cols = [
            ("Composer", lambda r: r.composer, None, False, 170, lambda r: r.composer.lower()),
            ("Title", lambda r: r.title, None, True, 260, lambda r: r.title.lower()),
            ("Song file", lambda r: r.output or "—", None, True, 220, lambda r: r.output.lower()),
            ("Status", lambda r: r.status_label, status_css, False, 120, lambda r: r.status_label),
            ("Simplified", lambda r: _yes_no(r.simplified) if r.is_score else "", None, False, 80,
             lambda r: str(r.simplified)),
            ("Orchestra", lambda r: _yes_no(r.orchestra) if r.is_score else "", None, False, 80,
             lambda r: str(r.orchestra)),
            ("PianoVision", lambda r: M.APP_STATE[r.pianovision] if r.is_score else "", lambda r: app_css(r.pianovision),
             False, 110, lambda r: r.pianovision),
            ("Note Waterfall", lambda r: M.APP_STATE[r.waterfall] if r.is_score else "", lambda r: app_css(r.waterfall),
             False, 120, lambda r: r.waterfall),
        ]
        for title, get, css, expand, width, key in cols:
            self.view.append_column(self._column(title, get, css, expand, width, key))

        sw = Gtk.ScrolledWindow(vexpand=True, hexpand=True)
        sw.set_child(self.view)
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        page.append(sw)
        rowbar = Gtk.Box(spacing=6)
        self.sel_lbl = Gtk.Label(xalign=0, hexpand=True, ellipsize=Pango.EllipsizeMode.END, max_width_chars=60,
                                 label="Select a score for its details. Double-click opens it in MuseScore.")
        self.sel_lbl.add_css_class("dim-label")
        self.mscore_btn = self._button("Open in MuseScore",
                                       self.mscore or "MuseScore 4 not found (mscore4portable / mscore)",
                                       self.open_in_musescore)
        self.reveal_score_btn = self._button("Show score file", "Show the .mscz in the file manager",
                                             lambda: self.reveal(score=True))
        self.reveal_song_btn = self._button("Show song file", "Show the PianoVision JSON in the file manager",
                                            lambda: self.reveal(score=False))
        for w in (self.sel_lbl, self.mscore_btn, self.reveal_score_btn, self.reveal_song_btn):
            rowbar.append(w)
        page.append(rowbar)
        return page

    def _column(self, title, get, css, expand, width, key):
        f = Gtk.SignalListItemFactory()

        def setup(_f, li):
            li.set_child(Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END, max_width_chars=30))

        def bind(_f, li):
            lbl, r = li.get_child(), li.get_item().row
            lbl.set_text(get(r))
            lbl.set_tooltip_text(r.detail or r.rel)
            for c in list(lbl.get_css_classes()):
                if c.startswith("status-"):
                    lbl.remove_css_class(c)
            if css:
                lbl.add_css_class(css(r))

        f.connect("setup", setup)
        f.connect("bind", bind)
        col = Gtk.ColumnViewColumn(title=title, factory=f, expand=expand, resizable=True, fixed_width=width)
        col.set_sorter(Gtk.CustomSorter.new(lambda a, b, _d: _cmp(key(a.row), key(b.row)), None))
        return col

    def _strays_page(self):
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin_top=6)
        top = Gtk.Box(spacing=8)
        lbl = Gtk.Label(xalign=0, hexpand=True, wrap=True, max_width_chars=90, label=(
            "Orphan songs are JSON files in the output folder that no score makes any more. Retiring moves them "
            "to the attic (PianoVision/.attic/<date>/); nothing is deleted. Stray .mid files are MIDI files in the "
            "scores folder without a score; they are not used. Files on the headset that left the library are "
            "listed in Push to headset…, where they can be removed after a confirmation."))
        top.append(lbl)
        self.retire_btn = self._button("Retire orphans…", "Move the orphan songs to the attic", self.retire_orphans)
        top.append(self.retire_btn)
        page.append(top)
        self.strays_view = _text_view("", height=60)
        page.append(self.strays_view)
        return page

    def _hands_page(self):
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin_top=6)
        top = Gtk.Box(spacing=6)
        lbl = Gtk.Label(xalign=0, hexpand=True, wrap=True, max_width_chars=90, label=(
            "Hand edits recorded with Note Waterfall's HAND REC. Pull copies them from the headset, Review shows "
            "what would change in each score (nothing is written), Apply asks for every score before it backs "
            "the score up to the attic and overwrites it. Close the scores in MuseScore first."))
        top.append(lbl)
        self.pull_btn = self._button("Pull from headset", "Copy the hand-edit files from the headset into "
                                     "PianoVision/.hands/inbox", self.hands_pull)
        self.review_btn = self._button("Review", "What would change, per score (writes nothing)", self.hands_review)
        self.apply_btn = self._button("Apply…", "Asks per score; backs up, overwrites, builds and deploys",
                                      self.hands_apply)
        for b in (self.pull_btn, self.review_btn, self.apply_btn):
            top.append(b)
        page.append(top)
        self.hands_view = _text_view("Press Review to see the hand edits in the inbox.", height=60)
        page.append(self.hands_view)
        return page

    def _log_box(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, margin_top=4)
        bar = Gtk.Box(spacing=8)
        self.job_lbl = Gtk.Label(xalign=0, label="Log", width_chars=24, ellipsize=Pango.EllipsizeMode.END)
        self.job_lbl.add_css_class("heading")
        self.progress = Gtk.ProgressBar(hexpand=True, valign=Gtk.Align.CENTER, show_text=False)
        self.cancel_btn = self._button("Cancel", "Stop the running job", self._cancel)
        self.cancel_btn.add_css_class("destructive-action")
        clear = Gtk.Button(icon_name="edit-clear-symbolic", tooltip_text="Clear the log")
        clear.connect("clicked", lambda *_: self.log_buf.set_text(""))
        for w in (self.job_lbl, self.progress, self.cancel_btn, clear):
            bar.append(w)
        box.append(bar)
        sw = _text_view("", height=60)
        self.log_tv = sw.get_child()
        self.log_buf = self.log_tv.get_buffer()
        self.log_end = self.log_buf.create_mark("end", self.log_buf.get_end_iter(), False)
        box.append(sw)
        return box

    # ------------------------------------------------------------------ log & progress
    def log(self, text: str) -> None:
        """Thread-safe: queued, flushed into the log pane by the main loop."""
        with self._log_lock:
            self._log_pending.append(text)

    def logln(self, text: str = "") -> None:
        self.log(text + "\n")

    def _flush_log(self):
        with self._log_lock:
            chunk = "".join(self._log_pending)
            self._log_pending.clear()
        if chunk:
            buf = self.log_buf
            buf.insert(buf.get_end_iter(), chunk)
            lines = buf.get_line_count()
            if lines > 6000:
                buf.delete(buf.get_start_iter(), buf.get_iter_at_line(lines - 5000)[1])
            self.log_tv.scroll_to_mark(self.log_end, 0.0, False, 0.0, 1.0)
        return True

    def _tick_progress(self):
        if self.busy:
            if self._fraction is None:
                self.progress.pulse()
            else:
                self.progress.set_fraction(self._fraction)
        return True

    def set_fraction(self, f: Optional[float]) -> None:
        self._fraction = f

    # ------------------------------------------------------------------ jobs
    def run_job(self, title: str, work: Callable[[M.CancelToken], object], done: Optional[Callable] = None,
                cancellable: bool = True, refresh_after: bool = False) -> bool:
        """Runs ``work(cancel)`` in a thread (prints go to the log), then ``done(result)`` on the main
        loop.  Exceptions become an error dialog.  One job at a time."""
        if self.busy:
            self.toast("Another job is still running")
            return False
        self.busy = True
        self.cancel = M.CancelToken()
        self._fraction = None
        self.job_lbl.set_text(title)
        self.cancel_btn.set_sensitive(cancellable)
        self.cancel_btn.set_visible(True)
        self._update_sensitivity()
        self.logln(time.strftime("[%H:%M:%S] ") + title)
        cancel = self.cancel

        def thread():
            out = M_stdout()
            out.set_sink(self.log)
            result, err = None, None
            try:
                result = work(cancel)
            except M.Cancelled:
                err = "cancelled"
            except Exception as e:
                err = e
                self.log(traceback.format_exc())
            finally:
                out.set_sink(None)
            GLib.idle_add(finish, result, err)

        def finish(result, err):
            self.busy = False
            self.cancel = None
            self.progress.set_fraction(0.0)
            self.job_lbl.set_text("Log")
            self.cancel_btn.set_visible(False)
            self._update_sensitivity()
            if err == "cancelled" or (cancel.cancelled and err is None and result is None):
                self.logln(time.strftime("[%H:%M:%S] ") + f"{title}: cancelled")
            elif err is not None:
                self.logln(time.strftime("[%H:%M:%S] ") + f"{title}: failed")
                self.error(f"{title} failed", f"{type(err).__name__}: {err}" if str(err) else type(err).__name__)
            else:
                self.logln(time.strftime("[%H:%M:%S] ") + f"{title}: done")
                if done is not None:
                    try:
                        done(result)
                    except Exception as e:
                        self.log(traceback.format_exc())
                        self.error(f"{title} failed", f"{type(e).__name__}: {e}")
            if refresh_after and not self.busy:
                self.refresh()
            elif self._device_dirty and not self.busy:
                self.refresh_device_files()
            return False

        threading.Thread(target=thread, name=title, daemon=True).start()
        return True

    def _cancel(self):
        if self.cancel is not None:
            self.logln("cancelling…")
            self.cancel.cancel()

    def _lib(self):
        from .library import Library
        return Library(self.cfg, log=lambda s: print(s))

    def _stream(self, argv: List[str], cancel: M.CancelToken, progress_re: Optional[str] = None) -> int:
        self.logln("$ " + " ".join(argv))
        rx = re.compile(progress_re) if progress_re else None

        def line(s):
            self.logln(s)
            if rx:
                m = rx.search(s)
                if m:
                    self.set_fraction(int(m.group(1)) / max(1, int(m.group(2))))
        rc = M.run_streaming(argv, line, cancel=cancel, cwd=M.package_root())
        if cancel.cancelled:
            raise M.Cancelled()
        return rc

    # ------------------------------------------------------------------ refresh
    def refresh(self) -> None:
        if self.cfg is None:
            self.error("Cannot read the settings", self.cfg_error)
            return
        serial = self.device.serial if self.device.connected else ""

        def work(cancel):
            lib = self._lib()
            st = lib.status()
            cancel.check()
            dev = M.device_files(lib, serial) if serial else None
            if dev and dev.error:
                print(f"headset: {dev.error}")
            return st, dev, M.snapshot(lib, st, dev)

        self._device_dirty = False

        def done(res):
            st, dev, snap = res
            self.status_cache = st
            self.device_files = dev
            self.show_snapshot(snap)
            if getattr(self.args, "screenshot", None):
                GLib.timeout_add(900, self._screenshot_and_quit)

        self.run_job("Refreshing", work, done)

    def refresh_device_files(self) -> None:
        """After the headset (dis)appears: only the headset columns, without a job slot."""
        if self.cfg is None or self.status_cache is None or self.busy:
            return                              # the end of the job looks at _device_dirty
        self._device_dirty = False
        serial = self.device.serial if self.device.connected else ""
        st = self.status_cache

        def thread():
            try:
                lib = self._lib()
                dev = M.device_files(lib, serial) if serial else None
                snap = M.snapshot(lib, st, dev)
            except Exception:
                return
            GLib.idle_add(lambda: (setattr(self, "device_files", dev), self.show_snapshot(snap), False)[-1])
        threading.Thread(target=thread, daemon=True).start()

    def show_snapshot(self, snap: M.Snapshot) -> None:
        self.snap = snap
        self.store.splice(0, self.store.get_n_items(), [Item(r) for r in snap.rows])
        s1, s2 = M.summary_lines(snap)
        self.summary1.set_text(s1)
        self.summary2.set_text(s2)
        self.paths_lbl.set_text(f"Scores: {snap.scores_dir}    Songs: {snap.output_dir}"
                                + (f"    Last build: {snap.build_time.replace('T', ' ')}" if snap.build_time else ""))
        self._show_on_headset()
        lines = [f"Orphan songs: {len(snap.orphans)} (in {snap.output_dir}, no score makes them)"]
        lines += [f"  {n}" for n in snap.orphans] or ["  (none)"]
        lines += ["", f"Stray .mid files: {len(snap.stray_midi)} (in {snap.scores_dir}, no score next to them)"]
        lines += [f"  {n}" for n in snap.stray_midi] or ["  (none)"]
        lines += ["", f"Companion .mid files next to scores (no longer needed): {snap.companion_midi}"]
        bad = [r for r in snap.rows if r.status in ("invalid", "conflict")]
        if bad:
            lines += ["", "Files that need a look:"] + [f"  {r.rel}: {r.status_label}: {r.detail}" for r in bad]
        self.strays_view.get_child().get_buffer().set_text("\n".join(lines))
        self._update_sensitivity()

    def _show_on_headset(self):
        snap = self.snap
        if snap is None or snap.device is None:
            self.onhs_lbl.set_text("")
            return
        parts = []
        for app, attr in ((M.PIANOVISION_APP, "pianovision"), (M.WATERFALL_APP, "waterfall")):
            if getattr(snap.device, attr) is None:
                parts.append(f"{app}: cannot list")
            else:
                cur, n = snap.on_device(app)
                parts.append(f"{app}: {cur}/{n} current")
        self.onhs_lbl.set_text("   ".join(parts))

    def _match(self, item, _data=None) -> bool:
        r = item.row
        key = FILTERS[self.filter_dd.get_selected()][0] if hasattr(self, "filter_dd") else "scores"
        q = self.search.get_text() if hasattr(self, "search") else ""
        return _filter_match(key, r) and (not q or r.matches(q))

    def _refilter(self):
        self.filter.changed(Gtk.FilterChange.DIFFERENT)

    def selected_row(self) -> Optional[M.Row]:
        it = self.selection.get_selected_item()
        return it.row if it is not None else None

    # ------------------------------------------------------------------ sensitivity
    def _update_sensitivity(self):
        idle = not self.busy
        writable = idle and not self.read_only and self.cfg is not None
        dev = self.device.connected and not self.no_device
        self.refresh_btn.set_sensitive(idle and self.cfg is not None)
        self.build_btn.set_sensitive(writable)
        self.sync_btn.set_sensitive(writable and dev)
        self.push_btn.set_sensitive(writable and dev)
        self.rename_btn.set_sensitive(writable)
        self._actions["push-pv"].set_enabled(writable and dev)
        self._actions["push-nw"].set_enabled(writable and dev)
        self._actions["rename"].set_enabled(writable)
        self.retire_btn.set_sensitive(writable and bool(self.snap and self.snap.orphans))
        self.pull_btn.set_sensitive(writable and dev)
        self.review_btn.set_sensitive(idle and self.cfg is not None)
        self.apply_btn.set_sensitive(writable and bool(self.reviews) and any(rv.ready for rv in self.reviews))
        for b in (self.push_btn, self.sync_btn, self.pull_btn):
            tip = self._tips.setdefault(b, b.get_tooltip_text())
            b.set_tooltip_text(tip if dev else f"{tip}\n(off: {self.device.label()})")
        r = self.selected_row()
        score_path = os.path.join(self.cfg.scores, r.rel) if (r and self.cfg) else ""
        self.mscore_btn.set_sensitive(bool(r and self.mscore and r.rel.endswith(".mscz") and os.path.exists(score_path)))
        self.reveal_score_btn.set_sensitive(bool(r and os.path.exists(score_path)))
        self.reveal_song_btn.set_sensitive(bool(r and r.output and self.cfg
                                                and os.path.exists(os.path.join(self.cfg.output, r.output))))
        if r:
            bits = [r.rel, r.status_label + (f": {r.detail}" if r.detail else "")]
            if r.new_name:
                bits.append(f"title changed: Rename… would call it {r.new_name}")
            self.sel_lbl.set_text("   ·   ".join(bits))
            self.sel_lbl.set_tooltip_text("\n".join(bits))

    # ------------------------------------------------------------------ device polling
    def _poll_device(self):
        if self._probing or self.cfg is None:
            return True
        self._probing = True
        details = time.monotonic() - self._last_details > DETAILS_EVERY
        prev = self.device

        def thread():
            try:
                st = M.probe_device(self.cfg.adb, self.cfg.serial, details=details)
            except Exception as e:
                st = M.DeviceState("none", message=str(e))
            GLib.idle_add(apply, st)

        def apply(st):
            self._probing = False
            if details:
                self._last_details = time.monotonic()
            elif st.connected and prev.connected and st.serial == prev.serial:
                st.wakefulness, st.free_bytes = prev.wakefulness, prev.free_bytes
            changed = (st.state, st.serial) != (prev.state, prev.serial)
            self.device = st
            self._show_device()
            if changed:
                self._last_details = 0.0 if st.connected and not details else self._last_details
                self.logln(time.strftime("[%H:%M:%S] ") + st.label())
                self._device_dirty = True
                self.refresh_device_files()
                self._update_sensitivity()
            return False

        threading.Thread(target=thread, daemon=True).start()
        return True

    def _show_device(self):
        st = self.device
        self.device_lbl.set_text(st.label())
        for c in ("device-ok", "device-bad"):
            self.device_lbl.remove_css_class(c)
        if st.connected:
            self.device_icon.set_from_icon_name("emblem-ok-symbolic")
            self.device_lbl.add_css_class("device-ok")
        else:
            self.device_icon.set_from_icon_name("network-offline-symbolic" if st.state != "unauthorized"
                                                else "dialog-warning-symbolic")
            if st.state not in ("disabled",):
                self.device_lbl.add_css_class("device-bad")
        self.device_lbl.set_tooltip_text(st.message or (f"serial {st.serial}" if st.serial else None))

    # ------------------------------------------------------------------ dialogs
    def dialog(self, heading: str, body: str, responses, on_response: Optional[Callable[[str], None]] = None,
               extra=None, default: Optional[str] = None, close: str = "cancel",
               destructive: Optional[str] = None, suggested: Optional[str] = None):
        """``responses``: [(id, label)].  ``on_response(id)`` on the main loop."""
        if Adw:
            d = Adw.AlertDialog(heading=heading, body=body)
            for rid, label in responses:
                d.add_response(rid, label)
            if destructive:
                d.set_response_appearance(destructive, Adw.ResponseAppearance.DESTRUCTIVE)
            if suggested:
                d.set_response_appearance(suggested, Adw.ResponseAppearance.SUGGESTED)
            d.set_default_response(default or close)
            d.set_close_response(close)
            if extra is not None:
                d.set_extra_child(extra)
            d.connect("response", lambda _d, rid: on_response and on_response(rid))
            if extra is not None:
                d.set_content_width(760)
            d.present(self)
            return d
        win = Gtk.Window(transient_for=self, modal=True, title=heading, default_width=760 if extra else 420)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, margin_top=16, margin_bottom=16,
                      margin_start=16, margin_end=16)
        h = Gtk.Label(label=heading, xalign=0, wrap=True, max_width_chars=80)
        h.add_css_class("title-3")
        box.append(h)
        if body:
            box.append(Gtk.Label(label=body, xalign=0, wrap=True, max_width_chars=80))
        if extra is not None:
            box.append(extra)
        row = Gtk.Box(spacing=8, halign=Gtk.Align.END)
        answered = []

        def respond(rid):
            answered.append(rid)
            win.destroy()
            if on_response:
                on_response(rid)
        for rid, label in responses:
            b = Gtk.Button(label=label)
            if rid == destructive:
                b.add_css_class("destructive-action")
            if rid == suggested:
                b.add_css_class("suggested-action")
            b.connect("clicked", lambda _b, r=rid: respond(r))
            row.append(b)
        box.append(row)
        win.set_child(box)
        win.connect("close-request", lambda *_: (not answered and on_response and on_response(close), False)[1])
        win.present()
        return win

    def error(self, heading: str, message: str) -> bool:
        extra = _text_view(message, height=160) if len(message) > 300 or message.count("\n") > 4 else None
        self.dialog(heading, "" if extra else message, [("close", "Close")], extra=extra, close="close")
        return True

    def toast(self, text: str) -> None:
        self.logln(text)

    def ask_blocking(self, build_dialog: Callable[[Callable[[str], None]], None]) -> str:
        """From a worker thread: shows a dialog on the main loop and waits for the answer."""
        ev = threading.Event()
        box = {}

        def answer(rid):
            box["rid"] = rid
            ev.set()

        GLib.idle_add(lambda: (build_dialog(answer), False)[1])
        while not ev.wait(0.2):
            if self.cancel is not None and self.cancel.cancelled:
                return "cancel"
        return box.get("rid", "cancel")

    # ------------------------------------------------------------------ actions: build
    def build(self, then: Optional[Callable[[], None]] = None) -> None:
        report = M.build_report_path(self.cfg.output)

        def work(cancel):
            rc = self._stream(M.formatter_argv(self._fargs(), "build", "--report", report), cancel)
            return rc

        def done(rc):
            rep = M.read_build_report(self.cfg.output)
            failed = rep.get("failed", [])
            if rc != 0 and failed:
                self.error(f"{len(failed)} score(s) failed to build",
                           "\n".join(f"{f['rel']}: {f['error']}" for f in failed))
            elif rc != 0:
                self.error("Build failed", f"python3 -m pianovision build exited with {rc}; see the log")
            if then is not None:
                GLib.idle_add(lambda: (then(), False)[1])

        self.run_job("Building", work, done, refresh_after=True)

    def _fargs(self):
        class A:
            pass
        a = A()
        a.config = self.cfg.path or getattr(self.args, "config", None)
        a.scores = getattr(self.args, "scores", None)
        a.output = getattr(self.args, "output", None)
        return a

    def sync(self) -> None:
        def after_build():
            # the refresh after the build runs first; push once it is done
            def wait():
                if self.busy:
                    return True
                self.push(None)
                return False
            GLib.timeout_add(200, wait)
        self.build(then=after_build)

    # ------------------------------------------------------------------ actions: push
    def push(self, apps: Optional[List[str]]) -> None:
        """Plans the push (reading the headset), shows it, pushes after OK; removals need a second OK."""
        if not self.device.connected:
            self.error("No headset", self.device.label())
            return
        apps = apps or [M.PIANOVISION_APP, M.WATERFALL_APP]
        serial = self.device.serial

        def work(cancel):
            from .device import Adb
            lib = self._lib()
            plans = []
            if M.PIANOVISION_APP in apps:
                print("checking PianoVision on the headset …")
                plans.append(M.pianovision_plan(lib, Adb(self.cfg.adb, serial)))
            cancel.check()
            if M.WATERFALL_APP in apps:
                print("checking Note Waterfall on the headset …")
                try:
                    mod = M.load_waterfall(M.waterfall_script(self.cfg))
                    plans.append(M.waterfall_plan(mod, lib.out, serial))
                except Exception as e:
                    plans.append(M.AppPlan(M.WATERFALL_APP, M.WATERFALL_SONGS, error=str(e)))
            for p in plans:
                print("  " + p.summary())
            return plans

        self.run_job("Checking the headset", work, lambda plans: self._push_dialog(plans, serial))

    def _push_dialog(self, plans: List[M.AppPlan], serial: str) -> None:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        checks, prunes = {}, {}
        for p in plans:
            frame = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            cb = Gtk.CheckButton(label=p.summary())
            cb.set_active(not p.error and bool(p.push))
            cb.set_sensitive(not p.error)
            checks[p.app] = cb
            frame.append(cb)
            if p.error:
                e = Gtk.Label(label=p.error, xalign=0, wrap=True, max_width_chars=80)
                e.add_css_class("error")
                frame.append(e)
            if p.push:
                exp = Gtk.Expander(label=f"Files to push to {p.target} ({len(p.push)})")
                exp.set_child(_text_view("\n".join(M.push_lines(p)), height=120))
                exp.set_expanded(len(p.push) <= 12)
                frame.append(exp)
            if p.remove:
                pr = Gtk.CheckButton(label=f"Also delete {len(p.remove)} file(s) from the headset that this tool "
                                           f"deployed and that left the library (asks again)")
                prunes[p.app] = pr
                exp = Gtk.Expander(label=f"No longer in the library ({len(p.remove)}): kept unless ticked")
                exp.set_child(_text_view("\n".join(p.remove), height=100))
                frame.append(pr)
                frame.append(exp)
            if p.foreign:
                exp = Gtk.Expander(label=f"Other files on the headset, never touched ({len(p.foreign)})")
                exp.set_child(_text_view("\n".join(p.foreign), height=80))
                frame.append(exp)
            box.append(frame)
        sw = Gtk.ScrolledWindow(min_content_height=260, max_content_height=520, propagate_natural_height=True)
        sw.set_child(box)
        anything = any((not p.error) and (p.push or p.remove) for p in plans)

        def respond(rid):
            if rid != "push":
                self.logln("push: not started")
                return
            chosen = [p for p in plans if checks[p.app].get_active() and not p.error]
            prune = [a for a, cb in prunes.items() if cb.get_active() and checks[a].get_active()]
            if not chosen:
                self.logln("push: nothing chosen")
                return
            if prune:
                self._confirm_removal(chosen, prune, serial)
            else:
                self._run_push(chosen, [], serial)

        self.dialog("Push to the headset", "Nothing to push: both apps are current." if not anything else
                    f"Headset {self.device.model or serial}. Tick what to push.",
                    [("cancel", "Cancel"), ("push", "Push")], respond, extra=sw, default="push" if anything else "cancel",
                    suggested="push")

    def _confirm_removal(self, chosen: List[M.AppPlan], prune: List[str], serial: str) -> None:
        lines = M.removal_lines(chosen, prune)

        def respond(rid):
            if rid == "delete":
                self._run_push(chosen, prune, serial, confirmed=lines)
            elif rid == "keep":
                self.logln("deleting declined: pushing only")
                self._run_push(chosen, [], serial)
            else:
                self.logln("push: not started")

        self.dialog(f"Delete {len(lines)} file(s) from the headset?",
                    "These files were deployed by this tool earlier and are no longer in the library. Exactly "
                    "these are deleted from the headset; nothing else is removed, and nothing in the library.",
                    [("cancel", "Cancel"), ("keep", "Push, keep them"), ("delete", f"Delete {len(lines)} and push")],
                    respond, extra=_text_view("\n".join(lines), height=200), destructive="delete")

    def _run_push(self, chosen: List[M.AppPlan], prune: List[str], serial: str,
                  confirmed: Optional[List[str]] = None) -> None:
        def work(cancel):
            from .device import Adb, DeviceError, execute_deploy
            lib = self._lib()
            for p in chosen:
                cancel.check()
                if p.app == M.PIANOVISION_APP:
                    dp = p.raw
                    do_prune = M.PIANOVISION_APP in prune
                    if not dp.push and not do_prune:
                        print("PianoVision: nothing to push")
                        continue
                    print(f"PianoVision: pushing {len(dp.push)} file(s) to {lib.cfg.device_dir}"
                          + (f", deleting {len(dp.remove)}" if do_prune else "") + " … (adb push cannot be cancelled)")
                    t = time.monotonic()
                    try:
                        execute_deploy(lib, Adb(self.cfg.adb, serial), dp, prune=do_prune)
                    except DeviceError as e:
                        raise RuntimeError(f"PianoVision: {e}")
                    print(f"PianoVision: pushed {len(dp.push)}" + (f", removed {len(dp.remove)}" if do_prune else "")
                          + f"; verified on the headset ({time.monotonic() - t:.0f}s)")
                else:
                    do_prune = M.WATERFALL_APP in prune
                    if do_prune:
                        # the list confirmed must still be exactly what deploy_songs.py --prune deletes
                        mod = M.load_waterfall(M.waterfall_script(self.cfg))
                        now = M.waterfall_plan(mod, lib.out, serial)
                        if now.error or now.remove != p.remove:
                            raise RuntimeError("Note Waterfall: the files to delete changed since you confirmed "
                                               "them; nothing was deleted. Open Push to headset… again.")
                    self.set_fraction(0.0)
                    rc = self._stream(M.waterfall_argv(self.cfg, lib.out, serial, do_prune), cancel,
                                      progress_re=r"\[(\d+)/(\d+)\]")
                    self.set_fraction(None)
                    if rc != 0:
                        raise RuntimeError(f"Note Waterfall: deploy_songs.py exited with {rc}; see the log")
            return True

        if confirmed:
            self.logln("confirmed for deletion:\n  " + "\n  ".join(confirmed))
        self.run_job("Pushing to the headset", work, refresh_after=True)

    # ------------------------------------------------------------------ actions: rename / orphans
    def rename(self) -> None:
        def work(cancel):
            return M.rename_candidates(self._lib())

        def done(cands):
            if not cands:
                self.dialog("Nothing to rename", "Every song file is named after its score's current title. After "
                            "you change a title in MuseScore, Build first: the new title shows up here then.",
                            [("close", "Close")], close="close")
                return
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            checks = []
            for rel, old, new, changed in cands:
                cb = Gtk.CheckButton(label=f"{rel}\n    {old}\n →  {new}"
                                     + ("" if changed else "\n    (title unchanged: newer naming rules only)"))
                cb.set_active(changed)
                checks.append((cb, rel))
                box.append(cb)
            sw = Gtk.ScrolledWindow(min_content_height=160, max_content_height=460, propagate_natural_height=True)
            sw.set_child(box)

            def respond(rid):
                if rid != "rename":
                    return
                sel = {rel for cb, rel in checks if cb.get_active()}
                if sel:
                    self._run_rename(sel)
            self.dialog("Rename songs after title changes",
                        "The song file (and its Note Waterfall parts file) gets the name the score's title gives "
                        "now. On the headset a renamed song is a new song: push afterwards; the old file there is "
                        "offered for deletion in Push to headset….",
                        [("cancel", "Cancel"), ("rename", "Rename")], respond, extra=sw, suggested="rename")

        self.run_job("Looking for title changes", work, done)

    def _run_rename(self, sel) -> None:
        def work(cancel):
            done = self._lib().rename(restrict=set(sel))
            for rel, old, new in done:
                print(f"  {old} -> {new}   ({rel})")
            print(f"{len(done)} renamed; building to write their Note Waterfall parts files under the new names")
            return done
        self.run_job("Renaming", work, lambda _done: self.build(), cancellable=False)

    def retire_orphans(self) -> None:
        names = list(self.snap.orphans) if self.snap else []
        if not names:
            return
        lines = M.orphan_lines(self.cfg.output, names)

        def respond(rid):
            if rid != "retire":
                return

            def work(cancel):
                rep = self._lib().retire_orphans(names)
                for a in rep.attic:
                    print(f"  -> {a}")
                print(f"{len(rep.retired)} orphan song(s) moved to the attic")
                return rep
            self.run_job("Retiring orphans", work, cancellable=False, refresh_after=True)

        self.dialog(f"Move {len(names)} orphan song(s) to the attic?",
                    "Nothing is deleted: they go to the output folder's .attic/<date>/ and can be moved back.",
                    [("cancel", "Cancel"), ("retire", f"Retire {len(names)}")], respond,
                    extra=_text_view("\n".join(lines), height=180), destructive="retire")

    # ------------------------------------------------------------------ actions: files
    def open_in_musescore(self) -> None:
        r = self.selected_row()
        if not r or not self.mscore or not r.rel.endswith(".mscz"):
            return
        path = os.path.join(self.cfg.scores, r.rel)
        try:
            subprocess.Popen([self.mscore, path], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
            self.logln(f"opening {r.rel} in MuseScore")
        except OSError as e:
            self.error("Cannot start MuseScore", str(e))

    def reveal(self, score: bool) -> None:
        r = self.selected_row()
        if not r:
            return
        path = os.path.join(self.cfg.scores, r.rel) if score else os.path.join(self.cfg.output, r.output)
        launcher = Gtk.FileLauncher.new(Gio.File.new_for_path(path))

        def cb(l, res):
            try:
                l.open_containing_folder_finish(res)
            except GLib.Error as e:
                if "dismissed" not in str(e).lower():
                    self.error("Cannot show the file", e.message)
        launcher.open_containing_folder(self, None, cb)

    def _open_path(self, path: str) -> None:
        if not path:
            return
        launcher = Gtk.FileLauncher.new(Gio.File.new_for_path(path))

        def cb(l, res):
            try:
                l.launch_finish(res)
            except GLib.Error as e:
                if "dismissed" not in str(e).lower():
                    self.error("Cannot open", f"{path}: {e.message}")
        launcher.launch(self, None, cb)

    # ------------------------------------------------------------------ actions: hand edits
    def _hands_paths(self):
        lib_out = self.cfg.output
        return self.cfg.hands_dir, os.path.join(lib_out, ".hands", "inbox")

    def hands_pull(self) -> None:
        serial = self.device.serial
        device_dir, inbox = self._hands_paths()

        def work(cancel):
            from . import handedits as H
            from .device import Adb
            names = H.pull(Adb(self.cfg.adb, serial), device_dir, inbox)
            print(f"pulled {len(names)} hand-edit file(s) from {serial}:{device_dir} into {inbox}")
            for n in names:
                print(f"  {n}")
            return names

        def done(names):
            self.reviews = None
            self.hands_view.get_child().get_buffer().set_text(
                f"{len(names)} hand-edit file(s) pulled. Press Review." if names else
                "No hand-edit files on the headset.")
            self._update_sensitivity()
            if names:
                GLib.idle_add(lambda: (self.hands_review(), False)[1])

        self.run_job("Pulling hand edits", work, done)

    def hands_review(self) -> None:
        import glob
        _device_dir, inbox = self._hands_paths()

        def work(cancel):
            from . import handedits as H
            files = sorted(glob.glob(os.path.join(inbox, "*" + H.SIDECAR_EXT)))
            if not files:
                print(f"no hand-edit files in {inbox}")
                return None, []
            lib = self._lib()
            print(f"reviewing {len(files)} hand-edit file(s) …")
            reviews = []
            for song, scs in sorted(H.load_sidecars(files).items()):
                cancel.check()
                print(f"  reviewing {song} …")
                reviews.append(H.review_song(lib, song, scs))
            for rv in reviews:
                print("")
                for ln in H.format_review(rv):
                    print(ln)
            return lib, reviews

        def done(res):
            lib, reviews = res
            self.reviews_lib, self.reviews = lib, reviews
            self.hands_view.get_child().get_buffer().set_text(self._review_text(reviews, inbox))
            self._update_sensitivity()
            if Adw:
                self.stack.set_visible_child_name("hands")

        self.run_job("Reviewing hand edits", work, done)

    @staticmethod
    def _review_text(reviews, inbox) -> str:
        if not reviews:
            return f"No hand-edit files in {inbox}. Pull them from the headset first."
        ready = [rv for rv in reviews if rv.ready]
        out = [f"{len(ready)} score(s) can be updated, {sum(rv.moved_notes for rv in ready)} note(s) change hands", ""]
        for rv in reviews:
            out.append(f"{rv.rel or '?'}  ->  {rv.song}" + ("   [ready]" if rv.ready else ""))
            for sc in rv.sidecars:
                out.append(f"  {sc.name}: {len(sc.edits)} edit(s)" + (f", recorded {sc.updated}" if sc.updated else ""))
            for n in rv.notes:
                out.append(f"  note: {n}")
            if rv.error:
                out.append(f"  NOT APPLICABLE: {rv.error}")
            lines = M.review_lines(rv)
            if lines:
                out.append(f"  {'measure':<26} {'pitch':<6} {'from -> to':<16} {'status':<9} reason")
                for ln in lines:
                    out.append(f"  {ln.where:<26} {ln.pitch:<6} {ln.from_hand + ' -> ' + ln.to_hand:<16} "
                               f"{ln.status:<9} {ln.reason}")
            for c in rv.carried:
                out.append(f"    + {c}")
            out.append("")
        return "\n".join(out)

    def hands_apply(self) -> None:
        reviews, lib = self.reviews, self.reviews_lib
        if not reviews or not any(rv.ready for rv in reviews):
            return
        device_dir, inbox = self._hands_paths()
        connected = self.device.connected
        serial = self.device.serial

        def confirm(rv) -> bool:
            def build(answer):
                lines = M.review_lines(rv)
                txt = [f"{rv.rel}  ->  {rv.song}", "",
                       f"{'measure':<26} {'pitch':<6} {'from -> to':<16} {'status':<9} reason"]
                txt += [f"{ln.where:<26} {ln.pitch:<6} {ln.from_hand + ' -> ' + ln.to_hand:<16} {ln.status:<9} "
                        f"{ln.reason}" for ln in lines]
                txt += [f"  + {c}" for c in rv.carried]
                self.dialog(f"Overwrite {os.path.basename(rv.rel)}?",
                            f"{rv.moved_notes} note(s) change hands ({rv.to_right} to the right hand, {rv.to_left} to "
                            f"the left). The score is copied to the attic first, then overwritten, rebuilt"
                            + (" and pushed to both apps." if connected else "; the headset is not connected, so "
                               "push afterwards.") + " Close it in MuseScore first.",
                            [("skip", "Skip this score"), ("apply", "Overwrite score")], answer,
                            extra=_text_view("\n".join(txt), height=220), default="skip", close="skip",
                            destructive="apply")
            return self.ask_blocking(build) == "apply"

        def work(cancel):
            from . import handedits as H
            from .cli import _list, hands_archive_local, hands_callbacks
            from .device import Adb
            adb = Adb(self.cfg.adb, serial if connected else self.cfg.serial)
            stamp = time.strftime("%Y-%m-%d_%H%M%S")
            if not connected:
                print("headset not connected: the scores are rebuilt but not pushed; the hand-edit files stay on it")

            def run(argv):
                return M.Result(self._stream(argv, cancel))
            deploy_pv, deploy_nw, archive = hands_callbacks(lib, adb, device_dir, reviews, stamp,
                                                            deploy=connected, run=run)
            gate = M.ReviewGate(reviews, confirm)
            rep = H.apply_reviews(lib, reviews, ask=gate, log=print, deploy_pianovision=deploy_pv,
                                  deploy_waterfall=deploy_nw, archive=archive)
            _list("declined", rep.declined)
            _list("scores overwritten", rep.written)
            _list("backups", rep.backups)
            _list("songs rebuilt", rep.built)
            _list("hand-edit files moved to applied/ on the headset", rep.archived)
            hands_archive_local(lib, inbox, rep.archived, stamp)
            _list("FAILED", [f"{r}: {e}" for r, e in rep.failed], 50)
            for m in rep.messages:
                print(m)
            return rep

        def done(rep):
            self.reviews = None                 # the scores changed: review again
            self.hands_view.get_child().get_buffer().set_text(
                f"Applied: {len(rep.written)} score(s) overwritten, {len(rep.declined)} skipped, "
                f"{len(rep.failed)} failed. See the log. Review again for what is left.")
            if rep.failed or rep.messages:
                self.error("Hand edits: not everything went through",
                           "\n".join([f"{r}: {e}" for r, e in rep.failed] + list(rep.messages)))

        self.run_job("Applying hand edits", work, done, refresh_after=True)

    # ------------------------------------------------------------------ screenshot
    def _screenshot_and_quit(self):
        path = self.args.screenshot
        try:
            w, h = self.get_width(), self.get_height()
            paintable = Gtk.WidgetPaintable.new(self)
            snap = Gtk.Snapshot()
            paintable.snapshot(snap, w, h)
            node = snap.to_node()
            if node is None:
                raise RuntimeError(f"nothing drawn yet ({w}x{h}, mapped={self.get_mapped()})")
            tex = self.get_renderer().render_texture(node, Graphene.Rect().init(0, 0, w, h))
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
            tex.save_to_png(path)
            print(f"screenshot: {path} ({w}x{h})", file=sys.__stdout__, flush=True)
        except Exception as e:
            print(f"screenshot failed: {e}", file=sys.__stdout__, flush=True)
        self.get_application().quit()
        return False


# ------------------------------------------------------------------------------

_stdout: Optional[M.ThreadStdout] = None


def M_stdout() -> M.ThreadStdout:
    global _stdout
    if _stdout is None:
        _stdout = M.ThreadStdout(sys.stdout)
        sys.stdout = _stdout
    return _stdout


def M_load_config(args):
    from .library import load_config
    cfg = load_config(getattr(args, "config", None), scores=getattr(args, "scores", None),
                      output=getattr(args, "output", None))
    if not cfg.scores:
        raise ValueError("no scores folder: set [library] scores in pianovision.toml")
    if not os.path.isdir(cfg.scores):
        raise FileNotFoundError(f"scores folder not found: {cfg.scores}")
    return cfg


def run_gui(args) -> int:
    M_stdout()
    base = Adw.Application if Adw else Gtk.Application
    flags = Gio.ApplicationFlags.NON_UNIQUE if getattr(args, "screenshot", None) else Gio.ApplicationFlags.DEFAULT_FLAGS
    app = base(application_id=APP_ID, flags=flags)
    GLib.set_application_name(TITLE)

    def activate(a):
        win = a.get_active_window()
        if win is None:
            win = Window(a, args)
        win.present()

    app.connect("activate", activate)
    return app.run([sys.argv[0]])
