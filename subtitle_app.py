import os
import sys

# --- Фикс для сборки с --windowed/--noconsole: там stdout/stderr равны None ---
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w", encoding="utf-8")

import re
import shutil
import tempfile
import textwrap
import threading
import traceback
import subprocess
import multiprocessing
from collections import deque
from pathlib import Path
from types import SimpleNamespace
import tkinter as tk
from tkinter import filedialog, messagebox, colorchooser
from tkinter import font as tkfont

import customtkinter as ctk
import whisper


APP_NAME = "FuckingVideo"

ctk.set_appearance_mode("Dark")
ctk.set_default_color_theme("blue")

NO_WINDOW = (
    getattr(subprocess, "CREATE_NO_WINDOW", 0)
    if sys.platform == "win32"
    else 0
)

VIDEO_EXTENSIONS = {
    ".mp4", ".mkv", ".avi", ".mov", ".webm", ".flv", ".wmv", ".m4v", ".ts",
}

# Предпросмотр: libass по умолчанию считает SRT в системе 384x288,
# поэтому размер в предпросмотре масштабируется от высоты 288.
REF_HEIGHT = 288
PREVIEW_HEIGHT = 200
PREVIEW_SCALE = PREVIEW_HEIGHT / REF_HEIGHT
PREVIEW_WRAP = 560
PREVIEW_MARGIN_V = 35

SAMPLE_TEXTS = [
    "ываываываываываываываы",
    "ваываы",
    "ываы",
    "быстро быстро быстро",
    "Բարև աշխարհ",
]


PREVIEW_MARGIN_H = 10

# Сетка положения субтитров (как на цифровой клавиатуре).
ALIGN_LAYOUT = [
    [7, 8, 9],   # верх
    [4, 5, 6],   # середина
    [1, 2, 3],   # низ
]

# Номер ASS Alignment -> (вертикаль, горизонталь)
ALIGN_INFO = {
    1: ("bottom", "left"), 2: ("bottom", "center"), 3: ("bottom", "right"),
    4: ("mid", "left"),    5: ("mid", "center"),    6: ("mid", "right"),
    7: ("top", "left"),    8: ("top", "center"),    9: ("top", "right"),
}


class Cancelled(Exception):
    """Пользователь отменил обработку."""


# ----------------------------------------------------------------------------
# Вспомогательные функции
# ----------------------------------------------------------------------------

def get_app_directory():
    """Папка EXE или исходного Python-файла."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def get_ffmpeg_path():
    """Ищет FFmpeg рядом с программой или в PATH."""
    app_dir = get_app_directory()

    candidates = [
        app_dir / "ffmpeg.exe",
        app_dir / "ffmpeg",
        app_dir / "bin" / "ffmpeg.exe",
        app_dir / "bin" / "ffmpeg",
    ]

    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)

    found = shutil.which("ffmpeg")
    if found:
        return found

    raise FileNotFoundError(
        "Не найден FFmpeg!\n\n"
        "Положи ffmpeg.exe рядом с программой "
        "или установи FFmpeg и добавь его в PATH."
    )


def add_to_path(folder):
    """Whisper сам вызывает 'ffmpeg' из PATH — добавляем туда нашу папку."""
    folder = str(folder)
    current = os.environ.get("PATH", "")
    if folder not in current.split(os.pathsep):
        os.environ["PATH"] = folder + os.pathsep + current


def get_system_fonts(root):
    """Список шрифтов, установленных в Windows."""
    try:
        names = {f for f in tkfont.families(root) if f and not f.startswith("@")}
    except Exception:
        names = set()

    names = sorted(names, key=str.lower)
    if "Arial" not in names:
        names.insert(0, "Arial")
    return names


def probe_media(ffmpeg, path):
    """Берёт из вывода ffmpeg длительность и наличие видео/аудио дорожек."""
    result = subprocess.run(
        [ffmpeg, "-hide_banner", "-nostdin", "-i", path],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        creationflags=NO_WINDOW,
    )
    text = result.stderr or ""

    duration = 0.0
    match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", text)
    if match:
        duration = (
            int(match.group(1)) * 3600
            + int(match.group(2)) * 60
            + float(match.group(3))
        )

    return {
        "duration": duration,
        "has_video": bool(re.search(r"Stream #.*Video:", text)),
        "has_audio": bool(re.search(r"Stream #.*Audio:", text)),
    }


def format_time(seconds):
    """Преобразует секунды в формат SRT."""
    milliseconds_total = max(0, round(float(seconds) * 1000))

    hours = milliseconds_total // 3_600_000
    minutes = (milliseconds_total % 3_600_000) // 60_000
    secs = (milliseconds_total % 60_000) // 1000
    milliseconds = milliseconds_total % 1000

    return f"{hours:02}:{minutes:02}:{secs:02},{milliseconds:03}"


def build_cues(segments, max_chars=84, line_width=42, max_duration=6.0):
    """
    Режет результат Whisper на короткие субтитры (до двух строк),
    чтобы текст не вылезал за кадр. Использует тайминги слов, если есть.
    """
    raw = []

    for seg in segments:
        words = seg.get("words") or []

        if not words:
            text = seg.get("text", "").strip()
            if text:
                raw.append([float(seg["start"]), float(seg["end"]), text])
            continue

        current = []
        cue_start = None
        cue_end = None

        for word in words:
            token = word.get("word", "")
            if not token.strip():
                continue

            if not current:
                cue_start = float(word["start"])

            current.append(token)
            cue_end = float(word["end"])
            text = "".join(current).strip()
            ends_sentence = token.strip()[-1:] in ".!?…"

            if (
                len(text) >= max_chars
                or cue_end - cue_start >= max_duration
                or (ends_sentence and len(text) >= 25)
            ):
                raw.append([cue_start, cue_end, text])
                current = []

        if current:
            raw.append([cue_start, cue_end, "".join(current).strip()])

    # Убираем наложения и нулевую длительность.
    for i, cue in enumerate(raw):
        if i + 1 < len(raw) and cue[1] > raw[i + 1][0]:
            cue[1] = max(cue[0] + 0.05, raw[i + 1][0])
        if cue[1] <= cue[0]:
            cue[1] = cue[0] + 0.5

    cues = []
    for start, end, text in raw:
        text = re.sub(r"\s+", " ", text).strip()
        if not text:
            continue
        wrapped = "\n".join(textwrap.wrap(text, width=line_width))
        cues.append((start, end, wrapped))

    return cues


def write_srt(cues, path):
    """Сохраняет субтитры в SRT."""
    with open(path, "w", encoding="utf-8-sig") as file:
        for index, (start, end, text) in enumerate(cues, start=1):
            file.write(
                f"{index}\n{format_time(start)} --> {format_time(end)}\n"
                f"{text}\n\n"
            )


def hex_to_ass(hex_color):
    """#RRGGBB -> &H00BBGGRR (формат ASS)."""
    hex_color = hex_color.lstrip("#")
    r, g, b = hex_color[0:2], hex_color[2:4], hex_color[4:6]
    return f"&H00{b}{g}{r}".upper()


def text_color_for(hex_color):
    """Чёрный или белый текст — что читаемее на данном фоне."""
    hex_color = hex_color.lstrip("#")
    r = int(hex_color[0:2], 16)
    g = int(hex_color[2:4], 16)
    b = int(hex_color[4:6], 16)
    return "black" if (0.299 * r + 0.587 * g + 0.114 * b) > 150 else "white"


def log_error(text):
    """Пишет подробную ошибку в лог-файл. Возвращает путь или None."""
    for base in (get_app_directory(), Path(tempfile.gettempdir())):
        try:
            log_path = base / "fvs_log.txt"
            with open(log_path, "a", encoding="utf-8") as file:
                file.write(text + "\n" + "-" * 60 + "\n")
            return str(log_path)
        except OSError:
            continue
    return None


def to_number(value, default):
    try:
        return float(str(value).replace(",", "."))
    except ValueError:
        return default


# ----------------------------------------------------------------------------
# Приложение
# ----------------------------------------------------------------------------

class AutoSubtitleApp(ctk.CTk):
    def __init__(self):
        super().__init__()

        self.title("FuckingVideo — AI Subtitle Studio")
        self.geometry("740x860")
        self.minsize(660, 600)

        self.video_path = ""
        self.model = None
        self.loaded_model_key = None
        self.busy = False
        self.proc = None
        self.temp_dir = None
        self.cancel_event = threading.Event()

        self.colors = {"text": "#FFFFFF", "outline": "#000000"}
        self.align = 2  # снизу по центру
        self.align_buttons = {}
        self.color_buttons = {}

        self.font_list = get_system_fonts(self)
        self.font_var = tk.StringVar(value="Arial")
        self.preview_text_var = tk.StringVar(value=SAMPLE_TEXTS[0])

        self.protocol("WM_DELETE_WINDOW", self.on_close)

        # --- Нижняя панель (всегда видна) ---
        bottom = ctk.CTkFrame(self, fg_color="transparent")
        bottom.pack(side="bottom", fill="x")

        self.label_status = ctk.CTkLabel(
            bottom,
            text="Готов к работе",
            text_color="#52C7A5",
            wraplength=680,
        )
        self.label_status.pack(padx=20, pady=(10, 4))

        self.progress = ctk.CTkProgressBar(bottom)
        self.progress.pack(fill="x", padx=25, pady=4)
        self.progress.set(0)

        self.label_progress = ctk.CTkLabel(bottom, text="Ожидание")
        self.label_progress.pack(pady=(0, 4))

        buttons = ctk.CTkFrame(bottom, fg_color="transparent")
        buttons.pack(fill="x", padx=25, pady=(6, 4))
        buttons.grid_columnconfigure(0, weight=1)

        self.btn_process = ctk.CTkButton(
            buttons,
            text="Создать видео с субтитрами",
            height=46,
            font=("Arial", 15, "bold"),
            fg_color="#168B55",
            hover_color="#116B41",
            command=self.start_processing,
        )
        self.btn_process.grid(row=0, column=0, sticky="ew", padx=(0, 8))

        self.btn_cancel = ctk.CTkButton(
            buttons,
            text="Отмена",
            height=46,
            width=110,
            fg_color="#A33A3A",
            hover_color="#7E2D2D",
            state="disabled",
            command=self.request_cancel,
        )
        self.btn_cancel.grid(row=0, column=1)

        self.btn_open_folder = ctk.CTkButton(
            bottom,
            text="Открыть папку результата",
            command=self.open_output_folder,
            fg_color="transparent",
            border_width=1,
        )
        self.btn_open_folder.pack(fill="x", padx=25, pady=(4, 12))

        # --- Прокручиваемая основная часть ---
        body = ctk.CTkScrollableFrame(self)
        body.pack(side="top", fill="both", expand=True, padx=8, pady=(8, 0))

        ctk.CTkLabel(
            body, text="FuckingVideo", font=("Arial", 28, "bold")
        ).pack(pady=(8, 2))

        ctk.CTkLabel(
            body,
            text="AI Subtitle Studio • Автоматические субтитры",
            text_color="gray",
        ).pack(pady=(0, 12))

        # Выбор видео
        video_frame = ctk.CTkFrame(body)
        video_frame.pack(fill="x", padx=10, pady=5)

        self.btn_video = ctk.CTkButton(
            video_frame,
            text="Выбрать видео",
            command=self.select_video,
            height=38,
        )
        self.btn_video.pack(padx=12, pady=(12, 5), fill="x")

        self.label_video = ctk.CTkLabel(
            video_frame,
            text="Видео не выбрано",
            text_color="gray",
            wraplength=600,
        )
        self.label_video.pack(padx=10, pady=(0, 12))

        # Путь результата
        output_frame = ctk.CTkFrame(body)
        output_frame.pack(fill="x", padx=10, pady=8)

        ctk.CTkLabel(
            output_frame,
            text="Куда сохранить готовое видео",
            font=("Arial", 14, "bold"),
        ).pack(anchor="w", padx=12, pady=(10, 4))

        output_row = ctk.CTkFrame(output_frame, fg_color="transparent")
        output_row.pack(fill="x", padx=10, pady=(0, 10))

        self.entry_output = ctk.CTkEntry(
            output_row, placeholder_text="Путь к выходному MP4"
        )
        self.entry_output.pack(side="left", fill="x", expand=True, padx=(0, 8))

        self.btn_output = ctk.CTkButton(
            output_row, text="Обзор", width=90, command=self.select_output
        )
        self.btn_output.pack(side="right")

        # Настройки
        self.settings = ctk.CTkFrame(body)
        self.settings.pack(fill="x", padx=10, pady=8)
        self.settings.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(
            self.settings,
            text="Настройки субтитров",
            font=("Arial", 16, "bold"),
        ).grid(row=0, column=0, columnspan=2, padx=12, pady=(12, 6), sticky="w")

        # Шрифт (можно выбрать из списка или ввести вручную)
        ctk.CTkLabel(self.settings, text="Шрифт").grid(
            row=1, column=0, padx=12, pady=6, sticky="w"
        )
        self.font_combo = ctk.CTkComboBox(
            self.settings,
            values=self.font_list,
            variable=self.font_var,
        )
        self.font_combo.grid(row=1, column=1, padx=12, pady=6, sticky="ew")
        self.font_var.trace_add("write", lambda *args: self.update_preview())

        # Размер (слайдер)
        ctk.CTkLabel(self.settings, text="Размер шрифта").grid(
            row=2, column=0, padx=12, pady=6, sticky="w"
        )
        size_row = ctk.CTkFrame(self.settings, fg_color="transparent")
        size_row.grid(row=2, column=1, padx=12, pady=6, sticky="ew")

        self.size_slider = ctk.CTkSlider(
            size_row,
            from_=8,
            to=72,
            number_of_steps=64,
            command=lambda value: self.update_preview(),
        )
        self.size_slider.set(18)
        self.size_slider.pack(side="left", fill="x", expand=True)

        self.size_value_label = ctk.CTkLabel(size_row, text="18", width=50)
        self.size_value_label.pack(side="right", padx=(8, 0))

        self.add_color_row("Цвет текста", "text", 3)
        self.add_color_row("Цвет обводки", "outline", 4)

        self.entry_outline = self.add_entry("Толщина обводки", "2", 5)
        self.entry_shadow = self.add_entry("Тень", "1", 6)
        self.entry_outline.bind("<KeyRelease>", lambda e: self.update_preview())
        self.entry_shadow.bind("<KeyRelease>", lambda e: self.update_preview())

        ctk.CTkLabel(self.settings, text="Модель Whisper").grid(
            row=7, column=0, padx=12, pady=6, sticky="w"
        )
        self.model_menu = ctk.CTkOptionMenu(
            self.settings, values=["tiny", "base", "small", "medium"]
        )
        self.model_menu.set("base")
        self.model_menu.grid(row=7, column=1, padx=12, pady=6, sticky="ew")

        ctk.CTkLabel(self.settings, text="Язык речи").grid(
            row=8, column=0, padx=12, pady=6, sticky="w"
        )
        self.language_menu = ctk.CTkOptionMenu(
            self.settings,
            values=[
                "Автоопределение",
                "Русский",
                "Английский",
                "Украинский",
                "Испанский",
                "Немецкий",
                "Французский",
                "Армянский",
            ],
        )
        self.language_menu.set("Автоопределение")
        self.language_menu.grid(row=8, column=1, padx=12, pady=6, sticky="ew")

        ctk.CTkLabel(self.settings, text="Положение").grid(
            row=9, column=0, padx=12, pady=6, sticky="w"
        )
        pos_frame = ctk.CTkFrame(self.settings, fg_color="transparent")
        pos_frame.grid(row=9, column=1, padx=12, pady=6, sticky="w")

        for r, row_codes in enumerate(ALIGN_LAYOUT):
            for c, code in enumerate(row_codes):
                button = ctk.CTkButton(
                    pos_frame,
                    text="",
                    width=44,
                    height=30,
                    fg_color=("#168B55" if code == self.align
                              else ("gray70", "gray30")),
                    command=lambda k=code: self.set_alignment(k),
                )
                button.grid(row=r, column=c, padx=3, pady=3)
                self.align_buttons[code] = button

        self.save_srt_var = tk.BooleanVar(value=True)
        self.check_srt = ctk.CTkCheckBox(
            self.settings,
            text="Сохранить отдельный файл .SRT",
            variable=self.save_srt_var,
        )
        self.check_srt.grid(
            row=10, column=0, columnspan=2, padx=12, pady=(8, 12), sticky="w"
        )

        # Предпросмотр
        self.build_preview(body)
        self.update_preview()

    # ------------------------------------------------------------------ UI

    def add_entry(self, title, default, row):
        ctk.CTkLabel(self.settings, text=title).grid(
            row=row, column=0, padx=12, pady=6, sticky="w"
        )
        entry = ctk.CTkEntry(self.settings)
        entry.insert(0, default)
        entry.grid(row=row, column=1, padx=12, pady=6, sticky="ew")
        return entry

    def add_color_row(self, title, key, row):
        ctk.CTkLabel(self.settings, text=title).grid(
            row=row, column=0, padx=12, pady=6, sticky="w"
        )
        color = self.colors[key]
        button = ctk.CTkButton(
            self.settings,
            text=f"{color}  (нажми, чтобы изменить)",
            fg_color=color,
            hover_color=color,
            text_color=text_color_for(color),
            border_width=1,
            command=lambda: self.pick_color(key),
        )
        button.grid(row=row, column=1, padx=12, pady=6, sticky="ew")
        self.color_buttons[key] = button

    def pick_color(self, key):
        _, chosen = colorchooser.askcolor(
            color=self.colors[key], title="Выбери цвет"
        )
        if not chosen:
            return
        chosen = chosen.upper()
        self.colors[key] = chosen
        self.color_buttons[key].configure(
            text=f"{chosen}  (нажми, чтобы изменить)",
            fg_color=chosen,
            hover_color=chosen,
            text_color=text_color_for(chosen),
        )
        self.update_preview()

    # ----------------------------------------------------------- Предпросмотр

    def build_preview(self, parent):
        frame = ctk.CTkFrame(parent)
        frame.pack(fill="x", padx=10, pady=8)

        ctk.CTkLabel(
            frame, text="Предпросмотр", font=("Arial", 16, "bold")
        ).pack(anchor="w", padx=12, pady=(10, 4))

        # Имитация кадра видео
        self.preview_box = ctk.CTkFrame(
            frame, height=PREVIEW_HEIGHT, fg_color="#3b3b3b", corner_radius=8
        )
        self.preview_box.pack(fill="x", padx=12, pady=4)
        self.preview_box.pack_propagate(False)

        # Слои: тень, обводка (8 смещений), основной текст
        self.prev_shadow = self._make_preview_label()
        self.prev_outline = [self._make_preview_label() for _ in range(8)]
        self.prev_main = self._make_preview_label()

        # Текст для проверки
        ctk.CTkEntry(
            frame,
            textvariable=self.preview_text_var,
            placeholder_text="Введи текст для проверки",
        ).pack(fill="x", padx=12, pady=(8, 4))
        self.preview_text_var.trace_add("write", lambda *args: self.update_preview())

        # Быстрые образцы
        samples = ctk.CTkFrame(frame, fg_color="transparent")
        samples.pack(fill="x", padx=12, pady=(2, 4))
        for sample in SAMPLE_TEXTS:
            ctk.CTkButton(
                samples,
                text=sample,
                height=28,
                command=lambda t=sample: self.preview_text_var.set(t),
            ).pack(side="left", padx=(0, 6), pady=4)

        ctk.CTkLabel(
            frame,
            text=(
                "Примерный вид: размер подогнан под кадр 16:9. "
                "Точный результат — в готовом видео."
            ),
            text_color="gray",
            wraplength=600,
            font=("Arial", 11),
        ).pack(anchor="w", padx=12, pady=(0, 12))

    def set_alignment(self, code):
        self.align = code
        for key, button in self.align_buttons.items():
            button.configure(
                fg_color="#168B55" if key == code else ("gray70", "gray30")
            )
        self.update_preview()

    def _make_preview_label(self):
        return ctk.CTkLabel(
            self.preview_box,
            text="",
            justify="center",
            wraplength=PREVIEW_WRAP,
        )

    def update_preview(self, *args):
        if not hasattr(self, "prev_main"):
            return

        try:
            size = int(round(self.size_slider.get()))
        except (tk.TclError, AttributeError):
            return

        self.size_value_label.configure(text=str(size))

        family = self.font_var.get().strip() or "Arial"
        # px на экране -> pt (96 dpi): pt = px * 0.75
        px = max(6.0, size * PREVIEW_SCALE)
        font = (family, max(1, round(px * 0.75)))

        text = self.preview_text_var.get() or " "
        outline = to_number(self.entry_outline.get(), 0)
        shadow = to_number(self.entry_shadow.get(), 0)

        vertical, horizontal = ALIGN_INFO[self.align]

        rely = {"top": 0.0, "mid": 0.5, "bottom": 1.0}[vertical]
        relx = {"left": 0.0, "center": 0.5, "right": 1.0}[horizontal]
        anchor = (
            {"top": "n", "mid": "", "bottom": "s"}[vertical]
            + {"left": "w", "center": "", "right": "e"}[horizontal]
        ) or "center"

        mh = round(PREVIEW_MARGIN_H * PREVIEW_SCALE)
        mv = round(PREVIEW_MARGIN_V * PREVIEW_SCALE)
        x_base = {"left": mh, "center": 0, "right": -mh}[horizontal]
        y_base = {"top": mv, "mid": 0, "bottom": -mv}[vertical]

        def place(label, dx, dy, color):
            label.configure(text=text, font=font, text_color=color)
            label.place(
                relx=relx,
                rely=rely,
                anchor=anchor,
                x=x_base + dx,
                y=y_base + dy,
            )

        # Тень
        sh = round(shadow * PREVIEW_SCALE)
        if sh > 0:
            place(self.prev_shadow, sh, sh, "#000000")
        else:
            self.prev_shadow.place_forget()

        # Обводка
        r = round(outline * PREVIEW_SCALE)
        offsets = [
            (r, 0), (-r, 0), (0, r), (0, -r),
            (r, r), (-r, r), (r, -r), (-r, -r),
        ]
        for label, (dx, dy) in zip(self.prev_outline, offsets):
            if r > 0:
                place(label, dx, dy, self.colors["outline"])
            else:
                label.place_forget()

        # Основной текст
        place(self.prev_main, 0, 0, self.colors["text"])

    # ------------------------------------------------------------- Файлы

    def select_video(self):
        file_path = filedialog.askopenfilename(
            title="Выбери видео",
            filetypes=[
                ("Видеофайлы", "*.mp4 *.mkv *.avi *.mov *.webm *.m4v *.flv *.wmv"),
                ("Все файлы", "*.*"),
            ],
        )

        if not file_path:
            return

        self.video_path = file_path
        self.label_video.configure(text=file_path, text_color="white")

        suggested = str(
            Path(file_path).with_name(Path(file_path).stem + "_subtitled.mp4")
        )
        self.entry_output.delete(0, "end")
        self.entry_output.insert(0, suggested)

    def select_output(self):
        initial = self.entry_output.get().strip()

        file_path = filedialog.asksaveasfilename(
            title="Сохранить готовое видео",
            defaultextension=".mp4",
            initialfile=Path(initial).name if initial else "subtitled.mp4",
            filetypes=[("MP4 видео", "*.mp4")],
            confirmoverwrite=False,
        )

        if file_path:
            self.entry_output.delete(0, "end")
            self.entry_output.insert(0, file_path)

    def set_status(self, message, progress=None):
        """Вызывается только в главном потоке."""
        self.label_status.configure(text=message)

        if progress is not None:
            progress = max(0.0, min(1.0, progress))
            self.progress.set(progress)
            self.label_progress.configure(text=f"{int(progress * 100)}%")

    def ui(self, callback, *args):
        """Безопасно передаёт обновление в GUI-поток."""
        try:
            self.after(0, lambda: callback(*args))
        except (RuntimeError, tk.TclError):
            pass

    # ------------------------------------------------------ Запуск / отмена

    def normalize_output(self, output):
        path = Path(output)
        suffix = path.suffix.lower()
        if suffix == ".mp4":
            return str(path)
        if suffix in VIDEO_EXTENSIONS:
            return str(path.with_suffix(".mp4"))
        return str(path) + ".mp4"

    def start_processing(self):
        if self.busy:
            return

        if not self.video_path or not os.path.isfile(self.video_path):
            messagebox.showwarning(
                "Нет видео", "Сначала выбери существующий видеофайл."
            )
            return

        output = self.entry_output.get().strip()

        if not output:
            messagebox.showwarning("Нет пути", "Выбери путь сохранения видео.")
            return

        output = os.path.abspath(self.normalize_output(output))

        if os.path.normcase(output) == os.path.normcase(
            os.path.abspath(self.video_path)
        ):
            messagebox.showwarning(
                "Ошибка", "Выходной файл не должен заменять исходное видео."
            )
            return

        try:
            size = int(round(self.size_slider.get()))
            outline = float(self.entry_outline.get().strip().replace(",", "."))
            shadow = float(self.entry_shadow.get().strip().replace(",", "."))

            if not 8 <= size <= 200:
                raise ValueError("Размер шрифта должен быть от 8 до 200.")

            if not 0 <= outline <= 10 or not 0 <= shadow <= 10:
                raise ValueError("Обводка и тень должны быть от 0 до 10.")

            font = self.font_var.get().strip()

            if not font or any(ch in font for ch in ",:'\\="):
                raise ValueError("Укажи корректное имя шрифта, например Arial.")

        except ValueError as error:
            messagebox.showwarning("Проверь настройки", str(error))
            return

        save_srt = self.save_srt_var.get()
        final_srt = str(Path(output).with_suffix(".srt"))

        existing = []
        if os.path.exists(output):
            existing.append(output)
        if save_srt and os.path.exists(final_srt):
            existing.append(final_srt)

        if existing:
            if not messagebox.askyesno(
                "Перезаписать?",
                "Эти файлы уже существуют и будут перезаписаны:\n\n"
                + "\n".join(existing),
            ):
                return

        try:
            os.makedirs(os.path.dirname(output), exist_ok=True)
        except OSError as error:
            messagebox.showerror("Ошибка", f"Не удалось создать папку:\n{error}")
            return

        self.entry_output.delete(0, "end")
        self.entry_output.insert(0, output)

        settings = {
            "video": self.video_path,
            "output": output,
            "final_srt": final_srt,
            "font": font,
            "size": size,
            "color": hex_to_ass(self.colors["text"]),
            "outline_color": hex_to_ass(self.colors["outline"]),
            "outline": outline,
            "shadow": shadow,
            "model": self.model_menu.get(),
            "language": self.language_menu.get(),
            "save_srt": save_srt,
            "align": self.align,
        }

        self.cancel_event.clear()
        self.busy = True
        self.btn_process.configure(state="disabled")
        self.btn_video.configure(state="disabled")
        self.btn_output.configure(state="disabled")
        self.btn_cancel.configure(state="normal")
        self.progress.configure(mode="determinate")
        self.set_status("Подготовка...", 0)

        threading.Thread(
            target=self.process_video, args=(settings,), daemon=True
        ).start()

    def request_cancel(self):
        if not self.busy:
            return
        self.cancel_event.set()
        self.kill_process()
        self.set_status("Отмена... (распознавание остановится через пару секунд)")

    def kill_process(self):
        proc = self.proc
        if proc is not None and proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass

    def check_cancel(self):
        if self.cancel_event.is_set():
            raise Cancelled()

    def on_close(self):
        if self.busy and not messagebox.askyesno(
            "Выход", "Идёт обработка. Прервать и выйти?"
        ):
            return
        self.cancel_event.set()
        self.kill_process()
        if self.temp_dir:
            shutil.rmtree(self.temp_dir, ignore_errors=True)
        self.destroy()

    # ------------------------------------------------------------ Обработка

    def get_model(self, name):
        try:
            import torch
            device = "cuda" if torch.cuda.is_available() else "cpu"
        except Exception:
            device = "cpu"

        key = (name, device)
        if self.model is None or self.loaded_model_key != key:
            self.ui(
                self.set_status,
                f"Загрузка модели Whisper «{name}» ({device.upper()}). "
                "При первом запуске она скачивается из интернета — "
                "это может занять несколько минут...",
                0.08,
            )
            self.model = whisper.load_model(name, device=device)
            self.loaded_model_key = key

        return self.model, device

    def transcribe(self, model, device, video, language):
        """Распознавание с реальным прогрессом и возможностью отмены."""
        app = self

        class ProgressBar:
            def __init__(self, *args, total=None, **kwargs):
                self.total = total or 1
                self.n = 0

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def update(self, n=1):
                self.n += n
                fraction = min(self.n / self.total, 1.0)
                app.ui(
                    app.set_status,
                    "Распознавание речи...",
                    0.12 + 0.43 * fraction,
                )
                app.check_cancel()

            def close(self):
                pass

        module = sys.modules.get("whisper.transcribe")
        original_tqdm = getattr(module, "tqdm", None) if module else None

        if module is not None and original_tqdm is not None:
            module.tqdm = SimpleNamespace(tqdm=ProgressBar)

        try:
            for word_timestamps in (True, False):
                try:
                    return model.transcribe(
                        video,
                        language=language,
                        verbose=False,
                        fp16=(device == "cuda"),
                        word_timestamps=word_timestamps,
                        condition_on_previous_text=False,
                    )
                except Cancelled:
                    raise
                except Exception:
                    if not word_timestamps:
                        raise
                    log_error(
                        "word_timestamps не сработали, пробуем без них:\n"
                        + traceback.format_exc()
                    )
        finally:
            if module is not None and original_tqdm is not None:
                module.tqdm = original_tqdm

    def render(self, ffmpeg, settings, srt_path, duration, part_output):
        style = (
            f"FontName={settings['font']},"
            f"FontSize={settings['size']},"
            f"PrimaryColour={settings['color']},"
            f"OutlineColour={settings['outline_color']},"
            f"BorderStyle=1,"
            f"Outline={settings['outline']},"
            f"Shadow={settings['shadow']},"
            f"MarginL={PREVIEW_MARGIN_H},"
            f"MarginR={PREVIEW_MARGIN_H},"
            f"MarginV={PREVIEW_MARGIN_V},"
            f"Alignment={settings['align']}"
        )

        # Файл субтитров лежит в cwd процесса — экранировать путь не нужно.
        video_filter = (
            "scale=trunc(iw/2)*2:trunc(ih/2)*2,"
            f"subtitles=filename='{os.path.basename(srt_path)}':"
            f"force_style='{style}'"
        )

        cmd = [
            ffmpeg,
            "-hide_banner",
            "-nostdin",
            "-nostats",
            "-y",
            "-i", settings["video"],
            "-map", "0:v:0",
            "-map", "0:a:0?",
            "-vf", video_filter,
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-crf", "21",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac",
            "-b:a", "192k",
            "-movflags", "+faststart",
            "-progress", "pipe:1",
            part_output,
        ]

        self.proc = subprocess.Popen(
            cmd,
            cwd=os.path.dirname(srt_path),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=NO_WINDOW,
        )

        tail = deque(maxlen=40)
        last_fraction = -1.0

        for line in self.proc.stdout:
            line = line.strip()

            if line.startswith(("out_time_us=", "out_time_ms=")):
                try:
                    microseconds = int(line.split("=", 1)[1])
                except ValueError:
                    continue
                if duration > 0:
                    fraction = min(microseconds / 1_000_000 / duration, 1.0)
                    if fraction - last_fraction >= 0.002:
                        last_fraction = fraction
                        self.ui(
                            self.set_status,
                            "Рендеринг видео через FFmpeg...",
                            0.60 + 0.40 * fraction,
                        )
            elif line and "=" not in line.split(" ")[0]:
                tail.append(line)

        code = self.proc.wait()
        self.proc = None

        self.check_cancel()

        if code != 0:
            raise RuntimeError(
                "FFmpeg завершился с ошибкой:\n\n" + "\n".join(tail)
            )

    def process_video(self, settings):
        part_output = None

        try:
            self.check_cancel()
            self.ui(self.set_status, "Проверка FFmpeg и видео...", 0.03)

            ffmpeg = get_ffmpeg_path()
            add_to_path(Path(ffmpeg).resolve().parent)

            info = probe_media(ffmpeg, settings["video"])

            if not info["has_video"]:
                raise RuntimeError("В файле нет видеодорожки.")
            if not info["has_audio"]:
                raise RuntimeError(
                    "В видео нет звуковой дорожки — распознавать нечего."
                )

            self.check_cancel()
            model, device = self.get_model(settings["model"])
            self.check_cancel()

            language_map = {
                "Русский": "ru",
                "Английский": "en",
                "Украинский": "uk",
                "Испанский": "es",
                "Немецкий": "de",
                "Французский": "fr",
                "Армянский": "hy",
            }
            language = language_map.get(settings["language"])

            self.ui(self.set_status, "Распознавание речи...", 0.12)

            result = self.transcribe(
                model, device, settings["video"], language
            )
            self.check_cancel()

            segments = result.get("segments", [])
            cues = build_cues(segments)

            if not cues:
                raise RuntimeError(
                    "Whisper не обнаружил речь. Проверь аудио в видео."
                )

            self.ui(self.set_status, "Создание файла субтитров...", 0.57)

            self.temp_dir = tempfile.mkdtemp(prefix="fvs_")
            srt_path = os.path.join(self.temp_dir, "subs.srt")
            write_srt(cues, srt_path)

            output_path = Path(settings["output"])
            part_output = str(
                output_path.with_name(output_path.stem + ".part.mp4")
            )

            self.ui(self.set_status, "Рендеринг видео через FFmpeg...", 0.60)

            self.render(
                ffmpeg, settings, srt_path, info["duration"], part_output
            )

            if (
                not os.path.isfile(part_output)
                or os.path.getsize(part_output) == 0
            ):
                raise RuntimeError("FFmpeg завершился, но видео не создано.")

            os.replace(part_output, settings["output"])
            part_output = None

            if settings["save_srt"]:
                shutil.copyfile(srt_path, settings["final_srt"])

            self.ui(self.set_status, "Готово!", 1.0)
            self.ui(self.processing_success, settings)

        except Cancelled:
            self.ui(self.set_status, "Обработка отменена", 0)

        except Exception as error:
            details = traceback.format_exc()
            log_path = log_error(details)
            self.ui(self.processing_error, str(error), log_path)

        finally:
            self.proc = None

            if part_output and os.path.exists(part_output):
                try:
                    os.remove(part_output)
                except OSError:
                    pass

            if self.temp_dir:
                shutil.rmtree(self.temp_dir, ignore_errors=True)
                self.temp_dir = None

            self.ui(self.finish_processing)

    # ------------------------------------------------------- Результаты

    def processing_success(self, settings):
        message = f"Видео успешно создано!\n\nВидео:\n{settings['output']}"

        if settings["save_srt"]:
            message += f"\n\nСубтитры:\n{settings['final_srt']}"

        messagebox.showinfo("Успешно", message)

    def processing_error(self, error, log_path=None):
        self.label_status.configure(text="Ошибка обработки")

        lines = [line for line in error.strip().splitlines() if line.strip()]
        short = "\n".join(lines[-8:]) if lines else "Неизвестная ошибка"

        if log_path:
            short += f"\n\nПодробности в логе:\n{log_path}"

        messagebox.showerror("Ошибка", short)

    def finish_processing(self):
        self.busy = False
        self.btn_process.configure(state="normal")
        self.btn_video.configure(state="normal")
        self.btn_output.configure(state="normal")
        self.btn_cancel.configure(state="disabled")

    def open_output_folder(self):
        output = self.entry_output.get().strip()

        if output:
            folder = os.path.dirname(os.path.abspath(output))
        elif self.video_path:
            folder = os.path.dirname(self.video_path)
        else:
            folder = str(get_app_directory())

        if not os.path.isdir(folder):
            messagebox.showwarning(
                "Папка не найдена", "Папка результата ещё не создана."
            )
            return

        try:
            if sys.platform == "win32":
                os.startfile(folder)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", folder])
            else:
                subprocess.Popen(["xdg-open", folder])
        except Exception as error:
            messagebox.showerror("Ошибка", f"Не удалось открыть папку:\n{error}")


if __name__ == "__main__":
    multiprocessing.freeze_support()
    app = AutoSubtitleApp()
    app.mainloop()
