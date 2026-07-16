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
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile
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

# Поднимай перед каждым тегированием нового релиза в GitHub — иначе
# автообновление не увидит новую версию.
VERSION = "1.0.0"
GITHUB_REPO = "KusakinDev/genshin-autoskip"
RELEASES_API_URL = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
UPDATE_ASSET_NAME = "GenshinAutoSkip.zip"
UPDATE_EXE_NAMES = ["DialogueSkipper.exe", "CaptureScreenshot.exe"]


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

# config.json — личная калибровка пользователя (свой монитор/резолюция/чувствительность).
# Автообновление этот файл никогда не трогает.
DEFAULT_CONFIG = {
    "region": {"top": 520, "left": 880, "width": 460, "height": 580},
    "gate_threshold": 0.7,
    "f_threshold": 0.75,
    "toggle_hotkey": "f9",
}

# detection.json — то, что относится к самим эталонам распознавания. Обновляется
# автообновлением вместе с папкой templates/, так что новые/улучшенные шаблоны
# доходят до пользователей без ручного скачивания zip заново.
DETECTION_CONFIG_FILENAME = "detection.json"
DEFAULT_DETECTION_CONFIG = {
    "check_interval": 0.15,
    "key_cooldown": 0.4,
    "gate_files": ["gate_bar.png", "gate_bar_2.png", "gate_bar_3.png"],
    "f_files": ["f_icon.png", "f_icon_2.png"],
}


def base_dir():
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def parse_version(v):
    v = v.strip().lstrip("vV")
    parts = []
    for p in v.split("."):
        digits = "".join(ch for ch in p if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts) or (0,)


def check_for_update():
    """Возвращает (tag, ссылка_на_zip) если на GitHub есть версия новее текущей, иначе None."""
    try:
        req = urllib.request.Request(
            RELEASES_API_URL,
            headers={"Accept": "application/vnd.github+json", "User-Agent": "GenshinAutoSkip"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        latest_tag = data.get("tag_name", "")
        if not latest_tag or parse_version(latest_tag) <= parse_version(VERSION):
            return None
        for asset in data.get("assets", []):
            if asset.get("name") == UPDATE_ASSET_NAME:
                return latest_tag, asset.get("browser_download_url")
        return None
    except Exception:
        return None  # нет интернета / GitHub недоступен / что угодно ещё — просто не обновляемся


def download_and_apply_update(asset_url):
    """Скачивает zip с релиза и обновляет: exe-файлы, detection.json и папку
    templates/ (эталоны распознавания). config.json (личная калибровка —
    region/пороги/хоткей) не трогает никогда."""
    if not getattr(sys, "frozen", False):
        print("Автообновление работает только для собранного .exe, не для запуска из исходников.")
        return False

    print("Скачиваю обновление...")
    tmp_dir = tempfile.mkdtemp(prefix="genshin_autoskip_update_")
    zip_path = os.path.join(tmp_dir, "update.zip")
    urllib.request.urlretrieve(asset_url, zip_path)

    extract_dir = os.path.join(tmp_dir, "extracted")
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(extract_dir)

    target_dir = base_dir()
    file_names = UPDATE_EXE_NAMES + [DETECTION_CONFIG_FILENAME]
    copy_lines = "\n".join(
        f'copy /y "{os.path.join(extract_dir, name)}" "{os.path.join(target_dir, name)}"'
        for name in file_names
        if os.path.exists(os.path.join(extract_dir, name))
    )
    if not copy_lines:
        print("В скачанном архиве не нашлось файлов для обновления — обновление отменено.")
        return False

    templates_src = os.path.join(extract_dir, "templates")
    templates_line = ""
    if os.path.isdir(templates_src):
        templates_line = (
            f'xcopy /y /e /i "{templates_src}" "{os.path.join(target_dir, "templates")}" > nul\r\n'
        )

    bat_path = os.path.join(tmp_dir, "apply_update.bat")
    bat_content = (
        "@echo off\r\n"
        "timeout /t 2 /nobreak > nul\r\n"
        f"{copy_lines}\r\n"
        f"{templates_line}"
        f'start "" "{os.path.join(target_dir, "DialogueSkipper.exe")}"\r\n'
        'del "%~f0"\r\n'
    )
    with open(bat_path, "w", encoding="utf-8") as f:
        f.write(bat_content)

    subprocess.Popen(["cmd", "/c", bat_path], creationflags=subprocess.CREATE_NO_WINDOW)
    return True


def offer_update():
    update = check_for_update()
    if not update:
        return
    latest_tag, asset_url = update
    if not asset_url:
        return
    print(f"Доступно обновление: {latest_tag} (у тебя {VERSION})")
    answer = input("Скачать и установить сейчас? [y/N]: ").strip().lower()
    if answer in ("y", "yes", "д", "да"):
        if download_and_apply_update(asset_url):
            print("Обновление скачано — программа сейчас перезапустится...")
            sys.exit(0)


def load_json_config(filename, default):
    path = os.path.join(base_dir(), filename)
    if not os.path.exists(path):
        print(f"{filename} не найден, создаю с настройками по умолчанию: {path}")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(default, f, ensure_ascii=False, indent=2)
        return dict(default)
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_config():
    # Личная калибровка и шаблоны/тайминги лежат в двух разных файлах, но
    # остальному коду удобнее работать с одним объединённым словарём.
    config = load_json_config("config.json", DEFAULT_CONFIG)
    detection = load_json_config(DETECTION_CONFIG_FILENAME, DEFAULT_DETECTION_CONFIG)
    config.update(detection)
    return config


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
        tpls.append(tpl)
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
    stats.add_row("Версия:", VERSION)
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
            gate_score = best_match_score(frame_gray, gate_templates)

            f_score = 0.0
            if gate_score >= gate_threshold:
                f_score = best_match_score(frame_gray, f_templates)

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
        offer_update()
        main()
    except KeyboardInterrupt:
        print("\nОстановлено.")
    except Exception as exc:  # для не-технического пользователя: окно не должно закрыться мгновенно
        print(f"\nПроизошла ошибка: {exc}")
        input("Нажми Enter, чтобы закрыть это окно...")
