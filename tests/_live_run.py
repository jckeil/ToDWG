# -*- coding: utf-8 -*-
"""Живой прогон указанного .sdd через Schemagee (без GUI).

Использование:
    python tests\\_live_run.py "D:\\ToDXF\\Примеры\\file.sdd" [--sheets N]

Без --sheets число листов определяется автоматически (диалог печати).
Во время работы нельзя трогать мышь и клавиатуру.
"""
import argparse
import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ToDXF_v1 as app

try:                        # консоль Windows может быть в cp1251
    sys.stdout.reconfigure(errors="replace")
    sys.stderr.reconfigure(errors="replace")
except Exception:
    pass


def emit(kind, payload=None):
    if payload is None:
        print(f"[{kind}]", flush=True)
    else:
        print(f"[{kind}] {payload}", flush=True)


def main():
    parser = argparse.ArgumentParser(description="Живой прогон одного .sdd")
    parser.add_argument("sdd", help="путь к файлу .sdd")
    parser.add_argument("--sheets", type=int, default=None,
                        help="число листов (по умолчанию — авто)")
    opts = parser.parse_args()

    if not os.path.isfile(opts.sdd):
        print("нет файла:", opts.sdd)
        return 1

    app.init_log()
    app.setup_logging_bridge()
    print(f"START {opts.sdd} sheets={opts.sheets}", flush=True)
    try:
        app.run_pipeline(opts.sdd, opts.sheets, emit)
    except Exception:
        print("ОШИБКА:\n" + traceback.format_exc(), flush=True)
        return 1
    finally:
        # Если сбой случился до штатного закрытия, Schemagee остаётся
        # открытым и блокирует следующий запуск — закрываем его здесь.
        if app._OWNED_SCHEMAGEE.get("opened") and app.find_schemagee_window()[0]:
            try:
                with app.stop_suspended():
                    app.close_schemagee(timeout=5.0)
            except Exception:
                pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
