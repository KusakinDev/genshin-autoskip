"""
Автоскип диалогов в Genshin Impact.

Каждые CHECK_INTERVAL секунд захватывает область экрана и ищет шаблоном
две иконки-подсказки:
    - continue_icon.png -> нажимает SPACE (пропуск реплики)
    - f_icon.png         -> нажимает F (выбор варианта, без разницы какого)

Настройки берутся из config.json, который лежит рядом с exe/скриптом.
Эталонные иконки берутся из папки templates/, которая тоже лежит рядом.

Запуск игры должен быть в фокусе. Останов: Ctrl+C или закрыть окно консоли.
"""

import ctypes
import json
import os
import sys
import time
from collections import deque

import cv2
import numpy as np
import mss
import pydirectinput
from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.table import Table

pydirectinput.PAUSE = 0  # не тормозить между вызовами pydirectinput

try:
    # Без этого при масштабировании экрана (125%/150%) и/или запуске от администратора
    # координаты захвата экрана (mss) могут не совпадать с тем, что откалибровано по
    # обычному скриншоту — Windows виртуализирует DPI по-разному в зависимости от контекста.
    ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
except Exception:
    pass

LOG_MAX_LINES = 8
KEY_HOLD_SECONDS = 0.05  # без явного удержания игра может не считать нажатие

DEFAULT_CONFIG = {
    "region": {"top": 520, "left": 880, "width": 460, "height": 580},
    "match_threshold": 0.75,
    "check_interval": 0.15,
    "key_cooldown": 0.4,
    "templates": {
        "space": {
            "files": ["continue_icon.png"],
            "key": "space",
            # ромбик "продолжить" всегда ярко-оранжевый/жёлтый — совпадение по форме
            # засчитывается только если цвет найденного участка тоже похож, это отсекает
            # случайные фоновые объекты, похожие по силуэту, но другого цвета
            "color_filter": {"hue_min": 8, "hue_max": 40, "min_saturation": 60, "min_value": 90},
        },
        "f": {"files": ["f_icon.png"], "key": "f"},
    },
}


def base_dir():
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def load_config():
    config_path = os.path.join(base_dir(), "config.json")
    if not os.path.exists(config_path):
        print(f"config.json не найден, создаю с настройками по умолчанию: {config_path}")
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_CONFIG, f, ensure_ascii=False, indent=2)
        return DEFAULT_CONFIG
    with open(config_path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_templates(config):
    templates_dir = os.path.join(base_dir(), "templates")
    loaded = {}
    for name, spec in config["templates"].items():
        files = spec.get("files") or ([spec["file"]] if "file" in spec else [])
        tpls = []
        for file_name in files:
            path = os.path.join(templates_dir, file_name)
            tpl = cv2.imread(path, cv2.IMREAD_GRAYSCALE)
            if tpl is None:
                raise FileNotFoundError(
                    f"Не найден файл-эталон: {path}\n"
                    f"Положи картинку иконки '{file_name}' в папку templates/ рядом с программой."
                )
            tpls.append(tpl)
        if not tpls:
            raise ValueError(f"У шаблона '{name}' в config.json не указано ни одного файла (files)")
        loaded[name] = {
            "templates": tpls,
            "key": spec["key"],
            "color_filter": spec.get("color_filter"),
        }
    return loaded


def press_key(key):
    pydirectinput.keyDown(key)
    time.sleep(KEY_HOLD_SECONDS)
    pydirectinput.keyUp(key)


def grab_frame(sct, region):
    shot = sct.grab(region)
    frame_bgra = np.array(shot)
    frame_bgr = frame_bgra[:, :, :3]
    frame_gray = cv2.cvtColor(frame_bgra, cv2.COLOR_BGRA2GRAY)
    return frame_bgr, frame_gray


def find_best_match(frame_gray, templates):
    best = {"name": None, "key": None, "score": 0.0, "loc": None, "shape": None}
    for name, spec in templates.items():
        for tpl in spec["templates"]:
            if tpl.shape[0] > frame_gray.shape[0] or tpl.shape[1] > frame_gray.shape[1]:
                continue
            result = cv2.matchTemplate(frame_gray, tpl, cv2.TM_CCOEFF_NORMED)
            _, max_val, _, max_loc = cv2.minMaxLoc(result)
            if max_val > best["score"]:
                best.update(name=name, key=spec["key"], score=max_val, loc=max_loc, shape=tpl.shape)
    return best


def passes_color_filter(frame_bgr, loc, shape, color_filter):
    if not color_filter:
        return True
    x, y = loc
    h, w = shape
    patch = frame_bgr[y : y + h, x : x + w]
    if patch.size == 0:
        return False
    hsv = cv2.cvtColor(patch, cv2.COLOR_BGR2HSV)
    mean_h, mean_s, mean_v = hsv.reshape(-1, 3).mean(axis=0)
    return (
        color_filter.get("hue_min", 0) <= mean_h <= color_filter.get("hue_max", 179)
        and mean_s >= color_filter.get("min_saturation", 0)
        and mean_v >= color_filter.get("min_value", 0)
    )


def render_dashboard(config, counters, last_event, log_lines, live_name, live_score):
    region = config["region"]

    stats = Table.grid(padding=(0, 2))
    stats.add_column(justify="right", style="bold")
    stats.add_column()
    stats.add_row("Статус:", "[bold green]работает[/bold green]")
    stats.add_row(
        "Область экрана:",
        f"top={region['top']} left={region['left']} "
        f"width={region['width']} height={region['height']}",
    )
    stats.add_row("Порог совпадения:", str(config["match_threshold"]))
    stats.add_row(
        "Текущий лучший score:",
        f"{live_name or '-'}: {live_score:.2f}",
    )
    stats.add_row("Нажатий SPACE:", str(counters.get("space", 0)))
    stats.add_row("Нажатий F:", str(counters.get("f", 0)))
    stats.add_row("Последнее действие:", last_event or "-")

    log_text = "\n".join(log_lines) if log_lines else "(пока ничего не обнаружено)"

    body = Table.grid()
    body.add_row(stats)
    body.add_row("")
    body.add_row(Panel(log_text, title="Журнал", border_style="grey50"))

    return Panel(
        body,
        title="Genshin Autoskip — [bold]запущен[/bold]",
        subtitle="Ctrl+C или закрытие окна — остановить",
        border_style="cyan",
    )


def main():
    console = Console()
    config = load_config()
    templates = load_templates(config)
    region = config["region"]
    match_threshold = config["match_threshold"]
    check_interval = config["check_interval"]
    key_cooldown = config["key_cooldown"]

    counters = {"space": 0, "f": 0}
    last_event = None
    log_lines = deque(maxlen=LOG_MAX_LINES)
    last_press_time = 0.0

    with mss.mss() as sct, Live(
        render_dashboard(config, counters, last_event, log_lines, None, 0.0),
        console=console,
        refresh_per_second=8,
        screen=True,
    ) as live:
        while True:
            frame_bgr, frame_gray = grab_frame(sct, region)
            match = find_best_match(frame_gray, templates)
            name, key, score = match["name"], match["key"], match["score"]

            if score >= match_threshold:
                color_filter = templates[name]["color_filter"] if name else None
                if not passes_color_filter(frame_bgr, match["loc"], match["shape"], color_filter):
                    last_event = f"{name} (score={score:.2f}) отклонено — не тот цвет"
                    log_lines.appendleft(f"[{time.strftime('%H:%M:%S')}] {last_event}")
                else:
                    now = time.time()
                    if now - last_press_time >= key_cooldown:
                        press_key(key)
                        last_press_time = now
                        counters[name] = counters.get(name, 0) + 1
                        last_event = f"{name} (score={score:.2f}) -> press {key}"
                        log_lines.appendleft(f"[{time.strftime('%H:%M:%S')}] {last_event}")

            live.update(render_dashboard(config, counters, last_event, log_lines, name, score))
            time.sleep(check_interval)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nОстановлено.")
    except Exception as exc:  # для не-технического пользователя: окно не должно закрыться мгновенно
        print(f"\nПроизошла ошибка: {exc}")
        input("Нажми Enter, чтобы закрыть это окно...")
