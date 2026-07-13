"""
Помощник для калибровки: делает скриншот экрана, чтобы из него вырезать
иконки-эталоны и подобрать координаты области (region) в config.json.

Запуск:
    python capture_templates.py
или (если это exe):
    двойной клик по CaptureScreenshot.exe

Сохранит full_screenshot.png рядом с программой — открой его в любом
редакторе (Paint, GIMP, Photoshop) и вырежи из него:
    - иконку "продолжить" (маленький ромбик/стрелку внизу текста реплики)
      -> сохрани как templates/continue_icon.png
    - иконку "F" (квадратик с буквой F при выборе ответа)
      -> сохрани как templates/f_icon.png

Также посмотри координаты угла, где находятся эти иконки на full_screenshot.png,
и впиши их в config.json в поле "region".
"""

import os
import sys

import mss
import mss.tools


def base_dir():
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


with mss.mss() as sct:
    monitor = sct.monitors[1]  # основной монитор
    shot = sct.grab(monitor)
    out_path = os.path.join(base_dir(), "full_screenshot.png")
    mss.tools.to_png(shot.rgb, shot.size, output=out_path)
    print(f"Сохранено: {out_path}")
    print(f"Разрешение монитора: {shot.size}")

input("Нажми Enter, чтобы закрыть это окно...")
