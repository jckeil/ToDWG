# -*- coding: utf-8 -*-
# =============================================================================
#
#   ToDXF v1.0
#   Один файл, один exe: Schemagee (.sdd) -> DXF -> DWG.
#
#   Структура файла (блоки идут строго по порядку):
#       БЛОК 0. НАСТРОЙКИ, ПУТИ, ЛОГИРОВАНИЕ
#       БЛОК 1. ОФОРМЛЕНИЕ (цвета, шрифты, вспомогательные виджеты)
#       БЛОК 2. КЛИКЕР: Schemagee -> DXF
#       БЛОК 3. КОНВЕРТЕР: DXF -> DWG
#       БЛОК 4. СЦЕНАРИЙ: кликер + конвертер + отчёт
#       БЛОК 5. ОКНА (главное, прогресс, успех, ошибка, остановка)
#       БЛОК 6. ТОЧКА ВХОДА
#
#   exe не зависит от места запуска и соседних файлов: иконка вшита,
#   результаты и лог пишутся в папку рядом с exe
#   (<ИМЯ>_<ГГГГ-ММ-ДД-ЧЧ-ММ-СС>).
#
#   Ключи командной строки (только для отладки/проверки):
#       --dxf-folder ПАПКА   пропустить кликер, собрать альбом из готовых DXF;
#       --name ИМЯ           имя итогового файла без расширения;
#       --headless           без окон: выполнить и напечатать отчёт.
#
# =============================================================================

import argparse
import ctypes
import ctypes.wintypes
import datetime
import glob
import io
import json
import logging
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback

import tkinter as tk
from tkinter import filedialog, ttk

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
    HAS_DND = True
except Exception:            # библиотека не установлена — работаем без DnD
    DND_FILES = None
    TkinterDnD = None
    HAS_DND = False

import keyboard
import pyperclip
import win32con
import win32gui

import ezdxf
from ezdxf import bbox, xref
from ezdxf.math import Matrix44
from ezdxf.xref import ConflictPolicy


# =============================================================================
# БЛОК 0. НАСТРОЙКИ, ПУТИ, ЛОГИРОВАНИЕ
# =============================================================================

APP_NAME = "ToDXF"
APP_VERSION = "v1.2"
APP_FULL_TITLE = f"{APP_NAME} {APP_VERSION} — Schemagee → DXF → DWG"
APP_INNER_TEXT = "Schemagee → DXF → DWG"
APP_USER_MODEL_ID = "Sintek.ToDXF.V1"

ICON_FILENAME = "Logo_toDXF.ico"

# Настройки, которые переживают перезапуск (последняя папка выбора файла).
SETTINGS_FILENAME = "ToDXF_settings.json"

LOG_FILENAME = "ToDXF.log"
LOG_MAX_BYTES = 2 * 1024 * 1024

STOP_HOTKEY = "ctrl+alt+0"
STOP_EVENT = threading.Event()


def app_dir():
    """Папка exe (в исходниках — папка .py): рядом с ней пишем результаты."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


BASE_DIR = app_dir()


def resource_path(relative_path):
    """Путь к вшитому ресурсу: работает и в .py, и в exe (PyInstaller onefile)."""
    try:
        base_path = sys._MEIPASS
    except Exception:
        base_path = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base_path, relative_path)


def set_app_user_model_id():
    """Иконка приложения на панели задач вместо стандартной от Python."""
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            APP_USER_MODEL_ID
        )
    except Exception as e:
        print(f"AppUserModelID: {e}")


def apply_icon(window):
    try:
        icon_path = resource_path(ICON_FILENAME)
        if os.path.exists(icon_path):
            window.iconbitmap(icon_path)
        else:
            print(f"Иконка не найдена: {icon_path}")
    except Exception as e:
        print(f"Иконка: {e}")


# --- Стартовая папка диалога выбора файла ------------------------------------


def desktop_dir():
    """Рабочий стол текущего пользователя. В сеть не ходит и не подвисает."""
    try:
        buf = ctypes.create_unicode_buffer(260)
        # CSIDL_DESKTOPDIRECTORY = 0x0010
        if ctypes.windll.shell32.SHGetFolderPathW(None, 0x0010, None, 0, buf) == 0:
            path = buf.value
            if path and os.path.isdir(path):
                return path
    except Exception:
        pass
    home = os.path.expanduser("~")
    for name in ("Desktop", "Рабочий стол",
                 os.path.join("OneDrive", "Desktop"),
                 os.path.join("OneDrive", "Рабочий стол")):
        candidate = os.path.join(home, name)
        if os.path.isdir(candidate):
            return candidate
    return home


def is_network_path(path):
    """UNC-путь (\\\\сервер\\папка): обращение к нему может ждать таймаут сети."""
    if not path:
        return False
    p = str(path).strip()
    return p.startswith("\\\\") or p.startswith("//")


def load_settings():
    try:
        with open(os.path.join(BASE_DIR, SETTINGS_FILENAME),
                  encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_settings(data):
    target = os.path.join(BASE_DIR, SETTINGS_FILENAME)
    try:
        with open(target, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log(f"Не удалось сохранить настройки: {e}")


def remember_dir(path):
    """Запоминает папку последнего выбранного файла (только локальную)."""
    try:
        folder = os.path.dirname(os.path.normpath(path))
        if not folder or is_network_path(folder) or not os.path.isdir(folder):
            return
        settings = load_settings()
        if settings.get("last_dir") != folder:
            settings["last_dir"] = folder
            save_settings(settings)
    except Exception as e:
        log(f"Не удалось запомнить папку: {e}")


def choose_start_dir():
    """С чего начинать диалог: последняя локальная папка, иначе Рабочий стол.

    Сетевые папки намеренно не используются: если сервер не отвечает,
    стандартный диалог Windows открывается десятки секунд.
    """
    try:
        last = str(load_settings().get("last_dir") or "").strip()
    except Exception:
        last = ""
    if last and not is_network_path(last) and os.path.isdir(last):
        return last
    start = desktop_dir()
    if is_network_path(start):
        # Профиль с перенаправленными папками: берём локальный диск.
        start = os.environ.get("SystemDrive", "C:") + "\\"
    return start


def is_elevated():
    """Программа запущена от имени администратора?"""
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


# --- Лог: общий рядом с exe + отдельный на каждый запуск ----------------------
_global_log = None
_run_log = None
_run_log_path = None


def _rotate(path):
    try:
        if os.path.isfile(path) and os.path.getsize(path) > LOG_MAX_BYTES:
            old = path + ".1"
            if os.path.isfile(old):
                os.remove(old)
            os.replace(path, old)
    except Exception:
        pass


def init_log():
    """Открывает общий лог рядом с exe (с ротацией)."""
    global _global_log
    path = os.path.join(BASE_DIR, LOG_FILENAME)
    try:
        _rotate(path)
        _global_log = open(path, "a", encoding="utf-8", buffering=1)
        _global_log.write("\n" + "=" * 70 + "\n")
        _global_log.write(f"ЗАПУСК: {datetime.datetime.now():%Y-%m-%d %H:%M:%S}\n")
        _global_log.write("=" * 70 + "\n")
    except Exception:
        _global_log = None


def attach_run_log(path):
    """Подключает лог конкретного запуска (лежит в папке результата)."""
    global _run_log, _run_log_path
    try:
        _run_log = open(path, "a", encoding="utf-8", buffering=1)
        _run_log_path = path
    except Exception:
        _run_log = None
        _run_log_path = None


def close_logs():
    global _global_log, _run_log
    for fp in (_run_log, _global_log):
        try:
            if fp:
                fp.close()
        except Exception:
            pass
    _run_log = None
    _global_log = None


def log(msg=""):
    """Пишет строку в общий лог, лог запуска и stdout (если он есть)."""
    line = str(msg)
    try:
        if sys.stdout is not None:
            print(line)
    except Exception:
        pass
    stamp = f"{datetime.datetime.now():%H:%M:%S} | "
    try:
        if _global_log:
            _global_log.write(stamp + line + "\n")
            _global_log.flush()
    except Exception:
        pass
    try:
        if _run_log:
            _run_log.write(stamp + line + "\n")
            _run_log.flush()
    except Exception:
        pass


def _log_exception(exc_type, exc_value, exc_tb):
    log("=" * 70)
    log("!!! НЕОБРАБОТАННОЕ ИСКЛЮЧЕНИЕ !!!")
    log("=" * 70)
    for line in traceback.format_exception(exc_type, exc_value, exc_tb):
        for sub in line.rstrip().split("\n"):
            log(sub)
    log("=" * 70)


sys.excepthook = _log_exception


class _LogBridge(logging.Handler):
    """Перенаправляет logging.* (конвертер) в общий лог программы."""

    def emit(self, record):
        try:
            prefix = "" if record.levelno <= logging.INFO else f"{record.levelname}: "
            log(prefix + record.getMessage())
        except Exception:
            pass


def setup_logging_bridge():
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for handler in list(root.handlers):
        root.removeHandler(handler)
    root.addHandler(_LogBridge())
    # Служебные INFO-сообщения ezdxf в лог не нужны, только предупреждения.
    logging.getLogger("ezdxf").setLevel(logging.WARNING)


def format_time(seconds):
    sec = int(seconds)
    ms = int(round((seconds - sec) * 1000))
    if ms >= 1000:
        sec += 1
        ms -= 1000
    return f"{sec}.{ms:03d}"


def format_duration(seconds):
    """'5.632 сек' для известного времени, '—' — если время неизвестно."""
    return "—" if seconds is None else f"{format_time(seconds)} сек"


# =============================================================================
# БЛОК 1. ОФОРМЛЕНИЕ (стиль Windows 11)
# =============================================================================

WIN11_BG       = "#F3F3F3"
WIN11_CARD_BG  = "#FFFFFF"
WIN11_TEXT     = "#1A1A1A"
WIN11_SUBTEXT  = "#5C5C5C"
WIN11_ACCENT   = "#0067C0"
WIN11_ACCENT_H = "#1A7DD8"
WIN11_DANGER   = "#C42B1C"
WIN11_BORDER   = "#E0E0E0"
WIN11_OK       = "#107C10"

FONT_TITLE    = ("Segoe UI Variable Display", 20, "bold")
FONT_H1       = ("Segoe UI Variable Display", 14, "bold")
FONT_BODY     = ("Segoe UI Variable Text", 11)
FONT_BODY_SM  = ("Segoe UI Variable Text", 10)
FONT_WARN     = ("Segoe UI Variable Display", 13, "bold")
FONT_VERSION  = ("Segoe UI Variable Text", 9)


def _center(win, w, h):
    sw = win.winfo_screenwidth()
    sh = win.winfo_screenheight()
    win.geometry(f"{w}x{h}+{(sw - w) // 2}+{(sh - h) // 2}")


def _place_corner(win, w, h, margin=28):
    """Правый нижний угол: не закрывает рабочую область Schemagee."""
    win.update_idletasks()
    sw = win.winfo_screenwidth()
    sh = win.winfo_screenheight()
    x = max(0, sw - w - margin)
    y = max(0, sh - h - margin - 48)
    win.geometry(f"{w}x{h}+{x}+{y}")


def _card(parent, w, h):
    card = tk.Frame(parent, bg=WIN11_CARD_BG, highlightthickness=1,
                    highlightbackground=WIN11_BORDER)
    card.place(relx=0.5, rely=0.5, anchor="center", width=w, height=h)
    return card


def _button(parent, text, command, primary=True):
    bg = WIN11_ACCENT if primary else "#E5E5E5"
    fg = "#FFFFFF" if primary else WIN11_TEXT
    active_bg = WIN11_ACCENT_H if primary else "#D6D6D6"
    btn = tk.Button(
        parent, text=text, command=command,
        font=("Segoe UI Variable Text", 11, "bold"),
        fg=fg, bg=bg, activeforeground=fg, activebackground=active_bg,
        relief="flat", borderwidth=0, highlightthickness=0,
        cursor="hand2", padx=26, pady=12,
    )
    btn.bind("<Enter>", lambda e: btn.config(bg=active_bg))
    btn.bind("<Leave>", lambda e: btn.config(bg=bg))
    return btn


def _open_folder(path):
    try:
        os.startfile(path)
        log(f"Открываем папку: {path}")
    except Exception as e:
        log(f"Не удалось открыть папку {path}: {e}")
        try:
            subprocess.Popen(["explorer", path])
        except Exception as e2:
            log(f"explorer тоже не сработал: {e2}")


def _row(parent, label, value, row):
    tk.Label(parent, text=label, font=FONT_BODY, bg=WIN11_CARD_BG,
             fg=WIN11_SUBTEXT, anchor="w").grid(row=row, column=0,
                                                sticky="w", pady=3)
    tk.Label(parent, text=value, font=FONT_H1, bg=WIN11_CARD_BG,
             fg=WIN11_TEXT, anchor="w").grid(row=row, column=1, sticky="w",
                                             padx=(14, 0), pady=3)


# =============================================================================
# БЛОК 2. КЛИКЕР: Schemagee -> DXF
#
#   Главное отличие от прежних версий: все паузы привязаны к событиям
#   (окно появилось / стало активным / диалог закрылся / файл создан),
#   а результат каждого листа проверяется по файлу на диске с повтором.
# =============================================================================

PRINT_DIALOG_TAB_COUNT = 13   # резервный путь: Tab до поля "по:"
TAB_SETTLE = 0.14             # пауза между нажатиями Tab

# Блок «Страницы печати» в диалоге Schemagee 3.5 (найдено снятием состава
# окна): радиокнопки «все»/«С:» и поля «с:»/«по:». Поля заполнены даже
# тогда, когда неактивны, поэтому число можно прочитать напрямую.
PRINT_RADIO_ALL = 1248
PRINT_RADIO_RANGE = 1249
PRINT_EDIT_FROM = 1250
PRINT_EDIT_TO = 1251
KEY_SETTLE = 0.35             # пауза после одиночной комбинации
SAVE_DIALOG_TIMEOUT = 8.0     # ожидание диалога "Сохранение"
FILE_APPEAR_TIMEOUT = 25.0    # ожидание появления DXF на диске
SHEET_ATTEMPTS = 3            # попыток сохранить один лист
MAX_RECOVERIES = 3            # перезапусков Schemagee на один файл
SHEET_OPEN_PAUSE = 2.5        # после появления окна Schemagee (отрисовка)
SHEET_RETRY_PAUSE = 0.8       # между попытками одного листа
SHEET_NEXT_PAUSE = 0.3        # перед Page Down
PAGE_TURN_PAUSE = 1.0         # после Page Down (следующий лист)

# Названия кнопки, запускающей конвертацию в форме Schemagee «Экспорт в DXF».
# Диалог «Сохранение» только запоминает путь — сам экспорт начинается
# отдельной кнопкой (в старом кликере её нажимали Tab+Enter).
EXPORT_BUTTON_TEXTS = (
    "Экспорт", "&Экспорт", "Экспортировать", "&Экспортировать",
    "Экспорт в DXF", "&Экспорт в DXF", "Экспорт...", "Экспорт…",
    "Export", "&Export", "ОК", "OK", "&OK", "Да", "&Да", "Yes", "&Yes",
    "Выполнить", "&Выполнить", "Применить", "&Применить",
)

# Заголовки диалога выбора файла: это НЕ форма экспорта, их пропускаем.
SAVE_DIALOG_TITLES = ("Сохранение", "Сохранить как", "Save As", "Save")


class TaskStopped(Exception):
    """Операция прервана пользователем (Ctrl+Alt+0)."""


# Признак того, что Schemagee открыли МЫ: только такое окно разрешено закрывать.
_OWNED_SCHEMAGEE = {"opened": False}

# Состояние конвейера — нужно сторожевому таймеру экстренной остановки.
_PIPELINE_STATE = {"running": False}

# Временно игнорировать остановку (когда мы УЖЕ останавливаемся и закрываем
# Schemagee, новые TaskStopped только помешали бы навести порядок).
_STOP_SUSPEND = {"depth": 0}


class stop_suspended:
    """Контекст: внутри него check_stop() не бросает TaskStopped."""

    def __enter__(self):
        _STOP_SUSPEND["depth"] += 1
        return self

    def __exit__(self, *_exc):
        _STOP_SUSPEND["depth"] -= 1
        return False


def stop_requested():
    return STOP_EVENT.is_set()


def check_stop():
    if _STOP_SUSPEND["depth"]:
        return
    if STOP_EVENT.is_set():
        raise TaskStopped()


def pause(seconds):
    """Пауза, которая прерывается экстренной остановкой."""
    end = time.time() + seconds
    while time.time() < end:
        check_stop()
        time.sleep(0.02)


def short_sleep(seconds):
    """Короткая пауза в циклах ожидания: реагирует на остановку мгновенно."""
    end = time.time() + seconds
    while time.time() < end:
        check_stop()
        time.sleep(0.02)


# --- Экстренная остановка Ctrl+Alt+0 -----------------------------------------
_HOTKEY = {"registered": False}


def _stop_watchdog(grace=8.0):
    """Гарантия остановки: если конвейер не вышел за grace секунд — выходим."""
    time.sleep(grace)
    if not _PIPELINE_STATE["running"]:
        return
    log(f"  Работа не остановилась за {grace:.0f} сек — принудительный выход.")
    try:
        keyboard.release('ctrl')
        keyboard.release('alt')
    except Exception:
        pass
    try:
        close_logs()
    except Exception:
        pass
    os._exit(2)


def request_stop(reason=""):
    """Мгновенная остановка: только флаг, таймер и одна строка в лог."""
    if STOP_EVENT.is_set():
        return
    STOP_EVENT.set()
    suffix = f" {reason}" if reason else ""
    log(f"Нажата {STOP_HOTKEY} — останавливаем задачу.{suffix}")
    threading.Thread(target=_stop_watchdog, args=(8.0,), daemon=True).start()


def start_stop_hotkey():
    """Регистрирует Ctrl+Alt+0 на уровне системы (RegisterHotKey).

    keyboard.add_hotkey обрабатывается в потоке низкоуровневого хука и ждёт,
    пока рабочий поток отправляет клавиши, поэтому остановка срабатывала с
    задержкой. RegisterHotKey обрабатывается системой в отдельном потоке —
    WM_HOTKEY приходит сразу, чем бы ни был занят Python.
    """
    MOD_ALT = 0x0001
    MOD_CONTROL = 0x0002
    MOD_NOREPEAT = 0x4000
    WM_HOTKEY = 0x0312
    VK_0 = 0x30

    def fallback():
        try:
            keyboard.add_hotkey(STOP_HOTKEY, request_stop)
            log(f"Горячая клавиша {STOP_HOTKEY} зарегистрирована "
                "(keyboard.add_hotkey).")
        except Exception as e:
            log(f"Не удалось зарегистрировать {STOP_HOTKEY}: {e}")

    def loop():
        user32 = ctypes.windll.user32
        if not user32.RegisterHotKey(None, 1,
                                     MOD_CONTROL | MOD_ALT | MOD_NOREPEAT,
                                     VK_0):
            log("  RegisterHotKey недоступен — используем keyboard.add_hotkey.")
            fallback()
            return
        _HOTKEY["registered"] = True
        log(f"Горячая клавиша {STOP_HOTKEY} зарегистрирована "
            "(системная, срабатывает мгновенно).")
        # Дублируем хуком keyboard: если WM_HOTKEY почему-то не придёт
        # (чужая сессия, политика Windows), остановка всё равно сработает.
        fallback()
        msg = ctypes.wintypes.MSG()
        try:
            while True:
                ret = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if ret <= 0:      # 0 — WM_QUIT, -1 — ошибка
                    break
                _HOTKEY["messages"] = _HOTKEY.get("messages", 0) + 1
                if msg.message != WM_HOTKEY:
                    continue
                try:
                    # Поля MSG в ctypes называются hWnd/wParam (не hwnd):
                    # ошибка в имени поля убивала поток, и горячая клавиша
                    # переставала работать после первого нажатия.
                    request_stop(f"(hwnd={msg.hWnd}, id={msg.wParam})")
                except Exception as e:
                    log(f"  Ошибка обработки WM_HOTKEY: {e}")
        except Exception as e:
            log(f"  Поток горячей клавиши остановлен: {e}")
        finally:
            user32.UnregisterHotKey(None, 1)

    threading.Thread(target=loop, name="stop-hotkey", daemon=True).start()


def _is_alive(hwnd):
    try:
        return bool(hwnd) and bool(win32gui.IsWindow(hwnd)) and \
            bool(win32gui.IsWindowVisible(hwnd))
    except Exception:
        return False


def wait_window_ready(hwnd, timeout=3.0):
    """Ждёт, пока окно станет видимым и доступным для ввода."""
    end = time.time() + timeout
    while time.time() < end:
        try:
            if win32gui.IsWindow(hwnd) and win32gui.IsWindowVisible(hwnd) \
                    and win32gui.IsWindowEnabled(hwnd):
                return True
        except Exception:
            return False
        short_sleep(0.05)
    return False


def _is_foreground(hwnd):
    try:
        return bool(hwnd) and win32gui.GetForegroundWindow() == hwnd
    except Exception:
        return False


def foreground_title():
    """Описание активного окна — для лога при поиске причины отказа."""
    try:
        hwnd = win32gui.GetForegroundWindow()
        return f"{win32gui.GetWindowText(hwnd)!r} (hwnd={hwnd})"
    except Exception:
        return "неизвестно"


def force_foreground(hwnd, timeout=3.0):
    """Делает окно активным и ПРОВЕРЯЕТ результат.

    SetForegroundWindow молча отказывает (возвращает 0), если Windows считает
    процесс фоновым, поэтому последовательно пробуем:
      1) BringWindowToTop + SetForegroundWindow;
      2) AttachThreadInput к потоку активного окна;
      3) кратковременный перевод окна в TOPMOST и обратно.
    """
    if not _is_alive(hwnd):
        return False

    try:
        if win32gui.IsIconic(hwnd):
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            time.sleep(0.25)
    except Exception:
        pass

    me = ctypes.windll.kernel32.GetCurrentThreadId()
    end = time.time() + timeout
    while time.time() < end:
        if _is_foreground(hwnd):
            return True

        # 1. Прямая попытка.
        try:
            win32gui.BringWindowToTop(hwnd)
            win32gui.SetForegroundWindow(hwnd)
        except Exception:
            pass
        if _is_foreground(hwnd):
            return True

        # 2. Через AttachThreadInput к потоку текущего активного окна.
        try:
            fg = win32gui.GetForegroundWindow()
            t_fg = ctypes.windll.user32.GetWindowThreadProcessId(fg, None)
            if t_fg and t_fg != me:
                ctypes.windll.user32.AttachThreadInput(me, t_fg, True)
                try:
                    win32gui.BringWindowToTop(hwnd)
                    win32gui.SetForegroundWindow(hwnd)
                finally:
                    ctypes.windll.user32.AttachThreadInput(me, t_fg, False)
        except Exception:
            pass
        if _is_foreground(hwnd):
            return True

        # 3. Кратковременно поверх всех окон.
        try:
            flags = (win32con.SWP_NOMOVE | win32con.SWP_NOSIZE |
                     win32con.SWP_SHOWWINDOW)
            win32gui.SetWindowPos(hwnd, win32con.HWND_TOPMOST, 0, 0, 0, 0, flags)
            win32gui.SetWindowPos(hwnd, win32con.HWND_NOTOPMOST, 0, 0, 0, 0, flags)
            win32gui.SetForegroundWindow(hwnd)
        except Exception:
            pass

        time.sleep(0.1)

    return _is_foreground(hwnd)


# Связь кликера с интерфейсом: окно прогресса можно спрятать, если оно
# мешает отдать фокус Schemagee. Заполняется в run_gui().
_UI_SIGNAL = {"emit": None}


def signal_ui(kind):
    """Просит интерфейс выполнить действие (например, спрятать прогресс)."""
    fn = _UI_SIGNAL.get("emit")
    if not fn:
        return False
    try:
        fn(kind)
        return True
    except Exception:
        return False


def ensure_foreground(hwnd, what="Schemagee", timeout=4.0):
    """Активирует окно или честно останавливает работу.

    Имитатор клавиш отправляет нажатия в АКТИВНОЕ окно. Если активно окно
    прогресса, Ctrl+P и Shift+D уходят в него и Schemagee ничего не делает.
    Поэтому без подтверждённой активации продолжать нельзя.
    """
    if force_foreground(hwnd, timeout=timeout):
        return True

    # Окно прогресса могло удержать фокус: прячем его, пробуем снова,
    # затем возвращаем на место и снова отдаём фокус Schemagee.
    if signal_ui("progress_hide"):
        time.sleep(0.4)
        if force_foreground(hwnd, timeout=timeout):
            signal_ui("progress_show")
            time.sleep(0.3)
            if force_foreground(hwnd, timeout=2.0):
                log(f"  Фокус передан окну {what} "
                    "(окно прогресса на время скрыто).")
                return True
            # Возвращённое окно прогресса снова забрало фокус — оставляем
            # его скрытым: важнее, чтобы нажатия шли в Schemagee.
            signal_ui("progress_hide")
            time.sleep(0.4)
            if force_foreground(hwnd, timeout=2.0):
                log(f"  Фокус передан окну {what} "
                    "(окно прогресса оставлено скрытым).")
                return True

    log(f"  Не удалось активировать окно {what}. "
        f"Сейчас активно: {foreground_title()}")
    raise RuntimeError(
        f"Не удалось сделать активным окно {what}.\n\n"
        f"Сейчас активно другое окно: {foreground_title()}.\n\n"
        "Нажатия клавиш уходят в активное окно, поэтому работа остановлена — "
        "иначе листы не сохранятся. Не переключайте окна во время работы "
        "и повторите запуск."
    )


def ensure_schemagee_active(hwnd, what="Schemagee"):
    """Отдаёт фокус Schemagee, но НЕ отбирает его у её же диалогов.

    Модальные окна Schemagee («Экспорт», «Сохранение», вопросы) получают фокус
    сами. Если в этот момент активировать главное окно, Enter/Tab уходят в
    него, а модальное окно остаётся висеть и блокирует следующие листы.
    """
    try:
        fg = win32gui.GetForegroundWindow()
    except Exception:
        fg = 0
    if fg and fg != hwnd:
        fg_pid = window_pid(fg)
        if fg_pid and fg_pid == window_pid(hwnd):
            return True       # активно другое окно Schemagee — не мешаем
    return ensure_foreground(hwnd, what=what)


def schemagee_windows():
    """Все видимые окна Schemagee: [(hwnd, title), ...].

    Окна собственного процесса (главное, прогресс, отчёт) отбрасываются:
    в их заголовках тоже встречается слово Schemagee.
    """
    found = []
    my_pid = os.getpid()

    def cb(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return
        pid = ctypes.c_ulong(0)
        ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value == my_pid:
            return
        title = win32gui.GetWindowText(hwnd)
        if "Schemagee" in title or ".sdd" in title:
            found.append((hwnd, title))

    win32gui.EnumWindows(cb, None)
    return found


def find_schemagee_window():
    """Первое видимое окно Schemagee (по заголовку)."""
    found = schemagee_windows()
    return found[0] if found else (None, None)


def wait_schemagee_window(timeout=15.0):
    end = time.time() + timeout
    next_note = time.time() + 5.0
    while time.time() < end:
        hwnd, title = find_schemagee_window()
        if hwnd:
            return hwnd, title
        if time.time() >= next_note:
            log("  …ждём окно Schemagee")
            next_note = time.time() + 5.0
        short_sleep(0.1)
    return None, None


def _find_dialog(title, timeout=5.0):
    """Ждёт видимый диалог класса #32770 с нужным заголовком.

    title — заголовок или кортеж допустимых заголовков.
    """
    titles = (title,) if isinstance(title, str) else tuple(title)
    end = time.time() + timeout
    while time.time() < end:
        found = []

        def cb(hwnd, _):
            if not win32gui.IsWindowVisible(hwnd):
                return
            if win32gui.GetClassName(hwnd) != "#32770":
                return
            if win32gui.GetWindowText(hwnd) in titles:
                found.append(hwnd)

        win32gui.EnumWindows(cb, None)
        if found:
            return found[0]
        short_sleep(0.05)
    return None


def find_print_dialog(timeout=6.0):
    return _find_dialog("Печать", timeout)


def find_save_dialog(timeout=8.0):
    return _find_dialog(("Сохранение", "Сохранить как", "Save As"), timeout)


def find_modal_dialog(timeout=1.5):
    """Любой модальный диалог (например, вопрос «Сохранить изменения?»)."""
    end = time.time() + timeout
    while time.time() < end:
        found = []

        def cb(hwnd, _):
            if not win32gui.IsWindowVisible(hwnd):
                return
            if win32gui.GetClassName(hwnd) == "#32770":
                found.append(hwnd)

        win32gui.EnumWindows(cb, None)
        if found:
            return found[0]
        short_sleep(0.05)
    return None


def window_pid(hwnd):
    """PID процесса, которому принадлежит окно (0 — не удалось)."""
    try:
        pid = ctypes.c_ulong(0)
        ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return int(pid.value)
    except Exception:
        return 0


def process_windows(pid, exclude=()):
    """Видимые окна процесса с непустым заголовком: [(hwnd, title), ...]."""
    found = []

    def cb(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return
        if hwnd in exclude:
            return
        if window_pid(hwnd) != pid:
            return
        title = win32gui.GetWindowText(hwnd)
        if not title:
            return
        found.append((hwnd, title))

    try:
        win32gui.EnumWindows(cb, None)
    except Exception:
        pass
    return found


def schemagee_popup(main_hwnd, timeout=1.5):
    """Окно Schemagee поверх главного: «Экспорт», «Сохранение», вопросы.

    Главное окно Schemagee трогать нельзя: пока висит модальное окно,
    клавиши нужно отправлять именно ему, иначе оно остаётся на экране
    и блокирует все следующие листы.
    """
    end = time.time() + timeout
    main_pid = window_pid(main_hwnd)
    while time.time() < end:
        if main_pid:
            dialogs = []
            owned = []
            for hwnd, title in process_windows(main_pid, exclude=(main_hwnd,)):
                try:
                    cls = win32gui.GetClassName(hwnd)
                    owner = win32gui.GetWindow(hwnd, win32con.GW_OWNER)
                except Exception:
                    continue
                if cls == "#32770":
                    dialogs.append((hwnd, title))
                elif owner:
                    owned.append((hwnd, title))
            if dialogs:
                return dialogs[0]
            if owned:
                return owned[0]
        short_sleep(0.05)
    return None, None


def dump_dialog_controls(dialog_hwnd, note="диалог"):
    """Пишет в лог состав диалога — нужен, если кнопка не находится."""
    rows = []

    def cb(hwnd, _):
        try:
            rows.append((win32gui.GetClassName(hwnd),
                         win32gui.GetDlgCtrlID(hwnd),
                         win32gui.GetWindowText(hwnd)))
        except Exception:
            pass

    try:
        win32gui.EnumChildWindows(dialog_hwnd, cb, None)
    except Exception:
        return
    for cls, ctrl_id, text in rows:
        log(f"    [{note}] {cls} id={ctrl_id} text={text!r}")


def confirm_dialog(dialog_hwnd, note="диалог", timeout=2.0):
    """Подтверждает диалог (кнопка по умолчанию) — без опоры на фокус.

    Enter через PostMessage срабатывает, только если диалог активен: если
    фокус увели, Schemagee ничего не сохраняет, а окно закрывается как
    «Отмена». WM_COMMAND с ID кнопки обрабатывает сам диалог, поэтому
    результат не зависит ни от фокуса, ни от названия кнопки
    («Сохранить», «&Сохранить», «OK», «Да»…).
    """
    if not dialog_hwnd:
        return False

    log(f"  {note}: подтверждаем, активно: {foreground_title()}")

    # 1. Кнопка по умолчанию (IDOK) — то же, что Enter, но без фокуса.
    try:
        win32gui.PostMessage(dialog_hwnd, win32con.WM_COMMAND,
                             win32con.IDOK, 0)
    except Exception as e:
        log(f"  {note}: не удалось отправить команду OK: {e}")
    if wait_dialog_close(dialog_hwnd, timeout=timeout):
        return True

    # 2. Клик по кнопке сохранения/подтверждения.
    button = 0
    for candidate in ("Сохранить", "&Сохранить", "Save", "&Save",
                      "OK", "ОК", "Да", "&Да", "Yes", "&Yes"):
        try:
            button = win32gui.FindWindowEx(dialog_hwnd, None, "Button",
                                           candidate)
        except Exception:
            button = 0
        if button:
            break
    if button:
        try:
            win32gui.SendMessage(button, win32con.BM_CLICK, 0, 0)
        except Exception:
            pass
        if wait_dialog_close(dialog_hwnd, timeout=timeout):
            return True

    # 3. Enter в сам диалог.
    for msg in (win32con.WM_KEYDOWN, win32con.WM_KEYUP):
        try:
            win32gui.PostMessage(dialog_hwnd, msg, win32con.VK_RETURN, 0)
        except Exception:
            pass
    if wait_dialog_close(dialog_hwnd, timeout=timeout):
        return True

    log(f"  {note}: не удалось подтвердить — состав окна:")
    dump_dialog_controls(dialog_hwnd, note)
    return False


def close_schemagee_popup(main_hwnd, timeout=3.0, look=1.5):
    """Закрывает модальное окно Schemagee («Экспорт выполнен», вопрос).

    Раньше здесь вслепую отправлялись Tab и Enter в главное окно: если фокус
    оказывался не в модальном окне, оно оставалось висеть и блокировало
    следующие листы. Теперь сначала находим это окно и работаем именно с ним.
    """
    with stop_suspended():
        popup, title = schemagee_popup(main_hwnd, timeout=look)
        if not popup:
            return True
        log(f"  Закрываем окно Schemagee: {title!r}")
        if confirm_dialog(popup, note=f"окно {title!r}", timeout=timeout):
            return True
        # Не поддалось — активируем и жмём Enter именно в него.
        if force_foreground(popup, timeout=1.5):
            keyboard.send('enter')
            if wait_dialog_close(popup, timeout=2.0):
                return True
        log(f"  ВНИМАНИЕ: окно {title!r} не закрылось — отправляем Esc.")
        cancel_dialog(popup)
        return not _is_alive(popup)


# Состав формы «Экспорт» пишем в лог один раз: если кнопка называется иначе,
# чем мы ожидаем, это сразу видно.
_EXPORT_FORM = {"dumped": False}


def _find_button(dialog_hwnd, texts):
    """Кнопка окна с текстом из texts (точное или частичное совпадение).

    Кнопку «по умолчанию» намеренно НЕ берём: в форме экспорта это может быть
    «Отмена», и тогда окно закроется без конвертации.
    """
    found = []

    def cb(hwnd, _):
        try:
            if win32gui.GetClassName(hwnd) != "Button":
                return
            if not win32gui.IsWindowVisible(hwnd):
                return
            text = win32gui.GetWindowText(hwnd).strip()
        except Exception:
            return
        if text in texts:
            found.append(hwnd)
            return
        for t in texts:
            if len(t) > 3 and text.startswith(t):
                found.append(hwnd)
                return

    try:
        win32gui.EnumChildWindows(dialog_hwnd, cb, None)
    except Exception:
        pass
    return found[0] if found else 0


def _export_form(main_hwnd, look=1.5):
    """Модальное окно Schemagee, в котором нужно нажать «Экспорт».

    Диалог выбора файла («Сохранение») пропускаем: его уже закрыли, а нажатие
    «Сохранить/OK» в нём ничего не экспортирует.
    """
    end = time.time() + look
    main_pid = window_pid(main_hwnd)
    while time.time() < end:
        if main_pid:
            dialogs = []
            owned = []
            for hwnd, title in process_windows(main_pid, exclude=(main_hwnd,)):
                if title.strip() in SAVE_DIALOG_TITLES:
                    continue
                try:
                    cls = win32gui.GetClassName(hwnd)
                    owner = win32gui.GetWindow(hwnd, win32con.GW_OWNER)
                except Exception:
                    continue
                if cls == "#32770":
                    dialogs.append((hwnd, title))
                elif owner:
                    owned.append((hwnd, title))
            if dialogs:
                return dialogs[0]
            if owned:
                return owned[0]
        short_sleep(0.05)
    return None, None


def press_export_button(main_hwnd, look=1.5, note="Экспорт"):
    """Нажимает кнопку, запускающую конвертацию, в форме Schemagee.

    Диалог «Сохранение» только запоминает путь: сам экспорт запускает кнопка
    в окне «Экспорт в DXF» (в старом кликере её нажимали Tab+Enter после
    подтверждения диалога). Без этого нажатия окно висит, а DXF не создаётся.
    """
    with stop_suspended():
        form, title = _export_form(main_hwnd, look=look)
        if not form:
            log("  Окно «Экспорт» не найдено — вероятно, конвертация уже идёт.")
            return False

        log(f"  Запускаем конвертацию: окно {title!r}")
        if not _EXPORT_FORM["dumped"]:
            dump_dialog_controls(form, title or note)
            _EXPORT_FORM["dumped"] = True

        button = _find_button(form, EXPORT_BUTTON_TEXTS)
        if button:
            try:
                text = win32gui.GetWindowText(button)
                log(f"    нажимаем кнопку {text!r} "
                    f"(id={win32gui.GetDlgCtrlID(button)})")
                win32gui.SendMessage(button, win32con.BM_CLICK, 0, 0)
            except Exception as e:
                log(f"    BM_CLICK не сработал: {e}")
            if wait_dialog_close(form, timeout=4.0):
                return True

        # Запасные варианты: команда OK, затем Enter в само окно.
        try:
            win32gui.PostMessage(form, win32con.WM_COMMAND, win32con.IDOK, 0)
        except Exception as e:
            log(f"    команда OK не отправлена: {e}")
        if wait_dialog_close(form, timeout=2.0):
            return True

        for msg in (win32con.WM_KEYDOWN, win32con.WM_KEYUP):
            try:
                win32gui.PostMessage(form, msg, win32con.VK_RETURN, 0)
            except Exception:
                pass
        if wait_dialog_close(form, timeout=2.0):
            return True

        # Как в старом кликере: активировать окно и нажать Tab+Enter.
        if force_foreground(form, timeout=1.5):
            keyboard.send('tab')
            pause(0.2)
            keyboard.send('enter')
            if wait_dialog_close(form, timeout=2.0):
                return True

        log(f"  ВНИМАНИЕ: окно {title!r} не закрылось после «Экспорт».")
        return False


def find_filename_edit(dialog_hwnd):
    """Поле имени файла в диалоге «Сохранение»."""
    result = []

    def cb(hwnd, _):
        try:
            cls = win32gui.GetClassName(hwnd)
            if cls == "Edit" and win32gui.IsWindowVisible(hwnd):
                result.append(hwnd)
        except Exception:
            pass

    win32gui.EnumChildWindows(dialog_hwnd, cb, None)
    return result[0] if result else None


def set_save_path(dialog_hwnd, path):
    edit_hwnd = find_filename_edit(dialog_hwnd)
    if not edit_hwnd:
        log("  НЕ НАШЛИ поле Edit в диалоге сохранения.")
        dump_dialog_controls(dialog_hwnd, "Сохранение")
        return False
    try:
        # В диалогах Windows поле имени файла часто лежит внутри ComboBox:
        # текст нужно задать обоим, иначе диалог возьмёт прежнее имя.
        parent = win32gui.GetParent(edit_hwnd)
        if parent and win32gui.GetClassName(parent) == "ComboBox":
            win32gui.SendMessage(parent, win32con.WM_SETTEXT, 0, path)
        win32gui.SendMessage(edit_hwnd, win32con.EM_SETSEL, 0, -1)
        win32gui.SendMessage(edit_hwnd, win32con.WM_SETTEXT, 0, path)
        log(f"  Путь установлен: {path} "
            f"(поле id={win32gui.GetDlgCtrlID(edit_hwnd)})")
        return True
    except Exception as e:
        log(f"  Не удалось установить путь: {e}")
        return False


def click_save_button(dialog_hwnd):
    result = []

    def cb(hwnd, _):
        try:
            cls = win32gui.GetClassName(hwnd)
            text = win32gui.GetWindowText(hwnd)
            if cls == "Button" and text in ("Сохранить", "&Сохранить",
                                            "Save", "&Save"):
                result.append(hwnd)
        except Exception:
            pass

    win32gui.EnumChildWindows(dialog_hwnd, cb, None)
    if result:
        win32gui.SendMessage(result[0], win32con.BM_CLICK, 0, 0)
        return True
    return False


def wait_dialog_close(dialog_hwnd, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        try:
            if not win32gui.IsWindow(dialog_hwnd):
                return True
        except Exception:
            return True
        short_sleep(0.05)
    return False


def cancel_dialog(dialog_hwnd):
    """Закрывает диалог по Esc (если он ещё открыт)."""
    if not dialog_hwnd:
        return
    with stop_suspended():
        _cancel_dialog_now(dialog_hwnd)


def _cancel_dialog_now(dialog_hwnd):
    try:
        if not win32gui.IsWindow(dialog_hwnd):
            return
        force_foreground(dialog_hwnd, timeout=1.0)
        keyboard.send('esc')
        if wait_dialog_close(dialog_hwnd, timeout=2.0):
            return
        win32gui.PostMessage(dialog_hwnd, win32con.WM_KEYDOWN,
                             win32con.VK_ESCAPE, 0)
        win32gui.PostMessage(dialog_hwnd, win32con.WM_KEYUP,
                             win32con.VK_ESCAPE, 0)
        if wait_dialog_close(dialog_hwnd, timeout=1.0):
            return
        win32gui.PostMessage(dialog_hwnd, win32con.WM_CLOSE, 0, 0)
    except Exception as e:
        log(f"  Не удалось закрыть диалог: {e}")


def wait_file_created(path, timeout=10.0):
    """Ждёт, пока DXF появится и его размер перестанет меняться."""
    end = time.time() + timeout
    last_size = -1
    while time.time() < end:
        try:
            if os.path.isfile(path):
                size = os.path.getsize(path)
                if size > 0 and size == last_size:
                    return True
                last_size = size
        except OSError:
            pass
        short_sleep(0.1)
    return False


# --- Число листов -------------------------------------------------------------
def clear_clipboard():
    """Очищает буфер обмена и убеждается, что он действительно пуст."""
    for attempt in range(1, 4):
        try:
            pyperclip.copy("")
        except Exception as e:
            log(f"  Буфер обмена: ошибка очистки ({attempt}/3): {e}")
            time.sleep(0.15)
            continue
        time.sleep(0.12)
        try:
            current = pyperclip.paste() or ""
        except Exception:
            current = ""
        if not str(current).strip():
            return True
        log(f"  Буфер обмена не пуст после очистки: {str(current)[:40]!r}")
    return False


def parse_sheet_count(text):
    """Строго: только целое число 1..500, никакого «мусора» из буфера."""
    if text is None:
        return None
    cleaned = str(text).strip()
    if not re.fullmatch(r"\d{1,3}", cleaned):
        return None
    value = int(cleaned)
    if 1 <= value <= 500:
        return value
    return None


def _control_text(hwnd):
    """Текст контрола другого процесса.

    win32gui.GetWindowText() для чужих Edit возвращает пустую строку,
    а WM_GETTEXT через SendMessage отдаёт настоящий текст поля.
    """
    try:
        length = win32gui.SendMessage(hwnd, win32con.WM_GETTEXTLENGTH, 0, 0)
        buf = ctypes.create_unicode_buffer(length + 1)
        win32gui.SendMessage(hwnd, win32con.WM_GETTEXT, length + 1, buf)
        return buf.value
    except Exception:
        return ""


def _dialog_controls(dialog_hwnd):
    """Контролы диалога: (hwnd, класс, id, текст, (l, t, r, b))."""
    items = []

    def cb(hwnd, _):
        try:
            items.append((hwnd, win32gui.GetClassName(hwnd),
                          win32gui.GetDlgCtrlID(hwnd), _control_text(hwnd),
                          win32gui.GetWindowRect(hwnd)))
        except Exception:
            pass

    try:
        win32gui.EnumChildWindows(dialog_hwnd, cb, None)
    except Exception:
        pass
    return items


def _find_edit_after_label(items, label_start):
    """Edit справа от статического текста («с:»/«по:») на той же строке."""
    for hwnd, cls, cid, text, rect in items:
        if cls != "Static" or not text.strip().lower().startswith(label_start):
            continue
        center_y = (rect[1] + rect[3]) // 2
        right = [i for i in items
                 if i[1] == "Edit" and i[4][0] >= rect[2] - 2
                 and abs((i[4][1] + i[4][3]) // 2 - center_y) <= 12]
        right.sort(key=lambda i: i[4][0])
        if right:
            return right[0]
    return None


def read_pages_count(dialog_hwnd):
    """Число листов из блока «Страницы печати» (поле «по:»).

    Поле содержит последнюю страницу (обычно = числу листов) и заполнено
    даже тогда, когда недоступно для ввода, поэтому читаем текст
    контрола, а не буфер обмена. None — если поле пустое/не найдено.
    """
    items = _dialog_controls(dialog_hwnd)
    if not items:
        return None

    states = {}
    for hwnd, cls, cid, text, rect in items:
        if cls == "Button" and cid in (PRINT_RADIO_ALL, PRINT_RADIO_RANGE):
            try:
                states[cid] = win32gui.SendMessage(hwnd, win32con.BM_GETCHECK,
                                                   0, 0)
            except Exception:
                states[cid] = "?"

    to_edit = None
    from_text = ""
    for item in items:
        if item[1] != "Edit":
            continue
        if item[2] == PRINT_EDIT_TO:
            to_edit = item
        elif item[2] == PRINT_EDIT_FROM:
            from_text = item[3]
    if to_edit is None:
        to_edit = _find_edit_after_label(items, "по")

    to_text = to_edit[3] if to_edit else None
    log(f"  [печать] «все»={states.get(PRINT_RADIO_ALL)}, "
        f"«С:»={states.get(PRINT_RADIO_RANGE)}, "
        f"с={from_text!r}, по={to_text!r}")

    if to_edit is None:
        return None
    return parse_sheet_count(to_edit[3])


def switch_pages_to_range(dialog_hwnd):
    """Включает «Страницы печати» → «С: ... по: ...» (когда поле пустое)."""
    for hwnd, cls, cid, text, rect in _dialog_controls(dialog_hwnd):
        if cls != "Button":
            continue
        if cid == PRINT_RADIO_RANGE or text.strip().lower() in ("с:", "с :"):
            try:
                win32gui.SendMessage(hwnd, win32con.BM_CLICK, 0, 0)
            except Exception as e:
                log(f"  [печать] Не удалось включить диапазон: {e}")
                return False
            pause(0.4)
            log("  [печать] Включён диапазон страниц «С:».")
            return True
    return False


def read_sheet_count_from_print_dialog():
    """Ctrl+P → читаем «по: N» из блока «Страницы печати».

    Раньше число вытягивалось 13 нажатиями Tab и копированием в буфер:
    при неактивном поле («печатать все») копирование не срабатывало, и
    число терялось. Поле содержит текст и в неактивном состоянии,
    поэтому читаем его напрямую; Tab+буфер оставлены резервным путём.
    """
    check_stop()
    log("  [печать] Определяем число листов через диалог печати…")

    hwnd, title = wait_schemagee_window(timeout=5.0)
    if not hwnd:
        log("  [печать] Окно Schemagee не найдено.")
        return None
    ensure_foreground(hwnd, what="Schemagee (диалог печати)")
    log(f"  [печать] Активное окно: {foreground_title()}")
    pause(0.3)

    keyboard.send('ctrl+p')

    dialog_hwnd = find_print_dialog(timeout=8.0)
    if not dialog_hwnd:
        log("  [печать] Диалог «Печать» не открылся.")
        return None

    log(f"  [печать] Диалог найден (hwnd={dialog_hwnd}).")
    if not wait_window_ready(dialog_hwnd, timeout=3.0):
        log("  [печать] Диалог не стал доступным для ввода.")
    pause(0.4)

    count = read_pages_count(dialog_hwnd)
    if count is None and switch_pages_to_range(dialog_hwnd):
        count = read_pages_count(dialog_hwnd)

    if count is not None:
        cancel_dialog(dialog_hwnd)
        log(f"  [печать] Число листов: {count} (поле «по:»)")
        return count

    # Резерв: старый путь через Tab и буфер обмена.
    log("  [печать] Поле не прочитано — резервный путь через буфер.")
    if not clear_clipboard():
        log("  [печать] Буфер обмена не очищен — доверять копированию нельзя.")
        cancel_dialog(dialog_hwnd)
        return None

    for _ in range(PRINT_DIALOG_TAB_COUNT):
        check_stop()
        keyboard.send('tab')
        time.sleep(TAB_SETTLE)
    pause(0.3)

    keyboard.send('ctrl+a')
    pause(0.2)
    keyboard.send('ctrl+c')
    pause(0.45)

    copied = ""
    try:
        copied = pyperclip.paste() or ""
    except Exception as e:
        log(f"  [печать] Не удалось прочитать буфер обмена: {e}")

    log(f"  [печать] В буфере: {str(copied)[:60]!r}")

    cancel_dialog(dialog_hwnd)

    count = parse_sheet_count(copied)
    if count is None:
        log(f"  [печать] Это не целое число 1..500: {str(copied)[:60]!r}")
        return None

    log(f"  [печать] Число листов: {count}")
    return count


def read_sheet_count_auto(attempts=2):
    """Диалог печати с повтором. None — если оба раза не получилось."""
    for attempt in range(1, attempts + 1):
        check_stop()
        log(f"Определение числа листов: попытка {attempt}/{attempts}.")
        count = read_sheet_count_from_print_dialog()
        if count is not None:
            return count
        if attempt < attempts:
            log("  Повторяем определение числа листов…")
            pause(1.2)
    return None


def read_sheet_count_from_sdd(path):
    """Число листов по штампам «Формат А<цифра>» внутри .sdd.

    Резервный способ, когда диалог печати не отдал число листов: на
    больших проектах Ctrl+C в диалоге иногда не срабатывает, а штамп
    каждого листа всегда содержит «Формат А2»/«А3»/«А4» (cp1251).
    """
    try:
        with open(path, 'rb') as fp:
            data = fp.read()
        count = len(re.findall('Формат А[0-9]'.encode('cp1251'), data))
    except Exception as e:
        log(f"Подсчёт листов по .sdd: ошибка: {e}")
        return None
    log(f"Листов по штампам в .sdd: {count}")
    return count or None


def switch_to_english(hwnd=None):
    """Английская раскладка у окна Schemagee — иначе Ctrl+P/Shift+D не сработают.

    Раскладка привязана к потоку окна, поэтому переключать её нужно у окна
    Schemagee, а не у нашего интерфейса.
    """
    try:
        if hwnd is None:
            hwnd = ctypes.windll.user32.GetForegroundWindow()
        hkl_en = ctypes.windll.user32.LoadKeyboardLayoutW("00000409", 1)
        target_thread = ctypes.windll.user32.GetWindowThreadProcessId(hwnd, None)
        current_hkl = ctypes.windll.user32.GetKeyboardLayout(target_thread)
        if (current_hkl & 0xFFFF) == 0x0409:
            log("Раскладка уже английская.")
            return
        ctypes.windll.user32.PostMessageW(hwnd, 0x0050, 0, hkl_en)
        time.sleep(0.25)
        current_hkl = ctypes.windll.user32.GetKeyboardLayout(target_thread)
        if (current_hkl & 0xFFFF) != 0x0409:
            force_foreground(hwnd)
            keyboard.send('alt+shift')
            time.sleep(0.25)
        log("Раскладка переключена на английскую.")
    except Exception as e:
        log(f"Ошибка переключения раскладки: {e}")


def open_file_in_schemagee(path):
    """Открывает .sdd так, как это делает проводник (двойной клик)."""
    normalized = os.path.normpath(path)
    log(f"Открываем файл: {normalized}")

    try:
        result = ctypes.windll.shell32.ShellExecuteW(
            None, "open", normalized, None, None, 1
        )
        if result > 32:
            log(f"  ShellExecuteW: успешно (код {result}).")
            return True
        log(f"  ShellExecuteW вернул код {result}.")
    except Exception as e:
        log(f"  ShellExecuteW не сработал: {e}")

    try:
        subprocess.Popen(f'start "" "{normalized}"', shell=True)
        log("  cmd start: успешно.")
        return True
    except Exception as e:
        log(f"  cmd start не сработал: {e}")

    log("  НИ ОДИН СПОСОБ ОТКРЫТИЯ ФАЙЛА НЕ СРАБОТАЛ.")
    return False


def close_schemagee(timeout=8.0):
    """Закрывает Schemagee без сохранения (Alt+F4 -> «Нет»)."""
    log("Закрываем Schemagee…")
    hwnd, _ = wait_schemagee_window(timeout=2.0)
    if not hwnd:
        log("  Окно Schemagee уже закрыто.")
        return True

    # Модальное окно («Экспорт», сообщение) перехватит Alt+F4 — сначала
    # закрываем его, иначе Schemagee останется открытым.
    close_schemagee_popup(hwnd, timeout=2.0, look=0.5)

    if not force_foreground(hwnd, timeout=3.0):
        log(f"  ВНИМАНИЕ: не удалось активировать Schemagee "
            f"(активно: {foreground_title()}). Закройте его вручную — "
            "Alt+F4 в чужое окно не отправляем.")
        return False
    keyboard.send('alt+f4')
    time.sleep(0.8)

    dialog = find_modal_dialog(timeout=1.5)
    if dialog:
        log("  Появился вопрос о сохранении — отвечаем «Нет».")
        force_foreground(dialog, timeout=1.0)
        keyboard.send('n')
        time.sleep(0.3)
    else:
        log("  Вопроса о сохранении не было.")

    end = time.time() + timeout
    while time.time() < end:
        try:
            if not win32gui.IsWindow(hwnd) or not win32gui.IsWindowVisible(hwnd):
                log("  Schemagee закрыт.")
                return True
        except Exception:
            log("  Schemagee закрыт.")
            return True
        time.sleep(0.1)

    log("  ВНИМАНИЕ: Schemagee не закрылся за отведённое время.")
    return False


def window_responsive(hwnd, timeout_ms=1200):
    """Окно отвечает на WM_NULL — значит не зависло."""
    if not hwnd:
        return False
    try:
        result = ctypes.windll.user32.SendMessageTimeoutW(
            hwnd, 0x0000, 0, 0, 0x0002, int(timeout_ms), None)
        return bool(result)
    except Exception:
        return False


def schemagee_hung(main_hwnd=None):
    """True, если окно Schemagee не отвечает на сообщения."""
    hwnd = main_hwnd or find_schemagee_window()[0]
    if not hwnd:
        return False
    try:
        if ctypes.windll.user32.IsHungAppWindow(hwnd):
            return True
    except Exception:
        pass
    return not window_responsive(hwnd)


def kill_schemagee():
    """Принудительно завершает зависший Schemagee."""
    hwnd, title = find_schemagee_window()
    if not hwnd:
        return True
    pid = window_pid(hwnd)
    log(f"  Schemagee не отвечает — завершаем процесс {pid}.")
    try:
        subprocess.run(["taskkill", "/PID", str(pid), "/F", "/T"],
                       capture_output=True, timeout=10)
    except Exception as e:
        log(f"  taskkill: {e}")
    end = time.time() + 8.0
    while time.time() < end:
        if not find_schemagee_window()[0]:
            log("  Schemagee завершён.")
            return True
        time.sleep(0.2)
    log("  ВНИМАНИЕ: Schemagee всё ещё в памяти.")
    return False


def reopen_schemagee(path, sheet_num):
    """Перезапускает Schemagee после зависания и переходит на нужный лист."""
    log(f"  Перезапуск Schemagee (нужен лист {sheet_num})…")
    kill_schemagee()
    pause(1.0)
    if not open_file_in_schemagee(path):
        return 0
    hwnd, title = wait_schemagee_window(timeout=30.0)
    if not hwnd:
        log("  Окно Schemagee не появилось после перезапуска.")
        return 0
    wait_window_ready(hwnd, timeout=10.0)
    wait_schemagee_ready(hwnd, timeout=60.0)
    ensure_foreground(hwnd, what="Schemagee")
    switch_to_english(hwnd)
    pause(SHEET_OPEN_PAUSE)
    for _ in range(max(0, sheet_num - 1)):
        check_stop()
        ensure_schemagee_active(hwnd, what="Schemagee")
        keyboard.send('page down')
        pause(PAGE_TURN_PAUSE)
    _OWNED_SCHEMAGEE["opened"] = True
    log("  Schemagee перезапущен.")
    return hwnd


def wait_schemagee_ready(hwnd, timeout=60.0, stable=3, interval=0.5):
    """Ждёт, пока Schemagee закончит открытие документа и начнёт отвечать.

    После появления окна приложение ещё читает файл: если отправить
    команду сразу, она теряется и процесс «стоит». Ждём несколько подряд
    успешных откликов на WM_NULL и отсутствия модальных окон.
    """
    end = time.time() + timeout
    good = 0
    while time.time() < end:
        check_stop()
        if not _is_alive(hwnd):
            return False
        ok = window_responsive(hwnd, timeout_ms=1500)
        if ok:
            popup, _title = schemagee_popup(hwnd, timeout=0.2)
            ok = popup is None
        if ok:
            good += 1
            if good >= stable:
                return True
        else:
            good = 0
        short_sleep(interval)
    return False


def log_recent_files(folder, since, limit=6):
    """Пишет в лог файлы, появившиеся в папке за время попытки.

    Нужно, чтобы сразу увидеть, куда Schemagee сохранил лист, если имя или
    расширение получились другими.
    """
    try:
        rows = []
        for name in os.listdir(folder):
            path = os.path.join(folder, name)
            try:
                mtime = os.path.getmtime(path)
                size = os.path.getsize(path)
            except OSError:
                continue
            if mtime >= since:
                rows.append((mtime, name, size))
        if not rows:
            log("    в папке результата новых файлов нет.")
            return
        for _mtime, name, size in sorted(rows)[-limit:]:
            log(f"    появился файл: {name} ({size} байт)")
    except Exception as e:
        log(f"    не удалось посмотреть папку результата: {e}")


def _save_current_sheet(out_path, main_hwnd=None):
    """Одна попытка сохранить текущий лист в DXF. True — файл на диске."""
    attempt_start = time.time()
    if os.path.isfile(out_path):
        try:
            os.remove(out_path)   # чтобы старый файл не сошёл за новый
        except OSError as e:
            log(f"  Не удалось удалить прежний файл {out_path}: {e}")

    pause(KEY_SETTLE)
    keyboard.send('shift+d')
    # Диалог открывается сам; Enter отправляем только если его нет, иначе
    # можно закрыть уже открытый диалог раньше, чем впишем путь.
    dialog_hwnd = find_save_dialog(timeout=3.0)
    if not dialog_hwnd:
        keyboard.send('enter')
        dialog_hwnd = find_save_dialog(timeout=SAVE_DIALOG_TIMEOUT - 3.0)
    if not dialog_hwnd:
        log("  ВНИМАНИЕ: окно «Сохранение» не появилось.")
        return False

    if not wait_window_ready(dialog_hwnd, timeout=3.0):
        log("  ВНИМАНИЕ: окно «Сохранение» не готово к вводу.")
    pause(0.2)

    if not set_save_path(dialog_hwnd, out_path):
        cancel_dialog(dialog_hwnd)
        return False

    pause(0.15)
    # Подтверждаем по ID кнопки, а не «слепым» Enter: Enter срабатывает,
    # только когда диалог активен, иначе Schemagee закрывает его как
    # «Отмена» — окно исчезает, а DXF не появляется.
    confirm_dialog(dialog_hwnd, note="«Сохранение»")

    if not wait_dialog_close(dialog_hwnd, timeout=3.0):
        log("  ВНИМАНИЕ: диалог сохранения не закрылся.")
        cancel_dialog(dialog_hwnd)

    # Путь выбран, но Schemagee сам конвертацию не начинает: нужно нажать
    # «Экспорт/ОК» в форме «Экспорт в DXF» (в старом кликере — Tab+Enter).
    if main_hwnd:
        press_export_button(main_hwnd, look=1.0)

    created = wait_file_created(out_path, timeout=FILE_APPEAR_TIMEOUT)
    if created and not _dxf_has_content(out_path):
        log("  Файл создан, но пустой/битый — считаем попытку неудачной.")
        created = False

    if not created and main_hwnd:
        # Окно могло остаться (нажатие не дошло) — пробуем ещё раз.
        log("  Файл не появился — повторно нажимаем «Экспорт».")
        if press_export_button(main_hwnd, look=0.5):
            created = wait_file_created(out_path, timeout=5.0)

    if not created:
        log_recent_files(os.path.dirname(out_path), since=attempt_start)

    # Schemagee показывает модальное окно («Экспорт выполнен»). Если его не
    # закрыть, оно висит поверх и блокирует следующие листы.
    if created and main_hwnd:
        close_schemagee_popup(main_hwnd, timeout=3.0)

    return created


def verify_sheet_file(path):
    """Проверка листа перед переходом к следующему: файл есть и читается.

    Экспорт может «успеть» создать файл, но без геометрии. Такой лист
    нельзя считать сохранённым — его нужно переэкспортировать, иначе
    схема молча пропадёт из альбома.
    """
    if not os.path.isfile(path):
        log(f"  ПРОВЕРКА: файла нет: {os.path.basename(path)}")
        return False
    try:
        size = os.path.getsize(path)
    except OSError as e:
        log(f"  ПРОВЕРКА: не удалось прочитать размер: {e}")
        return False
    if size <= 0:
        log(f"  ПРОВЕРКА: файл пуст: {os.path.basename(path)}")
        return False
    if not _dxf_has_content(path):
        log(f"  ПРОВЕРКА: DXF без содержимого: {os.path.basename(path)}")
        return False
    log(f"  ПРОВЕРКА: лист в порядке ({size} байт).")
    return True


def algorithm_conversion(original_file_path, sheet_count, out_folder, emit=None):
    """Экспортирует каждый лист в отдельный DXF.

    Возвращает (produced, missing): список путей и список номеров листов,
    которые не удалось сохранить.
    """
    filename = os.path.basename(original_file_path)
    name = os.path.splitext(filename)[0]

    produced = []
    missing = []
    recoveries = 0

    for sheet_num in range(1, sheet_count + 1):
        check_stop()
        sheet_start = time.time()
        log(f"--- Лист {sheet_num} из {sheet_count} ---")
        if emit:
            emit("progress", f"Экспорт листа {sheet_num} из {sheet_count}…")

        out_filename = f"{name}_лист{sheet_num}.dxf"
        out_path = os.path.join(out_folder, out_filename)
        log(f"  Сохраняем в: {out_path}")

        hwnd, title = wait_schemagee_window(timeout=5.0)
        if not hwnd:
            raise RuntimeError(
                "Окно Schemagee не найдено — приложение закрылось "
                "или его свернули."
            )
        ensure_schemagee_active(hwnd, what="Schemagee")

        ok = False
        for attempt in range(1, SHEET_ATTEMPTS + 1):
            check_stop()
            log(f"  Попытка {attempt}/{SHEET_ATTEMPTS}.")
            # Schemagee мог не получить команду и зависнуть: не ждём
            # бесконечно — перезапускаем его и повторяем лист.
            if schemagee_hung(hwnd):
                if recoveries >= MAX_RECOVERIES:
                    raise RuntimeError(
                        "Schemagee зависает и не восстанавливается.\n\n"
                        "Закройте его вручную и повторите запуск.")
                recoveries += 1
                log("  Schemagee не отвечает — перезапускаем и повторяем лист.")
                hwnd = reopen_schemagee(original_file_path, sheet_num)
                if not hwnd:
                    raise RuntimeError(
                        "Не удалось перезапустить Schemagee после зависания.")
            # Застрявшее модальное окно Schemagee перекрывает главное — от
            # него сначала избавляемся, иначе Shift+D уйдёт в него.
            close_schemagee_popup(hwnd, timeout=2.0, look=0.3)
            ensure_schemagee_active(hwnd, what="Schemagee")
            try:
                if _save_current_sheet(out_path, main_hwnd=hwnd):
                    if not verify_sheet_file(out_path):
                        log("  Лист не прошёл проверку — повторяем.")
                        continue
                    ok = True
                    log(f"  УСПЕХ: файл создан "
                        f"({os.path.getsize(out_path)} байт).")
                    break
            except TaskStopped:
                raise
            except Exception as e:
                log(f"  Ошибка сохранения листа: {e}")
            log(f"  Файла {out_filename} нет — повторяем лист.")
            close_schemagee_popup(hwnd, timeout=2.0, look=0.3)
            pause(SHEET_RETRY_PAUSE)

        if ok:
            produced.append(out_path)
        else:
            missing.append(sheet_num)
            log(f"  ОШИБКА: лист {sheet_num} не сохранён "
                f"за {SHEET_ATTEMPTS} попытки.")

        # Страховка: если модальное окно Schemagee ещё висит — закрываем его.
        hwnd, _ = wait_schemagee_window(timeout=3.0)
        if hwnd:
            close_schemagee_popup(hwnd, timeout=3.0, look=0.5)

        if sheet_num < sheet_count:
            hwnd, _ = wait_schemagee_window(timeout=3.0)
            if not hwnd:
                raise RuntimeError(
                    f"Окно Schemagee пропало после листа {sheet_num}."
                )
            ensure_schemagee_active(hwnd, what="Schemagee")
            pause(SHEET_NEXT_PAUSE)
            keyboard.send('page down')
            pause(PAGE_TURN_PAUSE)

        log(f"  Лист обработан за "
            f"{format_time(time.time() - sheet_start)} сек.")

    return produced, missing


# =============================================================================
# БЛОК 3. КОНВЕРТЕР: DXF -> DWG
#
#   3.1. DXF -> DWG (ODA File Converter, иначе AutoCAD через COM)
#   3.2. Сборка альбома: схемы в ряд, по layout на лист A2, шрифт и сжатие текста
# =============================================================================

# --- 3.1. DXF -> DWG ---------------------------------------------------------
DEFAULT_DWG_VERSION = 'ACAD2018'

# Типовые места установки ODA File Converter.
ODA_PATTERNS = (
    r'C:\Program Files\ODA\*\ODAFileConverter.exe',
    r'C:\Program Files (x86)\ODA\*\ODAFileConverter.exe',
    r'C:\Program Files\ODA\ODAFileConverter*\ODAFileConverter.exe',
    r'C:\Program Files (x86)\ODA\ODAFileConverter*\ODAFileConverter.exe',
)


class NoBackendError(RuntimeError):
    """Ни ODA File Converter, ни AutoCAD не найдены."""


def find_oda():
    """Путь к ODAFileConverter.exe или None."""
    env = os.environ.get('ODA_CONVERTER')
    if env:
        return env if os.path.isfile(env) else None
    hits = []
    for pattern in ODA_PATTERNS:
        hits.extend(glob.glob(pattern))
    if not hits:
        return None
    return sorted(hits)[-1]   # самая свежая версия


def has_acad():
    """Есть ли установленный AutoCAD (проверка по реестру, без запуска)."""
    try:
        import winreg
    except ImportError:
        return False
    for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        for base in (r'SOFTWARE\Autodesk\AutoCAD',
                     r'SOFTWARE\Autodesk\AutoCAD LT'):
            for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
                try:
                    with winreg.OpenKey(root, base, 0,
                                        winreg.KEY_READ | view):
                        return True
                except OSError:
                    continue
    return False


def available_backends():
    """Доступные бэкенды в порядке приоритета."""
    result = []
    if find_oda():
        result.append('oda')
    if has_acad():
        result.append('acad')
    return result


def convert_with_oda(exe, dxf_path, dwg_path, version=DEFAULT_DWG_VERSION,
                     timeout=600):
    """Конвертация через ODA File Converter (папка -> папка)."""
    dxf_path = os.path.abspath(dxf_path)
    dwg_path = os.path.abspath(dwg_path)
    base = os.path.splitext(os.path.basename(dxf_path))[0]

    with tempfile.TemporaryDirectory(prefix='oda_') as tmp:
        in_dir = os.path.join(tmp, 'in')
        out_dir = os.path.join(tmp, 'out')
        os.makedirs(in_dir)
        os.makedirs(out_dir)
        shutil.copy2(dxf_path, os.path.join(in_dir, base + '.dxf'))

        cmd = [exe, in_dir, out_dir, version, 'DWG', '0', '1',
               base + '.dxf', base + '.dwg']
        log('ODA: ' + ' '.join(cmd))
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout)

        produced = os.path.join(out_dir, base + '.dwg')
        if not os.path.isfile(produced):
            raise RuntimeError(
                'ODA File Converter не создал DWG '
                f'(код возврата {proc.returncode}). '
                f'stdout: {(proc.stdout or "").strip()[:400]} '
                f'stderr: {(proc.stderr or "").strip()[:400]}'
            )

        os.makedirs(os.path.dirname(dwg_path), exist_ok=True)
        shutil.move(produced, dwg_path)

    log(f'DWG создан (ODA): {dwg_path}')
    return dwg_path


def convert_with_acad(dxf_path, dwg_path, attempts=10, delay=3.0):
    """Конвертация через COM AutoCAD (pywin32).

    AutoCAD, только что поднятый через COM, первые секунды отвечает
    RPC_E_CALL_REJECTED («Вызов был отклонён»). Поэтому SaveAs повторяем,
    НЕ переоткрывая документ.
    """
    try:
        import win32com.client
    except ImportError as e:
        raise RuntimeError('pywin32 не установлен: pip install pywin32') from e

    dxf_path = os.path.abspath(dxf_path)
    dwg_path = os.path.abspath(dwg_path)
    os.makedirs(os.path.dirname(dwg_path), exist_ok=True)

    try:
        acad = win32com.client.Dispatch('AutoCAD.Application')
    except Exception as e:
        raise RuntimeError(f'AutoCAD не запускается через COM: {e}') from e

    try:
        acad.Visible = False
    except Exception:
        pass

    doc = acad.Documents.Open(dxf_path)
    try:
        last = None
        for attempt in range(1, attempts + 1):
            try:
                doc.SaveAs(dwg_path)     # без формата — родной DWG
                last = None
                break
            except Exception as e:
                last = e
                log(f'AutoCAD SaveAs, попытка {attempt}/{attempts}: {e}')
                if attempt < attempts:
                    time.sleep(delay)
        if last is not None:
            raise RuntimeError(
                f'AutoCAD не смог сохранить DWG за {attempts} попыток: {last}'
            ) from last
    finally:
        try:
            doc.Close(False)
        except Exception as e:
            log(f'Не удалось закрыть документ {dxf_path}: {e}')
    log(f'DWG создан (AutoCAD): {dwg_path}')
    return dwg_path


def dxf_to_dwg_convert(dxf_path, dwg_path=None, backend='auto',
                       version=DEFAULT_DWG_VERSION):
    """DXF -> DWG выбранным бэкендом. Возвращает путь к DWG."""
    dxf_path = os.path.abspath(dxf_path)
    if not os.path.isfile(dxf_path):
        raise FileNotFoundError(f'нет файла DXF: {dxf_path}')
    if dwg_path is None:
        dwg_path = os.path.splitext(dxf_path)[0] + '.dwg'
    dwg_path = os.path.abspath(dwg_path)

    if backend == 'auto':
        found = available_backends()
        if not found:
            raise NoBackendError(
                'Не найден ни ODA File Converter, ни AutoCAD.\n'
                'Варианты:\n'
                '  1) установить ODA File Converter (бесплатный) и указать путь '
                'в переменной окружения ODA_CONVERTER;\n'
                '  2) установить AutoCAD (тогда работает COM-бэкенд).'
            )
        backend = found[0]
        log(f'Автовыбор бэкенда DXF->DWG: {backend}')

    if backend == 'oda':
        exe = find_oda()
        if not exe:
            raise NoBackendError(
                'ODA File Converter не найден. Укажите путь в ODA_CONVERTER.'
            )
        return convert_with_oda(exe, dxf_path, dwg_path, version=version)
    if backend == 'acad':
        return convert_with_acad(dxf_path, dwg_path)
    raise ValueError(f'неизвестный бэкенд: {backend}')


# --- 3.2. Сборка альбома ------------------------------------------------------
TEXT_COMPRESS = 0.6

# Слой с таблицами: текст, который стоит вплотную к линиям, сжимаем
# до минимально достаточного коэффициента из TABLE_COMPRESS_STEPS.
TABLE_LAYER = 'Таблицы'
TABLE_COMPRESS_STEPS = [0.55, 0.5, 0.45, 0.4, 0.35]
TABLE_CHAR_GAP = 0.1

# Шрифт, который ставим всем существующим стилям (TEXTSTYLE).
FONT_FILE = 'isocpeui.ttf'

# Параметры листа (A2 landscape, мм)
PAPER_W = 594.0
PAPER_H = 420.0
MARGIN = 5.0

# Форматы листов (мм) — альбомная ориентация. По умолчанию A2.
PAPER_FORMATS = {
    'A2': (594.0, 420.0),
    'A3': (420.0, 297.0),
    'A4': (297.0, 210.0),
}
PAPER_FORMAT_DEFAULT = 'A2'


def paper_size(fmt=PAPER_FORMAT_DEFAULT, landscape=True):
    """Размер листа (ширина, высота) в мм с учётом ориентации."""
    width, height = PAPER_FORMATS.get(fmt, PAPER_FORMATS[PAPER_FORMAT_DEFAULT])
    if not landscape:
        width, height = height, width
    return width, height
GAP = 10.0


_DXF_CODE_RE = re.compile(r'^\s*-?\d{1,4}\s*$')

# Кодовые страницы DXF ($DWGCODEPAGE) -> кодировка Python.
_DXF_CODEPAGES = {
    'ANSI_1250': 'cp1250', 'ANSI_1251': 'cp1251', 'ANSI_1252': 'cp1252',
    'ANSI_1253': 'cp1253', 'ANSI_1254': 'cp1254', 'ANSI_1255': 'cp1255',
    'ANSI_1256': 'cp1256', 'ANSI_1257': 'cp1257', 'ANSI_1258': 'cp1258',
    'DOS437': 'cp437', 'DOS850': 'cp850', 'DOS866': 'cp866',
    'UTF-8': 'utf-8', 'UTF8': 'utf-8',
}


def _dxf_encoding(path):
    """Кодировка DXF: UTF-8 для R2007+, иначе по $DWGCODEPAGE.

    В DXF R2007 и новее ($ACADVER >= AC1021) текст всегда в UTF-8, хотя
    $DWGCODEPAGE в заголовке может остаться «ANSI_1252» — ориентироваться
    на него в этом случае нельзя. Старые DXF (Schemagee пишет AC1015)
    используют ANSI-кодовую страницу, обычно ANSI_1251.
    """
    try:
        with open(path, 'rb') as fp:
            head = fp.read(4096)
        if head.startswith(b'\xef\xbb\xbf'):
            return 'utf-8-sig'
        text = head.decode('latin-1', errors='replace')
        version = re.search(r'\$ACADVER\s*\r?\n\s*1\s*\r?\n\s*(AC\d{4})',
                            text)
        if version and version.group(1) >= 'AC1021':
            return 'utf-8'
        match = re.search(r'\$DWGCODEPAGE\s*\r?\n\s*3\s*\r?\n\s*([^\r\n]+)',
                          text)
        if match:
            name = match.group(1).strip().upper()
            return _DXF_CODEPAGES.get(name, 'cp1251')
    except Exception as e:
        log(f"ВНИМАНИЕ: кодировка {os.path.basename(path)}: {e}")
    return 'cp1251'


def _repair_dxf_text(text):
    """Убирает «сырые» переводы строк внутри значений DXF.

    Schemagee при экспорте пишет многострочный текст (например
    «\\Pсерый ITK ...») как есть: после кода группы идёт пустая строка, а
    продолжение — отдельной строкой без кода. Для ezdxf это недопустимая
    структура («Invalid group code»), и файл целиком считается битым.
    Склеиваем такие продолжения с предыдущим значением через пробел —
    перевод строки в значении DXF недопустим.

    Возвращает (исправленный текст, число исправлений).
    """
    lines = text.splitlines()
    out = []
    fixed = 0
    i = 0
    total = len(lines)
    while i < total:
        line = lines[i]
        if _DXF_CODE_RE.match(line):
            out.append(line)
            if i + 1 < total:
                out.append(lines[i + 1])
                i += 2
            else:
                i += 1
            continue
        # Строка не похожа на код группы — это продолжение значения.
        if len(out) >= 2:
            out[-1] = (out[-1] + " " + line).strip()
            fixed += 1
        i += 1
    return "\n".join(out), fixed


def read_dxf(path):
    """Читает DXF с правильной кодировкой и починкой битых значений.

    Кодировка определяется по заголовку (`_dxf_encoding`): R2007+ — UTF-8,
    старые DXF — ANSI-страница, по умолчанию cp1251. ezdxf.readfile() для
    этого не годится: Schemagee пишет R12 (AC1009) без $DWGCODEPAGE, и
    ezdxf молча берёт cp1252 — кириллица превращается в мусор.
    """
    encoding = _dxf_encoding(path)
    with open(path, encoding=encoding, errors='replace') as fp:
        text = fp.read()
    try:
        return ezdxf.read(io.StringIO(text))
    except Exception as e:
        fixed_text, fixed = _repair_dxf_text(text)
        if not fixed:
            raise
        log(f"ВНИМАНИЕ: {os.path.basename(path)}: битый DXF ({e}); "
            f"исправлено строк: {fixed} — читаем после починки "
            f"({encoding}).")
        return ezdxf.read(io.StringIO(fixed_text))


def _dxf_has_content(path):
    """True, если DXF читается и в Model есть хотя бы одна сущность.

    Бывает, что Schemagee создаёт файл (размер > 0), но экспорт сорвался
    и геометрии в нём нет. Такой «успех» нельзя считать сохранённым
    листом — иначе схема молча пропадёт из альбома.
    """
    try:
        doc = read_dxf(path)
    except Exception as e:
        log(f"  DXF не читается: {e}")
        return False
    count = len(list(doc.modelspace()))
    if count == 0:
        log("  DXF читается, но Model пуста.")
        return False
    return True


def extract_sheet_number(filename):
    """Ищет 'листN' в имени файла. Если не находит — вернёт None."""
    m = re.search(r'лист[_\s\-]*(\d+)', filename, re.IGNORECASE)
    return int(m.group(1)) if m else None


def derive_name_from_dxf(files):
    """Имя альбома из имени DXF без хвоста '_листN'."""
    stem = os.path.splitext(os.path.basename(files[0]))[0]
    stem = re.sub(r"[_\s\-]*лист[_\s\-]*\d+$", "", stem, flags=re.IGNORECASE)
    return stem or "album"


def sort_dxf(paths):
    """Сортировка по номеру листа: лист2 раньше листа10."""
    return sorted(paths,
                  key=lambda p: (extract_sheet_number(os.path.basename(p)) or 0, p))


def set_font(doc, font_file=FONT_FILE):
    """Меняет шрифт у всех существующих стилей (TEXTSTYLE)."""
    n = 0
    for st in doc.styles:
        try:
            st.dxf.font = font_file
            n += 1
        except Exception:
            pass
    return n


def _iter_text_entities(container):
    """Текстовые сущности САМОГО контейнера (без провала в INSERT)."""
    for e in container.query('TEXT MTEXT ATTDEF ATTRIB'):
        yield e
    # ATTRIB самого INSERT (значения атрибутов) — без провала в блок
    for ins in container.query('INSERT'):
        try:
            for a in ins.attribs:
                yield a
        except Exception:
            continue


def _collect_line_segments(container):
    """Собирает отрезки линий контейнера (LINE/LWPOLYLINE/POLYLINE)."""
    segs = []
    for e in container.query('LINE LWPOLYLINE POLYLINE'):
        try:
            t = e.dxftype()
            if t == 'LINE':
                a, b = e.dxf.start, e.dxf.end
                segs.append((a.x, a.y, b.x, b.y))
            elif t == 'LWPOLYLINE':
                pts = [(p[0], p[1]) for p in e.get_points()]
                for a, b in zip(pts, pts[1:]):
                    segs.append((a[0], a[1], b[0], b[1]))
            elif t == 'POLYLINE':
                pts = [(v.dxf.location.x, v.dxf.location.y) for v in e.vertices]
                for a, b in zip(pts, pts[1:]):
                    segs.append((a[0], a[1], b[0], b[1]))
        except Exception:
            continue
    return segs


def _text_bbox(e):
    """Возвращает (x0, y0, x1, y1) или None."""
    try:
        bb = bbox.extents([e])
        if not bb.has_data:
            return None
        return (bb.extmin.x, bb.extmin.y, bb.extmax.x, bb.extmax.y)
    except Exception:
        return None


def _seg_rect_distance(seg, rect):
    """Минимальное расстояние между отрезком и прямоугольником.

    0.0 если пересекаются или касаются.
    """
    ax, ay, bx, by = seg
    x0, y0, x1, y1 = rect

    def _clip(px, py):
        # наиболее близкая точка на отрезке к прямоугольнику
        dx, dy = bx - ax, by - ay
        seg_len2 = dx * dx + dy * dy
        if seg_len2 == 0:
            t = 0.0
        else:
            t = ((px - ax) * dx + (py - ay) * dy) / seg_len2
            t = max(0.0, min(1.0, t))
        cx, cy = ax + t * dx, ay + t * dy
        ex = min(max(cx, x0), x1)
        ey = min(max(cy, y0), y1)
        # если точка внутри rect, то ex/ey == cx/cy -> 0
        if x0 <= cx <= x1 and y0 <= cy <= y1:
            return 0.0
        return ((cx - ex) ** 2 + (cy - ey) ** 2) ** 0.5

    if x0 <= ax <= x1 and y0 <= ay <= y1:
        return 0.0
    if x0 <= bx <= x1 and y0 <= by <= y1:
        return 0.0
    d1 = _clip(ax, ay)
    d2 = _clip(bx, by)
    dm1 = _clip(x0, y0)
    dm2 = _clip(x1, y0)
    dm3 = _clip(x0, y1)
    dm4 = _clip(x1, y1)
    return min(d1, d2, dm1, dm2, dm3, dm4)


def _shrink_rect_x(rect, factor, anchor_x):
    """Пробный bbox при сжатии ширины на factor относительно anchor_x."""
    x0, y0, x1, y1 = rect
    nx0 = anchor_x + (x0 - anchor_x) * factor
    nx1 = anchor_x + (x1 - anchor_x) * factor
    return (min(nx0, nx1), y0, max(nx0, nx1), y1)


def _text_anchor_x(e):
    """Точка, относительно которой DXF сжимает текст по width_factor."""
    try:
        ap = e.dxf.get('align_point', None)
        if ap is not None and (e.dxf.get('halign', 0) or e.dxf.get('valign', 0)):
            return ap.x
        if e.dxftype() == 'MTEXT':
            ip = e.dxf.insert
        else:
            ip = e.dxf.get('insert', None)
            if ip is None:
                ip = e.dxf.get('align_point', None)
        if ip is not None:
            return ip.x
    except Exception:
        pass
    return None


def compress_texts(doc, factor=TEXT_COMPRESS):
    """Сжимает текст по ширине (width_factor).

    Обходит modelspace и все блоки. Для текста на слое TABLE_LAYER,
    который вплотную подходит к линиям блока, подбирается минимальный
    достаточный коэффициент из TABLE_COMPRESS_STEPS.
    Возвращает (число изменённых, словарь распределения).
    """
    seen = set()
    changed = 0
    stats = {}

    def _choose_table_factor(e, vsegs):
        """Коэффициент, при котором текст перестаёт пересекать вертикали."""
        rect = _text_bbox(e)
        if rect is None or not vsegs:
            return factor
        height = rect[3] - rect[1]
        if height <= 0:
            return factor
        gap = TABLE_CHAR_GAP * height
        d0 = min((_seg_rect_distance(s, rect) for s in vsegs), default=1e18)
        if d0 >= gap:
            return factor  # не пересекает вертикаль

        ax = _text_anchor_x(e)
        if ax is None:
            ax = (rect[0] + rect[2]) / 2.0
        best = TABLE_COMPRESS_STEPS[-1]
        for f in TABLE_COMPRESS_STEPS:
            trial = _shrink_rect_x(rect, f, ax)
            d = min((_seg_rect_distance(s, trial) for s in vsegs), default=1e18)
            if d >= gap:
                return f
        return best  # даже 0.35 не хватило — оставляем минимум

    def _process(container, segs):
        nonlocal changed
        vsegs = [s for s in segs if abs(s[2] - s[0]) < 1e-6
                 and abs(s[3] - s[1]) > 1e-9]
        for e in _iter_text_entities(container):
            if id(e) in seen:
                continue
            seen.add(id(e))
            try:
                if e.dxf.get('layer', '0') == TABLE_LAYER:
                    f = _choose_table_factor(e, vsegs)
                else:
                    f = factor
                w = e.dxf.get('width', 1.0) or 1.0
                e.dxf.width = w * f
                changed += 1
                stats[round(f, 3)] = stats.get(round(f, 3), 0) + 1
            except Exception:
                pass

    msp = doc.modelspace()
    _process(msp, _collect_line_segments(msp))
    for block in doc.blocks:
        _process(block, _collect_line_segments(block))

    return changed, stats


def create_layout_for(master, layout_name, item,
                      paper_w=PAPER_W, paper_h=PAPER_H):
    """Создаёт Layout с viewport, показывающим одну схему из модели."""
    try:
        layout = master.layouts.new(layout_name)
    except Exception as e:
        log(f"ВНИМАНИЕ: Layout '{layout_name}' не создан: {e}")
        return None

    # Формат листа задаётся параметрами (A2 по умолчанию, A3/A4 — по выбору)
    layout.page_setup(
        size=(paper_w, paper_h),
        margins=(MARGIN, MARGIN, MARGIN, MARGIN),
        units='mm',
    )

    x0, y0, x1, y1 = item['x0'], item['y0'], item['x1'], item['y1']
    w = x1 - x0
    h = y1 - y0

    usable_w = paper_w - 2 * MARGIN
    usable_h = paper_h - 2 * MARGIN

    # Масштаб: во сколько раз уменьшить модель, чтобы схема влезла в лист
    scale = min(usable_w / w, usable_h / h)

    view_center = ((x0 + x1) / 2.0, (y0 + y1) / 2.0)
    vp_center = (paper_w / 2.0, paper_h / 2.0)
    vp_size = (usable_w, usable_h)
    view_height = usable_h / scale    # видимая высота модели в единицах чертежа

    layout.add_viewport(
        center=vp_center,
        size=vp_size,
        view_center_point=view_center,
        view_height=view_height,
    )
    return layout


def _delete_extra_layouts(doc, keep_prefix='Лист'):
    """Удаляет дефолтные пустые layout'ы (Layout1 и т.п.).

    ezdxf не даёт удалить последний paperspace layout, поэтому вызывать
    можно только ПОСЛЕ создания листов альбома.
    Возвращает список удалённых имён.
    """
    removed = []
    for lyt in list(doc.layouts):
        if lyt.is_modelspace:
            continue
        lname = lyt.name  # имя нужно запомнить ДО удаления
        if lname.startswith(keep_prefix):
            continue
        try:
            doc.layouts.delete(lname)
            removed.append(lname)
        except Exception as e:
            log(f"ВНИМАНИЕ: Layout '{lname}' не удалён: {e}")
    return removed


def export_dwg(dxf_path, dwg_path, keep_dxf=False, attempts=3, delay=5.0):
    """Конвертирует собранный DXF в итоговый DWG, возвращает путь к DWG.

    Если конвертера нет (ни ODA File Converter, ни AutoCAD) — DXF остаётся,
    результат сборки не теряется, а в лог уходит понятная ошибка.
    """
    path = None
    for attempt in range(1, attempts + 1):
        try:
            path = dxf_to_dwg_convert(dxf_path, dwg_path)
            break
        except NoBackendError as e:
            first = str(e).splitlines()[0] if str(e) else 'нет конвертера'
            log(f"ОШИБКА: DXF->DWG не выполнен: {first}. Оставлен {dxf_path}")
            return None
        except Exception as e:
            log(f"ВНИМАНИЕ: DXF->DWG, попытка {attempt}/{attempts}: {e}")
            if attempt < attempts:
                time.sleep(delay)
            else:
                log(f"ОШИБКА: DXF->DWG не выполнен: {e}. Оставлен {dxf_path}")
                return None

    if not path:
        return None

    log(f"Записан {path} ({os.path.getsize(path)} байт)")
    if not keep_dxf:
        try:
            os.remove(dxf_path)
            log(f"Промежуточный DXF удалён: {dxf_path}")
        except OSError as e:
            log(f"ВНИМАНИЕ: не удалось удалить {dxf_path}: {e}")
    return path


def merge_all(input_dir, output_dir, name=None, make_dwg=True, keep_dxf=False,
              delete_sources=False, paper_w=PAPER_W, paper_h=PAPER_H):
    """Собирает альбом из DXF: схемы в ряд в модели + layout на каждый лист.

    input_dir      — папка с DXF (имя файла содержит 'листN');
    output_dir     — папка результата (<ИМЯ>.dwg, placed.json);
    name           — имя итогового файла без расширения;
    delete_sources — удалить входные DXF листов после успешной сборки DWG;
    paper_w/h      — размер листа в мм (A2 по умолчанию, A3/A4 — по выбору).
    """
    os.makedirs(output_dir, exist_ok=True)

    files = [
        os.path.join(input_dir, f)
        for f in os.listdir(input_dir)
        if f.lower().endswith('.dxf')
    ]
    # Сортируем по номеру листа (лист2 раньше листа10), а не по строке.
    files.sort(key=lambda p: (extract_sheet_number(os.path.basename(p)) or 0, p))

    if name is None:
        name = derive_name_from_dxf(files) if files else "album"
    # Срезаем только реальное расширение: в имени бывает точка (122.270_v1).
    if name.lower().endswith(('.dxf', '.dwg')):
        name = os.path.splitext(name)[0]

    output_dxf = os.path.join(output_dir, name + '.dxf')
    output_dwg = os.path.join(output_dir, name + '.dwg')
    output_json = os.path.join(output_dir, 'placed.json')

    # На всякий случай не берём в сборку собственный результат прошлого запуска.
    files = [p for p in files
             if os.path.normcase(p) != os.path.normcase(output_dxf)]

    log(f"Найдено DXF: {len(files)}")
    if not files:
        raise RuntimeError(f"В папке нет DXF-файлов: {input_dir}")

    master = ezdxf.new('R2018', setup=True)
    log(f"Формат листа: {paper_w:g}×{paper_h:g} мм")

    # Дефолтный Layout1 удалить СРАЗУ нельзя: ezdxf запрещает удалять последний
    # paperspace-layout. Поэтому чистим дефолтные листы после создания
    # листов альбома (см. _delete_extra_layouts ниже).

    placed = []
    errors = []
    x_cursor = 0.0

    # --- Этап 1: сборка всех схем в ряд в Model ---
    for i, path in enumerate(files, 1):
        file_name = os.path.basename(path)
        try:
            src = read_dxf(path)
        except Exception as e:
            log(f"ОШИБКА: [{i}] {file_name}: не читается: {e}")
            errors.append((file_name, str(e)))
            continue

        n_font = set_font(src)
        log(f"[{i}] {file_name}: шрифт {FONT_FILE} установлен "
            f"для {n_font} стилей")
        n_txt, txt_stats = compress_texts(src)
        log(f"[{i}] {file_name}: сжато текстовых сущностей: {n_txt}; "
            f"по коэффициентам: {dict(sorted(txt_stats.items()))}")

        src_msp = src.modelspace()
        ext = bbox.extents(src_msp)
        if not ext.has_data:
            log(f"ВНИМАНИЕ: [{i}] {file_name}: пустой Modelspace, пропуск")
            continue

        w, h = ext.size.x, ext.size.y

        dx = x_cursor - ext.extmin.x
        dy = -ext.extmin.y
        transform = Matrix44.translate(dx, dy, 0)
        for entity in src_msp:
            try:
                entity.transform(transform)
            except Exception:
                pass

        try:
            xref.load_modelspace(
                src,
                master,
                conflict_policy=ConflictPolicy.XREF_PREFIX,
            )
        except Exception as e:
            log(f"ОШИБКА: [{i}] {file_name}: ошибка импорта: {e}")
            errors.append((file_name, str(e)))
            continue

        item = {
            'file': file_name,
            'x0': x_cursor, 'y0': 0.0,
            'x1': x_cursor + w, 'y1': h,
            'width': w, 'height': h,
        }
        placed.append(item)
        log(f"[{i}/{len(files)}] OK {file_name}: {w:.1f}x{h:.1f} "
            f"@ X={x_cursor:.1f}")
        x_cursor += w + GAP

    # --- Этап 2: создаём Layouts для каждого листа ---
    log(f"Создаю {len(placed)} layouts…")
    for idx, item in enumerate(placed, start=1):
        file_name = item['file']
        sn = extract_sheet_number(file_name)
        if sn is None:
            sn = idx
        layout_name = f"Лист {sn}"
        layout = create_layout_for(master, layout_name, item,
                                   paper_w=paper_w, paper_h=paper_h)
        if layout is not None:
            log(f"  '{layout_name}'  ←  {file_name}")
        item['layout'] = layout_name

    # Чистим дефолтные листы (Layout1) — теперь, когда листы альбома уже есть.
    removed_layouts = _delete_extra_layouts(master)
    if removed_layouts:
        log(f"Удалены дефолтные layout'ы: {', '.join(removed_layouts)}")

    try:
        master.saveas(output_dxf)
    except OSError as e:
        raise RuntimeError(
            f"Не удалось записать {output_dxf}: {e}. "
            "Скорее всего этот файл открыт в AutoCAD — закройте его и повторите."
        ) from e

    with open(output_json, 'w', encoding='utf-8') as f:
        json.dump(placed, f, ensure_ascii=False, indent=2)

    # --- Этап 3: итоговый файл DWG ---
    dwg = export_dwg(output_dxf, output_dwg, keep_dxf=keep_dxf) \
        if make_dwg else None
    if not make_dwg:
        log(f"DWG не запрашивался, оставлен DXF: {output_dxf}")

    if dwg and delete_sources:
        removed = 0
        for path in files:
            try:
                os.remove(path)
                removed += 1
            except OSError as e:
                log(f"ВНИМАНИЕ: не удалось удалить {path}: {e}")
        log(f"Удалено исходных DXF: {removed} из {len(files)}")

    result = dwg or output_dxf
    log(f"Готово: схем {len(placed)}, layouts {len(placed)}, "
        f"ошибок {len(errors)}, файл {result} ({os.path.getsize(result)} байт)")
    return {'placed': placed, 'errors': errors, 'file': result, 'name': name}


# =============================================================================
# БЛОК 4. СЦЕНАРИЙ: кликер + конвертер + отчёт
# =============================================================================

def make_run_folder(name):
    """Создаёт папку запуска: <exe_dir>\\<ИМЯ>_<ГГГГ-ММ-ДД-ЧЧ-ММ-СС>."""
    stamp = datetime.datetime.now().strftime("%Y-%m-%d-%H-%M-%S")
    folder = os.path.join(BASE_DIR, f"{name}_{stamp}")
    os.makedirs(folder, exist_ok=True)
    return folder


def write_result_json(run_dir, payload):
    """Кладёт result.json в папку результата (машиночитаемый отчёт)."""
    path = os.path.join(run_dir, "result.json")
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        log(f"Результат записан: {path}")
    except Exception as e:
        log(f"Не удалось записать result.json: {e}")


def run_pipeline(file_path, manual_count, emit, delete_dxf=True,
                 paper_w=PAPER_W, paper_h=PAPER_H):
    """Полный цикл: открыть .sdd, сохранить листы, собрать DWG, отдать отчёт."""
    check_stop()
    _OWNED_SCHEMAGEE["opened"] = False
    source = os.path.normpath(file_path)
    name = os.path.splitext(os.path.basename(source))[0]

    # Работать с уже открытым Schemagee нельзя: скрипт начнёт сохранять
    # листы чужого документа. Лучше сразу остановиться и сказать об этом.
    already = schemagee_windows()
    if already:
        titles = "; ".join(t for _, t in already if t) or "без названия"
        raise RuntimeError(
            "Schemagee уже запущен.\n\n"
            "Закройте его окно и повторите запуск — иначе программа будет "
            "работать с чужим документом.\n\n"
            f"Открытые окна: {titles}"
        )

    log("=" * 70)
    log(f"{APP_NAME} {APP_VERSION}: {source}")
    if manual_count:
        log(f"Количество листов: вручную ({manual_count})")
    else:
        log("Количество листов: автоматически (диалог печати)")
    log("=" * 70)

    if is_elevated():
        log("ВНИМАНИЕ: программа запущена от администратора. Если Schemagee "
            "запущен без прав администратора, Windows может блокировать "
            "нажатия клавиш (UIPI).")
    check_stop()

    emit("progress", "Открываем файл в Schemagee…")
    t_open = time.time()
    if not open_file_in_schemagee(source):
        raise RuntimeError(
            "Не удалось открыть файл в Schemagee.\n\n"
            "Проверьте, что .sdd открывается двойным кликом и не занят "
            "другой программой."
        )

    hwnd, title = wait_schemagee_window(timeout=30.0)
    if not hwnd:
        raise RuntimeError(
            "Окно Schemagee не появилось за 30 секунд.\n\n"
            "Проверьте, что файл .sdd открывается и Schemagee не заблокирован."
        )
    log(f"Окно Schemagee: {title!r} "
        f"(появилось за {format_time(time.time() - t_open)} сек)")
    _OWNED_SCHEMAGEE["opened"] = True
    wait_window_ready(hwnd, timeout=10.0)
    t_ready = time.time()
    if wait_schemagee_ready(hwnd, timeout=60.0):
        log(f"Schemagee готов к работе "
            f"(через {format_time(time.time() - t_ready)} сек после окна).")
    else:
        log("ВНИМАНИЕ: Schemagee не подтвердил готовность за 60 с — "
            "продолжаем, но первая команда может потеряться.")
    ensure_foreground(hwnd, what="Schemagee")
    log(f"Активное окно: {foreground_title()}")
    switch_to_english(hwnd)
    pause(SHEET_OPEN_PAUSE)   # даём программе дорисовать лист

    if manual_count:
        sheet_count = manual_count
        count_source = "вручную"
    else:
        emit("progress", "Определяем число листов…")
        sheet_count = read_sheet_count_auto()
        count_source = "диалог печати"
        stamps = read_sheet_count_from_sdd(source)
        if sheet_count is None:
            sheet_count = stamps
            count_source = "штампы .sdd"
        elif stamps is not None and stamps != sheet_count:
            # В диалоге печати поле «по:» — число СТРАНИЦ печати: большая
            # схема может печататься на нескольких страницах (например,
            # 7 листов × 6 страниц = 42). Листов в документе столько,
            # сколько штампов «Формат А…».
            log(f"ВНИМАНИЕ: диалог печати даёт {sheet_count} страниц, "
                f"штампов в .sdd — {stamps}; берём штампы.")
            sheet_count = stamps
            count_source = "штампы .sdd"
        if sheet_count is None:
            raise RuntimeError(
                "Не удалось определить число листов автоматически.\n\n"
                "Диалог печати не открылся или в буфере обмена не оказалось "
                "целого числа 1..500, а штампы «Формат А…» в .sdd не "
                "найдены — работать с файлом нельзя, иначе часть листов "
                "потеряется.\n\n"
                "Включите «Выбрать настройки вручную», укажите количество "
                "листов и повторите запуск."
            )

    log(f"Число листов: {sheet_count} (источник: {count_source})")
    check_stop()

    run_dir = make_run_folder(name)
    attach_run_log(os.path.join(run_dir, LOG_FILENAME))
    log(f"Папка результата: {run_dir}")

    t_conv = time.time()
    try:
        produced, missing = algorithm_conversion(
            source, sheet_count, run_dir, emit=emit
        )
    finally:
        conv_time = time.time() - t_conv

    log(f"Сохранено DXF: {len(produced)} из {sheet_count}.")

    try:
        close_schemagee()
    except Exception as e:
        log(f"Не удалось закрыть Schemagee: {e}")

    if not produced:
        raise RuntimeError(
            f"Ни один лист не сохранён (0 из {sheet_count}).\n\n"
            f"Подробности: {os.path.join(run_dir, LOG_FILENAME)}"
        )
    check_stop()

    emit("progress", "Собираем альбом и конвертируем в DWG…")
    t_merge = time.time()
    merge = merge_all(input_dir=run_dir, output_dir=run_dir, name=name,
                      delete_sources=delete_dxf,
                      paper_w=paper_w, paper_h=paper_h)
    merge_time = time.time() - t_merge

    target = merge["file"]
    placed = len(merge["placed"])
    errors = len(merge["errors"])

    notes = []
    error_files = [n for n, _ in merge["errors"]]
    if error_files:
        notes.append("Не прочитаны DXF: " + ", ".join(error_files)
                     + " — проверьте исходные файлы.")
    if missing:
        notes.append("Не сохранены листы: "
                     + ", ".join(str(n) for n in missing)
                     + " — проверьте их вручную.")
    if len(produced) < sheet_count:
        notes.append(f"Сохранено {len(produced)} из {sheet_count} листов.")
    if placed < len(produced):
        notes.append(f"В альбом вошло {placed} схем из {len(produced)}.")
    if target.lower().endswith(".dxf"):
        notes.append("DWG не создан (нет ODA File Converter / AutoCAD) — "
                     "оставлен DXF.")

    stats = {
        "source": source,
        "name": name,
        "sheet_count": sheet_count,
        "sheet_count_source": count_source,
        "conv_time": conv_time,
        "merge_time": merge_time,
        "produced": len(produced),
        "missing": missing,
        "placed": placed,
        "errors": errors,
        "error_files": error_files,
        "target": target,
        "target_size": os.path.getsize(target) if os.path.isfile(target) else 0,
        "out_dir": run_dir,
        "notes": notes,
        "version": APP_VERSION,
    }
    write_result_json(run_dir, stats)
    emit("success", stats)
    return stats


def print_report(stats):
    """Текстовый отчёт для режима --headless."""
    log("=" * 62)
    log("ГОТОВО")
    log(f"  Исходный файл : {os.path.basename(stats['source'])}")
    log(f"  Листов        : {stats['sheet_count']} "
        f"({stats['sheet_count_source']})")
    log(f"  Экспорт       : {format_duration(stats.get('conv_time'))}")
    log(f"  Сборка        : {format_duration(stats.get('merge_time'))}")
    log(f"  Схем          : {stats['placed']}")
    log(f"  Ошибок        : {stats['errors']}")
    log(f"  Результат     : {stats['target']} ({stats['target_size']} байт)")
    for note in stats.get("notes") or []:
        log(f"  ! {note}")
    log("=" * 62)


def run_headless(opts):
    """Проверка конвертера без Schemagee: собрать альбом из готовых DXF."""
    folder = opts.get("dxf_folder")
    if not folder:
        log("ОШИБКА: --headless требует --dxf-folder ПАПКА "
            "(готовые DXF для сборки).")
        return 1
    if not os.path.isdir(folder):
        log(f"ОШИБКА: папка не найдена: {folder}")
        return 1

    files = sort_dxf(glob.glob(os.path.join(folder, "*.dxf")))
    if not files:
        log(f"ОШИБКА: в папке нет DXF: {folder}")
        return 1

    name = opts.get("name") or derive_name_from_dxf(files)
    run_dir = make_run_folder(name)
    attach_run_log(os.path.join(run_dir, LOG_FILENAME))
    log(f"Режим без кликера: {len(files)} DXF из {folder}")
    log(f"Папка результата: {run_dir}")

    try:
        merge = merge_all(folder, run_dir, name=name)
    except Exception as e:
        log(f"ОШИБКА: {e}")
        log(traceback.format_exc())
        return 1

    error_files = [n for n, _ in merge["errors"]]
    notes = []
    if error_files:
        notes.append("Не прочитаны DXF: " + ", ".join(error_files) + ".")
    if len(merge["placed"]) < len(files):
        notes.append(f"В альбом вошло {len(merge['placed'])} схем "
                     f"из {len(files)} файлов.")

    stats = {
        "source": files[0],
        "name": merge["name"],
        "sheet_count": len(files),
        "sheet_count_source": "готовые DXF",
        "conv_time": None,
        "merge_time": None,
        "produced": len(files),
        "missing": [],
        "placed": len(merge["placed"]),
        "errors": len(merge["errors"]),
        "error_files": error_files,
        "target": merge["file"],
        "target_size": os.path.getsize(merge["file"])
        if os.path.isfile(merge["file"]) else 0,
        "out_dir": run_dir,
        "notes": notes,
        "version": APP_VERSION,
    }
    write_result_json(run_dir, stats)
    print_report(stats)
    return 0 if stats["placed"] else 1


# =============================================================================
# БЛОК 5. ОКНА (главное, прогресс, успех, ошибка, остановка)
# =============================================================================

def run_gui():
    """Главное окно. Возвращает код выхода: 0 — успех, 1 — ошибка, 2 — стоп."""
    result = {"code": 0}

    root = TkinterDnD.Tk() if HAS_DND else tk.Tk()
    root.title(APP_FULL_TITLE)
    root.resizable(False, False)
    root.configure(bg=WIN11_BG)
    apply_icon(root)
    initial_ttk_theme = ttk.Style().theme_use()
    _center(root, 733, 734)

    def _report_callback_exception(exc_type, exc_value, exc_tb):
        log("Ошибка в обработчике интерфейса:\n"
            + "".join(traceback.format_exception(exc_type, exc_value, exc_tb)))

    root.report_callback_exception = _report_callback_exception

    card = _card(root, 640, 620)
    ui_queue = queue.Queue()

    def emit(kind, payload=None):
        ui_queue.put((kind, payload))

    # Кликер через этот хук может спрятать окно прогресса, если оно
    # перехватило фокус у Schemagee.
    _UI_SIGNAL["emit"] = emit

    def close_app(code=0):
        result["code"] = code
        try:
            keyboard.clear_all_hotkeys()
        except Exception:
            pass
        try:
            root.destroy()
        except Exception:
            pass

    manual_var = tk.BooleanVar(value=False)
    count_var = tk.StringVar(value="1")
    hint_var = tk.StringVar(value="")
    delete_var = tk.BooleanVar(value=True)   # удалять DXF после конвертации
    report_var = tk.BooleanVar(value=True)   # показывать отчёт по завершении
    format_var = tk.BooleanVar(value=False)  # выбрать формат листа вручную
    format_value = tk.StringVar(value="A3")  # формат при ручном выборе
    orient_value = tk.StringVar(value="горизонтальный")

    def on_toggle():
        if manual_var.get():
            spin.config(state="normal")
            spin.focus_set()
            try:
                spin.selection_range(0, "end")
            except Exception:
                pass
        else:
            spin.config(state="disabled")
        hint_var.set("")

    tk.Label(card, text=APP_INNER_TEXT, font=FONT_TITLE,
             bg=WIN11_CARD_BG, fg=WIN11_TEXT).pack(pady=(43, 2))
    tk.Label(card, text="Сохраняет каждый лист в DXF и собирает альбом в DWG",
             font=FONT_BODY, bg=WIN11_CARD_BG,
             fg=WIN11_SUBTEXT).pack(pady=(0, 18))

    def read_manual_count():
        """(ok, значение): ok=False — в поле некорректное число."""
        if not manual_var.get():
            return True, None
        raw = count_var.get().strip()
        if not re.fullmatch(r"\d{1,3}", raw) or not (1 <= int(raw) <= 500):
            hint_var.set("Укажите число листов числом от 1 до 500.")
            return False, None
        hint_var.set("")
        return True, int(raw)

    def on_select():
        # Стартуем всегда с локальной папки (Рабочий стол): если открывать
        # диалог на сетевом пути, Windows ждёт ответа сервера и окно висит.
        initial = choose_start_dir()
        log(f"Диалог выбора файлов: стартовая папка {initial}")

        paths = filedialog.askopenfilenames(
            parent=root,
            title="Выберите файлы Schemagee",
            filetypes=[("Schemagee", "*.sdd"), ("Все файлы", "*.*")],
            initialdir=initial,
        )
        if paths:
            add_files(paths)

    def on_drop(event):
        try:
            paths = root.tk.splitlist(event.data)
        except Exception:
            paths = [event.data]
        files = [str(p) for p in paths if str(p).lower().endswith(".sdd")]
        if not files:
            log("Перетаскивание: файлов .sdd не найдено.")
            hint_var.set("Перетащите файлы Schemagee (.sdd).")
            return
        add_files(files)

    DROP_BG = "#F5F5F5"
    DROP_BORDER = "#D0D0D0"
    AREA_BG = "#FFFFFF"

    files_area = tk.Frame(card, bg=DROP_BG, width=410, height=200,
                          highlightthickness=1,
                          highlightbackground=DROP_BORDER)
    files_area.pack(padx=115, pady=(0, 0))
    files_area.pack_propagate(False)

    drop_placeholder = tk.Label(
        files_area,
        text=("Перенесите сюда файлы .sdd" if HAS_DND
              else "Drag & drop недоступен"),
        font=FONT_BODY, bg=DROP_BG, fg=WIN11_SUBTEXT, justify="center")
    drop_placeholder.pack(expand=True)

    files_canvas = tk.Canvas(files_area, bg=AREA_BG, height=198,
                             highlightthickness=0, bd=0)
    files_scroll = ttk.Scrollbar(files_area, orient="vertical",
                                 command=files_canvas.yview)
    files_canvas.configure(yscrollcommand=files_scroll.set)
    files_box = tk.Frame(files_canvas, bg=AREA_BG)
    files_canvas.create_window((0, 0), window=files_box, anchor="nw",
                               width=386)
    files_box.bind("<Configure>", lambda e: files_canvas.configure(
        scrollregion=files_canvas.bbox("all")))

    def scroll_files(event):
        bbox = files_canvas.bbox("all")
        if not bbox:
            return "break"
        if (bbox[3] - bbox[1]) <= files_canvas.winfo_height():
            return "break"
        try:
            delta = int(-1 * (event.delta / 120))
        except Exception:
            delta = -1 if event.delta > 0 else 1
        files_canvas.yview_scroll(delta, "units")
        return "break"

    for widget in (files_area, drop_placeholder, files_canvas, files_box,
                   files_scroll):
        widget.bind("<MouseWheel>", scroll_files)

    if HAS_DND:
        for widget in (files_area, drop_placeholder, files_canvas, files_box):
            widget.drop_target_register(DND_FILES)
            widget.dnd_bind("<<Drop>>", on_drop)

    pending = []
    theme_state = {"dark": False}

    LIGHT_TO_DARK = {
        "#F3F3F3": "#2B2B2B",   # фон окна
        "#FFFFFF": "#333333",   # карточка, список, зона
        "#1A1A1A": "#E6E6E6",   # основной текст
        "#5C5C5C": "#A8A8A8",   # вторичный текст
        "#C42B1C": "#D9807A",   # предупреждение
        "#0067C0": "#3B7EA1",   # акцент
        "#1A7DD8": "#4A90B8",   # акцент (наведение)
        "#E0E0E0": "#454545",   # границы
        "#E5E5E5": "#3A3A3A",   # вторичная кнопка
        "#D6D6D6": "#4A4A4A",   # вторичная кнопка (наведение)
        "#107C10": "#7FB37F",   # успех
        "#F5F5F5": "#2F2F2F",   # зона перетаскивания
        "#D0D0D0": "#4A4A4A",   # рамка зоны
    }
    DARK_TO_LIGHT = {v: k for k, v in LIGHT_TO_DARK.items()}

    def area_colors():
        if theme_state["dark"]:
            return "#2F2F2F", "#333333"
        return DROP_BG, AREA_BG

    def palette():
        if theme_state["dark"]:
            return {"accent": "#3B7EA1", "accent_h": "#4A90B8",
                    "border": "#454545", "subtext": "#A8A8A8"}
        return {"accent": WIN11_ACCENT, "accent_h": WIN11_ACCENT_H,
                "border": WIN11_BORDER, "subtext": WIN11_SUBTEXT}

    def paint_convert_button(active):
        colors = palette()
        if active:
            convert_btn.config(state="normal", bg=colors["accent"],
                               fg="#FFFFFF",
                               activebackground=colors["accent_h"],
                               activeforeground="#FFFFFF")
            convert_btn.bind("<Enter>",
                             lambda e: convert_btn.config(
                                 bg=colors["accent_h"]))
            convert_btn.bind("<Leave>",
                             lambda e: convert_btn.config(bg=colors["accent"]))
        else:
            convert_btn.config(state="disabled", bg=colors["border"],
                               fg=colors["subtext"],
                               activebackground=colors["border"])
            convert_btn.unbind("<Enter>")
            convert_btn.unbind("<Leave>")

    def render_pending():
        for child in files_box.winfo_children():
            child.destroy()
        drop_bg, area_bg = area_colors()
        text_color = "#E6E6E6" if theme_state["dark"] else WIN11_TEXT
        danger = "#D9807A" if theme_state["dark"] else WIN11_DANGER
        if not pending:
            files_canvas.pack_forget()
            files_scroll.pack_forget()
            files_area.configure(bg=drop_bg)
            drop_placeholder.configure(bg=drop_bg)
            drop_placeholder.pack(expand=True)
            return
        drop_placeholder.pack_forget()
        files_area.configure(bg=area_bg)
        files_canvas.configure(bg=area_bg)
        files_box.configure(bg=area_bg)
        files_canvas.pack(side="left", fill="both", expand=True)
        for path in pending:
            row = tk.Frame(files_box, bg=area_bg)
            row.pack(fill="x")
            tk.Button(row, text="×", command=lambda p=path: remove_file(p),
                      font=FONT_BODY_SM, bg=area_bg, fg=danger,
                      activebackground=area_bg, activeforeground=danger,
                      relief="flat", bd=0, highlightthickness=0,
                      cursor="hand2", width=2).pack(side="left")
            tk.Label(row, text=os.path.basename(path), font=FONT_BODY_SM,
                     bg=area_bg, fg=text_color,
                     anchor="w").pack(side="left", fill="x", expand=True)
            row.bind("<MouseWheel>", scroll_files)
            for widget in row.winfo_children():
                widget.bind("<MouseWheel>", scroll_files)

        files_box.update_idletasks()
        if files_box.winfo_reqheight() > files_canvas.winfo_height():
            files_scroll.pack(side="right", fill="y")
        else:
            files_scroll.pack_forget()
            files_canvas.yview_moveto(0)

    def remove_file(path):
        if path in pending:
            pending.remove(path)
            log(f"Удалён из очереди: {path}")
            refresh_pending()

    def refresh_pending():
        render_pending()
        paint_convert_button(bool(pending))

    def add_files(paths):
        added = 0
        for path in paths:
            path = os.path.normpath(str(path))
            if not path.lower().endswith(".sdd") or not os.path.isfile(path):
                continue
            if path not in pending:
                pending.append(path)
                added += 1
        if added:
            remember_dir(pending[-1])
            log(f"Добавлено файлов: {added}, всего {len(pending)}")
        refresh_pending()

    def on_convert():
        ok, manual_count = read_manual_count()
        if not ok or not pending:
            return
        paths = list(pending)
        pending.clear()
        refresh_pending()
        start_work(paths, manual_count)

    buttons = tk.Frame(card, bg=WIN11_CARD_BG)
    buttons.pack(pady=(20, 0))
    select_btn = _button(buttons, "Выбрать файлы", on_select)
    select_btn.config(width=14)
    select_btn.pack(side="left", padx=(0, 10))
    convert_btn = _button(buttons, "Конвертировать", on_convert)
    convert_btn.config(width=14)
    convert_btn.pack(side="left")
    refresh_pending()

    def on_toggle_format():
        state = "readonly" if format_var.get() else "disabled"
        format_combo.config(state=state)
        orient_combo.config(state=state)

    def paper_dimensions():
        if not format_var.get():
            return PAPER_W, PAPER_H
        fmt = format_value.get() or PAPER_FORMAT_DEFAULT
        landscape = orient_value.get() != "вертикальный"
        return paper_size(fmt, landscape)

    def make_check(parent, text, variable, command=None):
        return tk.Checkbutton(parent, text=text, variable=variable,
                              command=command, font=FONT_BODY_SM,
                              bg=WIN11_CARD_BG, fg=WIN11_TEXT,
                              activebackground=WIN11_CARD_BG,
                              activeforeground=WIN11_TEXT,
                              selectcolor=WIN11_CARD_BG, bd=0,
                              highlightthickness=0, cursor="hand2")

    opts = tk.Frame(card, bg=WIN11_CARD_BG)
    opts.pack(anchor="w", padx=140, pady=(16, 0))

    manual_row = tk.Frame(opts, bg=WIN11_CARD_BG)
    manual_row.pack(anchor="w")
    make_check(manual_row, "Указать количество листов вручную", manual_var,
               on_toggle).pack(side="left")
    spin = tk.Spinbox(manual_row, from_=1, to=500, width=4,
                      font=FONT_BODY_SM, textvariable=count_var,
                      state="disabled", justify="center", relief="flat",
                      bd=0, highlightthickness=1,
                      highlightbackground=WIN11_BORDER,
                      highlightcolor=WIN11_BORDER,
                      disabledbackground="#F0F0F0",
                      disabledforeground="#A0A0A0")
    spin.pack(side="left", padx=(12, 0))

    make_check(opts, "Удалять .dxf файлы после конвертации",
               delete_var).pack(anchor="w", pady=(2, 0))
    make_check(opts, "Выводить отчёт по завершении",
               report_var).pack(anchor="w", pady=(2, 0))

    format_row = tk.Frame(opts, bg=WIN11_CARD_BG)
    format_row.pack(anchor="w", pady=(2, 0))
    make_check(format_row, "Выбрать формат листа вручную", format_var,
               on_toggle_format).pack(side="left")
    format_combo = ttk.Combobox(format_row, values=list(PAPER_FORMATS),
                                state="disabled", width=4,
                                textvariable=format_value)
    format_combo.pack(side="left", padx=(10, 0))
    orient_combo = ttk.Combobox(format_row,
                                values=["горизонтальный", "вертикальный"],
                                state="disabled", width=15,
                                textvariable=orient_value)
    orient_combo.pack(side="left", padx=(6, 0))

    def style_dropdown(event):
        """Фон выпадающего списка — по текущей теме (light/dark)."""
        combo = event.widget
        if theme_state["dark"]:
            bg, fg = "#333333", "#E6E6E6"
            sel_bg, sel_fg = "#3B7EA1", "#FFFFFF"
        else:
            bg, fg = "#FFFFFF", "#1A1A1A"
            sel_bg, sel_fg = "#0067C0", "#FFFFFF"
        try:
            popdown = combo.tk.call("ttk::combobox::PopdownWindow",
                                    str(combo))
            listbox = f"{popdown}.f.l"
            combo.tk.call(listbox, "configure",
                          "-background", bg, "-foreground", fg,
                          "-selectbackground", sel_bg,
                          "-selectforeground", sel_fg)
        except Exception as e:
            log(f"Оформление списка: {e}")

    format_combo.bind("<<ComboboxDropdown>>", style_dropdown)
    orient_combo.bind("<<ComboboxDropdown>>", style_dropdown)

    red_top = tk.Label(
        card, text="НЕ ИСПОЛЬЗУЙТЕ КОМПЬЮТЕР ДО ОКОНЧАНИЯ КОНВЕРТАЦИИ",
        font=FONT_WARN, bg=WIN11_CARD_BG, fg=WIN11_DANGER, justify="center")
    red_top.pack(pady=(14, 0))
    tk.Label(card, text="ДЛЯ АВАРИЙНОЙ ОСТАНОВКИ НАЖМИТЕ CTRL+ALT+0",
             font=FONT_WARN, bg=WIN11_CARD_BG, fg=WIN11_DANGER,
             justify="center").pack()

    hint_label = tk.Label(card, textvariable=hint_var, font=FONT_BODY_SM,
                          bg=WIN11_CARD_BG, fg=WIN11_DANGER)

    def sync_hint(*_):
        if hint_var.get().strip():
            hint_label.pack(pady=(2, 0), before=red_top)
        else:
            hint_label.pack_forget()

    hint_var.trace_add("write", sync_hint)

    def apply_theme_tree(widget, dark):
        """Перекрашивает поддерево виджетов под светлую/тёмную палитру."""
        mapping = LIGHT_TO_DARK if dark else DARK_TO_LIGHT

        def walk(w):
            for option in ("bg", "fg", "activebackground",
                           "activeforeground", "selectcolor",
                           "highlightbackground", "highlightcolor"):
                try:
                    current = str(w.cget(option))
                except Exception:
                    continue
                new = mapping.get(current)
                if not new:
                    continue
                try:
                    w.configure(**{option: new})
                except Exception:
                    pass
            for child in w.winfo_children():
                walk(child)

        walk(widget)

        colors = palette()

        def fix_buttons(w):
            for child in w.winfo_children():
                if isinstance(child, tk.Button):
                    bg = str(child.cget("bg"))
                    if bg in ("#0067C0", "#1A7DD8", "#3B7EA1", "#4A90B8"):
                        child.configure(fg="#FFFFFF",
                                        activeforeground="#FFFFFF")
                    elif bg in ("#E0E0E0", "#E5E5E5", "#D6D6D6",
                                "#454545", "#3A3A3A", "#4A4A4A"):
                        child.configure(fg=colors["subtext"],
                                        activeforeground=colors["subtext"])
                fix_buttons(child)

        fix_buttons(widget)

    def set_theme(dark):
        """Мягкая тёмная/светлая тема: перекрашивает всё окно по палитре."""
        theme_state["dark"] = dark
        apply_theme_tree(root, dark)

        colors = palette()
        select_btn.configure(bg=colors["accent"], fg="#FFFFFF",
                             activebackground=colors["accent_h"],
                             activeforeground="#FFFFFF")
        paint_convert_button(bool(pending))
        draw_toggle()

        style = ttk.Style()
        try:
            style.theme_use("clam" if dark else initial_ttk_theme)
        except Exception:
            pass
        if dark:
            style.configure(
                "TCombobox",
                fieldbackground="#3A3A3A", background="#3A3A3A",
                foreground="#E6E6E6", arrowcolor="#E6E6E6",
                bordercolor="#4A4A4A", lightcolor="#4A4A4A",
                darkcolor="#4A4A4A", insertcolor="#E6E6E6",
                selectbackground="#3A3A3A", selectforeground="#E6E6E6")
            style.map(
                "TCombobox",
                fieldbackground=[("disabled", "#2E2E2E"),
                                 ("readonly", "#3A3A3A"),
                                 ("active", "#3A3A3A")],
                background=[("disabled", "#2E2E2E"),
                            ("readonly", "#3A3A3A"), ("active", "#3A3A3A")],
                foreground=[("disabled", "#7F7F7F"),
                            ("readonly", "#E6E6E6"), ("active", "#E6E6E6")],
                arrowcolor=[("disabled", "#6A6A6A"),
                            ("readonly", "#E6E6E6"), ("active", "#E6E6E6")],
                selectbackground=[("readonly", "#3A3A3A")],
                selectforeground=[("readonly", "#E6E6E6")])
            style.configure("Vertical.TScrollbar", background="#3A3A3A",
                            troughcolor="#2B2B2B", arrowcolor="#A8A8A8",
                            bordercolor="#454545", lightcolor="#3A3A3A",
                            darkcolor="#3A3A3A", borderwidth=1,
                            arrowsize=12)
            root.option_add("*TCombobox*Listbox.background", "#333333")
            root.option_add("*TCombobox*Listbox.foreground", "#E6E6E6")
            root.option_add("*TCombobox*Listbox.selectBackground", "#3B7EA1")
            root.option_add("*TCombobox*Listbox.selectForeground", "#FFFFFF")
            spin.configure(bg="#3A3A3A", fg="#E6E6E6",
                           insertbackground="#E6E6E6",
                           buttonbackground="#3A3A3A",
                           disabledbackground="#3A3A3A",
                           disabledforeground="#7F7F7F",
                           readonlybackground="#3A3A3A",
                           highlightbackground="#4A4A4A",
                           highlightcolor="#4A4A4A",
                           relief="flat", bd=0, highlightthickness=1)
        else:
            style.configure("TCombobox", fieldbackground="#FFFFFF",
                            background="#FFFFFF", foreground="#1A1A1A",
                            arrowcolor="#5C5C5C", bordercolor="#B0B0B0",
                            lightcolor="#FFFFFF", darkcolor="#FFFFFF",
                            selectbackground="#FFFFFF",
                            selectforeground="#1A1A1A")
            style.map("TCombobox",
                      fieldbackground=[("disabled", "#F0F0F0"),
                                       ("readonly", "#FFFFFF")],
                      background=[("disabled", "#F0F0F0"),
                                  ("readonly", "#FFFFFF")],
                      foreground=[("disabled", "#A0A0A0"),
                                  ("readonly", "#1A1A1A")],
                      arrowcolor=[("disabled", "#A0A0A0"),
                                  ("readonly", "#5C5C5C")])
            style.configure("Vertical.TScrollbar", background="#F0F0F0",
                            troughcolor="#FFFFFF", arrowcolor="#5C5C5C",
                            bordercolor="#D0D0D0", lightcolor="#FFFFFF",
                            darkcolor="#FFFFFF")
            root.option_add("*TCombobox*Listbox.background", "#FFFFFF")
            root.option_add("*TCombobox*Listbox.foreground", "#1A1A1A")
            spin.configure(bg="#FFFFFF", fg="#1A1A1A",
                           insertbackground="#1A1A1A",
                           buttonbackground="#F0F0F0",
                           disabledbackground="#F0F0F0",
                           disabledforeground="#A0A0A0",
                           readonlybackground="#FFFFFF",
                           highlightbackground="#B0B0B0",
                           highlightcolor="#B0B0B0",
                           relief="flat", bd=0, highlightthickness=1)

        render_pending()

    dark_var = tk.BooleanVar(value=False)
    theme_row = tk.Frame(card, bg=WIN11_CARD_BG)
    theme_row.place(relx=1.0, x=-16, y=12, anchor="ne")
    theme_label = tk.Label(theme_row, text="Темная тема", font=FONT_BODY_SM,
                           bg=WIN11_CARD_BG, fg=WIN11_TEXT)
    theme_label.pack(side="left", padx=(0, 8))
    theme_toggle = tk.Canvas(theme_row, width=42, height=22,
                             bg=WIN11_CARD_BG, highlightthickness=0, bd=0,
                             cursor="hand2")
    theme_toggle.pack(side="left")

    def draw_toggle():
        theme_toggle.delete("all")
        dark = theme_state["dark"]
        track = "#3B7EA1" if dark else "#C9C9C9"
        theme_toggle.create_oval(1, 1, 21, 21, fill=track, outline=track)
        theme_toggle.create_oval(21, 1, 41, 21, fill=track, outline=track)
        theme_toggle.create_rectangle(11, 1, 31, 21, fill=track,
                                      outline=track)
        x0 = 22 if dark else 2
        theme_toggle.create_oval(x0, 2, x0 + 18, 20, fill="#FFFFFF",
                                 outline="#FFFFFF")

    def toggle_theme(event=None):
        dark_var.set(not theme_state["dark"])
        set_theme(dark_var.get())
        draw_toggle()

    theme_toggle.bind("<Button-1>", toggle_theme)
    theme_label.bind("<Button-1>", toggle_theme)
    draw_toggle()
    tk.Label(card, text=APP_VERSION, font=FONT_VERSION,
             bg=WIN11_CARD_BG, fg=WIN11_SUBTEXT).pack(side="bottom", pady=(0, 6))

    prog = {"win": None, "label": None}

    def hide_progress():
        win = prog["win"]
        if win is not None:
            try:
                win.destroy()
            except Exception:
                pass
        prog["win"] = None
        prog["label"] = None

    def show_progress(text="Работаем…"):
        if prog["win"] is not None and prog["label"] is not None:
            try:
                prog["label"].config(text=text)
                return
            except Exception:
                pass
        hide_progress()
        win = tk.Toplevel(root)
        win.title(APP_NAME)
        win.resizable(False, False)
        win.configure(bg=WIN11_BG)
        win.protocol("WM_DELETE_WINDOW", lambda: None)
        apply_icon(win)
        _place_corner(win, 580, 240)
        card_p = _card(win, 520, 180)
        tk.Label(card_p, text="ИДЁТ РАБОТА",
                 font=("Segoe UI Variable Display", 13, "bold"),
                 bg=WIN11_CARD_BG, fg=WIN11_ACCENT).pack(pady=(20, 6))
        lbl = tk.Label(card_p, text=text, font=FONT_BODY, bg=WIN11_CARD_BG,
                       fg=WIN11_TEXT, wraplength=410, justify="center")
        lbl.pack()
        tk.Label(card_p, text=f"Экстренная остановка: {STOP_HOTKEY.upper()}",
                 font=FONT_VERSION, bg=WIN11_CARD_BG,
                 fg=WIN11_SUBTEXT).pack(side="bottom", pady=(0, 10))
        # Окно прогресса НЕ выводим поверх остальных и не забираем фокус:
        # иначе имитатор клавиш печатает в него, а Schemagee ничего не делает.
        apply_theme_tree(win, theme_state["dark"])
        win.attributes("-topmost", False)
        win.update_idletasks()
        prog["win"] = win
        prog["label"] = lbl

    def show_success(stats):
        hide_progress()
        win = tk.Toplevel(root)
        win.title("Готово")
        win.resizable(False, False)
        win.configure(bg=WIN11_BG)
        win.protocol("WM_DELETE_WINDOW", lambda: close_app(0))
        apply_icon(win)
        _center(win, 780, 640)
        card_s = _card(win, 720, 580)

        tk.Label(card_s, text="✓  АЛЬБОМ СОБРАН",
                 font=("Segoe UI Variable Display", 16, "bold"),
                 bg=WIN11_CARD_BG, fg=WIN11_OK).pack(pady=(20, 10))
        tk.Frame(card_s, bg=WIN11_BORDER, height=1).pack(fill="x", padx=30,
                                                          pady=(0, 12))

        tk.Label(card_s, text="Исходный файл:", font=FONT_BODY_SM,
                 bg=WIN11_CARD_BG, fg=WIN11_SUBTEXT).pack(anchor="w", padx=36)
        tk.Label(card_s, text=os.path.basename(stats["source"]), font=FONT_BODY,
                 bg=WIN11_CARD_BG, fg=WIN11_TEXT, wraplength=640,
                 justify="left").pack(anchor="w", padx=36, pady=(0, 10))

        info = tk.Frame(card_s, bg=WIN11_CARD_BG)
        info.pack(fill="x", padx=36, anchor="w")
        _row(info, "Листов:", f"{stats['sheet_count']} "
                              f"({stats['sheet_count_source']})", 0)
        _row(info, "Экспорт листов:", format_duration(stats["conv_time"]), 1)
        _row(info, "Сборка альбома:", format_duration(stats["merge_time"]), 2)
        _row(info, "Схем в альбоме:", str(stats["placed"]), 3)
        _row(info, "Ошибок:", str(stats["errors"]), 4)

        tk.Label(card_s, text="Результат:", font=FONT_BODY_SM, bg=WIN11_CARD_BG,
                 fg=WIN11_SUBTEXT).pack(anchor="w", padx=36, pady=(12, 2))
        size_kb = stats["target_size"] / 1024.0
        tk.Label(card_s, text=f"{stats['target']}  ({size_kb:.1f} КБ)",
                 font=FONT_BODY, bg=WIN11_CARD_BG, fg=WIN11_TEXT,
                 wraplength=640, justify="left").pack(anchor="w", padx=36)

        notes = stats.get("notes") or []
        if notes:
            tk.Label(card_s, text="Замечания:", font=FONT_BODY_SM,
                     bg=WIN11_CARD_BG, fg=WIN11_SUBTEXT).pack(anchor="w",
                                                              padx=36,
                                                              pady=(12, 2))
            tk.Label(card_s, text="\n".join("• " + n for n in notes),
                     font=FONT_BODY_SM, bg=WIN11_CARD_BG, fg=WIN11_DANGER,
                     wraplength=640, justify="left").pack(anchor="w", padx=36)

        btns = tk.Frame(card_s, bg=WIN11_CARD_BG)
        btns.pack(side="bottom", pady=(16, 0))
        _button(btns, "Открыть папку",
                lambda: _open_folder(stats["out_dir"])).pack(side="left",
                                                             padx=(0, 10))
        _button(btns, "Закрыть", lambda: close_app(0),
                primary=False).pack(side="left")

        apply_theme_tree(win, theme_state["dark"])
        win.attributes("-topmost", True)
        win.focus_force()

    def show_batch(results):
        hide_progress()
        win = tk.Toplevel(root)
        win.title("Отчёт о конвертации")
        win.resizable(False, False)
        win.configure(bg=WIN11_BG)
        win.protocol("WM_DELETE_WINDOW", lambda: close_app(0))
        apply_icon(win)
        _center(win, 840, 620)
        card_b = _card(win, 780, 560)

        ok_count = sum(1 for r in results if r and not r.get("error"))
        fail_count = len(results) - ok_count
        tk.Label(card_b, text=f"ГОТОВО: {ok_count} из {len(results)}",
                 font=("Segoe UI Variable Display", 16, "bold"),
                 bg=WIN11_CARD_BG,
                 fg=WIN11_OK if not fail_count else WIN11_DANGER).pack(
            pady=(20, 10))
        tk.Frame(card_b, bg=WIN11_BORDER, height=1).pack(fill="x", padx=30,
                                                         pady=(0, 10))

        text = tk.Text(card_b, height=17, font=FONT_BODY_SM,
                       bg=WIN11_CARD_BG, fg=WIN11_TEXT, bd=0,
                       highlightthickness=0, wrap="word")
        text.pack(fill="both", expand=True, padx=34)
        for item in results:
            source = os.path.basename(item.get("source") or "?")
            if item.get("error"):
                text.insert("end", f"✗ {source}\n   {item['error']}\n\n")
            else:
                text.insert(
                    "end",
                    f"✓ {source}\n"
                    f"   листов {item.get('sheet_count')}, "
                    f"схем {item.get('placed')}, "
                    f"ошибок {item.get('errors')}\n"
                    f"   {item.get('target')}\n\n")
        text.config(state="disabled")

        _button(card_b, "Закрыть", lambda: close_app(0)).pack(side="bottom",
                                                             pady=(14, 0))
        apply_theme_tree(win, theme_state["dark"])
        win.attributes("-topmost", True)
        win.focus_force()

    def show_error(message):
        hide_progress()
        win = tk.Toplevel(root)
        win.title("Ошибка")
        win.resizable(False, False)
        win.configure(bg=WIN11_BG)
        win.protocol("WM_DELETE_WINDOW", lambda: close_app(1))
        apply_icon(win)
        _center(win, 700, 470)
        card_e = _card(win, 640, 410)

        tk.Label(card_e, text="✗  ОШИБКА",
                 font=("Segoe UI Variable Display", 16, "bold"),
                 bg=WIN11_CARD_BG, fg=WIN11_DANGER).pack(pady=(22, 10))
        tk.Frame(card_e, bg=WIN11_BORDER, height=1).pack(fill="x", padx=30,
                                                          pady=(0, 12))
        tk.Label(card_e, text=message, font=FONT_BODY_SM, bg=WIN11_CARD_BG,
                 fg=WIN11_TEXT, wraplength=560, justify="left").pack(
            anchor="w", padx=36)
        _button(card_e, "OK", lambda: close_app(1)).pack(side="bottom",
                                                         pady=(16, 0))

        apply_theme_tree(win, theme_state["dark"])
        win.attributes("-topmost", True)
        win.focus_force()

    def show_stopped():
        hide_progress()
        win = tk.Toplevel(root)
        win.title("Остановлено")
        win.resizable(False, False)
        win.configure(bg=WIN11_BG)
        win.protocol("WM_DELETE_WINDOW", lambda: close_app(2))
        apply_icon(win)
        _center(win, 640, 360)
        card_t = _card(win, 580, 300)

        tk.Label(card_t, text="■  ОСТАНОВЛЕНО",
                 font=("Segoe UI Variable Display", 16, "bold"),
                 bg=WIN11_CARD_BG, fg=WIN11_DANGER).pack(pady=(24, 12))
        tk.Frame(card_t, bg=WIN11_BORDER, height=1).pack(fill="x", padx=30,
                                                          pady=(0, 12))
        tk.Label(card_t, text="Работа прервана пользователем.\n"
                              "Незавершённые файлы могли остаться в папке "
                              "результата — проверьте её перед повторным "
                              "запуском.",
                 font=FONT_BODY_SM, bg=WIN11_CARD_BG, fg=WIN11_TEXT,
                 wraplength=500, justify="left").pack(anchor="w", padx=36)
        _button(card_t, "OK", lambda: close_app(2)).pack(side="bottom",
                                                         pady=(16, 0))

        apply_theme_tree(win, theme_state["dark"])
        win.attributes("-topmost", True)
        win.focus_force()

    def handle(kind, payload=None):
        if kind == "progress":
            show_progress(payload or "Работаем…")
        elif kind == "progress_hide":
            if prog["win"] is not None:
                try:
                    prog["win"].withdraw()
                except Exception:
                    pass
        elif kind == "progress_show":
            if prog["win"] is not None:
                try:
                    prog["win"].deiconify()
                except Exception:
                    pass
        elif kind == "success":
            if report_var.get():
                show_success(payload)
            else:
                close_app(0)
        elif kind == "batch":
            if report_var.get():
                show_batch(payload)
            else:
                close_app(0)
        elif kind == "error":
            show_error(payload or "Неизвестная ошибка.")
        elif kind == "stopped":
            show_stopped()

    def poll():
        try:
            while True:
                kind, payload = ui_queue.get_nowait()
                handle(kind, payload)
        except queue.Empty:
            pass
        except Exception:
            log("Ошибка в UI-потоке:\n" + traceback.format_exc())
        root.after(100, poll)

    def worker(paths, manual_count, delete_dxf, paper_w, paper_h):
        _PIPELINE_STATE["running"] = True
        results = []
        try:
            for index, path in enumerate(paths, 1):
                check_stop()
                if len(paths) > 1:
                    emit("progress", f"Файл {index} из {len(paths)}: "
                                     f"{os.path.basename(path)}")

                def inner(kind, payload=None):
                    # «success» в пакетном режиме не показываем по файлам:
                    # итог будет один — сводный отчёт.
                    if kind != "success":
                        emit(kind, payload)

                try:
                    stats = run_pipeline(path, manual_count, inner,
                                         delete_dxf=delete_dxf,
                                         paper_w=paper_w, paper_h=paper_h)
                    results.append(stats)
                except TaskStopped:
                    raise
                except Exception as e:
                    log("ОШИБКА:\n" + traceback.format_exc())
                    results.append({
                        "source": path,
                        "name": os.path.splitext(os.path.basename(path))[0],
                        "error": f"{type(e).__name__}: {e}",
                        "out_dir": "",
                    })

            if len(paths) > 1:
                emit("batch", results)
            elif results:
                emit("success", results[0])
        except TaskStopped:
            log("ЗАДАЧА ПРЕРВАНА ПОЛЬЗОВАТЕЛЕМ.")
            emit("stopped")
        except Exception as e:
            log("ОШИБКА:\n" + traceback.format_exc())
            emit("error", f"{type(e).__name__}: {e}")
        finally:
            _PIPELINE_STATE["running"] = False
            try:
                # Остановка уже случилась — закрываем Schemagee, не прерываясь.
                with stop_suspended():
                    if _OWNED_SCHEMAGEE["opened"] and find_schemagee_window()[0]:
                        close_schemagee(timeout=5.0)
            except Exception:
                pass

    def start_work(paths, manual_count):
        paths = [p for p in paths if os.path.isfile(p)]
        if not paths:
            return
        STOP_EVENT.clear()
        root.withdraw()
        text = ("Открываем файл в Schemagee…" if len(paths) == 1
                else f"Файлов: {len(paths)}. Начинаем…")
        show_progress(text)
        paper_w, paper_h = paper_dimensions()
        threading.Thread(target=worker,
                         args=(paths, manual_count, delete_var.get(),
                               paper_w, paper_h),
                         daemon=True).start()

    start_stop_hotkey()

    root.protocol("WM_DELETE_WINDOW", lambda: close_app(0))

    root.after(100, poll)
    root.mainloop()

    try:
        keyboard.clear_all_hotkeys()
    except Exception:
        pass
    return result["code"]


# =============================================================================
# БЛОК 6. ТОЧКА ВХОДА
# =============================================================================

def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description=f"{APP_NAME} {APP_VERSION}: Schemagee (.sdd) -> DXF -> DWG"
    )
    parser.add_argument(
        "--dxf-folder", default=None,
        help="пропустить кликер и собрать альбом из готовых DXF (отладка)",
    )
    parser.add_argument(
        "--name", default=None,
        help="имя итогового файла без расширения",
    )
    parser.add_argument(
        "--headless", action="store_true",
        help="без окон: выполнить и напечатать отчёт",
    )
    return parser.parse_args(argv)


def main():
    opts = vars(parse_args())

    # Включаем DPI-awareness — чтобы шрифты были чёткими на любых мониторах
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

    set_app_user_model_id()
    init_log()
    setup_logging_bridge()
    STOP_EVENT.clear()

    log("=" * 70)
    log(f"{APP_NAME} {APP_VERSION}: старт")
    log(f"Папка программы: {BASE_DIR}")
    log(f"Drag & drop: {'доступен' if HAS_DND else 'недоступен'}")

    code = 0
    try:
        if opts.get("headless") or opts.get("dxf_folder"):
            code = run_headless(opts)
        else:
            code = run_gui()
    except Exception:
        log("КРИТИЧЕСКАЯ ОШИБКА:\n" + traceback.format_exc())
        code = 1
    finally:
        log(f"{APP_NAME}: выход, код {code}")
        close_logs()

    return code


if __name__ == "__main__":
    sys.exit(main())
