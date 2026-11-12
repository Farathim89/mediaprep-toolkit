"""Small shared widgets: translated drop-downs (KeyedCombobox), fixed-size
labels, theme-aware status colours, the log tab, the page container
(ScrollFrame), window-only bindings and file drag-and-drop.

The other shared widgets: ui/timeentry.py (TimeEntry), ui/tooltips.py
(Tooltip, add_tooltip, info_icon), ui/helpwin.py ("?" help buttons, help
windows, the Info / Recommended pages), ui/infobar.py (InfoBar)."""
import tkinter as tk
from tkinter import ttk

from . import themes
from ..i18n import tr


# ======================= translated drop-downs =======================
class KeyedCombobox(ttk.Combobox):
    """A ttk.Combobox that SHOWS translated labels while its `textvariable`
    keeps the stable KEY (the English text the settings / dicts use).

        KeyedCombobox(parent, textvariable=self.codec_var,
                      values=list(CODECS), state="readonly", width=40)

    values = the keys; labels default to tr(key) (labels=[...] overrides,
    translate=False shows the keys as-is). var.get() / cb.get() return the
    key, var.set(key) / cb.set(key) select it. configure(values=...),
    cb["values"] = ... and cget("values") work with keys too. Text typed into
    an editable box that isn't a known label is passed through as the key, so
    free values (e.g. "-14") keep working."""

    def __init__(self, master=None, textvariable=None, values=(), labels=None,
                 translate=True, **kw):
        self._keyvar = textvariable if textvariable is not None else tk.StringVar(master=master)
        self._translate = translate
        self._keys, self._k2l, self._l2k = [], {}, {}
        self._sync = False
        super().__init__(master, **kw)
        self._disp = tk.StringVar(master=self)
        super().configure(textvariable=self._disp)
        self._set_values(values, labels)
        self._t_key = self._keyvar.trace_add("write", self._key_changed)
        self._disp.trace_add("write", self._disp_changed)
        self.bind("<Destroy>", self._untrace, add="+")
        self._key_changed()

    # -- mapping
    def _label(self, key):
        key = str(key)
        if not self._translate:
            return key
        from ..i18n import tr
        return tr(key)

    def _set_values(self, keys, labels=None):
        if isinstance(keys, str):
            keys = self.tk.splitlist(keys)
        self._keys = [str(k) for k in (keys or ())]
        labels = list(labels) if labels is not None else [self._label(k) for k in self._keys]
        self._k2l = dict(zip(self._keys, labels))
        self._l2k = {lbl: k for k, lbl in zip(self._keys, labels)}
        super().configure(values=labels)
        self._key_changed()

    def _key_changed(self, *_a):
        if self._sync:
            return
        k = self._keyvar.get()
        self._sync = True
        try:
            self._disp.set(self._k2l.get(k, k))
        finally:
            self._sync = False

    def _disp_changed(self, *_a):
        if self._sync:
            return
        d = self._disp.get()
        self._sync = True
        try:
            self._keyvar.set(self._l2k.get(d, d))
        finally:
            self._sync = False

    def _untrace(self, e):
        if e.widget is self and self._t_key is not None:
            try:
                self._keyvar.trace_remove("write", self._t_key)
            except (tk.TclError, ValueError):
                pass
            self._t_key = None

    # -- Combobox API with keys
    def configure(self, cnf=None, **kw):
        if isinstance(cnf, dict):
            kw = dict(cnf, **kw)
            cnf = None
        handled = False
        if "values" in kw or "labels" in kw:
            self._set_values(kw.pop("values", self._keys), kw.pop("labels", None))
            handled = True
        if handled and cnf is None and not kw:
            return None
        return super().configure(cnf, **kw)

    config = configure

    def __setitem__(self, key, value):
        if key == "values":
            self._set_values(value)
        else:
            super().__setitem__(key, value)

    def cget(self, key):
        if key == "values":
            return tuple(self._keys)
        if key == "textvariable":
            return str(self._keyvar)
        return super().cget(key)

    def __getitem__(self, key):
        return self.cget(key)

    def get(self):
        return self._keyvar.get()

    def set(self, value):
        self._keyvar.set(value)

    def label(self):
        """The text shown (translated)."""
        return self._disp.get()


def auto_wrap(label, margin=4, minimum=80, width=None):
    """Give a ttk/tk Label a FIXED wrap width so a long (translated) hint
    wraps instead of widening the page. Pages have a fixed layout (they
    scroll instead of reflowing when the window is small), so there is
    nothing to follow on <Configure>. width = px at 100 % scaling (default:
    the label's own wraplength if it has one, else 560)."""
    try:
        cur = int(float(str(label.cget("wraplength")) or 0))
    except (tk.TclError, ValueError):
        cur = 0
    w = themes.px(width) if width else (themes.px(cur) if cur > 0 else themes.px(560))
    try:
        label.configure(wraplength=max(minimum, w - margin))
    except tk.TclError:
        pass
    return label


# ======================= theme-aware status colours =======================
# result colours (OK / broken / warning) come from the theme palette (ok /
# error / warn), which keeps them readable (>= 4.5:1) on every theme's lists
_STATUS_KEYS = {"good": "ok", "bad": "error", "warn": "warn"}


def status_palette(_widget=None):
    """{"good", "bad", "warn"} -> colour for the current theme."""
    pal = themes.current()
    return {kind: pal[key] for kind, key in _STATUS_KEYS.items()}


def bind_status_colors(widget, mapping):
    """Colour Treeview tags by status kind, e.g. {"bad": "bad", "unknown":
    "warn"}, and re-colour them whenever the theme changes."""
    def apply(pal):
        for tag, kind in mapping.items():
            widget.tag_configure(tag, foreground=pal[_STATUS_KEYS[kind]])
    themes.on_palette(widget, apply)


# ======================= log Text size cap =======================
MAX_LOG_LINES = 20000


def trim_text_lines(text, max_lines=MAX_LOG_LINES, chunk=2000):
    """Keep a Text log from growing forever: once it holds more than
    max_lines, drop the oldest lines in one chunk (not one line per append)."""
    try:
        lines = int(text.index("end-1c").split(".")[0])
        if lines > max_lines + chunk:
            text.delete("1.0", f"{lines - max_lines}.0")
    except Exception:
        pass


def bind_window(top, sequence, fn):
    """Bind fn to `sequence` of the toplevel `top` ITSELF. top.bind() fires
    for every widget inside it as well (a toplevel's path is a bindtag of all
    its children): a <Configure> binding there costs a Python call for each
    widget laid out - hundreds when a page re-lays out. A private bindtag
    that only the toplevel carries avoids that."""
    tag = "MPWindow" + str(top).replace(".", "_")
    tags = top.bindtags()
    if tag not in tags:
        top.bindtags((tag,) + tuple(tags))
    top.bind_class(tag, sequence, fn, add="+")


# ======================= vertically scrollable frame =======================
def _wheel_should_pass(widget, own_canvas):
    """True if the mouse wheel over `widget` should be left to the widget
    itself (Text boxes, list boxes, tree views and other scrollable canvases
    scroll their own content)."""
    w = widget
    while w is not None and w is not own_canvas:
        try:
            cls = w.winfo_class()
        except Exception:
            return False
        if cls in ("Text", "Listbox", "Treeview", "TCombobox"):
            return True
        if cls == "Canvas":
            try:
                if w.cget("yscrollcommand"):
                    return True
            except Exception:
                pass
        w = getattr(w, "master", None)
    return False


class ScrollFrame(ttk.Frame):
    """A tab's container: `.interior` for the content, `.bottom` for a strip
    pinned below it (progress bars).

    Pages have a FIXED natural size: they don't reflow with the window. The
    window's page area (ui/pageview.PageView) scrolls the whole page - both
    directions, scrollbars only when the window is smaller than the page -
    so this frame no longer scrolls by itself; it just passes its content's
    natural size up. (The name and attributes are kept for the tabs.)"""

    def __init__(self, master, canvas_width=None, bottom_master=None, **kw):
        super().__init__(master, **kw)
        # bottom_master = the window's fixed footer (app.py): the strip is
        # built there - outside the scrolling page - and app.py shows the
        # strip of the visible page only. Without it the strip sits below
        # the content (scrolls with the page).
        if bottom_master is not None:
            self.bottom = ttk.Frame(bottom_master)
        else:
            self.bottom = ttk.Frame(self)
            self.bottom.pack(side="bottom", fill="x")
        self.interior = ttk.Frame(self)
        self.interior.pack(side="top", fill="both", expand=True)


def fixed_label(parent, samples, style="TLabel", anchor="w", **kw):
    """A ttk.Label inside a frame of FIXED size - the widest of `samples`
    (texts) in the label's font, measured with every digit 0-9 - so a text
    that changes many times a second (a time / frame counter, a percentage)
    never changes the label's size and never makes the layout around it
    shift. `samples` may be a callable returning the texts. Re-measured
    when the theme (fonts) changes, or by holder.remeasure() (e.g. once a
    longer value becomes possible). Returns (holder_frame, label); place
    the holder."""
    import tkinter.font as tkfont
    holder = ttk.Frame(parent)
    holder.pack_propagate(False)
    lbl = ttk.Label(holder, style=style, anchor=anchor, **kw)
    lbl.pack(fill="both", expand=True)

    def measure(_p=None, idle=True):
        try:
            font = lbl.cget("font") or ttk.Style(lbl).lookup(style, "font") or "TkDefaultFont"
            f = tkfont.Font(root=lbl, font=font)
            pad = ttk.Style(lbl).lookup(style, "padding") or 0
            try:
                extra = sum(int(float(v)) for v in str(pad).split()[:1]) * 2
            except ValueError:
                extra = 0
            w = 0
            for s in (samples() if callable(samples) else samples):
                w = max(w, max(f.measure("".join(d if ch.isdigit() else ch for ch in s))
                               for d in "0123456789"))
            if idle:
                lbl.update_idletasks()
            holder.configure(width=w + extra + themes.px(4),
                             height=max(f.metrics("linespace") + themes.px(2),
                                        lbl.winfo_reqheight()))
        except tk.TclError:
            pass

    def on_theme(_p=None):
        from . import dpi
        if not dpi.manager.busy:        # a display scaling change: see below
            measure()
    measure()
    themes.on_palette(holder, on_theme)
    # another display scaling (ui/dpi.py): once the rescaled UI is laid out
    # (one layout pass for all of them, not one per label)
    from . import dpi
    dpi.on_change(holder, lambda _r: measure(idle=False), late=True)
    holder.remeasure = measure
    return holder, lbl


# ======================= drag-and-drop (optional) =======================
import re as _re

try:
    from tkinterdnd2 import TkinterDnD as _TkinterDnD, DND_FILES as _DND_FILES
    _HAS_DND = True
except Exception:
    _HAS_DND = False


def make_root():
    """A drag-and-drop capable Tk root if tkinterdnd2 is installed, else plain Tk."""
    if _HAS_DND:
        try:
            return _TkinterDnD.Tk()
        except Exception:
            pass
    return tk.Tk()


def _dropped_paths(data, widget=None):
    """The paths of a file drop. tkdnd hands them over as a Tcl list: a path
    with spaces is braced, one with braces in it too - Plex-style names like
    {C:/Show (2019) {tvdb-1}/ep.mkv} - or backslash-escaped, so Tcl itself
    splits it (a regex can't follow nested braces)."""
    if not data:
        return []
    try:
        tkapp = widget.tk if widget is not None else tk._default_root.tk
        return [str(p) for p in tkapp.splitlist(data)]
    except (tk.TclError, AttributeError):
        pass
    out = []                    # not a valid Tcl list: best effort
    for p in _re.findall(r"\{[^}]*\}|\S+", str(data)):
        out.append(p[1:-1] if p.startswith("{") and p.endswith("}") else p)
    return out


def _first_dropped_path(data, widget=None):
    parts = _dropped_paths(data, widget)
    return parts[0] if parts else ""


def enable_file_drop(widget, callback):
    """Let a video file be dropped onto `widget`; calls callback(path).
    No-op (returns False) when tkinterdnd2 isn't available."""
    if not _HAS_DND:
        return False
    try:
        widget.drop_target_register(_DND_FILES)
        widget.dnd_bind("<<Drop>>",
                        lambda e: callback(_first_dropped_path(e.data, widget)))
        return True
    except Exception:
        return False


def enable_file_drop_deep(widget, callback):
    """Register file-drop on `widget` AND every descendant, so a drop anywhere
    inside a composite widget (e.g. the whole video player - canvas, timeline,
    buttons, empty space) works, not just on one sub-widget."""
    if not _HAS_DND:
        return False
    enable_file_drop(widget, callback)
    for child in widget.winfo_children():
        enable_file_drop_deep(child, callback)
    return True


def _all_dropped_paths(data, widget=None):
    return _dropped_paths(data, widget)


def enable_paths_drop(widget, callback):
    """Drop one or more files/folders onto `widget`; calls callback([paths]).
    No-op (returns False) when tkinterdnd2 isn't available."""
    if not _HAS_DND:
        return False
    try:
        widget.drop_target_register(_DND_FILES)
        widget.dnd_bind("<<Drop>>",
                        lambda e: callback(_all_dropped_paths(e.data, widget)))
        return True
    except Exception:
        return False


def build_log_tab(notebook, text=None):
    """Add a '  Log  ' page to *notebook* holding a full-height, scrollable log
    box plus a 'Clear log' button. Returns the (read-only) Text widget so the
    caller can keep appending to it. Used by every tool tab so the log lives in
    its own sub-tab and the main view gets the whole window."""
    frame = ttk.Frame(notebook, padding=themes.pad(0, 10, 0, 6))
    notebook.add(frame, text=text if text is not None else f"  {tr('Log')}  ")
    frame.columnconfigure(0, weight=1)
    frame.rowconfigure(0, weight=1)
    # small requested height so this page doesn't force a hug-content notebook
    # tall; it still expands to fill via the row weight above
    box = tk.Text(frame, state="disabled", font="MPMono", wrap="none", height=8,
                  padx=themes.px(8), pady=themes.px(6))
    box.grid(row=0, column=0, sticky="nsew")
    sb = ttk.Scrollbar(frame, orient="vertical", command=box.yview)
    box.configure(yscrollcommand=sb.set)
    sb.grid(row=0, column=1, sticky="ns")

    def _clear():
        box.configure(state="normal")
        box.delete("1.0", "end")
        box.configure(state="disabled")
    from .icons import decorate
    decorate(ttk.Button(frame, text=tr("Clear log"), command=_clear), "trash").grid(
        row=1, column=0, sticky="w", pady=themes.pad(8, 0))
    return box
