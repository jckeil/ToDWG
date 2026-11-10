# -*- coding: utf-8 -*-
"""Проверка готового DWG через AutoCAD: число layouts и объектов в модели.

Использование:
    python tests\\_check_dwg.py "путь\\альбом.dwg"
"""
import os
import sys

import win32com.client


def main():
    if len(sys.argv) < 2:
        print("usage: _check_dwg.py file.dwg")
        return 1
    path = os.path.abspath(sys.argv[1])
    if not os.path.isfile(path):
        print("нет файла:", path)
        return 1

    acad = win32com.client.Dispatch("AutoCAD.Application")
    try:
        acad.Visible = False
    except Exception:
        pass

    doc = acad.Documents.Open(path)
    try:
        count = doc.Layouts.Count
        names = [doc.Layouts.Item(i).Name for i in range(count)]
        print("layouts:", count)
        print("model entities:", doc.ModelSpace.Count)
        print("first:", names[0], "| last:", names[-1])
    finally:
        doc.Close(False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
