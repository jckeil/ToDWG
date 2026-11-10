# -*- coding: utf-8 -*-
"""Тест интерфейса: строит главное окно и опрашивает виджеты."""
import os
import sys
import tempfile

import tkinter as tk

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ToDXF_v1 as app

report = []


def mainloop_stub(self, n=0):
    def walk(w):
        yield w
        for c in w.winfo_children():
            yield from walk(c)

    check = None
    spin = None
    for w in walk(self):
        try:
            if isinstance(w, tk.Label):
                text = w.cget("text")
                if text:
                    report.append(("label", text))
            elif isinstance(w, tk.Checkbutton):
                check = w
                report.append(("check", w.cget("text"), w.cget("state")))
            elif isinstance(w, tk.Spinbox):
                spin = w
                report.append(("spin", w.cget("from"), w.cget("to"),
                               w.cget("state")))
        except Exception as e:
            report.append(("error", str(e)))

    report.append(("title", self.title()))
    report.append(("geometry", self.geometry()))

    if check is not None and spin is not None:
        check.invoke()
        self.update()
        report.append(("spin_after_on", spin.cget("state")))
        report.append(("hint_after_on", spin.get()))
        check.invoke()
        self.update()
        report.append(("spin_after_off", spin.cget("state")))

    self.destroy()


tk.Tk.mainloop = mainloop_stub
code = app.run_gui()

out_path = os.path.join(tempfile.gettempdir(), "ToDXF_gui_selftest.txt")
with open(out_path, "w", encoding="utf-8") as f:
    for item in report:
        f.write("|".join(str(x) for x in item) + "\n")
    f.write(f"EXIT_CODE={code}\n")
print(out_path)
sys.exit(0)
