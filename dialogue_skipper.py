"""
Автоскип диалогов в Genshin Impact.

Логика в два уровня:
    1. Gate: ищем широкую золотую полосу-разделитель под именем персонажа —
       она есть всегда, пока на экране открыт диалог (и во время печати текста,
       и во время выбора варианта). Это большой, специфичный по форме элемент,
       случайно совпасть с чем-то посторонним ему практически нереально —
       в отличие от маленького ромбика "продолжить", который легко путается
       с другими мелкими яркими объектами на экране.
    2. Если gate найден — смотрим, есть ли рядом иконка F (выбор варианта):
       если да, жмём F; если нет, жмём Space (это безопасно в любой момент
       монолога: если текст ещё печатается, игра просто домотает его мгновенно,
       если уже дописан — сразу продолжит).

Настройки берутся из config.json, который лежит рядом с exe/скриптом.
Эталонные картинки берутся из папки templates/, которая тоже лежит рядом.

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
import keyboard
from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.table import Table

pydirectinput.PAUSE = 0  # не тормозить между вызовами pydirectinput


def ensure_admin():
    # Симулированные нажатия клавиш не доходят до игры, если та запущена от
    # администратора, а этот процесс — нет (Windows блокирует ввод в окна с
    # более высокими правами). Проще всегда самим запрашивать повышение,
    # чем каждый раз объяснять пользователю нажать "Запуск от имени администратора".
    try:
        is_admin = ctypes.windll.shell32.IsUserAnAdmin()
    except Exception:
        return  # не Windows или API недоступен — продолжаем как есть
    if is_admin:
        return
    if getattr(sys, "frozen", False):
        exe = sys.executable
        args = sys.argv[1:]
    else:
        exe = sys.executable
        args = [os.path.abspath(__file__)] + sys.argv[1:]
    params = " ".join(f'"{a}"' for a in args)
    ret = ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, params, None, 1)
    if ret > 32:
        sys.exit(0)
    print("Не удалось запросить права администратора — продолжаю без них.")


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
    "gate_threshold": 0.7,
    "f_threshold": 0.75,
    "check_interval": 0.15,
    "key_cooldown": 0.4,
    "gate_files": ["gate_bar.png", "gate_bar_2.png"],
    "f_files": ["f_icon.png", "f_icon_2.png"],
    "toggle_hotkey": "f9",
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


def to_edges(gray_img):
    # Сравнение по контурам вместо сырой яркости — форма линии/буквы не меняется
    # от того, что происходит на заднем фоне (разные локации, освещение, цвета),
    # а именно фон и был причиной нестабильных совпадений.
    blurred = cv2.GaussianBlur(gray_img, (3, 3), 0)
    return cv2.Canny(blurred, 50, 150)


def load_template_group(config, key):
    templates_dir = os.path.join(base_dir(), "templates")
    tpls = []
    for file_name in config[key]:
        path = os.path.join(templates_dir, file_name)
        tpl = cv2.imread(path, cv2.IMREAD_GRAYSCALE)
        if tpl is None:
            raise FileNotFoundError(
                f"Не найден файл-эталон: {path}\n"
                f"Положи картинку '{file_name}' в папку templates/ рядом с программой."
            )
        tpls.append(to_edges(tpl))
    if not tpls:
        raise ValueError(f"В config.json список '{key}' пуст — нужен хотя бы один файл")
    return tpls


def press_key(key):
    pydirectinput.keyDown(key)
    time.sleep(KEY_HOLD_SECONDS)
    pydirectinput.keyUp(key)


def grab_frame(sct, region):
    shot = sct.grab(region)
    frame_bgra = np.array(shot)
    frame_gray = cv2.cvtColor(frame_bgra, cv2.COLOR_BGRA2GRAY)
    return frame_gray


def best_match_score(frame_gray, tpls):
    best_score = 0.0
    for tpl in tpls:
        if tpl.shape[0] > frame_gray.shape[0] or tpl.shape[1] > frame_gray.shape[1]:
            continue
        result = cv2.matchTemplate(frame_gray, tpl, cv2.TM_CCOEFF_NORMED)
        _, max_val, _, _ = cv2.minMaxLoc(result)
        if max_val > best_score:
            best_score = max_val
    return best_score


def render_dashboard(config, counters, last_event, log_lines, gate_score, f_score, enabled):
    region = config["region"]

    stats = Table.grid(padding=(0, 2))
    stats.add_column(justify="right", style="bold")
    stats.add_column()
    status_text = "[bold green]работает[/bold green]" if enabled else "[bold yellow]на паузе[/bold yellow]"
    stats.add_row("Статус:", status_text)
    stats.add_row(
        "Хоткей вкл/выкл:",
        f"[bold]{config['toggle_hotkey'].upper()}[/bold]",
    )
    stats.add_row(
        "Область экрана:",
        f"top={region['top']} left={region['left']} "
        f"width={region['width']} height={region['height']}",
    )
    gate_ok = gate_score >= config["gate_threshold"]
    gate_style = "green" if gate_ok else "grey50"
    stats.add_row(
        "Gate (диалог открыт):",
        f"[{gate_style}]{gate_score:.2f}[/{gate_style}] (порог {config['gate_threshold']})",
    )
    f_style = "green" if f_score >= config["f_threshold"] else "grey50"
    stats.add_row(
        "Score иконки F:",
        f"[{f_style}]{f_score:.2f}[/{f_style}] (порог {config['f_threshold']})",
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
        subtitle=f"{config['toggle_hotkey'].upper()} — пауза/продолжить  |  Ctrl+C или закрытие окна — остановить",
        border_style="cyan",
    )


def main():
    console = Console()
    config = load_config()
    gate_templates = load_template_group(config, "gate_files")
    f_templates = load_template_group(config, "f_files")
    region = config["region"]
    gate_threshold = config["gate_threshold"]
    f_threshold = config["f_threshold"]
    check_interval = config["check_interval"]
    key_cooldown = config["key_cooldown"]

    counters = {"space": 0, "f": 0}
    last_event = None
    log_lines = deque(maxlen=LOG_MAX_LINES)
    last_press_time = 0.0
    state = {"enabled": True}

    def toggle_enabled():
        state["enabled"] = not state["enabled"]
        status = "включён" if state["enabled"] else "на паузе"
        log_lines.appendleft(f"[{time.strftime('%H:%M:%S')}] {config['toggle_hotkey'].upper()} -> {status}")

    try:
        keyboard.add_hotkey(config["toggle_hotkey"], toggle_enabled)
    except Exception as exc:
        log_lines.appendleft(f"Не удалось назначить хоткей {config['toggle_hotkey']}: {exc}")

    with mss.mss() as sct, Live(
        render_dashboard(config, counters, last_event, log_lines, 0.0, 0.0, state["enabled"]),
        console=console,
        refresh_per_second=8,
        screen=True,
    ) as live:
        while True:
            if not state["enabled"]:
                live.update(render_dashboard(config, counters, last_event, log_lines, 0.0, 0.0, False))
                time.sleep(check_interval)
                continue

            frame_gray = grab_frame(sct, region)
            frame_edges = to_edges(frame_gray)
            gate_score = best_match_score(frame_edges, gate_templates)

            f_score = 0.0
            if gate_score >= gate_threshold:
                f_score = best_match_score(frame_edges, f_templates)

                now = time.time()
                if now - last_press_time >= key_cooldown:
                    if f_score >= f_threshold:
                        press_key("f")
                        last_press_time = now
                        counters["f"] += 1
                        last_event = f"gate={gate_score:.2f} f={f_score:.2f} -> press f"
                    else:
                        press_key("space")
                        last_press_time = now
                        counters["space"] += 1
                        last_event = f"gate={gate_score:.2f} f={f_score:.2f} -> press space"
                    log_lines.appendleft(f"[{time.strftime('%H:%M:%S')}] {last_event}")

            live.update(render_dashboard(config, counters, last_event, log_lines, gate_score, f_score, True))
            time.sleep(check_interval)


if __name__ == "__main__":
    try:
        ensure_admin()
        main()
    except KeyboardInterrupt:
        print("\nОстановлено.")
    except Exception as exc:  # для не-технического пользователя: окно не должно закрыться мгновенно
        print(f"\nПроизошла ошибка: {exc}")
        input("Нажми Enter, чтобы закрыть это окно...")
