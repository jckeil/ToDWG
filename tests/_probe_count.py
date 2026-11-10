# -*- coding: utf-8 -*-
"""Проверка определения числа листов: диалог печати и штампы .sdd.

Использование:
    python tests\\_probe_count.py "путь\\файл.sdd"
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ToDXF_v1 as m


def main():
    if len(sys.argv) < 2:
        print("usage: _probe_count.py file.sdd")
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
    m.pause(1.5)

    dialog_count = m.read_sheet_count_auto()
    stamp_count = m.read_sheet_count_from_sdd(path)
    print(f"RESULT dialog={dialog_count} stamps={stamp_count}")

    m.close_schemagee(timeout=8.0)
    return 0 if dialog_count else 1


if __name__ == "__main__":
    sys.exit(main())
