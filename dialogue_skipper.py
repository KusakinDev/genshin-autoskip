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

DEFAULT_CONFIG = {
    "region": {"top": 850, "left": 900, "width": 700, "height": 250},
    "match_threshold": 0.82,
    "check_interval": 0.15,
    "key_cooldown": 0.4,
    "templates": {
        "space": {"file": "continue_icon.png", "key": "space"},
        "f": {"file": "f_icon.png", "key": "f"},
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
        path = os.path.join(templates_dir, spec["file"])
        tpl = cv2.imread(path, cv2.IMREAD_GRAYSCALE)
        if tpl is None:
            raise FileNotFoundError(
                f"Не найден файл-эталон: {path}\n"
                f"Положи картинку иконки '{spec['file']}' в папку templates/ рядом с программой."
            )
        loaded[name] = (tpl, spec["key"])
    return loaded


def grab_region(sct, region):
    shot = sct.grab(region)
    frame = np.array(shot)
    return cv2.cvtColor(frame, cv2.COLOR_BGRA2GRAY)


def find_best_match(frame_gray, templates):
    best_name, best_key, best_score = None, None, 0.0
    for name, (tpl, key) in templates.items():
        if tpl.shape[0] > frame_gray.shape[0] or tpl.shape[1] > frame_gray.shape[1]:
            continue
        result = cv2.matchTemplate(frame_gray, tpl, cv2.TM_CCOEFF_NORMED)
        _, max_val, _, _ = cv2.minMaxLoc(result)
        if max_val > best_score:
            best_name, best_key, best_score = name, key, max_val
    return best_name, best_key, best_score


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
            frame_gray = grab_region(sct, region)
            name, key, score = find_best_match(frame_gray, templates)

            if score >= match_threshold:
                now = time.time()
                if now - last_press_time >= key_cooldown:
                    pydirectinput.press(key)
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
