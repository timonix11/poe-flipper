"""Selling session UI: the small always-on-top panel with the queue, the
hotkey that handles the next item, and the popup that asks for a quantity
or a price whenever the tick boxes say to ask.

Pricing is flipper.py's, untouched: flipper.build_suggestions decides, and
flipper.auto_fill types the amounts.  Place Order is always the user's click.
"""
import ctypes
import queue
import threading
import traceback
import tkinter as tk
import tkinter.font as tkfont
from ctypes import wintypes

import flipper
import seller

BG, FG, ACCENT, BTN_BG = flipper.BG, flipper.FG, flipper.ACCENT, flipper.BTN_BG
DIM, BAD, GOOD = "#8a8272", "#d06050", "#7aa86a"
HOTKEY_DEFAULT = "alt+w"


def fmt_int(n):
    return f"{int(round(n)):,}".replace(",", " ")


WARN = "#d0a050"


def auto_ok(sugg):
    """May a row be filled in without showing the list first?  Not when
    flipper flags the market as dead/thin ("prices may be junk"): then the
    list with its warning is shown, as in a normal flipper check."""
    return not sugg.get("dead")


def instant_note(sugg):
    """flipper's remark that the standing buyers already beat the listing
    price.  The row is still filled in automatically — it is the row Enter
    would pick — but the remark goes on the panel next to it."""
    return next((note for note in sugg.get("notes", [])
                 if "instant price" in note), None)


# The session panel is a low bar hugging the bottom edge of the screen,
# between the flasks and the skill bar.  It must stay out of everything the
# helper reads or clicks: the item picker reaches down to ~89% of the screen
# height (its search box is the lowest thing in it), and the first version
# of this panel sat right on that search box — the helper then OCR'd its own
# text instead of the search field and clicked its own buttons.
BAR_WIDTH = 0.40        # of the screen width, centred (30%..70%)
BAR_MAX_WIDTH = 0.70    # ... and never wider than this
BAR_BOTTOM = 0.995      # bottom edge, as a share of the screen height
BAR_CEILING = 0.90      # no part of the bar may be above this


def bar_rect(screen_w, screen_h, wanted_height, min_width=0):
    """(x, y, width, height) of the session panel.  `min_width` is what its
    buttons and a readable line of text need: on a small screen with large
    text scaling 40% of the width is not enough, so the bar widens (it may
    then cover part of the flask and skill bars, which nothing here reads).
    A panel that wants more height than the strip under the picker offers
    is cut off at the bottom rather than allowed to grow upwards."""
    width = min(max(int(screen_w * BAR_WIDTH), int(min_width)),
                int(screen_w * BAR_MAX_WIDTH))
    bottom = int(screen_h * BAR_BOTTOM)
    height = max(1, min(int(wanted_height),
                        bottom - int(screen_h * BAR_CEILING)))
    return ((screen_w - width) // 2, bottom - height, width, height)


class SellPopup(flipper.Popup):
    """flipper's own popup narrowed to one job: sell the item that is on the
    panel.  With `ask_qty` it starts on the quantity field (pre-filled with
    the whole stash stack, Enter takes it); with `auto` the "Undercut all"
    row is filled in without showing the list, unless flipper has a warning
    for that book — then the usual list appears."""

    def __init__(self, root, data, item, ask_qty, auto, on_fill, on_cancel):
        self._item, self._ask_qty, self._auto = item, ask_qty, auto
        self._on_fill, self._on_cancel = on_fill, on_cancel
        self._settled = False
        super().__init__(root, {"data": data, "error": None})
        if not ask_qty:
            self.win.after(0, lambda: self._on_submit(None))

    def _build_input_stage(self):
        qty = self._item["qty"]
        tk.Label(self.frame, text=f"Sell {self._item['name']}", bg=BG,
                 fg=ACCENT, font=("Segoe UI", 11, "bold")).pack(anchor="w")
        self.status = tk.Label(self.frame, text="", bg=BG, fg=DIM,
                               font=("Segoe UI", 8))
        self.status.pack(anchor="w")
        tk.Label(self.frame,
                 text=f"How many? {fmt_int(qty)} in the stash. "
                      "Enter = all of it, or type a number",
                 bg=BG, fg=FG, font=("Segoe UI", 9)).pack(anchor="w",
                                                         pady=(6, 0))
        entry = tk.Entry(self.frame, font=("Segoe UI", 14), width=12,
                         bg="#2b251e", fg=FG, insertbackground=FG,
                         relief="flat")
        entry.pack(anchor="w", ipady=3)
        entry.insert(0, str(qty))
        entry.select_range(0, "end")
        entry.icursor("end")
        entry.bind("<KeyPress>", lambda ev: self._entry_key(ev, alt=False))
        entry.bind("<Alt-KeyPress>", lambda ev: self._entry_key(ev, alt=True))
        self.entry_sell = self.entry = entry
        # the base class reads these two as well; they stay empty and unseen
        self.entry_buy = tk.Entry(self.frame)
        self.entry_qty = tk.Entry(self.frame)
        self._focus_target = entry
        entry.focus_set()

    def _build_result_stage(self, n, sell_sugg, m, buy_sugg, q=None,
                            qty_sugg=None):
        row = seller.choose_row(sell_sugg) if self._auto and sell_sugg else None
        market = self.ref["data"].get("market_ref")
        if row is not None and auto_ok(sell_sugg) and \
                seller.plausible(row["price"], market):
            tk.Label(self.frame,
                     text=f"Undercut all: give {row['used']} → "
                          f"get {row['want_total']}",
                     bg=BG, fg=ACCENT, font=("Segoe UI", 11, "bold")).pack()
            # the base class still places and focuses the window after this
            # returns, so the fill starts on the next turn of the event loop
            warn = instant_note(sell_sugg)
            self.win.after(0, lambda: self._do_fill(row, warn))
            return
        super()._build_result_stage(n, sell_sugg, m, buy_sugg, q, qty_sugg)

    def _fill_btn(self, parent, s):
        return tk.Button(parent, text="⤷ FILL",
                         command=lambda: self._do_fill(s), bg=ACCENT,
                         fg="#1e1a16", activebackground="#e0be6a",
                         activeforeground="#1e1a16", relief="flat",
                         font=("Segoe UI", 9, "bold"), padx=10,
                         cursor="hand2", highlightthickness=2,
                         highlightbackground=BG, highlightcolor="#f5f0e0")

    def _do_fill(self, row, warn=None):
        if self._settled:
            return
        self._settled = True
        super().close()
        self._on_fill(row, warn)

    def close(self):
        super().close()
        if not self._settled:
            self._settled = True
            self._on_cancel()


class SellSession:
    """One run through the queue.  `state` is the stash window's live state
    dict: its "sell_all" / "undercut" tick boxes are read for every item, so
    they can be changed mid-session."""

    def __init__(self, root, rows, state, on_end, min_total=None):
        self.root, self.state, self.on_end = root, state, on_end
        self.queue = seller.SellQueue(
            rows, state.get("min_total", 0.0) if min_total is None
            else min_total)
        self.game = seller.Game()
        self.q = queue.Queue()
        self.busy = False
        self.awaiting = False       # amounts are typed in, waiting for the
                                    # user's Place Order
        self.popup = None
        self.closed = False
        self.geo = None
        self.market = None          # Market Ratio of the item being handled
        self._hotkey_thread_id = None
        flipper.apply_config()
        self.hotkey = str(state.get("sell_hotkey") or HOTKEY_DEFAULT)
        try:
            self._mods, self._vk = flipper.parse_hotkey(self.hotkey)
        except ValueError:
            self.hotkey = HOTKEY_DEFAULT
            self._mods, self._vk = flipper.parse_hotkey(self.hotkey)
        self._build()
        threading.Thread(target=self._hotkey_listener, daemon=True).start()
        threading.Thread(target=flipper.warmup_ocr, daemon=True).start()
        self._show_current()
        self.root.after(60, self._poll)

    # ---------------------------------------------------------- overlay ---

    def _build(self):
        win = self.win = tk.Toplevel(self.root)
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        win.configure(bg=BG, highlightthickness=2, highlightbackground=ACCENT)
        frame = tk.Frame(win, bg=BG, padx=12, pady=5)
        frame.pack(fill="both", expand=True)
        buttons = tk.Frame(frame, bg=BG)
        buttons.pack(side="right", padx=(12, 0))
        self.btn_go = self._button(buttons, f"Next item ({self.hotkey})",
                                   self._go, primary=True)
        self.btn_go.pack(side="left")
        self.btn_skip = self._button(buttons, "Skip", self._skip)
        self.btn_skip.pack(side="left", padx=6)
        self._button(buttons, "Stop", self.stop).pack(side="left")
        text = tk.Frame(frame, bg=BG)
        text.pack(side="left", fill="both", expand=True)
        self.head = tk.Label(text, text="", bg=BG, fg=FG,
                             font=("Segoe UI", 10, "bold"), anchor="w")
        self.head.pack(fill="x")
        self.note = tk.Label(text, text="", bg=BG, fg=DIM,
                             font=("Segoe UI", 9), anchor="w", justify="left")
        self.note.pack(fill="x")
        win.update_idletasks()
        # everything but the text: buttons, their gap, the frame's padding
        # and the border.  The text gets room for ~80 characters a line:
        # the longest note is then two lines, which with the heading is all
        # the strip under the picker holds at 150% text scaling.
        chrome = buttons.winfo_reqwidth() + 12 + 2 * 12 + 4
        line = tkfont.Font(font=self.note["font"]).measure("0" * 80)
        self.min_width = chrome + line
        self.width = bar_rect(win.winfo_screenwidth(),
                              win.winfo_screenheight(), 1, self.min_width)[2]
        room = max(200, self.width - chrome)
        self.note.configure(wraplength=room)
        self.head.configure(wraplength=room)
        self._no_activate()
        self._place()

    def _button(self, parent, text, command, primary=False):
        return tk.Button(
            parent, text=text, command=command, relief="flat", padx=10,
            pady=1, cursor="hand2", font=("Segoe UI", 9, "bold"),
            bg=ACCENT if primary else BTN_BG, fg=BG if primary else FG,
            activebackground="#e0be6a" if primary else "#4a4234",
            activeforeground=BG if primary else FG, disabledforeground=DIM)

    def _no_activate(self):
        """Clicking the panel must not take keyboard focus from the game."""
        try:
            self.win.update_idletasks()
            u = ctypes.windll.user32
            hwnd = u.GetParent(self.win.winfo_id())
            u.GetWindowLongPtrW.restype = ctypes.c_ssize_t
            u.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
            u.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int,
                                            ctypes.c_ssize_t]
            style = u.GetWindowLongPtrW(hwnd, -20)             # GWL_EXSTYLE
            u.SetWindowLongPtrW(hwnd, -20, style | 0x08000000)  # NOACTIVATE
        except (OSError, AttributeError, tk.TclError):
            pass

    def _place(self):
        self.win.update_idletasks()
        x, y, w, h = bar_rect(self.win.winfo_screenwidth(),
                              self.win.winfo_screenheight(),
                              self.win.winfo_reqheight(), self.min_width)
        self.win.geometry(f"{w}x{h}+{x}+{y}")

    def area(self):
        """(x0, y0, x1, y1) of the panel on screen."""
        self.win.update_idletasks()
        x, y = self.win.winfo_rootx(), self.win.winfo_rooty()
        return (x, y, x + self.win.winfo_width(), y + self.win.winfo_height())

    def _set(self, note, colour=DIM):
        if self.closed:
            return
        self.note.configure(text=note, fg=colour)
        self._place()

    def _show_current(self):
        item = self.queue.current()
        if item is None:
            self.head.configure(
                text=f"Selling finished: listed "
                     f"{self.queue.count('done')}, skipped "
                     f"{self.queue.count('skipped')}")
            self.btn_go.configure(state="disabled")
            self.btn_skip.configure(state="disabled")
            self._set("Stop closes this bar.")
            return
        n, total = self.queue.position()
        self.head.configure(
            text=f"{n} of {total}:  {item['name']} × {fmt_int(item['qty'])}"
                 f"  ≈ {fmt_int(item['total'])} c")
        self._set(f"{self.hotkey} - pick it at the exchange, read the rates "
                  "and fill in the amounts.")

    # ----------------------------------------------------------- hotkey ---

    def _hotkey_listener(self):
        u = ctypes.windll.user32
        self._hotkey_thread_id = ctypes.windll.kernel32.GetCurrentThreadId()
        if not u.RegisterHotKey(None, 1, self._mods | 0x4000, self._vk):
            self.q.put(("no_hotkey", None))
            return
        msg = wintypes.MSG()
        while u.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == 0x0312:                           # WM_HOTKEY
                self.q.put(("go", None))
        u.UnregisterHotKey(None, 1)

    # ------------------------------------------------------------ steps ---

    def _popup_open(self):
        try:
            return self.popup is not None and self.popup.win.winfo_exists()
        except tk.TclError:
            return False

    def _go(self):
        if self.closed or self.busy or self._popup_open():
            return
        if self.awaiting:           # the previous item's order is the
            self.awaiting = False   # user's business now: move on
            self.queue.mark("done")
            self._show_current()
        item = self.queue.current()
        if item is None:
            return
        self.busy = True
        self._set("Picking the item and reading the rates...")
        threading.Thread(target=self._prepare, args=(item,),
                         daemon=True).start()

    def _prepare(self, item):
        try:
            got = seller.prepare_item(self.game, item["name"])
            self.q.put(("prepared", (item, got)))
        except seller.SellError as e:
            self.q.put(("failed", str(e)))
        except Exception:
            flipper.log("sell step failed:\n" + traceback.format_exc())
            self.q.put(("failed", "Failed - details in flipper.log"))

    def _prepared(self, item, got):
        self.busy = False
        self.geo = got["geo"]
        data = got["data"]
        self.market = data.get("market_ref")
        sell_all = bool(self.state.get("sell_all"))
        undercut = bool(self.state.get("undercut"))
        if sell_all and undercut:
            sugg = flipper.build_suggestions(data, item["qty"])
            row = seller.choose_row(sugg)
            if row is not None and not seller.plausible(row["price"],
                                                        self.market):
                self._ask(item, data, ask_qty=False, auto=False,
                          note=self._mismatch(row), colour=BAD)
            elif row is not None and auto_ok(sugg):
                self._fill(item, row, instant_note(sugg))
            else:
                self._ask(item, data, ask_qty=False, auto=False,
                          note="Flipper flags this market as dead or thin - "
                               "pick the row yourself.", colour=BAD)
            return
        self._ask(item, data, ask_qty=not sell_all, auto=undercut)

    def _mismatch(self, row):
        return (f"The table price ({row['price']:.4g} each) does not match "
                f"the Market Ratio on the panel ({self.market:.4g}) - looks like "
                "a misread. Check against the game.")

    def _ask(self, item, data, ask_qty, auto,
             note="Waiting for your choice in the flipper popup.", colour=DIM):
        self._set(note, colour)
        self.popup = SellPopup(
            self.root, data, item, ask_qty, auto,
            on_fill=lambda row, warn=None: self._fill(item, row, warn),
            on_cancel=lambda: self._set(
                f"Cancelled. {self.hotkey} = try again, or Skip."))

    def _fill(self, item, row, warn=None):
        self.busy = True
        have, want = row["used"], row["want_total"]
        if not seller.plausible(row["price"], self.market):
            warn = self._mismatch(row)      # the user picked it: say so, loudly
        elif warn:
            warn = "flipper: " + warn.split(" — ")[0]
        self._set(f"Filling in: give {fmt_int(have)} → get "
                  f"{fmt_int(want)}…")
        threading.Thread(target=self._fill_worker, args=(item, row, warn),
                         daemon=True).start()

    def _fill_worker(self, item, row, warn):
        try:
            flipper.auto_fill(row["used"], row["want_total"])
            if self.geo is not None:        # park the cursor on the button
                self.game.move(*self.geo.pt(seller.PLACE_ORDER_POS))
            self.q.put(("filled", (item, row, warn)))
        except Exception:
            flipper.log("sell fill failed:\n" + traceback.format_exc())
            self.q.put(("failed", "Could not fill in the amounts - "
                                  "details in flipper.log"))

    def _filled(self, item, row, warn=None):
        self.busy = False
        self.awaiting = True
        left = item["qty"] - row["used"]
        tail = f", {fmt_int(left)} left" if left > 0 else ""
        text = (f"Filled in {fmt_int(row['used'])} → "
                f"{fmt_int(row['want_total'])} ({row['w']}:{row['d']}){tail}"
                f" — check, press Place Order, then {self.hotkey}.")
        if warn:
            text += f"\n⚠ {warn}"
        self._set(text, WARN if warn else GOOD)

    def _skip(self):
        if self.closed or self.busy or self._popup_open():
            return
        self.awaiting = False
        self.queue.mark("skipped")
        self._show_current()

    # ---------------------------------------------------------- plumbing ---

    def _poll(self):
        if self.closed:
            return
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "go":
                    self._go()
                elif kind == "prepared":
                    self._prepared(*payload)
                elif kind == "filled":
                    self._filled(*payload)
                elif kind == "failed":
                    self.busy = False
                    self._set(f"{payload} {self.hotkey} = try again, or "
                              "Skip.", BAD)
                elif kind == "no_hotkey":
                    self._set(f"{self.hotkey} is taken by another program - "
                              "use the Next item button.",
                              BAD)
        except queue.Empty:
            pass
        self.root.after(60, self._poll)

    def stop(self):
        if self.closed:
            return
        self.closed = True
        if self._hotkey_thread_id:
            ctypes.windll.user32.PostThreadMessageW(
                self._hotkey_thread_id, 0x0012, 0, 0)           # WM_QUIT
        if self._popup_open():
            self.popup.close()
        try:
            self.win.destroy()
        except tk.TclError:
            pass
        self.on_end()
