"""The left navigation pane (Fluent NavigationView style).

One tk.Canvas draws every row: icon (Segoe Fluent Icons glyph) + label,
a rounded hover / selected background and the accent pill of the selected
page. Pages sit at the top, actions (theme toggle, clean up, settings, help)
in the footer. The ☰ button collapses the pane to icons only; collapsed rows
show their label as a tooltip.

Keyboard: Tab focuses the pane, Up / Down move, Enter / Space activate.
"""
import tkinter as tk
import tkinter.font as tkfont

from . import themes
from .icons import glyph

ROW_H = 40            # px at 100 %
PAD_X = 4
COLLAPSED_W = 48
MIN_W, MAX_W = 196, 280


class Sidebar(tk.Canvas):
    def __init__(self, master, on_select=None, on_toggle=None, title=""):
        super().__init__(master, highlightthickness=0, borderwidth=0, takefocus=1)
        self._on_select = on_select
        self._on_toggle = on_toggle
        self._title = title
        self._items = []           # dicts: key, icon, label, kind, command, tip, footer
        self._selected = None
        self._hover = None
        self._focus = None         # keyboard cursor (index into _items)
        self._has_focus = False
        self.collapsed = False
        self._imgs = {}            # (state, width) -> PhotoImage
        self._tip = None
        self._tip_job = None
        self._tip_for = None
        self._last_h = None
        self._relayout_job = None
        self.bind("<Motion>", self._motion)
        self.bind("<Leave>", self._leave)
        self.bind("<Button-1>", self._click)
        self.bind("<Configure>", self._on_configure)
        self.bind("<FocusIn>", lambda e: self._set_focus(True))
        self.bind("<FocusOut>", lambda e: self._set_focus(False))
        for seq, d in (("<Up>", -1), ("<Down>", 1)):
            self.bind(seq, lambda e, d=d: (self._move_focus(d), "break")[1])
        for seq in ("<Return>", "<space>", "<KP_Enter>"):
            self.bind(seq, lambda e: (self._activate_focus(), "break")[1])
        themes.on_palette(self, lambda p: self._redraw())

    # ------------------------------------------------------------ API
    def add_page(self, key, icon, label, tip=None):
        self._items.append(dict(key=key, icon=icon, label=label, kind="page",
                                command=None, tip=tip or label, footer=False))

    def add_action(self, key, icon, label, command, tip=None, footer=True):
        self._items.append(dict(key=key, icon=icon, label=label, kind="action",
                                command=command, tip=tip or label, footer=footer))

    def set_item(self, key, icon=None, label=None, tip=None):
        for it in self._items:
            if it["key"] == key:
                if icon is not None:
                    it["icon"] = icon
                if label is not None:
                    it["label"] = label
                    it["tip"] = tip or label
                if tip is not None:
                    it["tip"] = tip
        self._redraw()

    def select(self, key, notify=False):
        if key == self._selected:
            return
        self._selected = key
        self._draw_states()
        if notify and self._on_select:
            self._on_select(key)

    def selected(self):
        return self._selected

    def page_keys(self):
        return [it["key"] for it in self._items if it["kind"] == "page"]

    def expanded_width(self):
        """Width that fits the longest label (clamped)."""
        try:
            f = tkfont.nametofont("TkDefaultFont")
            longest = max((f.measure(it["label"]) for it in self._items), default=0)
        except tk.TclError:
            longest = 120
        w = themes.px(PAD_X * 2 + 44 + 16) + longest
        return max(themes.px(MIN_W), min(themes.px(MAX_W), w))

    def set_collapsed(self, collapsed, notify=True):
        self.collapsed = bool(collapsed)
        self.configure(width=themes.px(COLLAPSED_W) if self.collapsed
                       else self.expanded_width())
        self._redraw()
        if notify and self._on_toggle:
            self._on_toggle(self.collapsed)

    def toggle(self):
        self.set_collapsed(not self.collapsed)

    # ------------------------------------------------------------ geometry
    def _width(self):
        return themes.px(COLLAPSED_W) if self.collapsed else self.expanded_width()

    def _rows(self):
        """[(index, y_top)] for every item + y of the menu button row."""
        rh = themes.px(ROW_H)
        top = themes.px(4)
        out = []
        y = top + rh + themes.px(8)          # below the ☰ row
        for i, it in enumerate(self._items):
            if not it["footer"]:
                out.append((i, y))
                y += rh + themes.px(2)
        h = max(self.winfo_height(), y + rh)
        foot = [i for i, it in enumerate(self._items) if it["footer"]]
        y = h - themes.px(8) - len(foot) * (rh + themes.px(2))
        for i in foot:
            out.append((i, y))
            y += rh + themes.px(2)
        return out, top

    def _index_at(self, y):
        rh = themes.px(ROW_H)
        rows, top = self._rows()
        if top <= y < top + rh:
            return "menu"
        for i, y0 in rows:
            if y0 <= y < y0 + rh:
                return i
        return None

    # ------------------------------------------------------------ drawing
    def _photo(self, im):
        import base64
        import io
        buf = io.BytesIO()
        im.save(buf, "PNG")
        return tk.PhotoImage(master=self, data=base64.b64encode(buf.getvalue()))

    def _bg_img(self, state, w, ring=False):
        """Row background: rounded hover / selected fill, optionally with the
        keyboard focus ring - flattened onto the pane colour (opaque photos
        are a plain blit for Tk; alpha ones get blended on every redraw)."""
        key = (state, w, ring)
        img = self._imgs.get(key)
        if img is not None:
            return img
        try:
            from PIL import Image, ImageDraw
        except Exception:
            return None
        p = themes.current()
        h = themes.px(ROW_H)
        ss = 4
        im = Image.new("RGB", (w * ss, h * ss), p["sidebar_bg"])
        d = ImageDraw.Draw(im)
        r = themes.px(5) * ss
        fill = {"sel": p["sidebar_sel"], "hover": p["sidebar_hover"]}.get(state,
                                                                        p["sidebar_bg"])
        if ring:
            t = max(1, themes.px(2)) * ss
            d.rounded_rectangle((0, 0, w * ss - 1, h * ss - 1), r, fill=p["fg"])
            d.rounded_rectangle((t, t, w * ss - 1 - t, h * ss - 1 - t), max(0, r - t),
                                fill=fill)
        else:
            d.rounded_rectangle((0, 0, w * ss - 1, h * ss - 1), r, fill=fill)
        img = self._photo(im.resize((w, h), Image.LANCZOS))
        self._imgs[key] = img
        return img

    def _pill_img(self):
        """The accent pill of the selected row (on the selected fill)."""
        img = self._imgs.get("pill")
        if img is not None:
            return img
        try:
            from PIL import Image, ImageDraw
        except Exception:
            return None
        p = themes.current()
        w, h, ss = themes.px(3), themes.px(16), 4
        im = Image.new("RGB", (w * ss, h * ss), p["sidebar_sel"])
        ImageDraw.Draw(im).rounded_rectangle((0, 0, w * ss - 1, h * ss - 1), w * ss // 2,
                                             fill=p["accent"])
        img = self._photo(im.resize((w, h), Image.LANCZOS))
        self._imgs["pill"] = img
        return img

    def _redraw(self):
        """Rebuild every canvas item (theme change, collapse, resize)."""
        self._imgs = {}
        p = themes.current()
        self.configure(bg=p["sidebar_bg"])
        self.delete("all")
        w = self._width()
        rw = w - 2 * themes.px(PAD_X)
        rh = themes.px(ROW_H)
        x0 = themes.px(PAD_X)
        icon_cx = x0 + themes.px(20)
        text_x = x0 + themes.px(44)
        rows, top = self._rows()
        # ☰ + app name
        self.create_image(x0, top, anchor="nw", tags=("bg", "bg_menu"))
        self.create_text(icon_cx, top + rh // 2, text=glyph("menu") or "≡",
                         font="MPIcon" if glyph("menu") else "MPSubtitle",
                         fill=p["fg"], tags=("menu",))
        if not self.collapsed and self._title:
            self.create_text(text_x, top + rh // 2, text=self._title, anchor="w",
                             font="MPSemibold", fill=p["fg"], tags=("menu",))
        for i, y in rows:
            it = self._items[i]
            ft = ("foot",) if it["footer"] else ()
            self.create_image(x0, y, anchor="nw", tags=("bg", f"bg{i}") + ft)
            self.create_image(x0, y + rh // 2, anchor="w", tags=("pill", f"pill{i}") + ft,
                              state="hidden")
            ic = glyph(it["icon"])
            self.create_text(icon_cx, y + rh // 2, text=ic or it["label"][:1],
                             font="MPIcon" if ic else "MPSemibold", fill=p["fg"],
                             tags=(f"icon{i}",) + ft)
            if not self.collapsed:
                self.create_text(text_x, y + rh // 2, text=it["label"], anchor="w",
                                 font="TkDefaultFont", fill=p["fg"],
                                 tags=(f"label{i}",) + ft,
                                 width=max(10, w - text_x - themes.px(8)))
        # hairline between the pane and the content
        self.create_line(w - 1, 0, w - 1, max(self.winfo_height(), 4000),
                         fill=p["border"] if p.get("hc") else p["stroke"])
        self._rw = rw
        self._draw_states()

    def _draw_states(self):
        rw = getattr(self, "_rw", None)
        if rw is None:
            return
        for i, it in enumerate(self._items):
            state = None
            if it["key"] == self._selected and it["kind"] == "page":
                state = "sel"
            elif i == self._hover:
                state = "hover"
            ring = self._has_focus and i == self._focus
            img = self._bg_img(state, rw, ring) if (state or ring) else None
            self.itemconfigure(f"bg{i}", image=img or "")
            if state and img is None:                  # no Pillow: plain rect
                self._rect(i, state)
            else:
                self.delete(f"rect{i}")
        for i, it in enumerate(self._items):
            sel = it["key"] == self._selected and it["kind"] == "page"
            self.itemconfigure(f"pill{i}", image=self._pill_img() or "",
                               state="normal" if sel else "hidden")
        menu_hover = self._hover == "menu"
        menu_ring = self._has_focus and self._focus == "menu"
        self.itemconfigure("bg_menu", image=self._bg_img("hover" if menu_hover else None, rw,
                                                         menu_ring)
                           if (menu_hover or menu_ring) else "")

    def _rect(self, i, state):
        self.delete(f"rect{i}")
        p = themes.current()
        rows, _top = self._rows()
        for j, y in rows:
            if j == i:
                x0 = themes.px(PAD_X)
                self.create_rectangle(x0, y, x0 + self._rw, y + themes.px(ROW_H), width=0,
                                      fill=p["sidebar_sel" if state == "sel"
                                            else "sidebar_hover"], tags=(f"rect{i}",))
                self.tag_lower(f"rect{i}")

    # ------------------------------------------------------------ events
    def _on_configure(self, e):
        # only the height matters: the footer rows stick to the bottom - one
        # canvas move, nothing is redrawn or re-laid out
        if e.height == self._last_h:
            return
        old, self._last_h = self._last_h, e.height
        if old is None or getattr(self, "_rw", None) is None:
            self._redraw()
            return
        self.move("foot", 0, e.height - old)
        for i, it in enumerate(self._items):
            if it["footer"]:
                self.move(f"ring{i}", 0, e.height - old)
                self.move(f"rect{i}", 0, e.height - old)

    def _motion(self, e):
        i = self._index_at(e.y)
        if i != self._hover:
            self._hover = i
            self._draw_states()
            self.configure(cursor="hand2" if i is not None else "")
            self._schedule_tip(i)

    def _leave(self, _e=None):
        self._hover = None
        self._draw_states()
        self._schedule_tip(None)

    def _click(self, e):
        self._hide_tip()
        i = self._index_at(e.y)
        if i == "menu":
            self.toggle()
        elif i is not None:
            self._activate(i)

    def _activate(self, i):
        it = self._items[i]
        if it["kind"] == "page":
            self.select(it["key"], notify=True)
        elif callable(it["command"]):
            it["command"]()

    def _order(self):
        rows, _top = self._rows()
        return ["menu"] + [i for i, _y in sorted(rows, key=lambda r: r[1])]

    def _set_focus(self, on):
        self._has_focus = on
        if on and self._focus is None:
            sel = [i for i, it in enumerate(self._items) if it["key"] == self._selected]
            self._focus = sel[0] if sel else "menu"
        self._draw_states()

    def _move_focus(self, d):
        order = self._order()
        cur = order.index(self._focus) if self._focus in order else 0
        self._focus = order[(cur + d) % len(order)]
        self._draw_states()

    def _activate_focus(self):
        if self._focus == "menu":
            self.toggle()
        elif self._focus is not None:
            self._activate(self._focus)

    # ------------------------------------------------------------ tooltips
    def _schedule_tip(self, i):
        if self._tip_job is not None:
            self.after_cancel(self._tip_job)
            self._tip_job = None
        self._hide_tip()
        if i is None or not self.collapsed:
            return
        self._tip_job = self.after(350, lambda: self._show_tip(i))

    def _show_tip(self, i):
        self._tip_job = None
        if i == "menu":
            text = self._title
        else:
            text = self._items[i]["tip"]
        rows, top = self._rows()
        y = top
        for j, yy in rows:
            if j == i:
                y = yy
        p = themes.current()
        tip = tk.Toplevel(self)
        tip.wm_overrideredirect(True)
        try:
            tip.attributes("-topmost", True)
        except tk.TclError:
            pass
        tk.Label(tip, text=text, bg=p["tooltip_bg"], fg=p["tooltip_fg"], font="TkTooltipFont",
                 padx=themes.px(8), pady=themes.px(4), relief="solid", borderwidth=1).pack()
        tip.wm_geometry(f"+{self.winfo_rootx() + self.winfo_width() + themes.px(4)}"
                        f"+{self.winfo_rooty() + y + themes.px(8)}")
        self._tip = tip

    def _hide_tip(self):
        if self._tip is not None:
            try:
                self._tip.destroy()
            except tk.TclError:
                pass
            self._tip = None
