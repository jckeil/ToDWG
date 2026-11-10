# -*- coding: utf-8 -*-
"""Проверка текста в готовом DWG через AutoCAD (кириллица не испорчена).

Использование:
    python tests\\_check_dwg_text.py "путь\\альбом.dwg"
"""
import os
import sys

import win32com.client


def collect(space, found):
    """Собирает TextString из контейнера (модель, лист, блок)."""
    try:
        count = space.Count
    except Exception:
        return
    for i in range(count):
        try:
            obj = space.Item(i)
        except Exception:
            continue
        try:
            name = obj.ObjectName
        except Exception:
            continue
        if name in ("AcDbText", "AcDbMText"):
            try:
                entity = win32com.client.Dispatch(obj)
                found.append(entity.TextString)
            except Exception:
                pass


def main():
    if len(sys.argv) < 2:
        print("usage: _check_dwg_text.py file.dwg")
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
        found = []
        collect(doc.ModelSpace, found)
        for index in range(doc.Layouts.Count):
            try:
                collect(doc.Layouts.Item(index).Block, found)
            except Exception:
                pass
        try:
            blocks = doc.Blocks
            for index in range(blocks.Count):
                collect(blocks.Item(index), found)
        except Exception as e:
            print("blocks:", e)

        # Диагностика: какие объекты вообще есть в модели.
        try:
            names = {}
            for i in range(min(doc.ModelSpace.Count, 300)):
                try:
                    name = doc.ModelSpace.Item(i).ObjectName
                except Exception:
                    name = "?"
                names[name] = names.get(name, 0) + 1
            print("типы в модели:", names)
        except Exception as e:
            print("model scan:", e)

        print("текстовых объектов:", len(found))
        for text in found[:5]:
            print("  ", repr(text))
        joined = " ".join(found)
        ok = (("Формат" in joined or "Копировал" in joined)
              and "Ê" not in joined)
        print("кириллица корректна:", ok)
        return 0 if ok else 1
    finally:
        doc.Close(False)


if __name__ == "__main__":
    sys.exit(main())
