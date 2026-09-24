"""Desktop recording interface."""
import os
import re
import json
import time
import queue
import datetime
import threading
import traceback
import subprocess
import html
import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext, filedialog

import numpy as np
import mss
from PIL import Image, ImageTk
import imagehash
import sounddevice as sd
import pyaudiowpatch as pyaudio

from .paths import (BASE_DIR, FFMPEG_PATH, SETTINGS_PATH, MODELS_DIR,
                    ROLE_SELF, ROLE_OTHER, ROLE_TRACK_SELF, ROLE_TRACK_OTHER,
                    sanitize_name)
from .recording import (CaptureClock, TimelineSource, RecordingProcessor,
                        RATE as AUDIO_RATE, FRAMES as AUDIO_FRAMES)
from .asr.realtime import PartialLeakageGuard, remove_cross_role_duplicates
from .settings import load_glossary
from .postprocess.data import _read_jsonl, _read_metadata
from .postprocess.transcribe import detect_ja_en
from .postprocess.render import fmt_timestamp
from .postprocess.video import import_video_file
from .postprocess.pipeline import post_process_folder
from .postprocess.common import PostProcessCancelled

# Extra entry in the display picker: capture a dragged rectangle instead of a
# whole monitor.
REGION_CHOICE = "範囲を指定（ドラッグ）"
MIN_REGION = 40          # physical px; smaller selections are almost certainly slips

SUMMARY_PROVIDERS = [
    ("なし（要約しない）", "none"),
    ("Ollama（ローカル完結）", "ollama"),
    ("Claude API（外部送信）", "claude"),
]

REALTIME_BACKENDS = [
    ("高速 日本語/英語（ReazonSpeech）", "fast_ja_en"),
    ("従来（faster-whisper）", "whisper"),
]

LANG_OPTIONS = [
    ("自動（日本語/英語）", "auto"),
    ("日本語", "ja"), ("English", "en"), ("中文", "zh"), ("한국어", "ko"),
]

# Sentinel placed on a capture queue to signal end-of-stream to the mixer.
_EOF = object()

class _GlobalHotkey:
    """A system-wide hotkey, registered with Win32 on its own thread.

    tkinter's `bind` only fires while the app has focus, but during a meeting
    the foreground window is Teams or Zoom — so marking an important moment
    needs a hotkey the OS delivers no matter what is on top. RegisterHotKey
    binds to the calling thread's message queue, so registration and the
    message pump have to live on the same thread.
    """

    MODIFIERS = {"alt": 0x0001, "ctrl": 0x0002, "control": 0x0002,
                 "shift": 0x0004, "win": 0x0008}
    MOD_NOREPEAT = 0x4000
    WM_HOTKEY = 0x0312
    WM_QUIT = 0x0012

    def __init__(self, spec, callback, log=print):
        self.spec = spec
        self.callback = callback
        self.log = log
        self.mods, self.vk = self._parse(spec)
        self._thread = None
        self._thread_id = None
        self._ready = threading.Event()
        self.registered = False

    @classmethod
    def _parse(cls, spec):
        """'ctrl+shift+m' -> (modifier mask, virtual key code)."""
        mods = cls.MOD_NOREPEAT
        vk = None
        for part in str(spec).lower().split("+"):
            part = part.strip()
            if not part:
                continue
            if part in cls.MODIFIERS:
                mods |= cls.MODIFIERS[part]
            elif len(part) == 1:
                vk = ord(part.upper())
            elif part.startswith("f") and part[1:].isdigit():
                vk = 0x70 + int(part[1:]) - 1      # VK_F1 == 0x70
        return mods, vk

    def start(self):
        if os.name != "nt" or self.vk is None:
            self.log(f"[ホットキー] 使用できません: {self.spec}")
            return False
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self._ready.wait(timeout=2)
        return self.registered

    def _run(self):
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        self._thread_id = ctypes.windll.kernel32.GetCurrentThreadId()
        hotkey_id = 1
        try:
            ok = user32.RegisterHotKey(None, hotkey_id, self.mods, self.vk)
        except Exception as e:
            self.log(f"[ホットキー] 登録失敗: {e}")
            self._ready.set()
            return
        if not ok:
            # Almost always means another application already owns the combo.
            self.log(f"[ホットキー] {self.spec} は他のアプリが使用中のため登録できません")
            self._ready.set()
            return
        self.registered = True
        self._ready.set()
        try:
            msg = wintypes.MSG()
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if msg.message == self.WM_HOTKEY and msg.wParam == hotkey_id:
                    try:
                        self.callback()
                    except Exception as e:
                        print(f"[ホットキー処理警告] {e}")
        finally:
            user32.UnregisterHotKey(None, hotkey_id)
            self.registered = False

    def stop(self):
        if self._thread is None or self._thread_id is None:
            return
        try:
            import ctypes
            ctypes.windll.user32.PostThreadMessageW(
                self._thread_id, self.WM_QUIT, 0, 0)
        except Exception as e:
            print(f"[ホットキー終了警告] {e}")
        self._thread = None


class _PcmWriter:
    """An ffmpeg encoder fed stereo s16le PCM on stdin (the Captura pattern).

    Role tracks pass `transcription_only`: they are downmixed to 16kHz mono at
    64kbps because nothing ever listens to them — they exist to be handed to
    whisper — while audio_main.mp3 stays full quality. Both are fed the exact
    same PCM, so ffmpeg's resampling keeps them on one timeline.
    """

    def __init__(self, path, rate, transcription_only=False):
        self.path = path
        self.written = 0
        args = [FFMPEG_PATH, "-f", "s16le", "-acodec", "pcm_s16le",
                "-ar", str(rate), "-ac", "2", "-i", "-", "-c:a", "libmp3lame"]
        if transcription_only:
            args += ["-ar", "16000", "-ac", "1", "-b:a", "64k"]
        else:
            args += ["-b:a", "192k"]
        args += ["-y", path]
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        self.proc = subprocess.Popen(
            args, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, creationflags=flags)

    def write(self, pcm_bytes):
        if self.proc is None or self.proc.poll() is not None:
            return
        try:
            self.proc.stdin.write(pcm_bytes)
            self.written += len(pcm_bytes)
        except (BrokenPipeError, OSError, ValueError):
            pass   # one dead encoder must not stop the others

    def close(self):
        if self.proc is None:
            return
        try:
            self.proc.stdin.close()
        except Exception as e:
            print(f"[ffmpeg終了警告] stdin ({os.path.basename(self.path)}): {e}")
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            try:
                self.proc.wait(timeout=5)
            except Exception as e:
                print(f"[ffmpeg終了警告] kill: {e}")
        except Exception as e:
            print(f"[ffmpeg終了警告] wait: {e}")
        self.proc = None


def _com_initialize():
    """Initialise COM on the calling thread; True if we own the initialisation.

    WASAPI is COM based and COM apartments are per thread. PortAudio only sets
    COM up on the thread that first calls Pa_Initialize — later calls are
    reference-counted no-ops — so a second capture thread inherits no apartment
    and Pa_StartStream fails there with "Unanticipated host error" (-9999) even
    though the device opened fine. Every capture thread initialises COM itself.
    """
    if os.name != "nt":
        return False
    try:
        import ctypes
        hr = ctypes.windll.ole32.CoInitializeEx(None, 0x2)  # APARTMENTTHREADED
    except Exception as e:
        print(f"[COM初期化警告] {e}")
        return False
    # S_OK / S_FALSE: this thread owns an initialisation and must balance it.
    # RPC_E_CHANGED_MODE and other failures: leave the existing apartment alone.
    return hr in (0, 1)


def _com_uninitialize():
    try:
        import ctypes
        ctypes.windll.ole32.CoUninitialize()
    except Exception as e:
        print(f"[COM終了警告] {e}")


class MeetingRecorderGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("GijirokuStudio v2 - リモート会議記録システム")
        self.root.geometry("620x860")
        self.root.resizable(False, False)

        self.is_recording = False
        self.stop_event = threading.Event()
        self.ffmpeg_proc = None       # _PcmWriter for the mixed audio
        self._role_writers = {}       # role -> _PcmWriter (speaker separation)
        self.transcriber = None
        self.transcription_file = None
        self.refined_transcription_file = None
        self._transcribe_start = 0
        self._record_start = 0
        self._audio_level = 0.0
        self._preloaded_transcriber = None
        self._transcribe_thread = None
        self._rt_detected_lang = None
        self._fast_final_segments = []
        self._fast_refined_segments = []
        self._recording_dir = None
        self._recording_mon_idx = 0
        self._recording_rect = None   # frozen at record start, like the dHash threshold
        self._pipeline_ctx = None
        self._pipeline_thread = None
        self._last_record_dir = None
        self._queue_overflow_count = 0
        self._meeting_name = ""
        self._rt_lang = None  # None = auto (ja/en detect), else fixed lang code
        self._post_cancel = threading.Event()
        self._post_running = False
        self._marker_lock = threading.Lock()
        self._marker_count = 0
        self._hotkey = None
        self._browser_win = None

        self._audio_devices = []        # list of enumerated device dicts
        self._audio_queues = []         # per-device capture queues (during recording)
        self.transcribe_queue = None
        self.transcribe_role_queues = {}
        self._audio_finished = threading.Event()
        self._leakage_guard = PartialLeakageGuard()
        self._asr_load_ema = 0.0
        self._asr_load_level = 0
        # PortAudio init/open/terminate are not thread-safe: with several devices
        # starting at once, unrelated opens fail with bogus "invalid sample rate".
        self._open_lock = threading.Lock()
        self._pcm_written = 0           # bytes handed to ffmpeg this recording

        self.INTERVAL = 5.0
        self.DHASH_THRESHOLD = 10
        self.JPEG_QUALITY = 85
        self.AUDIO_GAIN = 2.0
        self.TARGET_RATE = AUDIO_RATE
        self.TRANSCRIBE_RATE = 16000
        self.TRANSCRIBE_CHUNK_SECONDS = 5.0   # real-time chunk length fed to faster-whisper
        self.TRANSCRIBE_OVERLAP_SECONDS = 1.0  # trailing overlap kept across chunks

        # faster-whisper (post-processing) settings — editable via settings.json
        self.WHISPER_MODEL = "large-v3-turbo"
        self.WHISPER_DEVICE = "cpu"
        self.WHISPER_COMPUTE = "int8"

        # faster-whisper (real-time) settings — smaller model for low latency
        self.REALTIME_WHISPER_MODEL = "base"
        self.REALTIME_BACKEND = "fast_ja_en"
        self.POSTPROCESS_BACKEND = "whisper"
        self.FAST_ASR_THREADS = 4
        self.ECHO_DELAY_MS = 0

        # System-wide key for marking an important moment mid-meeting
        self.MARKER_HOTKEY = "ctrl+shift+m"

        # Screen capture area: None = whole display, else an mss rect
        self.CAPTURE_REGION = None
        self.CAPTURE_MAX_EDGE = 1600   # 0 disables the downscale

        # Slide OCR during post-processing. Off by default: it needs the
        # Windows Japanese OCR language pack and adds time to every run.
        self.OCR_ENABLED = False

        # AI summary of the transcript, generated during post-processing
        self.SUMMARY_PROVIDER = "none"   # none | ollama | claude
        self.SUMMARY_MODEL = ""          # empty = provider default

        self._enumerate_audio_devices()
        self._load_settings()
        self.create_widgets()
        self._populate_audio_listbox()
        self._update_region_label()
        self._tick_level_meter()

    # --------------------------------------------------------- Settings persistence

    def _load_settings(self):
        try:
            if os.path.exists(SETTINGS_PATH):
                with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
                    s = json.load(f)
                self.DHASH_THRESHOLD = int(s.get("dhash_threshold", self.DHASH_THRESHOLD))
                self.AUDIO_GAIN = float(s.get("audio_gain", self.AUDIO_GAIN))
                self.JPEG_QUALITY = int(s.get("jpeg_quality", self.JPEG_QUALITY))
                self.WHISPER_MODEL = str(s.get("whisper_model", self.WHISPER_MODEL))
                self.WHISPER_DEVICE = str(s.get("whisper_device", self.WHISPER_DEVICE))
                self.WHISPER_COMPUTE = str(s.get("whisper_compute", self.WHISPER_COMPUTE))
                self.REALTIME_WHISPER_MODEL = str(s.get(
                    "realtime_whisper_model", self.REALTIME_WHISPER_MODEL))
                self.REALTIME_BACKEND = str(s.get(
                    "realtime_backend", self.REALTIME_BACKEND))
                self.POSTPROCESS_BACKEND = str(s.get(
                    "postprocess_backend", self.POSTPROCESS_BACKEND))
                self.FAST_ASR_THREADS = int(s.get(
                    "fast_asr_threads", self.FAST_ASR_THREADS))
                self.ECHO_DELAY_MS = int(s.get(
                    "echo_delay_ms", self.ECHO_DELAY_MS))
                self.MARKER_HOTKEY = str(s.get("marker_hotkey", self.MARKER_HOTKEY))
                self.CAPTURE_MAX_EDGE = int(s.get("capture_max_edge", self.CAPTURE_MAX_EDGE))
                region = s.get("capture_region")
                if isinstance(region, dict) and all(
                        k in region for k in ("left", "top", "width", "height")):
                    self.CAPTURE_REGION = {k: int(region[k])
                                           for k in ("left", "top", "width", "height")}
                self.OCR_ENABLED = bool(s.get("ocr_enabled", self.OCR_ENABLED))
                self.SUMMARY_PROVIDER = str(s.get("summary_provider", self.SUMMARY_PROVIDER))
                self.SUMMARY_MODEL = str(s.get("summary_model", self.SUMMARY_MODEL))
        except Exception as e:
            print(f"[設定読み込み警告] {e}")

    def _save_settings(self):
        try:
            # read-modify-write so manually-added keys (incl. whisper_*) are preserved
            existing = {}
            if os.path.exists(SETTINGS_PATH):
                try:
                    with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
                        existing = json.load(f)
                except Exception:
                    existing = {}
            existing.update({
                "dhash_threshold": self.DHASH_THRESHOLD,
                "audio_gain": self.AUDIO_GAIN,
                "jpeg_quality": self.JPEG_QUALITY,
                "whisper_model": self.WHISPER_MODEL,
                "whisper_device": self.WHISPER_DEVICE,
                "whisper_compute": self.WHISPER_COMPUTE,
                "realtime_whisper_model": self.REALTIME_WHISPER_MODEL,
                "realtime_backend": self.REALTIME_BACKEND,
                "postprocess_backend": self.POSTPROCESS_BACKEND,
                "fast_asr_threads": self.FAST_ASR_THREADS,
                "echo_delay_ms": self.ECHO_DELAY_MS,
                "marker_hotkey": self.MARKER_HOTKEY,
                "capture_region": self.CAPTURE_REGION,
                "capture_max_edge": self.CAPTURE_MAX_EDGE,
                "ocr_enabled": self.OCR_ENABLED,
                "summary_provider": self.SUMMARY_PROVIDER,
                "summary_model": self.SUMMARY_MODEL,
            })
            with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
                json.dump(existing, f, indent=2, ensure_ascii=False)
        except Exception as e:
            print(f"[設定保存警告] {e}")

    # ------------------------------------------------------------------ UI

    def create_widgets(self):
        # ---- Menu bar ----
        menubar = tk.Menu(self.root)
        menu_settings = tk.Menu(menubar, tearoff=0)
        menu_settings.add_command(label="設定を開く...", command=self._open_settings_dialog)
        menu_settings.add_separator()
        menu_settings.add_command(label="設定をリセット", command=self._reset_settings)
        menubar.add_cascade(label="設定", menu=menu_settings)
        menu_rec = tk.Menu(menubar, tearoff=0)
        menu_rec.add_command(label="記録一覧を開く...",
                             command=self._open_recordings_browser)
        menubar.add_cascade(label="記録", menu=menu_rec)
        self.root.config(menu=menubar)

        frame_info = ttk.LabelFrame(self.root, text=" システム概要 ", padding=10)
        frame_info.pack(fill="x", padx=15, pady=(8, 4))
        ttk.Label(frame_info, justify="left", text=(
            "・スピーカー/マイク音声をリアルタイムMP3録音（ffmpegエンコード）\n"
            "・5秒ごとに画面変化を検知し、スライド切替時のみJPEG保存\n"
            "・高負荷モード: ローカルAIでリアルタイム文字起こし"
        )).pack(anchor="w")

        frame_set = ttk.LabelFrame(self.root, text=" 録画設定 ", padding=10)
        frame_set.pack(fill="x", padx=15, pady=4)

        ttk.Label(frame_set, text="会議名:").grid(row=0, column=0, sticky="w", pady=2)
        self.entry_meeting = ttk.Entry(frame_set, width=44)
        self.entry_meeting.grid(row=0, column=1, padx=10, pady=2)

        ttk.Label(frame_set, text="対象画面:").grid(row=1, column=0, sticky="nw", pady=2)
        mon_frame = ttk.Frame(frame_set)
        mon_frame.grid(row=1, column=1, padx=10, pady=2, sticky="w")
        self.combo_monitor = ttk.Combobox(mon_frame, width=42, state="readonly")
        self.combo_monitor.pack(anchor="w")
        self.combo_monitor.bind("<<ComboboxSelected>>", self._on_monitor_changed)

        region_row = ttk.Frame(mon_frame)
        region_row.pack(anchor="w", pady=(3, 0))
        self.btn_region = ttk.Button(region_row, text="🖱 範囲を選択...",
            command=self._select_capture_region)
        self.btn_region.pack(side="left")
        self.btn_region_clear = ttk.Button(region_row, text="🗑 解除",
            width=7, command=self._clear_capture_region)
        self.btn_region_clear.pack(side="left", padx=(4, 0))
        self.label_region = ttk.Label(region_row, text="", font=("BIZ UDゴシック", 8),
            foreground="#6b7280")
        self.label_region.pack(side="left", padx=(8, 0))

        ttk.Label(frame_set, text="音声デバイス:").grid(row=2, column=0, sticky="nw", pady=2)
        audio_frame = ttk.Frame(frame_set)
        audio_frame.grid(row=2, column=1, padx=10, pady=2, sticky="w")

        lb_row = ttk.Frame(audio_frame)
        lb_row.pack(fill="x")
        self._audio_scroll = ttk.Scrollbar(lb_row, orient="vertical")
        self.listbox_audio = tk.Listbox(
            lb_row, selectmode="extended", height=5, width=52,
            font=("BIZ UDゴシック", 9), exportselection=False,
            yscrollcommand=self._audio_scroll.set)
        self._audio_scroll.config(command=self.listbox_audio.yview)
        self.listbox_audio.pack(side="left", fill="x", expand=True)
        self._audio_scroll.pack(side="right", fill="y")

        ttk.Label(audio_frame,
            text="※ Ctrl/Shift+クリックで複数選択（スピーカー・マイク混在可）",
            font=("BIZ UDゴシック", 8), foreground="#6b7280").pack(anchor="w", pady=(2, 0))
        audio_btn_row = ttk.Frame(audio_frame)
        audio_btn_row.pack(anchor="w", pady=(4, 0))
        self.btn_refresh_audio = ttk.Button(audio_btn_row, text="🔄 デバイス再検出",
            command=self._refresh_audio_devices)
        self.btn_refresh_audio.pack(side="left")
        self.btn_select_all_audio = ttk.Button(audio_btn_row, text="🎚 すべて選択",
            command=self._select_all_audio)
        self.btn_select_all_audio.pack(side="left", padx=(6, 0))

        ttk.Label(frame_set, text="動作モード:").grid(row=3, column=0, sticky="w", pady=2)
        self.combo_mode = ttk.Combobox(frame_set, width=42, state="readonly",
            values=["軽量（録音のみ）", "高負荷（リアルタイム文字起こし）"])
        self.combo_mode.grid(row=3, column=1, padx=10, pady=2)
        self.combo_mode.current(0)
        self.combo_mode.bind("<<ComboboxSelected>>", self._on_mode_changed)

        ttk.Label(frame_set, text="文字起こし言語:").grid(row=4, column=0, sticky="w", pady=2)
        self.combo_lang = ttk.Combobox(frame_set, width=42, state="readonly",
            values=[label for label, _ in LANG_OPTIONS])
        self.combo_lang.grid(row=4, column=1, padx=10, pady=2)
        self.combo_lang.current(0)
        self.combo_lang.bind("<<ComboboxSelected>>", self._on_lang_changed)

        frame_level = ttk.Frame(self.root, padding=(15, 2))
        frame_level.pack(fill="x")
        self._show_level_var = tk.BooleanVar(value=True)
        self.chk_level = tk.Checkbutton(
            frame_level, text="入力レベル:", variable=self._show_level_var,
            font=("BIZ UDゴシック", 9), command=self._toggle_level_meter)
        self.chk_level.pack(side="left")
        self.level_canvas = tk.Canvas(frame_level, height=14, bg="#1f2937",
            highlightthickness=1, highlightbackground="#374151")
        self.level_canvas.pack(side="left", fill="x", expand=True, padx=(8, 0))

        self._detect_monitors()

        frame_ctrl = ttk.Frame(self.root, padding=5)
        frame_ctrl.pack(fill="x", padx=15, pady=4)

        row_buttons = ttk.Frame(frame_ctrl)
        row_buttons.pack(fill="x", pady=3)
        self.btn_toggle = tk.Button(
            row_buttons, text="▶ 会議記録を開始", bg="#10b981", fg="white",
            font=("BIZ UDゴシック", 12, "bold"), relief="raised", padx=8, pady=8,
            command=self._toggle_recording)
        self.btn_toggle.pack(side="left", fill="x", expand=True)

        self.btn_manual_snap = tk.Button(
            row_buttons, text="📷 手動キャプチャ", bg="#6366f1", fg="white",
            font=("BIZ UDゴシック", 10, "bold"), relief="raised", padx=10, pady=8,
            command=self._manual_snapshot, state="disabled")
        self.btn_manual_snap.pack(side="right", padx=(6, 0))

        self.btn_marker = tk.Button(
            row_buttons, text="⭐ マーカー", bg="#f59e0b", fg="white",
            font=("BIZ UDゴシック", 10, "bold"), relief="raised", padx=10, pady=8,
            command=self._add_marker, state="disabled")
        self.btn_marker.pack(side="right", padx=(6, 0))

        row_post = ttk.Frame(frame_ctrl)
        row_post.pack(fill="x", pady=(3, 0))
        self.btn_postprocess = tk.Button(
            row_post, text="📄 議事録を生成（後処理）", bg="#0ea5e9", fg="white",
            font=("BIZ UDゴシック", 10, "bold"), relief="raised", padx=10, pady=6,
            command=self._run_postprocess)
        self.btn_postprocess.pack(side="left", fill="x", expand=True)
        self.btn_import_video = tk.Button(
            row_post, text="🎬 動画から生成", bg="#8b5cf6", fg="white",
            font=("BIZ UDゴシック", 10, "bold"), relief="raised", padx=10, pady=6,
            command=self._run_video_import)
        self.btn_import_video.pack(side="left", padx=(6, 0))
        self.btn_cancel_post = tk.Button(
            row_post, text="✖ 中断", bg="#ef4444", fg="white",
            font=("BIZ UDゴシック", 10, "bold"), relief="raised", padx=10, pady=6,
            command=self._cancel_postprocess, state="disabled")
        self.btn_cancel_post.pack(side="right", padx=(6, 0))

        self.var_video_snapshots = tk.BooleanVar(value=True)
        self.chk_video_snapshots = ttk.Checkbutton(
            frame_ctrl, text="動画入力時、画面変化を画像として保存・議事録に表示",
            variable=self.var_video_snapshots)
        self.chk_video_snapshots.pack(anchor="w", pady=(3, 0))

        row_prog = ttk.Frame(frame_ctrl)
        row_prog.pack(fill="x", pady=(4, 0))
        self.progress_post = ttk.Progressbar(row_prog, maximum=1000, value=0)
        self.progress_post.pack(side="left", fill="x", expand=True)
        self.label_progress = ttk.Label(row_prog, text="", width=30, anchor="w",
            font=("BIZ UDゴシック", 8), foreground="#6b7280")
        self.label_progress.pack(side="right", padx=(8, 0))

        self.label_status = ttk.Label(frame_ctrl, text="ステータス: 停止中",
            font=("BIZ UDゴシック", 10, "bold"), foreground="#6b7280")
        self.label_status.pack(anchor="w", pady=2)

        frame_log = ttk.LabelFrame(self.root, text=" 動作ログ ", padding=5)
        frame_log.pack(fill="both", expand=True, padx=15, pady=4)
        self.log_area = scrolledtext.ScrolledText(
            frame_log, height=7, font=("Consolas", 9), state="disabled")
        self.log_area.pack(fill="both", expand=True)

        frame_tr = ttk.LabelFrame(self.root, text=" 文字起こし（高負荷モード） ", padding=5)
        frame_tr.pack(fill="both", expand=True, padx=15, pady=(4, 8))
        self.transcript_area = scrolledtext.ScrolledText(
            frame_tr, height=6, font=("BIZ UDゴシック", 10), state="disabled")
        self.transcript_area.pack(fill="both", expand=True)
        self._set_transcript_enabled(False)

    # --------------------------------------------------------- Audio device enumeration

    # Host APIs that expose the same physical mic. Earlier = preferred.
    _HOSTAPI_PRIORITY = ("Windows WASAPI", "Windows WDM-KS", "Windows DirectSound", "MME")
    _HOSTAPI_SHORT = {
        "Windows WASAPI": "WASAPI", "Windows WDM-KS": "WDM-KS",
        "Windows DirectSound": "DSound", "MME": "MME",
    }

    # Virtual endpoints that just alias whatever the default input is. Listing
    # them means "select all" captures the default mic two or three times over.
    _VIRTUAL_INPUTS = (
        "サウンド マッパー", "サウンドマッパー", "sound mapper",
        "プライマリ サウンド キャプチャ", "primary sound capture",
    )

    @classmethod
    def _is_virtual_input(cls, name):
        low = name.lower()
        return any(v in low for v in cls._VIRTUAL_INPUTS)

    @staticmethod
    def _dedupe_key(name):
        """Normalized key matching one physical device across host APIs.
        MME truncates names to 31 chars, so compare a short normalized prefix."""
        return re.sub(r"[^0-9a-z぀-ヿ一-鿿]+", "", name.lower())[:30]

    def _enumerate_audio_devices(self):
        """Enumerate all capturable audio devices into a unified list.
        Each entry: {key, kind, native_idx, name, channels, rate, api}
          key: 'S<native_idx>' (speaker/loopback) / 'M<native_idx>' (mic)

        Mics are deduplicated across host APIs (a single jack is otherwise listed
        under MME / DirectSound / WASAPI / WDM-KS): selecting every entry would
        open the same hardware several times — some opens fail, and the ones that
        succeed get mixed into each other.
        """
        devices = []
        speaker_names = set()

        # --- Speaker/loopback via pyaudiowpatch ---
        p = pyaudio.PyAudio()
        try:
            for lb in p.get_loopback_device_info_generator():
                if lb.get("maxInputChannels", 0) > 0:
                    name = lb["name"]
                    speaker_names.add(name)
                    devices.append({
                        "key": f"S{lb['index']}",
                        "kind": "speaker",
                        "native_idx": lb["index"],
                        "name": name,
                        "channels": int(lb["maxInputChannels"]),
                        "rate": int(lb["defaultSampleRate"]),
                        "api": "WASAPI",
                    })
        except Exception as e:
            print(f"[デバイス列挙警告: speaker] {e}")
        finally:
            p.terminate()

        # --- Mic/input via sounddevice ---
        try:
            try:
                hostapis = list(sd.query_hostapis())
            except Exception:
                hostapis = []
            best = {}      # dedupe key -> (priority, entries from that API)
            order = []     # dedupe keys in first-seen order
            skipped = 0
            for info in sd.query_devices():
                if info.get("max_input_channels", 0) <= 0:
                    continue
                name = info["name"]
                # Skip WASAPI loopbacks sounddevice also surfaces as inputs, and
                # any input already listed on the speaker side (dedupe by name).
                if "Loopback" in name or name in speaker_names:
                    continue
                if self._is_virtual_input(name):
                    continue
                api_idx = info.get("hostapi", -1)
                api_name = hostapis[api_idx]["name"] if 0 <= api_idx < len(hostapis) else ""
                try:
                    prio = self._HOSTAPI_PRIORITY.index(api_name)
                except ValueError:
                    prio = len(self._HOSTAPI_PRIORITY)
                entry = {
                    "key": f"M{info['index']}",
                    "kind": "mic",
                    "native_idx": info["index"],
                    "name": name,
                    "channels": int(info["max_input_channels"]),
                    "rate": int(info["default_samplerate"]),
                    "api": self._HOSTAPI_SHORT.get(api_name, api_name),
                }
                k = self._dedupe_key(name)
                if k not in best:
                    best[k] = (prio, [entry])
                    order.append(k)
                else:
                    old_prio, entries = best[k]
                    if prio == old_prio:
                        # Two identical USB microphones can have identical
                        # names. Distinct indices within one API are real
                        # endpoints, not aliases of the same capture stream.
                        entries.append(entry)
                    elif prio < old_prio:
                        skipped += len(entries)
                        best[k] = (prio, [entry])
                    else:
                        skipped += 1
            devices.extend(entry for k in order for entry in best[k][1])
            if skipped:
                print(f"[デバイス列挙] 別ホストAPIの重複マイク {skipped}件を非表示")
        except Exception as e:
            print(f"[デバイス列挙警告: mic] {e}")

        self._audio_devices = devices

    def _populate_audio_listbox(self):
        """Reflect self._audio_devices into the Listbox, then apply default selection."""
        if not getattr(self, "listbox_audio", None):
            return
        self.listbox_audio.delete(0, tk.END)
        for dev in self._audio_devices:
            prefix = "🔊 " if dev["kind"] == "speaker" else "🎤 "
            api = dev.get("api", "")
            suffix = f"  [{api}]" if api and api != "WASAPI" else ""
            self.listbox_audio.insert(tk.END, f"{prefix}{dev['name']}{suffix}")
        # Default selection: first speaker + first mic (mirrors prior "both on")
        first_speaker = first_mic = None
        for i, dev in enumerate(self._audio_devices):
            if dev["kind"] == "speaker" and first_speaker is None:
                first_speaker = i
            elif dev["kind"] == "mic" and first_mic is None:
                first_mic = i
        for idx in (first_speaker, first_mic):
            if idx is not None:
                self.listbox_audio.selection_set(idx)

    def _refresh_audio_devices(self):
        if self.is_recording:
            return  # devices are locked while recording
        self._enumerate_audio_devices()
        self._populate_audio_listbox()
        n_spk = sum(1 for d in self._audio_devices if d["kind"] == "speaker")
        n_mic = sum(1 for d in self._audio_devices if d["kind"] == "mic")
        self._log(f"オーディオデバイス再検出: スピーカー{n_spk} / マイク{n_mic}")

    def _select_all_audio(self):
        """Select every listed device. Dead/idle ones are tolerated at capture time."""
        if self.is_recording:
            return
        self.listbox_audio.selection_set(0, tk.END)
        self._log(f"音声デバイスを全選択: {self.listbox_audio.size()}件")

    def _selected_audio_devices(self):
        """Return list of device dicts currently selected in the Listbox."""
        sel = self.listbox_audio.curselection()
        return [self._audio_devices[i] for i in sel]

    @staticmethod
    def _audio_source_label_n(selected):
        """Backward-compatible AUDIO_SOURCE string for metadata.txt.
        1 speaker / 1 mic / 1+1 map to legacy values; otherwise a readable summary.
        """
        n_speaker = sum(1 for d in selected if d["kind"] == "speaker")
        n_mic = sum(1 for d in selected if d["kind"] == "mic")
        if n_speaker == 1 and n_mic == 0:
            return "speaker_loopback"
        if n_speaker == 0 and n_mic == 1:
            return "microphone"
        if n_speaker == 1 and n_mic == 1:
            return "both_mixed"
        parts = []
        if n_speaker:
            parts.append(f"speaker_loopback x{n_speaker}")
        if n_mic:
            parts.append(f"microphone x{n_mic}")
        return " + ".join(parts) if parts else "none"

    # ---------------------------------------------------- Level meter & timer

    def _toggle_level_meter(self):
        if self._show_level_var.get():
            self.level_canvas.pack(side="left", fill="x", expand=True, padx=(8, 0))
        else:
            self.level_canvas.pack_forget()

    def _tick_level_meter(self):
        if self._show_level_var.get():
            self.level_canvas.delete("all")
            w = self.level_canvas.winfo_width()
            if w > 1:
                level = self._audio_level
                bar_w = max(1, int(level * w))
                color = "#10b981" if level < 0.6 else "#f59e0b" if level < 0.85 else "#ef4444"
                self.level_canvas.create_rectangle(0, 0, bar_w, 14, fill=color, outline="")
        self.root.after(80, self._tick_level_meter)

    def _update_level(self, pcm_bytes):
        samples = np.frombuffer(pcm_bytes, dtype=np.int16)
        if len(samples) > 0:
            rms = np.sqrt(np.mean(samples.astype(np.float32) ** 2)) / 32768.0
            self._audio_level = min(1.0, rms * 3.0)

    def _tick_elapsed_timer(self):
        if self.is_recording:
            elapsed = time.time() - self._record_start
            m, s = divmod(int(elapsed), 60)
            h, m = divmod(m, 60)
            self.label_status.config(
                text=f"ステータス: 記録中  {h:02d}:{m:02d}:{s:02d}",
                foreground="#ef4444")
            self.root.after(500, self._tick_elapsed_timer)

    # ------------------------------------------------ Mode change & pre-load

    def _on_mode_changed(self, _=None):
        is_full = self.combo_mode.current() == 1
        self._set_transcript_enabled(is_full)
        if is_full and self._preloaded_transcriber is None:
            self._preload_model()

    def _preload_model(self):
        def _load():
            try:
                if self.REALTIME_BACKEND == "fast_ja_en":
                    from .asr.fast import FastJapaneseEnglishASR
                    self.root.after(0, self._log,
                        "高速日本語/英語モデルを事前読み込み中...")
                    model_dir = MODELS_DIR
                    hotwords, replacements = self._prepare_fast_glossary(model_dir)
                    self._preloaded_transcriber = FastJapaneseEnglishASR(
                        model_dir, threads=self.FAST_ASR_THREADS,
                        hotwords_file=hotwords, replacements=replacements)
                    self.root.after(0, self._log, "高速日本語/英語モデル読み込み完了")
                else:
                    from faster_whisper import WhisperModel
                    self.root.after(0, self._log,
                        f"文字起こしモデルを事前読み込み中... ({self.REALTIME_WHISPER_MODEL})")
                    self._preloaded_transcriber = WhisperModel(
                        self.REALTIME_WHISPER_MODEL,
                        device=self.WHISPER_DEVICE, compute_type=self.WHISPER_COMPUTE)
                    self.root.after(0, self._log,
                        f"文字起こしモデル読み込み完了 ({self.REALTIME_WHISPER_MODEL})")
            except Exception as e:
                self.root.after(0, self._log, f"[事前読み込みエラー] {e}")
        threading.Thread(target=_load, daemon=True).start()

    @staticmethod
    def _prepare_fast_glossary(model_dir):
        terms = load_glossary()
        replacements = {}
        forms = []
        for term in terms:
            form = term.get("form", "").strip()
            if not form:
                continue
            forms.append(form)
            for alias in term.get("aliases", []):
                if alias and alias != form:
                    replacements[alias] = form
        path = os.path.join(model_dir, "gijiroku_hotwords.txt")
        os.makedirs(model_dir, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(forms))
        # This ReazonSpeech export uses byte-BPE tokens, so sherpa-onnx cannot
        # encode ordinary Japanese CJK hotword lines for modified beam search.
        # Keep the generated list for inspection/future model upgrades, while
        # applying the glossary aliases deterministically after every draft,
        # final, and refine decode (which works for both ja and en).
        return "", replacements

    def _on_lang_changed(self, _=None):
        label = LANG_OPTIONS[self.combo_lang.current()][0]
        lang = LANG_OPTIONS[self.combo_lang.current()][1]
        # A single faster-whisper model handles ja/en/etc., so no model
        # reload is needed on language change — just update the target lang.
        self._rt_lang = None if lang == "auto" else lang
        self._log(f"文字起こし言語設定: {label}")

        if self.is_recording:
            self._write_language_event(lang, label)
        else:
            # Pre-load for next recording (no-op if already preloaded)
            if self.combo_mode.current() == 1 and self._preloaded_transcriber is None:
                self._preload_model()

    def _run_postprocess(self, folder=None):
        if self._post_running:
            messagebox.showinfo("実行中", "後処理がすでに実行中です。")
            return
        if folder is None:
            initial_dir = self._last_record_dir if self._last_record_dir else None
            folder = filedialog.askdirectory(
                title="議事録生成対象フォルダを選択", initialdir=initial_dir)
        if not folder:
            return
        self._post_running = True
        self._post_cancel.clear()
        self.btn_postprocess.config(state="disabled", text="⏳ 処理中...")
        self.btn_import_video.config(state="disabled")
        self.btn_cancel_post.config(state="normal", text="✖ 中断")
        self.progress_post.config(value=0)
        self.label_status.config(text="ステータス: 議事録生成中...", foreground="#f59e0b")
        threading.Thread(target=self._postprocess_worker, args=(folder,), daemon=True).start()

    def _run_video_import(self):
        """Select a video, extract its audio, then run the normal post-process."""
        if self._post_running:
            messagebox.showinfo("実行中", "後処理がすでに実行中です。")
            return
        video_path = filedialog.askopenfilename(
            title="議事録を生成する動画を選択",
            filetypes=[
                ("動画ファイル", "*.mp4 *.mov *.mkv *.webm *.avi *.m4v *.mts *.m2ts"),
                ("すべてのファイル", "*.*"),
            ])
        if not video_path:
            return
        if not os.path.exists(FFMPEG_PATH):
            messagebox.showerror("ffmpegが見つかりません",
                f"動画の音声抽出にffmpeg.exeが必要です。\n\n期待パス:\n{FFMPEG_PATH}")
            return

        lang = LANG_OPTIONS[self.combo_lang.current()][1]
        capture_scenes = self.var_video_snapshots.get()
        self._post_running = True
        self._post_cancel.clear()
        self.btn_postprocess.config(state="disabled")
        self.btn_import_video.config(state="disabled", text="⏳ 取込中...")
        self.btn_cancel_post.config(state="normal", text="✖ 中断")
        self.progress_post.config(value=0)
        self.label_status.config(text="ステータス: 動画を取込中...", foreground="#8b5cf6")
        threading.Thread(target=self._video_import_worker,
                         args=(video_path, lang, capture_scenes), daemon=True).start()

    def _video_import_worker(self, video_path, lang, capture_scenes):
        result = "失敗"
        try:
            folder = import_video_file(
                video_path, language=lang, progress=self._post_progress,
                cancel=self._post_cancel.is_set, capture_scenes=capture_scenes,
                scene_interval=self.INTERVAL,
                scene_threshold=self.DHASH_THRESHOLD,
                max_edge=self.CAPTURE_MAX_EDGE,
                jpeg_quality=self.JPEG_QUALITY)
            self._last_record_dir = folder
            post_process_folder(folder, progress=self._post_progress,
                                cancel=self._post_cancel.is_set)
            result = "成功"
        except PostProcessCancelled:
            result = "中断"
        except Exception as e:
            self.root.after(0, self._log, f"[動画取込エラー] {e}")
            traceback.print_exc()
        finally:
            self._post_running = False
            self.root.after(0, self._log, f"動画からの議事録生成完了 ({result})")
            self.root.after(0, self._finish_postprocess_ui, result)

    def _cancel_postprocess(self):
        """Ask the worker to stop; it checks the flag at each phase boundary."""
        self._post_cancel.set()
        self.btn_cancel_post.config(state="disabled", text="中断中...")
        self._log("後処理の中断を要求しました（現在の処理が終わり次第停止します）")

    def _post_progress(self, frac, msg):
        """Progress callback — called from the worker thread."""
        self.root.after(0, self._apply_post_progress, frac, msg)

    def _apply_post_progress(self, frac, msg):
        if frac is not None:
            self.progress_post.config(value=max(0, min(1000, int(frac * 1000))))
        short = msg if len(msg) <= 30 else msg[:29] + "…"
        self.label_progress.config(text=short)
        self._log(f"[後処理] {msg}")

    def _postprocess_worker(self, folder):
        result = "失敗"
        try:
            post_process_folder(folder, progress=self._post_progress,
                                cancel=self._post_cancel.is_set)
            result = "成功"
        except PostProcessCancelled:
            result = "中断"
        except Exception as e:
            self.root.after(0, self._log, f"[後処理エラー] {e}")
            traceback.print_exc()
        finally:
            self._post_running = False
            self.root.after(0, self._log, f"後処理完了 ({result})")
            self.root.after(0, self._finish_postprocess_ui, result)

    def _finish_postprocess_ui(self, result):
        self.btn_postprocess.config(state="normal", text="📄 議事録を生成（後処理）")
        self.btn_import_video.config(state="normal", text="🎬 動画から生成")
        self.btn_cancel_post.config(state="disabled", text="✖ 中断")
        self.label_progress.config(text="" if result == "成功" else result)
        if result != "成功":
            self.progress_post.config(value=0)
        if not self.is_recording:
            self.label_status.config(text="ステータス: 停止中", foreground="#6b7280")

    # ------------------------------------------------------- Capture region

    @staticmethod
    def _display_scale(root, sct):
        """Physical pixels per tkinter unit.

        1.0 for a DPI-unaware process (Windows virtualizes both tkinter and the
        screen grab identically), but a DPI-aware host would make tkinter report
        logical units while mss keeps reporting physical ones.
        """
        try:
            logical = root.winfo_screenwidth()
            physical = sct.monitors[1]["width"] if len(sct.monitors) > 1 else logical
            if logical > 0 and physical > 0:
                return physical / logical
        except Exception as e:
            print(f"[DPI取得警告] {e}")
        return 1.0

    def _is_region_mode(self):
        return self.combo_monitor.get() == REGION_CHOICE

    def _update_region_label(self):
        region = self.CAPTURE_REGION
        if region:
            self.label_region.config(
                text=f"{region['width']}×{region['height']} "
                     f"(x={region['left']}, y={region['top']})",
                foreground="#0ea5e9")
        else:
            self.label_region.config(text="未選択", foreground="#6b7280")
        on = self._is_region_mode() and not self.is_recording
        self.btn_region.config(state="normal" if on else "disabled")
        self.btn_region_clear.config(
            state="normal" if on and region else "disabled")

    def _on_monitor_changed(self, _=None):
        self._update_region_label()
        if self._is_region_mode() and not self.CAPTURE_REGION:
            self._select_capture_region()

    def _clear_capture_region(self):
        self.CAPTURE_REGION = None
        self._save_settings()
        self._update_region_label()
        self._log("キャプチャ範囲を解除しました")

    def _select_capture_region(self):
        """Freeze the screen, dim it, and let the user drag out a rectangle.

        The overlay shows a still screenshot rather than being see-through: a
        genuinely transparent window would be click-through on Windows, which
        would swallow the drag we are trying to capture.
        """
        if self.is_recording:
            return
        try:
            with mss.MSS() as sct:
                virt = dict(sct.monitors[0])
                scale = self._display_scale(self.root, sct)
                shot = sct.grab(virt)
            base = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
        except Exception as e:
            messagebox.showerror("エラー", f"画面を取得できません:\n{e}")
            return

        win_w = max(1, int(round(virt["width"] / scale)))
        win_h = max(1, int(round(virt["height"] / scale)))
        win_x = int(round(virt["left"] / scale))
        win_y = int(round(virt["top"] / scale))
        if (win_w, win_h) != base.size:
            base = base.resize((win_w, win_h), Image.LANCZOS)

        win = tk.Toplevel(self.root)
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        win.geometry(f"{win_w}x{win_h}+{win_x}+{win_y}")
        canvas = tk.Canvas(win, width=win_w, height=win_h, highlightthickness=0,
                           cursor="crosshair", bg="black")
        canvas.pack()
        photo = ImageTk.PhotoImage(base)
        canvas.create_image(0, 0, image=photo, anchor="nw")
        canvas.image = photo   # keep a reference or Tk drops the image

        # Four dimmed panels around the selection give a real "hole" without
        # any window transparency, and updating them is four coordinate sets.
        shades = [canvas.create_rectangle(0, 0, 0, 0, fill="black",
                                          stipple="gray75", outline="")
                  for _ in range(4)]
        box = canvas.create_rectangle(0, 0, 0, 0, outline="#38bdf8", width=2)
        size_text = canvas.create_text(0, 0, text="", anchor="nw", fill="#ffffff",
                                       font=("BIZ UDゴシック", 12, "bold"))
        canvas.create_text(win_w // 2, 20, anchor="n", fill="#ffffff",
                           font=("BIZ UDゴシック", 13, "bold"),
                           text="ドラッグして範囲を選択    /    Esc でキャンセル")

        def _shade(x0, y0, x1, y1):
            canvas.coords(shades[0], 0, 0, win_w, y0)          # above
            canvas.coords(shades[1], 0, y1, win_w, win_h)      # below
            canvas.coords(shades[2], 0, y0, x0, y1)            # left
            canvas.coords(shades[3], x1, y0, win_w, y1)        # right

        # Before the first drag there is no hole to leave, and the four-panel
        # split cannot cover the screen on its own — stretch one panel over it.
        canvas.coords(shades[0], 0, 0, win_w, win_h)
        state = {"x0": 0, "y0": 0, "dragging": False, "rect": None}

        def _corners(event):
            return (min(state["x0"], event.x), min(state["y0"], event.y),
                    max(state["x0"], event.x), max(state["y0"], event.y))

        def _on_press(event):
            state.update(x0=event.x, y0=event.y, dragging=True)

        def _on_move(event):
            if not state["dragging"]:
                return
            x0, y0, x1, y1 = _corners(event)
            canvas.coords(box, x0, y0, x1, y1)
            _shade(x0, y0, x1, y1)
            canvas.itemconfig(size_text, text=f"{int((x1 - x0) * scale)} × "
                                              f"{int((y1 - y0) * scale)}")
            canvas.coords(size_text, x0 + 6, y0 + 6 if y0 + 30 < win_h else y0 - 26)

        def _on_release(event):
            if not state["dragging"]:
                return
            state["dragging"] = False
            x0, y0, x1, y1 = _corners(event)
            state["rect"] = {
                "left": int(round(virt["left"] + x0 * scale)),
                "top": int(round(virt["top"] + y0 * scale)),
                "width": int(round((x1 - x0) * scale)),
                "height": int(round((y1 - y0) * scale)),
            }
            win.destroy()

        canvas.bind("<ButtonPress-1>", _on_press)
        canvas.bind("<B1-Motion>", _on_move)
        canvas.bind("<ButtonRelease-1>", _on_release)
        # A borderless topmost window has no close button, so cancelling must
        # not hinge on one binding landing: Escape from anywhere in the app,
        # and right-click on the overlay itself.
        cancel = lambda _e=None: win.destroy()
        for widget in (win, canvas):
            widget.bind("<Escape>", cancel)
            widget.bind("<Button-3>", cancel)
        win.bind_all("<Escape>", cancel)

        def _poll_escape():
            """Watch the physical Escape key, not just Tk's focused widget.

            A borderless topmost window can end up without keyboard focus — if
            that happens the key bindings never fire and the overlay would be
            unclosable, so ask Windows directly instead.
            """
            if not win.winfo_exists():
                return
            try:
                import ctypes
                if ctypes.windll.user32.GetAsyncKeyState(0x1B) & 0x8000:  # VK_ESCAPE
                    win.destroy()
                    return
            except Exception as e:
                print(f"[範囲選択警告] Escape 監視を停止: {e}")
                return
            win.after(60, _poll_escape)

        if os.name == "nt":
            win.after(60, _poll_escape)
        win.focus_force()
        canvas.focus_set()
        win.grab_set()
        try:
            self.root.wait_window(win)
        finally:
            try:
                win.unbind_all("<Escape>")
            except tk.TclError:
                pass

        rect = state["rect"]
        if not rect:
            self._log("範囲選択をキャンセルしました")
            self._update_region_label()
            return
        if rect["width"] < MIN_REGION or rect["height"] < MIN_REGION:
            messagebox.showwarning("範囲が小さすぎます",
                f"{MIN_REGION}×{MIN_REGION} ピクセル以上を選択してください。\n"
                f"（選択: {rect['width']}×{rect['height']}）")
            self._update_region_label()
            return

        self.CAPTURE_REGION = rect
        if not self._is_region_mode():
            values = list(self.combo_monitor["values"])
            if REGION_CHOICE in values:
                self.combo_monitor.current(values.index(REGION_CHOICE))
        self._save_settings()
        self._update_region_label()
        self._log(f"キャプチャ範囲: {rect['width']}×{rect['height']} "
                  f"(x={rect['left']}, y={rect['top']})")

    def _capture_rect(self, sct):
        """The rectangle to grab — a chosen region, or the selected display."""
        if self._is_region_mode() and self.CAPTURE_REGION:
            return dict(self.CAPTURE_REGION)
        idx = min(max(self._recording_mon_idx, 0), len(sct.monitors) - 1)
        return dict(sct.monitors[idx])

    def _grab_image(self, sct, rect):
        """Grab a frame and shrink it if its long edge exceeds the limit."""
        shot = sct.grab(rect)
        img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
        limit = self.CAPTURE_MAX_EDGE
        if limit and max(img.size) > limit:
            img.thumbnail((limit, limit), Image.LANCZOS)
        return img

    def _validate_region(self):
        """Reject a stale region — displays get unplugged and resolutions change."""
        if not (self._is_region_mode() and self.CAPTURE_REGION):
            return True
        region = self.CAPTURE_REGION
        with mss.MSS() as sct:
            virt = sct.monitors[0]
        inside = (region["left"] >= virt["left"]
                  and region["top"] >= virt["top"]
                  and region["left"] + region["width"] <= virt["left"] + virt["width"]
                  and region["top"] + region["height"] <= virt["top"] + virt["height"])
        if inside:
            return True
        return messagebox.askyesno(
            "範囲が画面外です",
            f"保存されている範囲 ({region['left']},{region['top']} "
            f"{region['width']}×{region['height']}) が現在の画面に収まりません。\n"
            "ディスプレイ構成が変わった可能性があります。\n\n"
            "このまま録画を開始しますか？（画面外は黒く記録されます）")

    # ------------------------------------------------------ Recordings browser

    def _scan_recordings(self):
        """List recording folders under the app directory and the year folders.

        Scanned lazily when the window opens, not at startup — a few hundred
        meetings would otherwise slow every launch.
        """
        roots = [BASE_DIR]
        try:
            for name in os.listdir(BASE_DIR):
                path = os.path.join(BASE_DIR, name)
                if os.path.isdir(path) and re.fullmatch(r"\d{6,8}", name):
                    roots.append(path)   # e.g. an IC-recorder import folder
        except OSError as e:
            print(f"[記録一覧警告] {e}")

        seen = set()
        rows = []
        for root in roots:
            try:
                names = sorted(os.listdir(root), reverse=True)
            except OSError:
                continue
            for name in names:
                path = os.path.join(root, name)
                if path in seen or not os.path.isdir(path):
                    continue
                if not any(os.path.exists(os.path.join(path, a))
                           for a in ("audio_main.mp3", "audio_main.wav")):
                    continue
                seen.add(path)
                meta = _read_metadata(path)
                images = sum(1 for f in os.listdir(path)
                             if f.lower().endswith((".jpg", ".jpeg", ".png")))
                rows.append({
                    "path": path,
                    "name": meta.get("MEETING_NAME") or name,
                    "start": meta.get("START_TIME_STR", ""),
                    "images": images,
                    "markers": len(_read_jsonl(os.path.join(path, "markers.jsonl"))),
                    "roles": bool(meta.get("ROLE_TRACKS")),
                    "report": os.path.exists(os.path.join(path, "meeting_report.md")),
                    "html": os.path.exists(os.path.join(path, "meeting_report.html")),
                    "final_html": os.path.exists(os.path.join(path, "transcription_final.html")),
                })
        rows.sort(key=lambda r: (r["start"] or "", r["path"]), reverse=True)
        return rows

    def _open_recordings_browser(self):
        if getattr(self, "_browser_win", None) is not None:
            try:
                self._browser_win.lift()
                return
            except tk.TclError:
                pass

        win = tk.Toplevel(self.root)
        self._browser_win = win
        win.title("GijirokuStudio - 記録一覧")
        win.geometry("900x520")
        win.transient(self.root)

        top = ttk.Frame(win, padding=(10, 8))
        top.pack(fill="x")
        ttk.Label(top, text="検索:").pack(side="left")
        var_q = tk.StringVar()
        entry_q = ttk.Entry(top, width=30, textvariable=var_q)
        entry_q.pack(side="left", padx=(6, 10))
        var_full = tk.BooleanVar(value=False)
        ttk.Checkbutton(top, text="議事録の本文も検索", variable=var_full).pack(side="left")
        lbl_count = ttk.Label(top, text="", foreground="#6b7280")
        lbl_count.pack(side="right")

        cols = ("start", "name", "images", "markers", "roles", "report")
        tree = ttk.Treeview(win, columns=cols, show="headings", selectmode="browse")
        for col, text, width in (
                ("start", "開始時刻", 150), ("name", "会議名", 300),
                ("images", "画像", 60), ("markers", "⭐", 50),
                ("roles", "話者分離", 80), ("report", "議事録", 150)):
            tree.heading(col, text=text)
            tree.column(col, width=width, anchor="w")
        scroll = ttk.Scrollbar(win, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=scroll.set)
        tree.pack(side="left", fill="both", expand=True, padx=(10, 0), pady=4)
        scroll.pack(side="left", fill="y", padx=(0, 10), pady=4)

        rows = []
        by_item = {}

        def _matches(row, query, full_text):
            if not query:
                return True
            low = query.lower()
            if low in row["name"].lower() or low in row["start"].lower():
                return True
            if not full_text:
                return False
            for fname in ("meeting_report.md", "transcription_final.txt", "transcription.txt"):
                path = os.path.join(row["path"], fname)
                if not os.path.exists(path):
                    continue
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        if low in f.read().lower():
                            return True
                except OSError:
                    pass
            return False

        def _refill(*_):
            query = var_q.get().strip()
            tree.delete(*tree.get_children())
            by_item.clear()
            shown = 0
            for row in rows:
                if not _matches(row, query, var_full.get()):
                    continue
                if row["report"]:
                    report = "議事録 md + html" if row["html"] else "議事録 md"
                elif row["final_html"]:
                    report = "補正済みHTML"
                else:
                    report = "未生成"
                item = tree.insert("", "end", values=(
                    row["start"] or "-", row["name"], row["images"],
                    row["markers"] or "", "✓" if row["roles"] else "",
                    report))
                by_item[item] = row
                shown += 1
            lbl_count.config(text=f"{shown} / {len(rows)} 件")

        def _reload():
            rows.clear()
            rows.extend(self._scan_recordings())
            _refill()

        def _selected():
            sel = tree.selection()
            return by_item.get(sel[0]) if sel else None

        def _open_folder():
            row = _selected()
            if row:
                os.startfile(row["path"])

        def _open_report(kind):
            row = _selected()
            if not row:
                return
            path = os.path.join(row["path"], f"meeting_report.{kind}")
            if os.path.exists(path):
                os.startfile(path)
            else:
                messagebox.showinfo("未生成",
                    f"meeting_report.{kind} がありません。先に後処理を実行してください。")

        def _open_final_html():
            row = _selected()
            if not row:
                return
            path = os.path.join(row["path"], "transcription_final.html")
            if os.path.exists(path):
                os.startfile(path)
            else:
                messagebox.showinfo("未生成",
                    "transcription_final.html がありません。\n"
                    "高速日本語/英語ASRで録音した記録に生成されます。")

        def _run_post():
            row = _selected()
            if not row:
                return
            win.destroy()
            self._browser_win = None
            self._run_postprocess(row["path"])

        bar = ttk.Frame(win, padding=(10, 8))
        bar.pack(side="bottom", fill="x")
        for text, cmd in (("📄 議事録を生成/再生成", _run_post),
                          ("⚡ 補正済みHTML", _open_final_html),
                          ("📝 Markdown", lambda: _open_report("md")),
                          ("🌐 HTML", lambda: _open_report("html")),
                          ("📁 フォルダを開く", _open_folder),
                          ("🔄 再スキャン", _reload)):
            ttk.Button(bar, text=text, command=cmd).pack(side="left", padx=(0, 6))

        # Trace the variable rather than <KeyRelease> so paste, clear, and
        # programmatic edits all refresh the list.
        var_q.trace_add("write", _refill)
        var_full.trace_add("write", _refill)
        tree.bind("<Double-1>", lambda _e: _run_post())

        def _on_close():
            self._browser_win = None
            win.destroy()

        win.protocol("WM_DELETE_WINDOW", _on_close)
        _reload()

    def _open_settings_dialog(self):
        dlg = tk.Toplevel(self.root)
        dlg.title("GijirokuStudio - 設定")
        dlg.resizable(False, False)
        dlg.transient(self.root)
        dlg.grab_set()

        frame = ttk.LabelFrame(dlg, text=" 詳細設定 ", padding=15)
        frame.pack(padx=15, pady=15)

        # dHash threshold
        ttk.Label(frame, text="差分感度（dHash閾値）:").grid(row=0, column=0, sticky="w", pady=4)
        var_dhash = tk.IntVar(value=self.DHASH_THRESHOLD)
        scale_dhash = tk.Scale(frame, from_=1, to=30, orient="horizontal",
            variable=var_dhash, length=220, showvalue=False)
        scale_dhash.grid(row=0, column=1, padx=(10, 4), pady=4)
        lbl_dhash = ttk.Label(frame, text=str(self.DHASH_THRESHOLD), width=4)
        lbl_dhash.grid(row=0, column=2, pady=4)
        var_dhash.trace_add("write", lambda *_: lbl_dhash.config(text=str(var_dhash.get())))

        # Audio gain
        ttk.Label(frame, text="音声ゲイン:").grid(row=1, column=0, sticky="w", pady=4)
        var_gain = tk.DoubleVar(value=self.AUDIO_GAIN)
        scale_gain = tk.Scale(frame, from_=1.0, to=5.0, resolution=0.1,
            orient="horizontal", variable=var_gain, length=220, showvalue=False)
        scale_gain.grid(row=1, column=1, padx=(10, 4), pady=4)
        lbl_gain = ttk.Label(frame, text=f"{self.AUDIO_GAIN:.1f}", width=4)
        lbl_gain.grid(row=1, column=2, pady=4)
        var_gain.trace_add("write", lambda *_: lbl_gain.config(text=f"{var_gain.get():.1f}"))

        # JPEG quality
        ttk.Label(frame, text="JPEG画質:").grid(row=2, column=0, sticky="w", pady=4)
        var_jpeg = tk.IntVar(value=self.JPEG_QUALITY)
        scale_jpeg = tk.Scale(frame, from_=50, to=100, orient="horizontal",
            variable=var_jpeg, length=220, showvalue=False)
        scale_jpeg.grid(row=2, column=1, padx=(10, 4), pady=4)
        lbl_jpeg = ttk.Label(frame, text=str(self.JPEG_QUALITY), width=4)
        lbl_jpeg.grid(row=2, column=2, pady=4)
        var_jpeg.trace_add("write", lambda *_: lbl_jpeg.config(text=str(var_jpeg.get())))

        # Capture long-edge limit
        ttk.Label(frame, text="画像の長辺上限:").grid(row=3, column=0, sticky="w", pady=4)
        var_edge = tk.IntVar(value=self.CAPTURE_MAX_EDGE)
        scale_edge = tk.Scale(frame, from_=0, to=3840, resolution=160,
            orient="horizontal", variable=var_edge, length=220, showvalue=False)
        scale_edge.grid(row=3, column=1, padx=(10, 4), pady=4)
        lbl_edge = ttk.Label(frame, text=str(self.CAPTURE_MAX_EDGE), width=5)
        lbl_edge.grid(row=3, column=2, pady=4)
        var_edge.trace_add("write", lambda *_: lbl_edge.config(
            text=(str(var_edge.get()) if var_edge.get() else "無制限")))
        ttk.Label(frame, text="※ 0 で無効。超えた場合だけ縮小します",
            font=("BIZ UDゴシック", 8), foreground="#6b7280").grid(
            row=4, column=0, columnspan=3, sticky="w")

        # Slide OCR
        var_ocr = tk.BooleanVar(value=self.OCR_ENABLED)
        ttk.Checkbutton(frame, text="スライドOCR（後処理で画像から文字を抽出）",
            variable=var_ocr).grid(row=5, column=0, columnspan=3, sticky="w",
                                   pady=(8, 0))
        ttk.Label(frame, text="※ Windows の日本語OCR言語パックが必要です",
            font=("BIZ UDゴシック", 8), foreground="#6b7280").grid(
            row=6, column=0, columnspan=3, sticky="w")

        ttk.Label(frame, text="リアルタイムASR:").grid(
            row=7, column=0, sticky="w", pady=(10, 4))
        current_rt = next((i for i, (_, v) in enumerate(REALTIME_BACKENDS)
                           if v == self.REALTIME_BACKEND), 0)
        combo_rt = ttk.Combobox(frame, width=32, state="readonly",
            values=[label for label, _ in REALTIME_BACKENDS])
        combo_rt.grid(row=7, column=1, columnspan=2, padx=(10, 0), pady=(10, 4))
        combo_rt.current(current_rt)
        ttk.Label(frame, text="※ 高速版の初回導入: python setup_fast_asr.py",
            font=("BIZ UDゴシック", 8), foreground="#6b7280").grid(
            row=8, column=0, columnspan=3, sticky="w")

        ttk.Label(frame, text="後処理ASR:").grid(
            row=9, column=0, sticky="w", pady=(10, 4))
        current_post = next((i for i, (_, v) in enumerate(REALTIME_BACKENDS)
                             if v == self.POSTPROCESS_BACKEND), 1)
        combo_post = ttk.Combobox(frame, width=32, state="readonly",
            values=[label for label, _ in REALTIME_BACKENDS])
        combo_post.grid(row=9, column=1, columnspan=2, padx=(10, 0), pady=(10, 4))
        combo_post.current(current_post)

        # AI summary
        sum_frame = ttk.LabelFrame(dlg, text=" AI要約（後処理） ", padding=15)
        sum_frame.pack(padx=15, pady=(0, 10), fill="x")

        ttk.Label(sum_frame, text="プロバイダ:").grid(row=0, column=0, sticky="w", pady=4)
        current = next((i for i, (_, v) in enumerate(SUMMARY_PROVIDERS)
                        if v == self.SUMMARY_PROVIDER), 0)
        combo_prov = ttk.Combobox(sum_frame, width=34, state="readonly",
            values=[label for label, _ in SUMMARY_PROVIDERS])
        combo_prov.grid(row=0, column=1, padx=(10, 0), pady=4)
        combo_prov.current(current)

        ttk.Label(sum_frame, text="モデル:").grid(row=1, column=0, sticky="w", pady=4)
        entry_model = ttk.Entry(sum_frame, width=36)
        entry_model.grid(row=1, column=1, padx=(10, 0), pady=4)
        entry_model.insert(0, self.SUMMARY_MODEL)

        ttk.Label(sum_frame,
            text="※ 空欄で既定（Claude: claude-opus-5 / Ollama: qwen3）\n"
                 "※ Claude API を選ぶと文字起こしが外部に送信されます",
            font=("BIZ UDゴシック", 8), foreground="#b45309",
            justify="left").grid(row=2, column=0, columnspan=2, sticky="w", pady=(4, 0))

        # Buttons
        btn_frame = ttk.Frame(dlg)
        btn_frame.pack(padx=15, pady=(0, 15))

        def _ok():
            old_backend = self.REALTIME_BACKEND
            self.DHASH_THRESHOLD = var_dhash.get()
            self.AUDIO_GAIN = var_gain.get()
            self.JPEG_QUALITY = var_jpeg.get()
            self.CAPTURE_MAX_EDGE = var_edge.get()
            self.OCR_ENABLED = var_ocr.get()
            self.REALTIME_BACKEND = REALTIME_BACKENDS[combo_rt.current()][1]
            self.POSTPROCESS_BACKEND = REALTIME_BACKENDS[combo_post.current()][1]
            if old_backend != self.REALTIME_BACKEND:
                self._preloaded_transcriber = None
            self.SUMMARY_PROVIDER = SUMMARY_PROVIDERS[combo_prov.current()][1]
            self.SUMMARY_MODEL = entry_model.get().strip()
            self._save_settings()
            self._log(f"設定更新: dHash={self.DHASH_THRESHOLD}, ゲイン={self.AUDIO_GAIN:.1f}, "
                      f"JPEG={self.JPEG_QUALITY}, OCR={'有効' if self.OCR_ENABLED else '無効'}, "
                      f"リアルタイムASR={self.REALTIME_BACKEND}, "
                      f"後処理ASR={self.POSTPROCESS_BACKEND}, 要約={self.SUMMARY_PROVIDER}")
            dlg.destroy()

        def _cancel():
            dlg.destroy()

        tk.Button(btn_frame, text="  OK  ", command=_ok,
            font=("BIZ UDゴシック", 10)).pack(side="left", padx=8)
        tk.Button(btn_frame, text="キャンセル", command=_cancel,
            font=("BIZ UDゴシック", 10)).pack(side="left", padx=8)

        dlg.protocol("WM_DELETE_WINDOW", _cancel)

        # Center dialog on parent
        dlg.update_idletasks()
        x = self.root.winfo_x() + (self.root.winfo_width() - dlg.winfo_width()) // 2
        y = self.root.winfo_y() + (self.root.winfo_height() - dlg.winfo_height()) // 2
        dlg.geometry(f"+{x}+{y}")

    def _reset_settings(self):
        self.DHASH_THRESHOLD = 10
        self.AUDIO_GAIN = 2.0
        self.JPEG_QUALITY = 85
        self._save_settings()
        self._log("設定をデフォルトにリセットしました")

    def _set_transcript_enabled(self, on):
        self.transcript_area.config(
            state="normal" if on else "disabled",
            background="#fffef0" if on else "#f3f4f6")

    # -------------------------------------------------------- Language segment log

    def _write_language_event(self, lang, label):
        if not self._recording_dir:
            return
        elapsed = time.time() - self._record_start
        path = os.path.join(self._recording_dir, "language_segments.jsonl")
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps({
                    "elapsed": round(elapsed, 3),
                    "lang": lang,
                    "label": label
                }, ensure_ascii=False) + "\n")
        except Exception as e:
            print(f"[言語セグメント書き込み警告] {e}")

    def _start_hotkey(self):
        """Register the marker hotkey for the duration of the recording."""
        self._stop_hotkey()
        if not self.MARKER_HOTKEY:
            return
        self._hotkey = _GlobalHotkey(self.MARKER_HOTKEY, self._add_marker,
                                     log=lambda m: self.root.after(0, self._log, m))
        if self._hotkey.start():
            self._log(f"マーカーのホットキー: {self.MARKER_HOTKEY}（他アプリ使用中でも有効）")

    def _stop_hotkey(self):
        if self._hotkey is not None:
            self._hotkey.stop()
            self._hotkey = None

    def _add_marker(self, label=None):
        """Bookmark the current moment. Callable from the UI or the hotkey thread."""
        if not self.is_recording or not self._recording_dir:
            return
        elapsed = time.time() - self._record_start
        entry = {
            "elapsed": round(elapsed, 3),
            "epoch": round(time.time(), 3),
            "label": label or "重要",
        }
        try:
            with self._marker_lock:
                path = os.path.join(self._recording_dir, "markers.jsonl")
                with open(path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(entry, ensure_ascii=False) + "\n")
                self._marker_count += 1
            count = self._marker_count
        except Exception as e:
            self.root.after(0, self._log, f"[マーカー書き込み警告] {e}")
            return
        m, s = divmod(int(elapsed), 60)
        # May arrive from the hotkey thread, so touch the UI via after().
        self.root.after(0, self._log,
                        f"⭐ マーカー {count}: [{m:02d}:{s:02d}] {entry['label']}")

    def _write_snapshot_log(self, fname, snap_type, diff):
        if not self._recording_dir:
            return
        elapsed = time.time() - self._record_start
        epoch = time.time()
        path = os.path.join(self._recording_dir, "snapshots.jsonl")
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps({
                    "file": fname,
                    "epoch": round(epoch, 3),
                    "elapsed": round(elapsed, 3),
                    "type": snap_type,
                    "diff": int(diff) if diff is not None else None,
                }, ensure_ascii=False) + "\n")
        except Exception as e:
            # A dropped line means the image never reaches the report, so this
            # has to be visible in the app — a console print is not.
            self.root.after(0, self._log, f"[スナップショット記録エラー] {fname}: {e}")

    # ------------------------------------------------------------ Logging

    def _manual_snapshot(self):
        if not self.is_recording or self._recording_dir is None:
            return
        try:
            with mss.MSS() as sct:
                img = self._grab_image(sct, self._recording_rect or
                                       self._capture_rect(sct))
            now = datetime.datetime.now()
            ts = now.strftime('%H%M%S')
            ms = f"{now.microsecond // 1000:03d}"
            fname = f"manual_{ts}_{ms}.jpg"
            img.save(os.path.join(self._recording_dir, fname), "JPEG", quality=self.JPEG_QUALITY)
            self._log(f"手動キャプチャ -> {fname}")
            self._write_snapshot_log(fname, "manual", None)
        except Exception as e:
            self._log(f"[手動キャプチャエラー] {e}")

    def _log(self, text):
        self.log_area.config(state="normal")
        self.log_area.insert(tk.END,
            f"[{datetime.datetime.now().strftime('%H:%M:%S')}] {text}\n")
        self.log_area.see(tk.END)
        self.log_area.config(state="disabled")

    def _clear_partial_transcript(self, speaker=""):
        tag = "partial_" + (speaker or "mixed")
        ranges = self.transcript_area.tag_ranges(tag)
        for start, end in zip(ranges[0::2], ranges[1::2]):
            self.transcript_area.delete(start, end)

    def _log_transcript(self, text, speaker=""):
        self.transcript_area.config(state="normal")
        self._clear_partial_transcript(speaker)
        ts = datetime.datetime.now().strftime('%H:%M:%S')
        who = f" [{speaker}]" if speaker else ""
        self.transcript_area.insert(tk.END, f"[{ts}]{who} {text}\n")
        self.transcript_area.see(tk.END)
        self.transcript_area.config(state="disabled")

    def _show_partial_transcript(self, text, stable="", speaker=""):
        self.transcript_area.config(state="normal")
        self._clear_partial_transcript(speaker)
        tag = "partial_" + (speaker or "mixed")
        who = f"[{speaker}] " if speaker else ""
        self.transcript_area.insert(tk.END, f"~ {who}", tag)
        if stable and text.startswith(stable):
            self.transcript_area.insert(tk.END, stable, (tag, "partial_stable"))
            self.transcript_area.insert(tk.END, text[len(stable):], (tag, "partial_variable"))
        else:
            self.transcript_area.insert(tk.END, text, (tag, "partial_variable"))
        self.transcript_area.insert(tk.END, "\n", tag)
        self.transcript_area.tag_config(tag)
        self.transcript_area.tag_config("partial_stable", foreground="#111827",
                                        font=("BIZ UDゴシック", 10, "bold"))
        self.transcript_area.tag_config("partial_variable", foreground="#6b7280")
        self.transcript_area.see(tk.END)
        self.transcript_area.config(state="disabled")

    def _log_refined_transcript(self, text, speaker=""):
        self.transcript_area.config(state="normal")
        who = f" [{speaker}]" if speaker else ""
        self.transcript_area.insert(tk.END, f"  ↳ 補正{who}: {text}\n", "refine")
        self.transcript_area.tag_config("refine", foreground="#2563eb")
        self.transcript_area.see(tk.END)
        self.transcript_area.config(state="disabled")

    def _detect_monitors(self):
        with mss.MSS() as sct:
            vals = []
            for i, m in enumerate(sct.monitors):
                if i == 0:
                    vals.append(f"画面 [0]: 全画面 (Virtual Screen)")
                else:
                    vals.append(f"画面 [{i}]: ディスプレイ {i} ({m['width']}x{m['height']})")
            vals.append(REGION_CHOICE)
            self.combo_monitor["values"] = vals
            self.combo_monitor.current(0)

    # -------------------------------------------------------------- Control

    @staticmethod
    def _sanitize_name(name):
        """Make a string safe to embed in a folder/file name (Windows)."""
        return sanitize_name(name)

    def _toggle_recording(self):
        if not self.is_recording:
            if not os.path.exists(FFMPEG_PATH):
                self._log("[エラー] ffmpeg.exe が見つかりません")
                messagebox.showerror("エラー",
                    f"ffmpeg.exe が見つかりません。\n\n期待パス:\n{FFMPEG_PATH}")
                return
            selected = self._selected_audio_devices()
            if not selected:
                messagebox.showwarning("音声デバイス未選択",
                    "録音するオーディオデバイスを1つ以上選択してください。")
                return
            if not self._validate_region():
                return
            self._meeting_name = self.entry_meeting.get().strip()
            self.is_recording = True
            self.stop_event.clear()
            self._pcm_written = 0
            self._queue_overflow_count = 0
            self._audio_stop_time = None
            self._record_start = time.time()
            self._recording_mon_idx = self.combo_monitor.current()
            with mss.MSS() as sct:
                self._recording_rect = self._capture_rect(sct)
            self._marker_count = 0
            self.btn_toggle.config(text="■ 記録を停止して保存", bg="#ef4444")
            self.btn_manual_snap.config(state="normal")
            self.btn_marker.config(state="normal")
            self._start_hotkey()
            for w in (self.combo_monitor, self.combo_mode):
                w.config(state="disabled")
            self.listbox_audio.config(state="disabled")
            self.btn_refresh_audio.config(state="disabled")
            self.btn_select_all_audio.config(state="disabled")
            self.btn_region.config(state="disabled")
            self.btn_region_clear.config(state="disabled")
            self.entry_meeting.config(state="disabled")
            # combo_lang stays enabled for mid-recording language switching
            self._tick_elapsed_timer()
            self._pipeline_thread = threading.Thread(target=self._pipeline, daemon=True)
            self._pipeline_thread.start()
        else:
            # UI resets instantly; cleanup runs in background
            self._audio_stop_time = time.monotonic()
            self.is_recording = False
            self.stop_event.set()
            self._stop_hotkey()
            self.btn_toggle.config(state="disabled")
            self.btn_manual_snap.config(state="disabled")
            self.btn_marker.config(state="disabled")
            self.label_status.config(text="ステータス: 保存処理中...", foreground="#f59e0b")
            threading.Thread(target=self._async_cleanup, daemon=True).start()

    def _reset_ui(self):
        self.is_recording = False
        self.stop_event.clear()
        self._audio_level = 0.0
        self._queue_overflow_count = 0
        self._recording_dir = None
        self._stop_hotkey()
        self.btn_toggle.config(text="▶ 会議記録を開始", bg="#10b981", state="normal")
        self.btn_manual_snap.config(state="disabled")
        self.btn_marker.config(state="disabled")
        self.label_status.config(text="ステータス: 停止中", foreground="#6b7280")
        for w in (self.combo_monitor, self.combo_mode):
            w.config(state="readonly")
        self.listbox_audio.config(state="normal")
        self.btn_refresh_audio.config(state="normal")
        self.btn_select_all_audio.config(state="normal")
        self._recording_rect = None
        self._update_region_label()
        self.entry_meeting.config(state="normal")

    # ----------------------------------------------------------- Main pipeline

    def _pipeline(self):
        now = datetime.datetime.now()
        stamp = now.strftime("%Y%m%d_%H%M%S")
        safe = self._sanitize_name(self._meeting_name)
        suffix = safe if safe else "Meeting"
        dir_name = os.path.join(BASE_DIR, f"{stamp}_{suffix}")
        os.makedirs(dir_name, exist_ok=True)
        self._recording_dir = dir_name
        self.root.after(0, self._log, f"フォルダー作成: {dir_name}")

        lang = LANG_OPTIONS[self.combo_lang.current()][1]
        label = LANG_OPTIONS[self.combo_lang.current()][0]
        self._write_language_event(lang, label)

        start_epoch = time.time()
        self._audio_origin = time.monotonic()
        self._audio_error = None
        self._audio_packets = 0
        selected = self._selected_audio_devices()
        n = len(selected)
        mode_full = self.combo_mode.current() == 1
        out_mp3 = os.path.join(dir_name, "audio_main.mp3")

        # One dedicated queue per selected device
        self._audio_queues = [queue.Queue(maxsize=200) for _ in range(n)]
        self.transcribe_queue = None
        self.transcribe_role_queues = {}
        self._audio_finished.clear()

        # Speaker separation needs both sides captured; with one side there is
        # nothing to separate and the role tracks would just duplicate the mix.
        n_mic = sum(1 for d in selected if d["kind"] == "mic")
        n_spk = sum(1 for d in selected if d["kind"] == "speaker")
        separate = n > 1 and n_mic >= 1 and n_spk >= 1
        if mode_full and self.REALTIME_BACKEND == "fast_ja_en" and separate:
            self.transcribe_role_queues = {
                ROLE_SELF: queue.Queue(maxsize=400),
                ROLE_OTHER: queue.Queue(maxsize=400),
            }
        elif mode_full:
            self.transcribe_queue = queue.Queue(maxsize=400)

        try:
            self._recording_processor = RecordingProcessor(
                [d["kind"] for d in selected], self.AUDIO_GAIN, self.ECHO_DELAY_MS)
        except Exception as e:
            self.root.after(0, self._log, f"[録音開始失敗] 音声処理を初期化できません: {e}")
            self.root.after(0, messagebox.showerror, "録音開始失敗",
                            f"エコー除去を初期化できません。依存パッケージを確認してください。\n{e}")
            self.root.after(0, self._reset_ui)
            return
        self.root.after(0, self._log,
            "重複音声抑制: 有効 / " + ("マイク別AEC: 有効（録音・文字起こし共通）"
                                       if separate else "時刻同期: 有効"))

        if not self._start_writers(dir_name, out_mp3, separate):
            self.root.after(0, self._log, "[エラー] ffmpeg 起動失敗")
            self.root.after(0, self._reset_ui)
            return

        if mode_full:
            self._transcription_start(dir_name)

        active = threading.Event(); active.set()
        threads = []
        for i, dev in enumerate(selected):
            threads.append(threading.Thread(
                target=self._capture_device,
                args=(active, dev, self._audio_queues[i]), daemon=True))
        for t in threads:
            t.start()

        mixer_t = threading.Thread(
            target=self._mixer_loop_n,
            args=(active, self._audio_queues, selected), daemon=True)
        mixer_t.start()
        writer_t = None

        # Screen capture
        next_target = time.time() + self.INTERVAL
        last_hash = None
        snap_count = 0
        with mss.MSS() as sct:
            monitor = self._recording_rect or self._capture_rect(sct)
            self.root.after(0, self._log,
                f"キャプチャ対象: {monitor['width']}×{monitor['height']} "
                f"(x={monitor['left']}, y={monitor['top']})")
            while self.is_recording and not self.stop_event.is_set():
                dt = next_target - time.time()
                if dt > 0:
                    time.sleep(dt)
                try:
                    img = self._grab_image(sct, monitor)
                    h = imagehash.dhash(img)
                except Exception as e:
                    self.root.after(0, self._log, f"[画面エラー] {e}")
                    next_target += self.INTERVAL
                    continue

                diff = 0
                changed = last_hash is None or (h - last_hash) > self.DHASH_THRESHOLD
                if changed:
                    if last_hash is not None:
                        # int(): imagehash returns numpy.int64, which json
                        # cannot serialize — that silently dropped every
                        # snapshot after the first one from the log.
                        diff = int(h - last_hash)
                    snap_count += 1
                    ts = datetime.datetime.now().strftime('%H%M%S')
                    ms = f"{datetime.datetime.now().microsecond // 1000:03d}"
                    fname = f"snapshot_{ts}_{ms}.jpg"
                    img.save(os.path.join(dir_name, fname), "JPEG", quality=self.JPEG_QUALITY)
                    self.root.after(0, self._log, f"画面変化検知 -> {fname} (diff={diff})")
                    self._write_snapshot_log(fname, "auto", diff)
                    last_hash = h
                next_target += self.INTERVAL

        # Save context for async cleanup
        self._pipeline_ctx = {
            'active': active, 'threads': threads,
            'mixer_t': mixer_t, 'writer_t': writer_t,
            'mode_full': mode_full, 'start_epoch': start_epoch,
            'now': now, 'dir_name': dir_name,
            'audio_src_str': self._audio_source_label_n(selected),
            'snap_count': snap_count, 'lang': lang,
            'realtime_backend': self.REALTIME_BACKEND,
            'realtime_roles': bool(self.transcribe_role_queues),
            'meeting_name': self._meeting_name,
            'role_tracks': sorted(self._role_writers),
        }

    def _async_cleanup(self):
        """Run teardown in background so UI stays responsive."""
        # Wait for pipeline thread to finish setting _pipeline_ctx
        if self._pipeline_thread:
            self._pipeline_thread.join(timeout=10)
            self._pipeline_thread = None
        ctx = self._pipeline_ctx
        if ctx is None:
            self._stop_writers()
            self.root.after(0, self._reset_ui)
            return

        active = ctx['active']
        active.clear()
        for t in ctx['threads']:
            t.join(timeout=3)
        if ctx['mixer_t']:
            ctx['mixer_t'].join(timeout=5)
        if ctx['writer_t']:
            ctx['writer_t'].join(timeout=3)

        self._stop_writers()
        if ctx['mode_full']:
            self._transcription_stop()

        dir_name = ctx['dir_name']
        with open(os.path.join(dir_name, "metadata.txt"), "w", encoding="utf-8") as f:
            f.write(f"MEETING_NAME={ctx['meeting_name']}\n")
            f.write(f"START_TIME_EPOCH={ctx['start_epoch']}\n")
            f.write(f"START_TIME_STR={ctx['now'].strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write(f"AUDIO_SOURCE={ctx['audio_src_str']}\n")
            f.write(f"MODE={'full' if ctx['mode_full'] else 'light'}\n")
            f.write(f"SNAPSHOT_COUNT={ctx['snap_count']}\n")
            f.write(f"MARKER_COUNT={self._marker_count}\n")
            f.write("AUDIO_FILE=audio_main.mp3\n")
            f.write("AUDIO_PROCESSING=timestamped_per_mic_aec_dedup\n")
            if self._audio_error:
                f.write(f"AUDIO_PROCESSING_ERROR={self._audio_error}\n")
            f.write(f"LANGUAGE={ctx['lang']}\n")
            if ctx['role_tracks']:
                f.write(f"ROLE_TRACKS={','.join(ctx['role_tracks'])}\n")
                f.write(f"AUDIO_SELF_FILE={ROLE_TRACK_SELF}\n")
                f.write(f"AUDIO_OTHER_FILE={ROLE_TRACK_OTHER}\n")
            if ctx['mode_full']:
                f.write("TRANSCRIPTION_FILE=transcription.txt\n")
                f.write(f"REALTIME_BACKEND={ctx['realtime_backend']}\n")
                f.write(f"REALTIME_ROLE_SEPARATION={'true' if ctx['realtime_roles'] else 'false'}\n")
                if ctx['realtime_backend'] == "fast_ja_en":
                    f.write("REFINED_TRANSCRIPTION_FILE=transcription_refined.txt\n")
                    f.write("FINAL_TRANSCRIPTION_FILE=transcription_final.txt\n")
                    f.write("FINAL_TRANSCRIPTION_HTML=transcription_final.html\n")

        elapsed = time.time() - ctx['start_epoch']
        m, s = divmod(int(elapsed), 60)
        audio_sec = self._pcm_written / (self.TARGET_RATE * 4)   # stereo int16
        msg = (f"記録完了 ({m:02d}:{s:02d})。画像: {ctx['snap_count']}枚 "
               f"/ 音声: {audio_sec:.0f}秒 / 保存先: {dir_name}")
        if self._queue_overflow_count > 0:
            msg += f" / ⚠ キュー溢れ: {self._queue_overflow_count}回"
        self._pipeline_ctx = None
        self._last_record_dir = dir_name
        self.root.after(0, self._log, msg)
        if self._audio_error:
            self.root.after(0, messagebox.showwarning, "音声処理エラーで停止",
                f"処理済みの音声を保存しました。\n{self._audio_error}\n\n{dir_name}")
        elif self._pcm_written == 0 or self._audio_packets == 0:
            self.root.after(0, messagebox.showwarning, "音声なし",
                "音声デバイスから録音データを取得できませんでした。\n\n"
                "選択したデバイスがすべて無音／使用不可の可能性があります。\n"
                "動作ログの「[スキップ]」「[キャプチャエラー]」を確認してください。\n\n"
                f"フォルダー:\n{dir_name}")
        else:
            final_html = os.path.join(dir_name, "transcription_final.html")
            html_note = ("\n\n補正済みHTML: transcription_final.html"
                         if os.path.exists(final_html) else "")
            self.root.after(0, messagebox.showinfo, "完了",
                f"すべての記録が正常に保存されました。\n\n"
                f"音声: 約{audio_sec:.0f}秒{html_note}\n\nフォルダー:\n{dir_name}")
        self.root.after(0, self._reset_ui)

    # --------------------------------------------------- FFmpeg (Captura pattern)

    def _start_writers(self, dir_name, out_mp3, separate):
        """Open the mixed-audio encoder and, when separating speakers, the role
        tracks. Returns False if the main encoder could not start."""
        self.ffmpeg_proc = None
        self._role_writers = {}
        try:
            self.ffmpeg_proc = _PcmWriter(out_mp3, self.TARGET_RATE)
        except Exception as e:
            self.root.after(0, self._log, f"[ffmpegエラー] {e}")
            return False
        if not separate:
            return True
        for role, fname in ((ROLE_SELF, ROLE_TRACK_SELF),
                            (ROLE_OTHER, ROLE_TRACK_OTHER)):
            path = os.path.join(dir_name, fname)
            try:
                self._role_writers[role] = _PcmWriter(
                    path, self.TARGET_RATE, transcription_only=True)
            except Exception as e:
                self.root.after(0, self._log, f"[話者トラック警告] {role}: {e}")
        if self._role_writers:
            self.root.after(0, self._log,
                "話者分離を有効化（自分=マイク / 相手=スピーカー）")
        return True

    def _write_audio(self, main_pcm, role_chunks=None):
        """Write one output chunk to the mix and to every role track.

        Each role track advances by exactly as many bytes as the main mix —
        silence where that role contributed nothing — so the three files stay
        on one timeline and transcript timestamps remain comparable.
        """
        self._pcm_written += len(main_pcm)
        if self.ffmpeg_proc is not None:
            self.ffmpeg_proc.write(main_pcm)
        if not self._role_writers:
            return
        silence = None
        for role, writer in self._role_writers.items():
            chunk = role_chunks.get(role) if role_chunks else None
            if chunk is None:
                if silence is None:
                    silence = bytes(len(main_pcm))
                chunk = silence
            writer.write(chunk)

    def _stop_writers(self):
        for writer in list(self._role_writers.values()):
            writer.close()
        self._role_writers = {}
        if self.ffmpeg_proc is not None:
            self.ffmpeg_proc.close()
            self.ffmpeg_proc = None
            self.root.after(0, self._log, "ffmpeg エンコード完了 -> MP3 保存済み")

    # ------------------------------------------------- Transcribe forwarding

    def _push_transcribe(self, pcm, role_chunks=None):
        """Forward a PCM chunk to the transcription queue (no-op in light mode)."""
        if self.transcribe_role_queues:
            # The mixer is the only producer. Reserve both roles together;
            # dropping only one side would shift the ASR sample timelines.
            if any(q.full() for q in self.transcribe_role_queues.values()):
                self._queue_overflow_count += 1
                return
            silence = None
            for role, q in self.transcribe_role_queues.items():
                chunk = role_chunks.get(role) if role_chunks else None
                if chunk is None:
                    if silence is None:
                        silence = bytes(len(pcm))
                    chunk = silence
                try:
                    q.put_nowait(chunk)
                except queue.Full:
                    self._queue_overflow_count += 1
            return
        if self.transcribe_queue is not None:
            try:
                self.transcribe_queue.put_nowait(pcm)
            except queue.Full:
                self._queue_overflow_count += 1

    def _audio_processing_failed(self, detail):
        self._log(f"[音声処理エラー] {detail}")
        if self.is_recording:
            self._toggle_recording()
        messagebox.showerror("録音を停止しました",
            "音声処理に失敗したため録音を停止しました。\n"
            "処理済みの音声は保存します。\n" + detail)

    def _mixer_loop_n(self, active, queues, devices=None):
        """Render one shared clock; never append device tails sequentially."""
        sources = [TimelineSource() for _ in queues]
        ended = [False] * len(queues)
        origin = self._audio_origin
        emitted = 0
        stop_deadline = None
        pending = bytearray()
        pending_roles = {ROLE_SELF: bytearray(), ROLE_OTHER: bytearray()}
        last_duplicates = None
        last_report = 0

        def forward():
            if pending:
                self._push_transcribe(bytes(pending),
                    {role: bytes(data) for role, data in pending_roles.items()})
                pending.clear()
                for data in pending_roles.values():
                    data.clear()

        try:
            while True:
                for i, q in enumerate(queues):
                    for _ in range(200):
                        item = self._qget(q, timeout=0)
                        if item is None:
                            break
                        if item is _EOF:
                            ended[i] = True
                            break
                        sources[i].add(item)
                        self._audio_packets += 1
                now = time.monotonic()
                stopping = self.stop_event.is_set() or not active.is_set()
                if stopping:
                    if stop_deadline is None:
                        stop_deadline = now + 0.75
                    # Capture callbacks have already stopped on is_recording.
                    # Give their final packets time to arrive before the flush.
                    if not all(ended) and now < stop_deadline:
                        time.sleep(0.01)
                        continue
                    stop_at = self._audio_stop_time or now
                    limit = max(0, round((stop_at - origin) * self.TARGET_RATE))
                else:
                    # Fixed jitter budget; idle endpoints never block others.
                    limit = max(0, int((now - origin - 0.20) * self.TARGET_RATE))
                    limit -= limit % AUDIO_FRAMES
                while emitted < limit:
                    blocks = [source.read(origin + emitted / self.TARGET_RATE)
                              for source in sources]
                    raw, own, other = self._recording_processor.process(blocks)
                    count = min(AUDIO_FRAMES, limit - emitted)
                    raw, own, other = (x[:count * 4] for x in (raw, own, other))
                    roles = {ROLE_SELF: own, ROLE_OTHER: other}
                    self._update_level(raw)
                    self._write_audio(raw, roles)
                    pending.extend(raw)
                    for role, data in roles.items():
                        pending_roles[role].extend(data)
                    if len(pending) >= self.TARGET_RATE * 4 // 10:
                        forward()
                    emitted += count
                duplicates = (self._recording_processor.mic_mixer.duplicates,
                              self._recording_processor.speaker_mixer.duplicates)
                if duplicates != last_duplicates and now - last_report >= 5:
                    if any(duplicates) or last_duplicates is not None:
                        self.root.after(0, self._log,
                            f"[重複音声抑制] マイク {duplicates[0]} / スピーカー {duplicates[1]}")
                    last_duplicates, last_report = duplicates, now
                if stopping:
                    forward()
                    break
                time.sleep(0.005)
        except Exception as e:
            self._audio_error = str(e)
            forward()
            self.root.after(0, self._audio_processing_failed, str(e))
        finally:
            self._queue_overflow_count += sum(source.dropped for source in sources)
            self._audio_finished.set()

    @staticmethod
    def _qget(q, timeout=0.05):
        if q is None:
            return None
        try:
            return q.get(timeout=timeout)
        except queue.Empty:
            return None

    # --------------------------------------------------- Audio capture (generic)

    def _capture_device(self, active, dev, out_queue):
        """Capture one device; normalize PCM into out_queue. Emits _EOF on exit."""
        own_com = _com_initialize()
        try:
            if dev["kind"] == "speaker":
                self._capture_speaker_dev(active, dev, out_queue)
            else:
                self._capture_mic_dev(active, dev, out_queue)
        except Exception as e:
            self.root.after(0, self._log, f"[キャプチャエラー] {dev['name']}: {e}")
        finally:
            if own_com:
                _com_uninitialize()   # after the stream is closed, never before
            # Guarantee the EOF sentinel lands so the mixer can drain & terminate.
            for _ in range(3):
                try:
                    out_queue.put_nowait(_EOF)
                    break
                except queue.Full:
                    try:
                        out_queue.get_nowait()  # drop one stale PCM to make room
                    except queue.Empty:
                        break

    # ----------------------------------------- Speaker capture (pyaudiowpatch)

    @staticmethod
    def _format_candidates(channels, rate):
        """(channels, rate) combos to try, best first. A device's advertised
        default is not always openable — multi-channel endpoints in particular."""
        combos = []
        for c in (min(channels, 2), 1, channels):
            c = int(c)
            if c < 1:
                continue
            for r in (48000, rate, 44100):
                r = int(r)
                if (c, r) not in combos:
                    combos.append((c, r))
        return combos

    def _capture_speaker_dev(self, active, dev, out_queue):
        def _make_callback(ch, sr):
            clock = CaptureClock(sr, ch)
            def _callback(in_data, frame_count, time_info, status):
                # An exception raised here propagates into PortAudio's C callback
                # and can take the process down — never let one escape.
                try:
                    packet = clock.packet(in_data, time_info)
                    if status:
                        self._queue_overflow_count += 1
                    if len(packet.samples):
                        out_queue.put_nowait(packet)
                except queue.Full:
                    self._queue_overflow_count += 1
                except Exception:
                    self._queue_overflow_count += 1
                return (None, pyaudio.paContinue)
            return _callback

        stream = None
        open_err = None   # first failure = the device's own advertised format
        # PortAudio must be initialised on the very thread that opens the stream:
        # its WASAPI backend sets up COM per thread, so a handle created on
        # another thread makes every open fail with "invalid sample rate".
        with self._open_lock:
            p = pyaudio.PyAudio()
            for ch, sr in self._format_candidates(dev["channels"], dev["rate"]):
                try:
                    stream = p.open(
                        format=pyaudio.paInt16, channels=ch, rate=sr,
                        input=True, input_device_index=dev["native_idx"],
                        frames_per_buffer=1024, start=False,
                        stream_callback=_make_callback(ch, sr))
                    stream.start_stream()
                    self.root.after(0, self._log,
                        f"スピーカーキャプチャ開始: {dev['name']} "
                        f"({sr}Hz, {ch}ch, idx={dev['native_idx']})")
                    break
                except Exception as e:
                    if open_err is None:
                        open_err = e
                    if stream is not None:
                        try:
                            stream.close()
                        except Exception:
                            pass
                        stream = None

        def _terminate():
            with self._open_lock:
                try:
                    p.terminate()
                except Exception as e:
                    print(f"[PyAudio終了警告] {dev['name']}: {e}")

        if stream is None:
            # Device unusable (disabled, exclusive-mode, unsupported format) —
            # report it and let the rest of the selection keep recording.
            _terminate()
            self.root.after(0, self._log,
                f"[スキップ] スピーカー {dev['name']}: 開けません ({open_err})")
            return

        try:
            while active.is_set() and self.is_recording:
                time.sleep(0.5)
        finally:
            try:
                stream.stop_stream()
            except Exception as e:
                print(f"[停止警告] {dev['name']}: {e}")
            try:
                stream.close()
            except Exception as e:
                print(f"[クローズ警告] {dev['name']}: {e}")
            _terminate()

    # ------------------------------------------- Mic capture (sounddevice)

    def _capture_mic_dev(self, active, dev, out_queue):
        def _make_callback(ch, sr):
            clock = CaptureClock(sr, ch)
            def _callback(in_data, frames, time_info, status):
                try:
                    packet = clock.packet(in_data.tobytes(), time_info)
                    if status:
                        self._queue_overflow_count += 1
                    if len(packet.samples):
                        out_queue.put_nowait(packet)
                except queue.Full:
                    self._queue_overflow_count += 1
                except Exception:
                    self._queue_overflow_count += 1
            return _callback

        stream = None
        open_err = None   # first failure = the device's own advertised format
        with self._open_lock:
            for ch, sr in self._format_candidates(min(dev["channels"], 2), dev["rate"]):
                try:
                    stream = sd.InputStream(
                        device=dev["native_idx"], channels=ch, samplerate=sr,
                        dtype='int16', blocksize=1024, callback=_make_callback(ch, sr))
                    stream.start()
                    self.root.after(0, self._log,
                        f"マイクキャプチャ開始: {dev['name']} ({sr}Hz, {ch}ch)")
                    break
                except Exception as e:
                    if open_err is None:
                        open_err = e
                    if stream is not None:
                        try:
                            stream.close()
                        except Exception:
                            pass
                        stream = None

        if stream is None:
            self.root.after(0, self._log,
                f"[スキップ] マイク {dev['name']}: 開けません ({open_err})")
            return

        try:
            while active.is_set() and self.is_recording:
                time.sleep(0.5)
        finally:
            try:
                stream.stop()
            except Exception as e:
                print(f"[停止警告] {dev['name']}: {e}")
            try:
                stream.close()
            except Exception as e:
                print(f"[クローズ警告] {dev['name']}: {e}")

    # ------------------------------------------ Transcription (fast ja/en or whisper)

    def _transcription_start(self, dir_name):
        try:
            if self._preloaded_transcriber is not None:
                self.transcriber = self._preloaded_transcriber
                self._preloaded_transcriber = None
                self.root.after(0, self._log, "事前読み込み済みモデルを使用")
            else:
                if self.REALTIME_BACKEND == "fast_ja_en":
                    from .asr.fast import FastJapaneseEnglishASR
                    self.root.after(0, self._log, "高速日本語/英語モデルを読み込み中...")
                    model_dir = MODELS_DIR
                    hotwords, replacements = self._prepare_fast_glossary(model_dir)
                    self.transcriber = FastJapaneseEnglishASR(
                        model_dir, threads=self.FAST_ASR_THREADS,
                        hotwords_file=hotwords, replacements=replacements)
                else:
                    from faster_whisper import WhisperModel
                    self.root.after(0, self._log,
                        f"文字起こしモデルを読み込み中... ({self.REALTIME_WHISPER_MODEL})")
                    self.transcriber = WhisperModel(
                        self.REALTIME_WHISPER_MODEL,
                        device=self.WHISPER_DEVICE, compute_type=self.WHISPER_COMPUTE)

            lang = LANG_OPTIONS[self.combo_lang.current()][1]
            self._rt_lang = None if lang == "auto" else lang
            self._rt_detected_lang = None
            self._fast_final_segments = []
            self._fast_refined_segments = []
            self._leakage_guard = PartialLeakageGuard()
            self._asr_load_ema = 0.0
            self._asr_load_level = 0
            if self.REALTIME_BACKEND == "fast_ja_en" and self.transcribe_role_queues:
                base = self.transcriber
                self.transcriber = {
                    ROLE_SELF: base,
                    ROLE_OTHER: base.clone_session(),
                }
                self.root.after(0, self._log,
                    "リアルタイム話者分離: 自分 / 相手を別々に認識")

            self.transcription_file = open(
                os.path.join(dir_name, "transcription.txt"), "w", encoding="utf-8")
            if self.REALTIME_BACKEND == "fast_ja_en":
                self.refined_transcription_file = open(
                    os.path.join(dir_name, "transcription_refined.txt"),
                    "w", encoding="utf-8")
            self._transcribe_start = time.time()
            self.root.after(0, self._log, "文字起こしスレッド起動")
            loop = (self._transcribe_fast_loop if self.REALTIME_BACKEND == "fast_ja_en"
                    else self._transcribe_loop)
            self._transcribe_thread = threading.Thread(target=loop, daemon=True)
            self._transcribe_thread.start()

        except Exception as e:
            self.root.after(0, self._log, f"[文字起こしエラー] {e}")
            self.transcriber = None

    def _record_fast_results(self, events, speaker=""):
        for result in events:
            result.speaker = speaker
            text = result.text.strip()
            if not text:
                continue
            if speaker == ROLE_OTHER:
                self._leakage_guard.remember_other(text)
            elif (speaker == ROLE_SELF
                  and self._leakage_guard.is_leakage(text)):
                continue
            if result.kind == "partial":
                self.root.after(0, self._show_partial_transcript,
                                text, result.stable_text, speaker)
                continue
            if result.kind == "refine":
                self._fast_refined_segments.append(result)
                self.root.after(0, self._log_refined_transcript, text, speaker)
                if self.refined_transcription_file:
                    elapsed = time.time() - self._transcribe_start
                    who = f"[{speaker}] " if speaker else ""
                    self.refined_transcription_file.write(
                        f"[{elapsed:.1f}s] {who}{text}\n")
                    self.refined_transcription_file.flush()
                continue
            self._fast_final_segments.append(result)
            self.root.after(0, self._log_transcript, text, speaker)
            if self.transcription_file:
                elapsed = time.time() - self._transcribe_start
                who = f"[{speaker}] " if speaker else ""
                self.transcription_file.write(f"[{elapsed:.1f}s] {who}{text}\n")
                self.transcription_file.flush()
            if (self._rt_lang is None and result.language
                    and result.language != self._rt_detected_lang):
                self._rt_detected_lang = result.language
                label = "日本語" if result.language == "ja" else "English"
                self._write_language_event(result.language, label)

    def _update_asr_load(self, ratio, sessions):
        """Adapt draft frequency with hysteresis; finals are never disabled."""
        self._asr_load_ema = (ratio if self._asr_load_ema == 0
                              else self._asr_load_ema * 0.85 + ratio * 0.15)
        old = self._asr_load_level
        if self._asr_load_level == 0 and self._asr_load_ema > 0.55:
            self._asr_load_level = 1
        elif self._asr_load_level == 1:
            if self._asr_load_ema > 0.90:
                self._asr_load_level = 2
            elif self._asr_load_ema < 0.40:
                self._asr_load_level = 0
        elif self._asr_load_level == 2 and self._asr_load_ema < 0.70:
            self._asr_load_level = 1
        for session in sessions.values():
            session.partials_enabled = self._asr_load_level < 2
            session.partial_every = 1.0 if self._asr_load_level == 1 else 0.5
        if old != self._asr_load_level:
            labels = ("通常（暫定0.5秒）", "負荷軽減（暫定1秒）", "高負荷（確定のみ）")
            self.root.after(0, self._log,
                f"ASR負荷調整: {labels[self._asr_load_level]} "
                f"(RTF={self._asr_load_ema:.2f})")

    def _transcribe_fast_loop(self):
        """Feed mixed or role-separated PCM with adaptive partial decoding."""
        from scipy.signal import resample_poly

        if isinstance(self.transcriber, dict):
            sessions = self.transcriber
            queues = self.transcribe_role_queues
        else:
            sessions = {"": self.transcriber}
            queues = {"": self.transcribe_queue}

        while (not self._audio_finished.is_set()
               or any(q is not None and not q.empty() for q in queues.values())):
            got = False
            # Decode the clean far-end reference first. Its text can then guard
            # the mic result from being displayed as a leaked "self" partial.
            order = sorted(queues, key=lambda role: role != ROLE_OTHER)
            for speaker in order:
                q = queues[speaker]
                if q is None:
                    continue
                try:
                    pcm = q.get_nowait()
                except queue.Empty:
                    continue
                got = True
                try:
                    t0 = time.perf_counter()
                    if isinstance(pcm, np.ndarray):
                        mono_16k = pcm.astype(np.float32, copy=False)
                        mono = mono_16k
                        source_rate = self.TRANSCRIBE_RATE
                    else:
                        samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
                        mono = samples.reshape(-1, 2).mean(axis=1) if len(samples) >= 2 else samples
                        mono_16k = resample_poly(
                            mono, self.TRANSCRIBE_RATE, self.TARGET_RATE).astype(np.float32)
                        source_rate = self.TARGET_RATE
                    self._record_fast_results(
                        sessions[speaker].accept(mono_16k, self._rt_lang), speaker)
                    audio_s = max(len(mono) / source_rate, 0.001)
                    self._update_asr_load(
                        (time.perf_counter() - t0) / audio_s, sessions)
                except Exception as e:
                    self.root.after(0, self._log, f"[高速文字起こし処理エラー] {e}")
            if not got:
                time.sleep(0.02)

        for speaker, session in sessions.items():
            try:
                self._record_fast_results(session.flush(self._rt_lang), speaker)
            except Exception as e:
                self.root.after(0, self._log, f"[高速文字起こし終了警告] {e}")

    def _transcribe_loop(self):
        """Chunk-driven real-time transcription loop.

        Incoming PCM (48kHz stereo int16, from self.transcribe_queue) is
        down-mixed to mono and resampled to 16000Hz. Once ~5 seconds of audio
        has accumulated, faster-whisper transcribes the chunk (auto-detecting
        ja/en when self._rt_lang is None). ~1 second of trailing audio is kept
        as overlap so words aren't cut at chunk boundaries.
        """
        from scipy.signal import resample as _resample

        chunk_samples = int(self.TRANSCRIBE_RATE * self.TRANSCRIBE_CHUNK_SECONDS)
        overlap_samples = int(self.TRANSCRIBE_RATE * self.TRANSCRIBE_OVERLAP_SECONDS)
        min_flush_samples = int(self.TRANSCRIBE_RATE * 0.5)

        buf = np.zeros(0, dtype=np.float32)
        last_lang = None

        def _drop_backlog():
            # Backpressure: if the queue is backing up, drop the oldest
            # chunks so transcription keeps pace with real time.
            q = self.transcribe_queue
            if q is None:
                return
            qsize = q.qsize()
            if qsize > 200:
                for _ in range(qsize - 50):
                    try:
                        q.get_nowait()
                        self._queue_overflow_count += 1
                    except queue.Empty:
                        break

        while (not self._audio_finished.is_set()
               or (self.transcribe_queue is not None and not self.transcribe_queue.empty())):
            if self.transcribe_queue is None or self.transcriber is None:
                time.sleep(0.1)
                continue

            if self.is_recording:
                _drop_backlog()

            try:
                pcm = self.transcribe_queue.get(timeout=1.0)
            except queue.Empty:
                continue

            try:
                samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
                mono_44k = samples.reshape(-1, 2).mean(axis=1) if len(samples) >= 2 else samples
                n_out = int(len(mono_44k) * self.TRANSCRIBE_RATE / self.TARGET_RATE)
                if n_out > 0:
                    mono_16k = _resample(mono_44k, n_out).astype(np.float32)
                    buf = np.concatenate([buf, mono_16k])

                while len(buf) >= chunk_samples:
                    clip = buf[:chunk_samples]
                    last_lang = self._transcribe_chunk(clip, last_lang)
                    buf = buf[max(0, chunk_samples - overlap_samples):]
            except Exception as e:
                self.root.after(0, self._log, f"[文字起こし処理エラー] {e}")

        if len(buf) >= min_flush_samples and self.transcriber is not None:
            try:
                self._transcribe_chunk(buf, last_lang)
            except Exception as e:
                self.root.after(0, self._log, f"[バッファフラッシュ警告] {e}")

    def _transcribe_chunk(self, clip, prev_lang):
        """Transcribe one ~5s 16kHz mono chunk; return the language used."""
        try:
            lang = detect_ja_en(self.transcriber, clip) if self._rt_lang is None else self._rt_lang
            segments, info = self.transcriber.transcribe(
                clip, language=lang, vad_filter=True, beam_size=1)
            for seg in segments:
                text = seg.text.strip()
                if not text:
                    continue
                self.root.after(0, self._log_transcript, text)
                if self.transcription_file:
                    elapsed = time.time() - self._transcribe_start
                    self.transcription_file.write(f"[{elapsed:.1f}s] {text}\n")
                    self.transcription_file.flush()
            if self._rt_lang is None and lang != prev_lang:
                label = "日本語" if lang == "ja" else "English"
                self._write_language_event(lang, label)
            return lang
        except Exception as e:
            self.root.after(0, self._log, f"[文字起こし処理エラー] {e}")
            return prev_lang

    def _write_fast_final_outputs(self):
        """Merge fast finals with range-based refinements and export TXT/HTML."""
        if not self._recording_dir:
            return
        refinements = sorted(self._fast_refined_segments,
                             key=lambda e: (e.start_sample, e.end_sample))
        rows = []
        for event in self._fast_final_segments:
            covered = any(
                event.speaker == refined.speaker
                and event.start_sample < refined.end_sample
                and event.end_sample > refined.start_sample
                for refined in refinements)
            if not covered:
                rows.append(event)
        rows.extend(refinements)
        rows.sort(key=lambda e: e.start_sample)
        rows = remove_cross_role_duplicates(rows, ROLE_SELF, ROLE_OTHER)
        if not rows:
            return

        txt_path = os.path.join(self._recording_dir, "transcription_final.txt")
        with open(txt_path, "w", encoding="utf-8") as f:
            for event in rows:
                seconds = event.start_sample / self.TRANSCRIBE_RATE
                who = f" [{event.speaker}]" if event.speaker else ""
                f.write(f"[{fmt_timestamp(seconds)}]{who} [{event.language}] "
                        f"{event.text.strip()}\n")

        audio_name = "audio_main.mp3"
        items = []
        for event in rows:
            seconds = event.start_sample / self.TRANSCRIBE_RATE
            items.append(
                '<div class="line" data-time="{time:.3f}" tabindex="0">'
                '<span class="time">{stamp}</span>'
                '<span class="lang">{lang}</span>'
                '<span class="text">{text}</span></div>'.format(
                    time=seconds, stamp=fmt_timestamp(seconds),
                    lang=html.escape(
                        f"{event.speaker}/{event.language}" if event.speaker
                        else event.language), text=html.escape(event.text.strip())))
        title = html.escape(self._meeting_name or "文字起こし（補正済み）")
        html_path = os.path.join(self._recording_dir, "transcription_final.html")
        document = f"""<!doctype html>
<html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>{title}</title><style>
body{{font-family:"BIZ UDPGothic","Yu Gothic",sans-serif;max-width:960px;margin:32px auto;padding:0 18px;color:#1f2937}}
h1{{font-size:1.5rem}} audio{{position:sticky;top:0;width:100%;background:white;padding:8px 0}}
.line{{display:grid;grid-template-columns:76px 44px 1fr;gap:8px;padding:9px 6px;border-bottom:1px solid #e5e7eb;cursor:pointer}}
.line:hover{{background:#eff6ff}} .time{{color:#2563eb;font-family:monospace}} .lang{{color:#6b7280}}
</style></head><body><h1>{title}</h1>
<p>補正済みリアルタイム文字起こし。行をクリックすると音声が移動します。</p>
<audio id="audio" controls preload="metadata" src="{html.escape(audio_name)}"></audio>
<main>{''.join(items)}</main><script>
const audio=document.getElementById('audio');
document.querySelectorAll('.line').forEach(x=>x.addEventListener('click',()=>{{audio.currentTime=Number(x.dataset.time);audio.play();}}));
</script></body></html>"""
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(document)
        self.root.after(0, self._log,
            "補正済み最終稿 -> transcription_final.txt / transcription_final.html")

    def _transcription_stop(self):
        # Let the worker flush its VAD/tail before dropping the recognizer.
        if self._transcribe_thread is not None:
            self._transcribe_thread.join()
            self._transcribe_thread = None
        if self.REALTIME_BACKEND == "fast_ja_en":
            try:
                self._write_fast_final_outputs()
            except Exception as e:
                self.root.after(0, self._log, f"[補正済み最終稿の出力エラー] {e}")
        self.transcriber = None
        if self.transcription_file:
            self.transcription_file.close()
            self.transcription_file = None
            self.root.after(0, self._log, "文字起こし完了 -> transcription.txt")
        if self.refined_transcription_file:
            self.refined_transcription_file.close()
            self.refined_transcription_file = None
        self.transcribe_queue = None
        self.transcribe_role_queues = {}
