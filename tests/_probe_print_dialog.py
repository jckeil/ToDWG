# -*- coding: utf-8 -*-
"""Снимок состава диалога печати Schemagee (для доработки чтения листов).

Использование:
    python tests\\_probe_print_dialog.py "путь\\файл.sdd"

Открывает .sdd в Schemagee, вызывает Ctrl+P и печатает все дочерние
контролы диалога: класс, id, доступность, координаты и текст.
"""
import ctypes
import os
import sys

import win32con
import win32gui

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ToDXF_v1 as m


def main():
    if len(sys.argv) < 2:
        print("usage: _probe_print_dialog.py file.sdd")
        return 1
    path = os.path.abspath(sys.argv[1])
    m.init_log()
    m.setup_logging_bridge()

    if not m.open_file_in_schemagee(path):
        print("не удалось открыть файл")
        return 1
    hwnd, title = m.wait_schemagee_window(timeout=30.0)
    if not hwnd:
        print("окно Schemagee не появилось")
        return 1
    m.wait_window_ready(hwnd, timeout=10.0)
    m.ensure_foreground(hwnd, what="Schemagee")
    m.switch_to_english(hwnd)
    m.pause(2.0)

    m.keyboard.send('ctrl+p')
    dialog = m.find_print_dialog(timeout=8.0)
    print("dialog:", dialog)
    if not dialog:
        m.close_schemagee(timeout=5.0)
        return 1

    m.wait_window_ready(dialog, timeout=3.0)
    rows = []

    def cb(child, _):
        try:
            rows.append((
                win32gui.GetWindowRect(child)[1],
                win32gui.GetWindowRect(child)[0],
                win32gui.GetClassName(child),
                win32gui.GetDlgCtrlID(child),
                win32gui.IsWindowEnabled(child),
                win32gui.GetWindowText(child),
            ))
        except Exception as e:
            rows.append((0, 0, "ERR", 0, False, str(e)))

    win32gui.EnumChildWindows(dialog, cb, None)
    for top, left, cls, ctrl_id, enabled, text in sorted(rows):
        print(f"{cls:18} id={ctrl_id:<6} en={enabled!s:<5} "
              f"at=({left},{top}) text={text!r}")

    # Ищем радио «С:» и поля диапазона по id (3.5: 1249, 1250, 1251).
    radio_range = 0
    edit_from = 0
    edit_to = 0
    radio_all = 0

    def cb2(child, _):
        nonlocal radio_range, edit_from, edit_to, radio_all
        try:
            cls = win32gui.GetClassName(child)
            cid = win32gui.GetDlgCtrlID(child)
            if cls == "Button" and cid == 1249:
                radio_range = child
            elif cls == "Button" and cid == 1248:
                radio_all = child
            elif cls == "Edit" and cid == 1250:
                edit_from = child
            elif cls == "Edit" and cid == 1251:
                edit_to = child
        except Exception:
            pass

    win32gui.EnumChildWindows(dialog, cb2, None)
    print("range radio:", radio_range, "from:", edit_from, "to:", edit_to)

    def get_text(hwnd):
        try:
            length = win32gui.SendMessage(hwnd, win32con.WM_GETTEXTLENGTH, 0, 0)
            buf = ctypes.create_unicode_buffer(length + 1)
            win32gui.SendMessage(hwnd, win32con.WM_GETTEXT, length + 1, buf)
            return buf.value
        except Exception as e:
            return f"ERR {e}"

    def state(hwnd):
        try:
            return win32gui.SendMessage(hwnd, win32con.BM_GETCHECK, 0, 0)
        except Exception:
            return "ERR"

    print("BEFORE: all=", state(radio_all), "range=", state(radio_range),
          "from=", repr(get_text(edit_from)), "to=", repr(get_text(edit_to)))

    if radio_range:
        win32gui.SendMessage(radio_range, win32con.BM_CLICK, 0, 0)
        m.pause(1.0)
        print("AFTER click: all=", state(radio_all), "range=",
              state(radio_range), "from=", repr(get_text(edit_from)),
              "to=", repr(get_text(edit_to)))

    m.cancel_dialog(dialog)
    m.close_schemagee(timeout=5.0)
    return 0


if __name__ == "__main__":
    sys.exit(main())
