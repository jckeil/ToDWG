# -*- coding: utf-8 -*-
"""Проверки новых функций v1.8: папка (задача 3) и проверка доступности (задача 2)."""
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ToDWG_v1 as m   # noqa: E402

fails = []


def check(name, cond, extra=""):
    print(("OK   " if cond else "FAIL ") + name + (f"  [{extra}]" if extra else ""))
    if not cond:
        fails.append(name)


def main():
    root = tempfile.mkdtemp(prefix="ToDWG_test_")

    # --- unique_path: имя, (2), (3) -------------------------------------
    open(os.path.join(root, "a.dwg"), "w").close()
    open(os.path.join(root, "a (2).dwg"), "w").close()
    p3 = m.unique_path(root, "a", ".dwg")
    check("unique_path -> a (3).dwg", os.path.basename(p3) == "a (3).dwg", p3)
    free = m.unique_path(root, "b", ".dwg")
    check("unique_path свободное имя", os.path.basename(free) == "b.dwg", free)

    # --- find_sdd_recursive: рекурсивный поиск, сортировка --------------
    sub = os.path.join(root, "sub", "deep")
    os.makedirs(sub)
    for name in ("b.sdd", os.path.join("sub", "a.sdd"),
                 os.path.join("sub", "deep", "c.sdd")):
        full = os.path.join(root, name)
        open(full, "w").close()
    open(os.path.join(root, "ignore.txt"), "w").close()
    open(os.path.join(root, "sub", "x.SDD"), "w").close()   # регистр расширения
    found = m.find_sdd_recursive(root)
    names = sorted(os.path.basename(x) for x in found)
    check("find_sdd_recursive нашёл все .sdd (вкл. .SDD)",
          names == ["a.sdd", "b.sdd", "c.sdd", "x.SDD"], str(names))
    check("find_sdd_recursive отсортирован",
          found == sorted(found, key=lambda p: p.lower()))

    # --- relocate_result: перенос рядом с исходником + суффикс ----------
    work = tempfile.mkdtemp(prefix="ToDWG_work_")
    produced = os.path.join(work, "doc.dwg")
    with open(produced, "wb") as f:
        f.write(b"DWG")
    dest = m.relocate_result({"target": produced}, root)
    check("relocate_result перенёс файл", dest and os.path.isfile(dest), dest)
    check("relocate_result имя doc.dwg", os.path.basename(dest) == "doc.dwg")
    check("relocate_result исходник удалён (move)", not os.path.isfile(produced))

    with open(os.path.join(work, "doc.dwg"), "wb") as f:
        f.write(b"DWG2")
    dest2 = m.relocate_result({"target": os.path.join(work, "doc.dwg")}, root)
    check("relocate_result суффикс (2)", os.path.basename(dest2) == "doc (2).dwg",
          dest2)

    # --- probe_path: локальная папка доступна --------------------------
    ok, err = m.probe_path(root, need_write=True)
    check("probe_path локальная папка ok", ok, err)
    check("probe_path не оставляет пробный файл",
          not [n for n in os.listdir(root) if n.startswith(".ToDWG_probe_")])

    missing = os.path.join(root, "no_such_dir")
    ok2, err2 = m.probe_path(missing, need_write=True)
    check("probe_path отсутствующая папка -> ошибка", not ok2, err2)
    check("probe_path текст ошибки понятен", "не найдена" in err2.lower(), err2)

    # --- probe_path: недоступный сетевой путь -> таймаут/ошибка --------
    # Сетевые пути запрещены полностью: отказ без обращения в сеть.
    net = r"\\192.0.2.1\share"
    ok3, err3 = m.probe_path(net, timeout=2.0, need_write=True)
    check("probe_path сетевой путь -> отказ", not ok3 and "запрещ" in err3, err3)

    # --- запрет сетевых путей: единая проверка (v1.8) ------------------
    check("is_network_path UNC", m.is_network_path(net))
    check("is_network_path локальная папка", not m.is_network_path(root))
    bad = m.local_path_error(os.path.join(net, "a.sdd"), "файл")
    check("local_path_error отвергает сеть", "запрещ" in bad, bad)
    check("local_path_error: текст для папки",
          m.local_path_error(net, "папка").startswith(
              "Доступ к сетевой папке запрещён:"),
          bad)
    check("local_path_error: текст для папки результата",
          m.local_path_error(net, "папка результата").startswith(
              "Выбор сетевой папки запрещён:"), bad)
    check("local_path_error: текст для файла",
          m.local_path_error(net, "файл").startswith("Файл в сети запрещён:"),
          bad)
    check("local_path_error пропускает локальный путь",
          m.local_path_error(os.path.join(root, "a.sdd"), "файл") == "")
    check("check_local_path локальный -> ok", m.check_local_path(root)[0])

    try:
        m.make_run_folder("net", net)
        check("make_run_folder сеть -> RuntimeError", False)
    except RuntimeError as exc:
        check("make_run_folder сеть -> RuntimeError", "запрещ" in str(exc), exc)

    try:
        m.run_pipeline(os.path.join(net, "a.sdd"), None,
                       lambda *a, **k: None)
        check("run_pipeline сеть -> RuntimeError", False)
    except RuntimeError as exc:
        check("run_pipeline сеть -> RuntimeError", "запрещ" in str(exc), exc)

    check("relocate_result сеть -> None",
          m.relocate_result({"target": p3}, net) is None)

    tmp_dir = m.local_temp_dir()
    check("local_temp_dir локальный",
          bool(tmp_dir) and not m.is_network_path(tmp_dir), tmp_dir)
    work_dir = m.make_work_dir()
    check("make_work_dir локальный",
          os.path.isdir(work_dir) and not m.is_network_path(work_dir),
          work_dir)
    os.rmdir(work_dir)

    # --- аудит: длинные пути, прямые слэши, URL -------------------------
    long_p = "\\\\?\\"
    check("_strip_long_prefix убирает \\\\?\\",
          m._strip_long_prefix(long_p + "C:\\Temp") == "C:\\Temp")
    check("_strip_long_prefix превращает \\\\?\\UNC\\ в \\\\",
          m._strip_long_prefix(long_p + "UNC\\srv\\share") == "\\\\srv\\share")
    check("_strip_long_prefix убирает \\??\\",
          m._strip_long_prefix("\\??\\C:\\Temp") == "C:\\Temp")

    check("is_network_path //srv/share", m.is_network_path("//srv/share"))
    check("is_network_path \\\\?\\UNC\\",
          m.is_network_path(long_p + "UNC\\srv\\share"))
    check("is_network_path //?/UNC/", m.is_network_path("//?/UNC/srv/share"))
    check("is_network_path http://",
          m.is_network_path("http://example.org/a.sdd"))
    check("is_network_path file://", m.is_network_path("file:///C:/x.sdd"))
    local_tmp = tempfile.gettempdir()
    check("is_network_path \\\\?\\ локальный диск",
          not m.is_network_path(long_p + local_tmp))
    check("is_network_path //?/ локальный диск",
          not m.is_network_path("//?/" + local_tmp.replace("\\", "/")))
    check("is_network_path пустой/None",
          not m.is_network_path("") and not m.is_network_path(None))

    saved_drive_kind = m._drive_kind
    try:
        m._drive_kind = lambda p: m.DRIVE_REMOTE
        check("is_network_path DRIVE_REMOTE", m.is_network_path("Q:\\work"))
    finally:
        m._drive_kind = saved_drive_kind

    # --- временная папка: сетевой TEMP пропускается ---------------------
    saved_gettempdir = m.tempfile.gettempdir
    saved_localappdata = os.environ.get("LOCALAPPDATA")
    local_root = tempfile.mkdtemp(prefix="ToDWG_localapp_")
    os.makedirs(os.path.join(local_root, "Temp"))
    try:
        m.tempfile.gettempdir = lambda: r"\\192.0.2.1\share\temp"
        os.environ["LOCALAPPDATA"] = local_root
        fallback = m.local_temp_dir()
        check("local_temp_dir пропускает сетевой TEMP",
              bool(fallback) and not m.is_network_path(fallback)
              and os.path.abspath(fallback).startswith(
                  os.path.abspath(local_root)),
              fallback)
    finally:
        m.tempfile.gettempdir = saved_gettempdir
        if saved_localappdata is None:
            os.environ.pop("LOCALAPPDATA", None)
        else:
            os.environ["LOCALAPPDATA"] = saved_localappdata

    # --- настройки: сетевые «Документы» -> LOCALAPPDATA -----------------
    saved_documents = m.documents_dir
    local_docs = tempfile.mkdtemp(prefix="ToDWG_docs_")
    try:
        m.documents_dir = lambda: r"\\192.0.2.1\share\Docs"
        os.environ["LOCALAPPDATA"] = local_docs
        sdir = m.settings_dir()
        check("settings_dir обходит сетевые Документы",
              os.path.abspath(sdir).startswith(os.path.abspath(local_docs))
              and not m.is_network_path(sdir), sdir)
    finally:
        m.documents_dir = saved_documents
        if saved_localappdata is None:
            os.environ.pop("LOCALAPPDATA", None)
        else:
            os.environ["LOCALAPPDATA"] = saved_localappdata

    # --- публичные точки: отказ до обращения к ФС -----------------------
    check("run_headless сеть -> код 2",
          m.run_headless({"dxf_folder": net, "name": "check"}) == 2)

    try:
        m.merge_all(net, root)
        check("merge_all сетевой вход -> RuntimeError", False)
    except RuntimeError as exc:
        check("merge_all сетевой вход -> RuntimeError",
              "запрещ" in str(exc), exc)

    try:
        m.convert_with_oda(os.path.join(net, "ODAFileConverter.exe"),
                           os.path.join(root, "a.dxf"),
                           os.path.join(root, "a.dwg"))
        check("convert_with_oda сетевой exe -> RuntimeError", False)
    except RuntimeError as exc:
        check("convert_with_oda сетевой exe -> RuntimeError",
              "запрещ" in str(exc), exc)

    check("open_file_in_schemagee сеть -> False",
          m.open_file_in_schemagee(os.path.join(net, "a.sdd")) is False)
    m._open_folder(net)
    check("_open_folder сеть -> без запуска", True)

    # --- runtime-запрет сети -------------------------------------------
    try:
        m._no_network_audit("socket.connect", ("192.0.2.1", 80))
        check("аудит-хук блокирует socket.connect", False)
    except RuntimeError as exc:
        check("аудит-хук блокирует socket.connect",
              "запрещ" in str(exc), exc)

    guard_code = (
        "import sys; sys.path.insert(0, %r); import ToDWG_v1 as m; "
        "m.install_no_network_guard(); import socket; "
        "socket.create_connection(('192.0.2.1', 80), 2)"
        % os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    )
    try:
        proc = subprocess.run([sys.executable, "-c", guard_code],
                              capture_output=True, text=True, timeout=15)
        # В ToDWG_v1 sys.excepthook пишет в лог/stdout, поэтому смотрим оба потока.
        output = (proc.stdout or "") + (proc.stderr or "")
        check("runtime-запрет сети в процессе",
              proc.returncode != 0
              and ("Сетевой доступ запрещён" in output
                   or "СЕТЕВОЙ ДОСТУП ЗАПРЕЩЁН" in output),
              output[-200:])
    except subprocess.TimeoutExpired:
        check("runtime-запрет сети в процессе", False, "таймаут")

    # --- CLI: сетевой путь -> код 2, без запуска -----------------------
    saved_main_helpers = (m.init_log, m.setup_logging_bridge,
                          m.install_no_network_guard)
    saved_argv = m.sys.argv
    m.init_log = lambda: None
    m.setup_logging_bridge = lambda: None
    m.install_no_network_guard = lambda: None
    try:
        m.sys.argv = ["ToDWG_v1.py", "--headless", "--dxf-folder", net,
                      "--name", "check"]
        code = m.main()
        check("main CLI сеть -> код 2", code == 2, code)
    finally:
        m.sys.argv = saved_argv
        (m.init_log, m.setup_logging_bridge,
         m.install_no_network_guard) = saved_main_helpers


    # --- справка: ключевые требования на месте --------------------------
    help_text = m.HELP_TEXT
    for phrase in ("Shift+D", "«Параметры»", "«Клавиши»",
                   "одинаковыми правами", "Ctrl+Alt+0",
                   "Только локальные диски"):
        check("справка содержит: " + phrase, phrase in help_text)

    print("\nИТОГ:", "OK" if not fails else f"FAIL ({len(fails)})")
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
