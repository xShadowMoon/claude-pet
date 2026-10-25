# -*- coding: utf-8 -*-
"""启动日志、单实例检查和 Qt 桌宠应用。

用法：
    python main.py            正常启动
    pythonw main.py           启动且不带黑框（Windows）
    python main.py --port 5000  换一个事件端口
"""

import argparse
import os
import sys
import time
import traceback

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication

from petapp import winutil
from petapp.common import DATA_DIR


def setup_log():
    """pythonw 没有控制台，出了错就写到 data/pet.log 里，方便查。"""
    if sys.stderr is not None and sys.stdout is not None:
        return
    os.makedirs(DATA_DIR, exist_ok=True)
    path = os.path.join(DATA_DIR, "pet.log")
    try:
        if os.path.getsize(path) > 1_000_000:
            os.replace(path, path + ".old")
    except OSError:
        pass
    log = open(path, "a", encoding="utf-8", buffering=1)
    log.write(f"\n==== 启动 {time.strftime('%Y-%m-%d %H:%M:%S')} ====\n")
    sys.stdout = sys.stderr = log

    def hook(kind, value, tb):
        log.write(time.strftime("[%H:%M:%S] "))
        traceback.print_exception(kind, value, tb, file=log)

    sys.excepthook = hook


def main():
    setup_log()
    parser = argparse.ArgumentParser(description="Claude 桌宠")
    parser.add_argument("--port", type=int, default=None, help="本地事件端口，默认 47828")
    args = parser.parse_args()

    if args.port:
        QSettings("ClaudePet", "ClaudePet").setValue("port", args.port)

    # 已经有一只在跑了就别再开第二只（比如开会话时自动叫醒和你手动双击撞在一起）
    if not winutil.single_instance():
        print("桌宠已经在跑了。")
        return 0

    app = QApplication(sys.argv)
    app.setApplicationName("Claude 桌宠")
    app.setQuitOnLastWindowClosed(False)

    from petapp.window import Pet
    pet = Pet()
    pet.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
