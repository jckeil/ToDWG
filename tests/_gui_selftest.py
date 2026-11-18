# -*- coding: utf-8 -*-
"""Тест интерфейса: строит главное окно и опрашивает виджеты."""
import os
import sys
import tempfile

import tkinter as tk
from tkinter import ttk

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ToDWG_v1 as app

report = []


def mainloop_stub(self, n=0):
    def walk(w):
        yield w
        for c in w.winfo_children():
            yield from walk(c)

    check = None
    format_check = None
    spin = None
    combos = []
    buttons = []
    checks = []
    for w in walk(self):
        try:
            if isinstance(w, tk.Label):
                text = w.cget("text")
                if text:
                    report.append(("label", text))
            elif isinstance(w, tk.Checkbutton):
                text = w.cget("text")
                checks.append(text)
                if text == "Указать количество листов вручную":
                    check = w
                if text.startswith("Выбрать формат листа вручную"):
                    format_check = w
                report.append(("check", text, w.cget("state")))
            elif isinstance(w, tk.Spinbox):
                spin = w
                report.append(("spin", w.cget("from"), w.cget("to"),
                               w.cget("state")))
            elif isinstance(w, ttk.Combobox):
                combos.append(w)
                report.append(("combo", w.get(), w.cget("state")))
            elif isinstance(w, tk.Button):
                buttons.append(w)
                report.append(("button", w.cget("text"), w.cget("state")))
        except Exception as e:
            report.append(("error", str(e)))

    report.append(("title", self.title()))
    report.append(("geometry", self.geometry()))
    report.append(("reqheight", self.winfo_reqheight()))
    report.append(("checkboxes", ", ".join(checks)))

    self.update()
    card = self.winfo_children()[0]
    bottom = 0
    layout = []
    for child in card.winfo_children():
        text = ""
        try:
            text = str(child.cget("text"))[:24]
        except Exception:
            pass
        y = child.winfo_y()
        h = child.winfo_height()
        layout.append(f"{child.winfo_class()}:{text or '-'}@{y}+{h}")
        if text == app.APP_DISPLAY_VERSION:
            continue
        bottom = max(bottom, y + h)
    report.append(("content_bottom", bottom,
                   "card_h", card.winfo_height(),
                   "root_h", self.winfo_height()))
    report.append(("layout", " | ".join(layout)))

    canvas = None
    dark = None
    for w in walk(self):
        if isinstance(w, tk.Canvas):
            try:
                width = int(str(w.cget("width")))
            except Exception:
                width = 0
            if width == 42:
                dark = w
            else:
                canvas = w

    if canvas is not None:
        report.append(("canvas_wheel_bound",
                       bool(canvas.bind("<MouseWheel>"))))

    if dark is not None:
        style = ttk.Style()
        report.append(("theme_light", self.cget("bg"), card.cget("bg"),
                       spin.cget("bg") if spin is not None else "-",
                       style.lookup("TCombobox", "fieldbackground")))
        dark.event_generate("<Button-1>", x=5, y=5)
        self.update()
        report.append(("theme_dark", self.cget("bg"), card.cget("bg"),
                       spin.cget("bg") if spin is not None else "-",
                       spin.cget("disabledbackground")
                       if spin is not None else "-",
                       style.lookup("TCombobox", "fieldbackground")))
        dark.event_generate("<Button-1>", x=5, y=5)
        self.update()
        report.append(("theme_back", self.cget("bg"), card.cget("bg")))

    if check is not None and spin is not None:
        check.invoke()
        self.update()
        report.append(("spin_after_on", spin.cget("state")))
        report.append(("hint_after_on", spin.get()))
        check.invoke()
        self.update()
        report.append(("spin_after_off", spin.cget("state")))

    if format_check is not None and combos:
        format_check.invoke()
        self.update()
        report.append(("combo_after_on",
                       ", ".join(str(c.cget("state")) for c in combos)))
        format_check.invoke()
        self.update()
        report.append(("combo_after_off",
                       ", ".join(str(c.cget("state")) for c in combos)))

    # --- сетевые пути: диалоги обязаны отклонять выбор ------------------
    net_errors = []
    old_askdir = app.filedialog.askdirectory
    old_askfiles = app.filedialog.askopenfilenames
    old_showerror = app.messagebox.showerror
    app.filedialog.askdirectory = lambda **kw: r"\\192.0.2.1\share"
    app.filedialog.askopenfilenames = lambda **kw: (
        r"\\192.0.2.1\share\a.sdd",)
    app.messagebox.showerror = (
        lambda title, text, **kw: net_errors.append((title, text)))
    try:
        by_text = {b.cget("text"): b for b in buttons}
        folder_btn = by_text.get("Обработать папку…")
        select_btn = by_text.get("Добавить файлы")
        if folder_btn is not None:
            folder_btn.invoke()
            self.update()
        if select_btn is not None:
            select_btn.invoke()
            self.update()
    finally:
        app.filedialog.askdirectory = old_askdir
        app.filedialog.askopenfilenames = old_askfiles
        app.messagebox.showerror = old_showerror
    report.append((
        "net_dialogs_rejected",
        sum(1 for t, _ in net_errors if t == "Сетевые пути запрещены"),
        "|".join(t for t, _ in net_errors)))

    # --- папка результата: после локального выбора предупреждение снимается ---
    def visible_texts():
        texts = []
        for w in walk(self):
            try:
                if not w.winfo_ismapped():
                    continue
                text = str(w.cget("text"))
                var = str(w.cget("textvariable"))
                if var:
                    text = (text + " " + str(self.getvar(var))).strip()
                if text:
                    texts.append(text)
            except Exception:
                pass
        return texts

    local_dir = tempfile.mkdtemp(prefix="ToDWG_gui_out_")
    ask_values = [r"\\192.0.2.1\share", local_dir]
    app.filedialog.askdirectory = lambda **kw: ask_values.pop(0)
    app.messagebox.showerror = (
        lambda title, text, **kw: net_errors.append((title, text)))
    net_hint = False
    local_hint_cleared = False
    try:
        for w in walk(self):
            if (isinstance(w, tk.Checkbutton)
                    and w.cget("text") == "Выбрать папку для готовых файлов"):
                w.invoke()
                break
        self.update()
        by_text = {b.cget("text"): b for b in buttons}
        edit_btn = by_text.get("Изменить…")
        if edit_btn is not None:
            edit_btn.invoke()
            self.update()
            net_hint = any("запрещ" in t for t in visible_texts())
            edit_btn.invoke()
            self.update()
            local_hint_cleared = not any(
                "запрещ" in t for t in visible_texts())
    finally:
        app.filedialog.askdirectory = old_askdir
        app.messagebox.showerror = old_showerror
    report.append(("outdir_net_hint", net_hint))
    report.append(("outdir_local_hint_cleared", local_hint_cleared))

    self.destroy()


tk.Tk.mainloop = mainloop_stub
code = app.run_gui()

out_path = os.path.join(tempfile.gettempdir(), "ToDWG_gui_selftest.txt")
with open(out_path, "w", encoding="utf-8") as f:
    for item in report:
        f.write("|".join(str(x) for x in item) + "\n")
    f.write(f"EXIT_CODE={code}\n")
print(out_path)
sys.exit(0)
