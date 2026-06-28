import os
import re
import sys
import json
import time
import queue
import ctypes
import datetime
import threading
import subprocess
import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext, filedialog

import numpy as np
import mss
from PIL import Image
import imagehash
import sounddevice as sd
import pyaudiowpatch as pyaudio

FFMPEG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ffmpeg.exe")
SETTINGS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "settings.json")

LANG_OPTIONS = [("日本語", "ja"), ("English", "en"), ("中文", "zh"), ("한국어", "ko")]

# Sentinel placed on a capture queue to signal end-of-stream to the mixer.
_EOF = object()


class MeetingRecorderGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("SlideSnap v2 - リモート会議記録システム")
        self.root.geometry("620x860")
        self.root.resizable(False, False)

        self.is_recording = False
        self.stop_event = threading.Event()
        self.ffmpeg_proc = None
        self.transcriber = None
        self.transcription_file = None
        self._transcribe_start = 0
        self._record_start = 0
        self._audio_level = 0.0
        self._preloaded_transcriber = None
        self._recording_dir = None
        self._recording_mon_idx = 0
        self._pipeline_ctx = None
        self._pipeline_thread = None
        self._last_record_dir = None
        self._queue_overflow_count = 0
        self._meeting_name = ""

        self._audio_devices = []        # list of enumerated device dicts
        self._audio_queues = []         # per-device capture queues (during recording)
        self.transcribe_queue = None

        self.INTERVAL = 5.0
        self.DHASH_THRESHOLD = 10
        self.JPEG_QUALITY = 85
        self.AUDIO_GAIN = 2.0
        self.TARGET_RATE = 44100
        self.TRANSCRIBE_RATE = 16000
        self.TRANSCRIBE_BUF_SECONDS = 1.0

        self._enumerate_audio_devices()
        self._load_settings()
        self.create_widgets()
        self._populate_audio_listbox()
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
        except Exception as e:
            print(f"[設定読み込み警告] {e}")

    def _save_settings(self):
        try:
            with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
                json.dump({
                    "dhash_threshold": self.DHASH_THRESHOLD,
                    "audio_gain": self.AUDIO_GAIN,
                    "jpeg_quality": self.JPEG_QUALITY,
                }, f, indent=2, ensure_ascii=False)
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

        ttk.Label(frame_set, text="対象画面:").grid(row=1, column=0, sticky="w", pady=2)
        self.combo_monitor = ttk.Combobox(frame_set, width=42, state="readonly")
        self.combo_monitor.grid(row=1, column=1, padx=10, pady=2)

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
        self.btn_refresh_audio = ttk.Button(audio_frame, text="🔄 デバイス再検出",
            command=self._refresh_audio_devices)
        self.btn_refresh_audio.pack(anchor="w", pady=(4, 0))

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

        row_post = ttk.Frame(frame_ctrl)
        row_post.pack(fill="x", pady=(3, 0))
        self.btn_postprocess = tk.Button(
            row_post, text="📄 議事録を生成（後処理）", bg="#0ea5e9", fg="white",
            font=("BIZ UDゴシック", 10, "bold"), relief="raised", padx=10, pady=6,
            command=self._run_postprocess)
        self.btn_postprocess.pack(fill="x")
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

    def _enumerate_audio_devices(self):
        """Enumerate all capturable audio devices into a unified list.
        Each entry: {key, kind, native_idx, name, channels, rate}
          key: 'S<native_idx>' (speaker/loopback) / 'M<native_idx>' (mic)
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
                    })
        except Exception as e:
            print(f"[デバイス列挙警告: speaker] {e}")
        finally:
            p.terminate()

        # --- Mic/input via sounddevice ---
        try:
            for info in sd.query_devices():
                if info.get("max_input_channels", 0) <= 0:
                    continue
                name = info["name"]
                # Skip WASAPI loopbacks sounddevice also surfaces as inputs, and
                # any input already listed on the speaker side (dedupe by name).
                if "Loopback" in name or name in speaker_names:
                    continue
                devices.append({
                    "key": f"M{info['index']}",
                    "kind": "mic",
                    "native_idx": info["index"],
                    "name": name,
                    "channels": int(info["max_input_channels"]),
                    "rate": int(info["default_samplerate"]),
                })
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
            self.listbox_audio.insert(tk.END, f"{prefix}{dev['name']}")
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
        lang = LANG_OPTIONS[self.combo_lang.current()][1]

        def _load():
            try:
                from moonshine_voice import Transcriber, get_model_for_language
                label = LANG_OPTIONS[self.combo_lang.current()][0]
                self.root.after(0, self._log, f"文字起こしモデルを事前読み込み中... ({label})")
                model_path, model_arch = get_model_for_language(lang)
                self._preloaded_transcriber = Transcriber(
                    model_path=model_path, model_arch=model_arch,
                    update_interval=0.3)
                self.root.after(0, self._log, f"文字起こしモデル読み込み完了 ({label})")
            except Exception as e:
                self.root.after(0, self._log, f"[事前読み込みエラー] {e}")
        threading.Thread(target=_load, daemon=True).start()

    def _on_lang_changed(self, _=None):
        label = LANG_OPTIONS[self.combo_lang.current()][0]
        lang = LANG_OPTIONS[self.combo_lang.current()][1]

        if self.is_recording and self.transcriber is not None:
            # Switch transcriber mid-recording
            def _switch():
                try:
                    from moonshine_voice import Transcriber, get_model_for_language
                    from moonshine_voice.transcriber import LineCompleted

                    self.root.after(0, self._log, f"言語切替中... → {label}")

                    # Stop current
                    try:
                        self.transcriber.stop()
                        self.transcriber.close()
                    except Exception:
                        pass
                    self.transcriber = None

                    # Load new
                    model_path, model_arch = get_model_for_language(lang)
                    self.transcriber = Transcriber(
                        model_path=model_path, model_arch=model_arch,
                        update_interval=0.3)

                    gui = self
                    def _on_event(event):
                        if isinstance(event, LineCompleted) and event.line.text.strip():
                            text = event.line.text.strip()
                            gui.root.after(0, gui._log_transcript, text)
                            if gui.transcription_file:
                                elapsed = time.time() - gui._transcribe_start
                                gui.transcription_file.write(f"[{elapsed:.1f}s] {text}\n")
                                gui.transcription_file.flush()
                    self.transcriber.add_listener(_on_event)
                    self.transcriber.start()
                    self.root.after(0, self._log, f"言語切替完了 → {label}")
                    self._write_language_event(lang, label)
                except Exception as e:
                    self.root.after(0, self._log, f"[言語切替エラー] {e}")
            threading.Thread(target=_switch, daemon=True).start()
        else:
            # Pre-load for next recording
            self._preloaded_transcriber = None
            if self.combo_mode.current() == 1:
                self._preload_model()

    def _run_postprocess(self):
        initial_dir = self._last_record_dir if self._last_record_dir else None
        folder = filedialog.askdirectory(
            title="議事録生成対象フォルダを選択", initialdir=initial_dir)
        if not folder:
            return
        self.btn_postprocess.config(state="disabled", text="⏳ 処理中...")
        self.label_status.config(text="ステータス: 議事録生成中...", foreground="#f59e0b")
        self._log(f"後処理開始: {folder}")
        threading.Thread(target=self._postprocess_worker, args=(folder,), daemon=True).start()

    def _postprocess_worker(self, folder):
        import io

        old_stdout = sys.stdout
        old_stderr = sys.stderr
        captured = io.StringIO()
        try:
            sys.stdout = captured
            sys.stderr = captured
            post_process_folder(folder)
        except Exception as e:
            self.root.after(0, self._log, f"[後処理エラー] {e}")
        finally:
            sys.stdout = old_stdout
            sys.stderr = old_stderr
            output = captured.getvalue()
            for line in output.strip().splitlines():
                self.root.after(0, self._log, f"[後処理] {line}")
            md_path = os.path.join(folder, "meeting_report.md")
            result = "成功" if os.path.exists(md_path) else "失敗（Markdown未生成）"
            self.root.after(0, self._log, f"後処理完了 ({result})")
            self.root.after(0, self.btn_postprocess.config,
                {"state": "normal", "text": "📄 議事録を生成（後処理）"})
            if not self.is_recording:
                self.root.after(0, self.label_status.config,
                    {"text": "ステータス: 停止中", "foreground": "#6b7280"})

    def _open_settings_dialog(self):
        dlg = tk.Toplevel(self.root)
        dlg.title("SlideSnap - 設定")
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

        # Buttons
        btn_frame = ttk.Frame(dlg)
        btn_frame.pack(padx=15, pady=(0, 15))

        def _ok():
            self.DHASH_THRESHOLD = var_dhash.get()
            self.AUDIO_GAIN = var_gain.get()
            self.JPEG_QUALITY = var_jpeg.get()
            self._save_settings()
            self._log(f"設定更新: dHash={self.DHASH_THRESHOLD}, ゲイン={self.AUDIO_GAIN:.1f}, JPEG={self.JPEG_QUALITY}")
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
                    "diff": diff,
                }, ensure_ascii=False) + "\n")
        except Exception as e:
            print(f"[snapshot log書き込み警告] {e}")

    # ------------------------------------------------------------ Logging

    def _manual_snapshot(self):
        if not self.is_recording or self._recording_dir is None:
            return
        try:
            with mss.MSS() as sct:
                monitor = sct.monitors[self._recording_mon_idx]
                cap = sct.grab(monitor)
            img = Image.frombytes("RGB", cap.size, cap.bgra, "raw", "BGRX")
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

    def _log_transcript(self, text):
        self.transcript_area.config(state="normal")
        ts = datetime.datetime.now().strftime('%H:%M:%S')
        self.transcript_area.insert(tk.END, f"[{ts}] {text}\n")
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
            self.combo_monitor["values"] = vals
            self.combo_monitor.current(0)

    # -------------------------------------------------------------- Control

    @staticmethod
    def _sanitize_name(name):
        """Make a string safe to embed in a folder/file name (Windows)."""
        cleaned = re.sub(r'[\\/:*?"<>|]', "_", name)   # invalid filename chars
        cleaned = re.sub(r"\s+", " ", cleaned).strip()  # collapse whitespace
        cleaned = cleaned.rstrip(". ")                  # trailing dots/spaces
        return cleaned

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
            self._meeting_name = self.entry_meeting.get().strip()
            self.is_recording = True
            self.stop_event.clear()
            self._record_start = time.time()
            self._recording_mon_idx = self.combo_monitor.current()
            self.btn_toggle.config(text="■ 記録を停止して保存", bg="#ef4444")
            self.btn_manual_snap.config(state="normal")
            for w in (self.combo_monitor, self.combo_mode):
                w.config(state="disabled")
            self.listbox_audio.config(state="disabled")
            self.btn_refresh_audio.config(state="disabled")
            self.entry_meeting.config(state="disabled")
            # combo_lang stays enabled for mid-recording language switching
            self._tick_elapsed_timer()
            self._pipeline_thread = threading.Thread(target=self._pipeline, daemon=True)
            self._pipeline_thread.start()
        else:
            # UI resets instantly; cleanup runs in background
            self.is_recording = False
            self.stop_event.set()
            self.btn_toggle.config(state="disabled")
            self.btn_manual_snap.config(state="disabled")
            self.label_status.config(text="ステータス: 保存処理中...", foreground="#f59e0b")
            threading.Thread(target=self._async_cleanup, daemon=True).start()

    def _reset_ui(self):
        self.is_recording = False
        self.stop_event.clear()
        self._audio_level = 0.0
        self._queue_overflow_count = 0
        self._recording_dir = None
        self.btn_toggle.config(text="▶ 会議記録を開始", bg="#10b981", state="normal")
        self.btn_manual_snap.config(state="disabled")
        self.label_status.config(text="ステータス: 停止中", foreground="#6b7280")
        for w in (self.combo_monitor, self.combo_mode):
            w.config(state="readonly")
        self.listbox_audio.config(state="normal")
        self.btn_refresh_audio.config(state="normal")
        self.entry_meeting.config(state="normal")

    # ----------------------------------------------------------- Main pipeline

    def _pipeline(self):
        now = datetime.datetime.now()
        stamp = now.strftime("%Y%m%d_%H%M%S")
        safe = self._sanitize_name(self._meeting_name)
        suffix = safe if safe else "Meeting"
        dir_name = os.path.join(os.path.dirname(os.path.abspath(__file__)), f"{stamp}_{suffix}")
        os.makedirs(dir_name, exist_ok=True)
        self._recording_dir = dir_name
        self.root.after(0, self._log, f"フォルダー作成: {dir_name}")

        lang = LANG_OPTIONS[self.combo_lang.current()][1]
        label = LANG_OPTIONS[self.combo_lang.current()][0]
        self._write_language_event(lang, label)

        start_epoch = time.time()
        selected = self._selected_audio_devices()
        n = len(selected)
        mode_full = self.combo_mode.current() == 1
        out_mp3 = os.path.join(dir_name, "audio_main.mp3")

        # One dedicated queue per selected device
        self._audio_queues = [queue.Queue(maxsize=200) for _ in range(n)]
        self.transcribe_queue = queue.Queue(maxsize=400) if mode_full else None

        self.ffmpeg_proc = self._ffmpeg_start(out_mp3)
        if self.ffmpeg_proc is None:
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

        if n == 1:
            writer_t = threading.Thread(
                target=self._single_writer,
                args=(active, self._audio_queues[0]), daemon=True)
            writer_t.start()
            mixer_t = None
        else:
            mixer_t = threading.Thread(
                target=self._mixer_loop_n,
                args=(active, self._audio_queues), daemon=True)
            mixer_t.start()
            writer_t = None

        # Screen capture
        next_target = time.time() + self.INTERVAL
        last_hash = None
        snap_count = 0
        mon_idx = self.combo_monitor.current()

        with mss.MSS() as sct:
            monitor = sct.monitors[mon_idx]
            while self.is_recording and not self.stop_event.is_set():
                dt = next_target - time.time()
                if dt > 0:
                    time.sleep(dt)
                try:
                    img = Image.frombytes(
                        "RGB", (cap := sct.grab(monitor)).size,
                        cap.bgra, "raw", "BGRX")
                    h = imagehash.dhash(img)
                except Exception as e:
                    self.root.after(0, self._log, f"[画面エラー] {e}")
                    next_target += self.INTERVAL
                    continue

                diff = 0
                changed = last_hash is None or (h - last_hash) > self.DHASH_THRESHOLD
                if changed:
                    if last_hash is not None:
                        diff = h - last_hash
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
            'meeting_name': self._meeting_name,
        }

    def _async_cleanup(self):
        """Run teardown in background so UI stays responsive."""
        # Wait for pipeline thread to finish setting _pipeline_ctx
        if self._pipeline_thread:
            self._pipeline_thread.join(timeout=10)
            self._pipeline_thread = None
        ctx = self._pipeline_ctx
        if ctx is None:
            self._ffmpeg_stop()
            self.root.after(0, self._reset_ui)
            return

        active = ctx['active']
        active.clear()
        for t in ctx['threads']:
            t.join(timeout=3)
        if ctx['mixer_t']:
            ctx['mixer_t'].join(timeout=3)
        if ctx['writer_t']:
            ctx['writer_t'].join(timeout=3)

        self._ffmpeg_stop()
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
            f.write("AUDIO_FILE=audio_main.mp3\n")
            f.write(f"LANGUAGE={ctx['lang']}\n")
            if ctx['mode_full']:
                f.write("TRANSCRIPTION_FILE=transcription.txt\n")

        elapsed = time.time() - ctx['start_epoch']
        m, s = divmod(int(elapsed), 60)
        msg = f"記録完了 ({m:02d}:{s:02d})。画像: {ctx['snap_count']}枚 / 保存先: {dir_name}"
        if self._queue_overflow_count > 0:
            msg += f" / ⚠ キュー溢れ: {self._queue_overflow_count}回"
        self._pipeline_ctx = None
        self._last_record_dir = dir_name
        self.root.after(0, self._log, msg)
        self.root.after(0, messagebox.showinfo, "完了",
            f"すべての記録が正常に保存されました。\n\nフォルダー:\n{dir_name}")
        self.root.after(0, self._reset_ui)

    # --------------------------------------------------- FFmpeg (Captura pattern)

    def _ffmpeg_start(self, path):
        try:
            flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            return subprocess.Popen(
                [FFMPEG_PATH,
                 "-f", "s16le", "-acodec", "pcm_s16le",
                 "-ar", str(self.TARGET_RATE), "-ac", "2", "-i", "-",
                 "-c:a", "libmp3lame", "-b:a", "192k", "-y", path],
                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, creationflags=flags)
        except Exception as e:
            self.root.after(0, self._log, f"[ffmpegエラー] {e}")
            return None

    def _ffmpeg_write(self, pcm_bytes):
        if self.ffmpeg_proc and self.ffmpeg_proc.poll() is None:
            try:
                self.ffmpeg_proc.stdin.write(pcm_bytes)
            except (BrokenPipeError, OSError, ValueError):
                pass

    def _ffmpeg_stop(self):
        if not self.ffmpeg_proc:
            return
        try:
            self.ffmpeg_proc.stdin.close()
        except Exception as e:
            print(f"[ffmpeg終了警告] stdin: {e}")
        try:
            self.ffmpeg_proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.ffmpeg_proc.kill()
            try:
                self.ffmpeg_proc.wait(timeout=5)
            except Exception as e:
                print(f"[ffmpeg終了警告] kill: {e}")
        except Exception as e:
            print(f"[ffmpeg終了警告] wait: {e}")
        self.root.after(0, self._log, "ffmpeg エンコード完了 -> MP3 保存済み")
        self.ffmpeg_proc = None

    # ---------------------------------------------------- Audio normalization

    def _to_stereo_s16le(self, raw_bytes, channels, rate):
        data = np.frombuffer(raw_bytes, dtype=np.int16).astype(np.float32)
        if channels == 1:
            data = np.column_stack([data, data]).flatten()
        elif channels > 2:
            data = data.reshape(-1, channels)[:, :2].flatten()
        if rate != self.TARGET_RATE and len(data) >= 4:
            stereo = data.reshape(-1, 2)
            n_in = len(stereo)
            n_out = max(1, int(n_in * self.TARGET_RATE / rate))
            x_in = np.arange(n_in, dtype=np.float64)
            x_out = np.linspace(0, n_in - 1, n_out)
            left = np.interp(x_out, x_in, stereo[:, 0])
            right = np.interp(x_out, x_in, stereo[:, 1])
            data = np.column_stack([left, right]).flatten()
        data *= self.AUDIO_GAIN
        np.clip(data, -32768, 32767, out=data)
        return data.astype(np.int16).tobytes()

    # ------------------------------------------------- Transcribe forwarding

    def _push_transcribe(self, pcm):
        """Forward a PCM chunk to the transcription queue (no-op in light mode)."""
        if self.transcribe_queue is not None:
            try:
                self.transcribe_queue.put_nowait(pcm)
            except queue.Full:
                self._queue_overflow_count += 1

    # ------------------------------------------- Single-source writer thread

    def _single_writer(self, active, src_queue):
        """Direct writer for the single-device case (no mixing)."""
        while (active.is_set() or not src_queue.empty()) and not self.stop_event.is_set():
            try:
                pcm = src_queue.get(timeout=0.2)
            except queue.Empty:
                continue
            if pcm is _EOF:
                break
            self._update_level(pcm)
            self._ffmpeg_write(pcm)
            self._push_transcribe(pcm)

    # ------------------------------------------------- Mixer thread (N inputs)

    def _mixer_loop_n(self, active, queues):
        """Mix N capture queues into one PCM stream.

        Each input queue may receive the _EOF sentinel to mark end-of-stream for
        that source. Sources that reach EOF contribute silence; mixing continues
        with the remaining live sources until every source is exhausted.
        """
        N = len(queues)
        MIX_FRAMES = 1024
        FRAME_BYTES = 4                      # stereo int16 = 4 bytes
        MIX_BYTES = MIX_FRAMES * FRAME_BYTES  # 4096 bytes per output chunk
        OVERFLOW = MIX_BYTES * 50            # ~1.2 sec buffer limit

        bufs = [bytearray() for _ in range(N)]
        eof = [False] * N

        def _emit(raw):
            self._update_level(raw)
            self._ffmpeg_write(raw)
            self._push_transcribe(raw)

        def _drain(i):
            for _ in range(30):
                item = self._qget(queues[i], timeout=0)
                if item is None:
                    return
                if item is _EOF:
                    eof[i] = True
                    return
                bufs[i].extend(item)

        while not self.stop_event.is_set():
            live = [i for i in range(N) if not eof[i]]

            got = False
            for i in live:
                before = len(bufs[i])
                _drain(i)
                if len(bufs[i]) != before or eof[i]:
                    got = True

            # Mix aligned chunks while every live source has enough data
            while live and all(len(bufs[i]) >= MIX_BYTES for i in live):
                acc = np.zeros(MIX_BYTES // 2, dtype=np.float32)
                for i in live:
                    chunk = np.frombuffer(bytes(bufs[i][:MIX_BYTES]),
                        dtype=np.int16).astype(np.float32)
                    acc += chunk
                    del bufs[i][:MIX_BYTES]
                acc *= (1.0 / len(live))     # average-mix: amplitude stays ≤1.0 as sources grow
                np.clip(acc, -32768, 32767, out=acc)
                _emit(acc.astype(np.int16).tobytes())

            # Overflow fallback: one source bloated while others lag → emit solo
            for i in live:
                others_short = all(
                    len(bufs[k]) < MIX_BYTES
                    for k in range(N) if k != i and not eof[k])
                if others_short and len(bufs[i]) > OVERFLOW:
                    while len(bufs[i]) >= MIX_BYTES:
                        _emit(bytes(bufs[i][:MIX_BYTES]))
                        del bufs[i][:MIX_BYTES]

            # Termination: stop requested and every source reached EOF
            if not active.is_set() and all(eof[i] for i in range(N)):
                break

            if not got:
                time.sleep(0.01)

        # Flush remaining whole-frame data
        for i in range(N):
            usable = len(bufs[i]) - (len(bufs[i]) % FRAME_BYTES)
            if usable:
                _emit(bytes(bufs[i][:usable]))

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
        try:
            if dev["kind"] == "speaker":
                self._capture_speaker_dev(active, dev, out_queue)
            else:
                self._capture_mic_dev(active, dev, out_queue)
        except Exception as e:
            self.root.after(0, self._log, f"[キャプチャエラー] {dev['name']}: {e}")
        finally:
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

    def _capture_speaker_dev(self, active, dev, out_queue):
        p = pyaudio.PyAudio()
        try:
            ch, sr = dev["channels"], dev["rate"]
            self.root.after(0, self._log,
                f"スピーカーキャプチャ開始: {dev['name']} ({sr}Hz, {ch}ch, idx={dev['native_idx']})")

            def _callback(in_data, frame_count, time_info, status):
                pcm = self._to_stereo_s16le(in_data, ch, sr)
                try:
                    out_queue.put_nowait(pcm)
                except queue.Full:
                    pass
                return (None, pyaudio.paContinue)

            stream = p.open(
                format=pyaudio.paInt16, channels=ch, rate=sr,
                input=True, input_device_index=dev["native_idx"],
                frames_per_buffer=1024, stream_callback=_callback)
            stream.start_stream()
            while active.is_set() and self.is_recording:
                time.sleep(0.5)
            stream.stop_stream()
            stream.close()
        finally:
            p.terminate()

    # ------------------------------------------- Mic capture (sounddevice)

    def _capture_mic_dev(self, active, dev, out_queue):
        ch = min(dev["channels"], 2)
        sr = dev["rate"]
        self.root.after(0, self._log, f"マイクキャプチャ開始: {dev['name']} ({sr}Hz)")

        def _callback(in_data, frames, time_info, status):
            pcm = self._to_stereo_s16le(in_data.tobytes(), ch, sr)
            try:
                out_queue.put_nowait(pcm)
            except queue.Full:
                pass

        with sd.InputStream(device=dev["native_idx"], channels=ch,
                            samplerate=sr, dtype='int16',
                            blocksize=1024, callback=_callback):
            while active.is_set() and self.is_recording:
                time.sleep(0.5)

    # ------------------------------------------ Transcription (moonshine-voice)

    def _transcription_start(self, dir_name):
        try:
            from moonshine_voice.transcriber import LineCompleted

            if self._preloaded_transcriber is not None:
                self.transcriber = self._preloaded_transcriber
                self._preloaded_transcriber = None
                self.root.after(0, self._log, "事前読み込み済みモデルを使用")
            else:
                from moonshine_voice import Transcriber, get_model_for_language
                lang = LANG_OPTIONS[self.combo_lang.current()][1]
                label = LANG_OPTIONS[self.combo_lang.current()][0]
                self.root.after(0, self._log, f"文字起こしモデルを読み込み中... ({label})")
                model_path, model_arch = get_model_for_language(lang)
                self.transcriber = Transcriber(
                    model_path=model_path, model_arch=model_arch,
                    update_interval=0.3)

            gui = self
            def _on_event(event):
                if isinstance(event, LineCompleted) and event.line.text.strip():
                    text = event.line.text.strip()
                    gui.root.after(0, gui._log_transcript, text)
                    if gui.transcription_file:
                        elapsed = time.time() - gui._transcribe_start
                        gui.transcription_file.write(f"[{elapsed:.1f}s] {text}\n")
                        gui.transcription_file.flush()

            self.transcriber.add_listener(_on_event)
            self.transcriber.start()

            self.transcription_file = open(
                os.path.join(dir_name, "transcription.txt"), "w", encoding="utf-8")
            self._transcribe_start = time.time()
            self.root.after(0, self._log, "文字起こしスレッド起動")
            threading.Thread(target=self._transcribe_loop, daemon=True).start()

        except Exception as e:
            self.root.after(0, self._log, f"[文字起こしエラー] {e}")
            self.transcriber = None

    def _transcribe_loop(self):
        buf_size = int(self.TARGET_RATE * 2 * self.TRANSCRIBE_BUF_SECONDS)
        buf = np.zeros(buf_size, dtype=np.float32)
        buf_pos = 0

        while self.is_recording and not self.stop_event.is_set():
            if self.transcribe_queue is None or self.transcriber is None:
                time.sleep(0.1)
                continue
            try:
                pcm = self.transcribe_queue.get(timeout=1.0)
            except queue.Empty:
                continue
            try:
                samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
                mono = samples.reshape(-1, 2).mean(axis=1) if len(samples) >= 2 else samples
                n = min(len(mono), buf_size - buf_pos)
                buf[buf_pos:buf_pos + n] = mono[:n]
                buf_pos += n
                if buf_pos >= buf_size:
                    self._feed_transcriber(buf[:buf_pos])
                    buf_pos = 0
            except Exception as e:
                self.root.after(0, self._log, f"[文字起こし処理エラー] {e}")

        if buf_pos > 0 and self.transcriber is not None:
            try:
                self._feed_transcriber(buf[:buf_pos])
            except Exception as e:
                self.root.after(0, self._log, f"[バッファフラッシュ警告] {e}")

    def _feed_transcriber(self, mono_float32):
        n_out = int(len(mono_float32) * self.TRANSCRIBE_RATE / self.TARGET_RATE)
        if n_out <= 0:
            return
        from scipy.signal import resample as _resample
        resampled = _resample(mono_float32, n_out)
        c_arr = (ctypes.c_float * len(resampled))(*resampled)
        self.transcriber.add_audio(c_arr, self.TRANSCRIBE_RATE)

    def _transcription_stop(self):
        if self.transcriber:
            try:
                self.transcriber.stop()
                self.transcriber.close()
            except Exception as e:
                print(f"[文字起こし停止警告] {e}")
            self.transcriber = None
        if self.transcription_file:
            self.transcription_file.close()
            self.transcription_file = None
            self.root.after(0, self._log, "文字起こし完了 -> transcription.txt")
        self.transcribe_queue = None


# ======================================================================
# CLI: Post-processing — whisper transcription + markdown generation
# ======================================================================

def post_process_folder(folder):
    """Transcribe audio in a meeting folder and generate combined markdown."""
    print(f"SlideSnap Post-Processor")
    print(f"Folder: {folder}")
    print()

    if not os.path.isdir(folder):
        raise FileNotFoundError(f"Folder not found: {folder}")

    # Find audio file
    audio_file = None
    for name in ("audio_main.mp3", "audio_main.wav"):
        path = os.path.join(folder, name)
        if os.path.exists(path):
            audio_file = path
            break
    if audio_file is None:
        raise RuntimeError(f"No audio file found in: {folder}")
    print(f"Audio: {os.path.basename(audio_file)}")

    # Read metadata
    meta = {}
    meta_path = os.path.join(folder, "metadata.txt")
    if os.path.exists(meta_path):
        with open(meta_path, "r", encoding="utf-8") as f:
            for line in f:
                if "=" in line:
                    k, v = line.strip().split("=", 1)
                    meta[k] = v
    start_time_str = meta.get("START_TIME_STR", "Unknown")
    default_lang = meta.get("LANGUAGE", "ja")
    meeting_name = meta.get("MEETING_NAME", "")
    print(f"Language: {default_lang}")

    # Read language segments
    lang_segments = []
    seg_path = os.path.join(folder, "language_segments.jsonl")
    if os.path.exists(seg_path):
        with open(seg_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    lang_segments.append(json.loads(line))
        print(f"Language segments: {len(lang_segments)}")

    # List images sorted by name
    images = sorted(
        [f for f in os.listdir(folder) if f.lower().endswith((".jpg", ".jpeg", ".png"))])
    print(f"Images: {len(images)}")

    # Load image entries — prefer snapshots.jsonl, fall back to filename parsing
    img_entries = []
    snap_log_path = os.path.join(folder, "snapshots.jsonl")
    if os.path.exists(snap_log_path):
        with open(snap_log_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    entry = json.loads(line)
                    img_entries.append({
                        "file": entry["file"],
                        "time": entry["elapsed"],
                        "type": entry.get("type", "auto"),
                    })
        print(f"Image entries (from snapshots.jsonl): {len(img_entries)}")
    else:
        # Fallback: parse timestamps from filenames
        def parse_image_time(fname):
            try:
                parts = fname.replace(".jpg", "").replace(".jpeg", "").replace(".png", "").split("_")
                hhmmss = parts[1]
                h, m, s = int(hhmmss[:2]), int(hhmmss[2:4]), int(hhmmss[4:6])
                if "START_TIME_STR" in meta:
                    st = datetime.datetime.strptime(meta["START_TIME_STR"], "%Y-%m-%d %H:%M:%S")
                    img_time = st.replace(hour=h, minute=m, second=s)
                    return (img_time - st).total_seconds()
            except Exception:
                pass
            return None

        for img in images:
            t = parse_image_time(img)
            if t is not None:
                img_entries.append({"file": img, "time": t, "type": "auto"})
        print(f"Image entries (from filename parsing): {len(img_entries)}")
    img_entries.sort(key=lambda x: x["time"])

    # Decode audio to float32 16kHz mono
    wav_tmp = os.path.join(folder, "_tmp_16k.wav")
    subprocess.run(
        [FFMPEG_PATH, "-y", "-i", audio_file,
         "-ar", "16000", "-ac", "1", "-f", "wav", wav_tmp],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)

    import wave
    with wave.open(wav_tmp, "rb") as wf:
        n_frames = wf.getnframes()
        sr = wf.getframerate()
        raw = wf.readframes(n_frames)
    os.remove(wav_tmp)
    audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    duration = len(audio) / sr
    print(f"Audio duration: {duration:.1f}s ({sr}Hz)")

    # Transcribe — segment by language if segments exist, else single pass
    print("Transcribing with moonshine-voice...")
    from moonshine_voice import Transcriber, get_model_for_language

    lines = []

    if lang_segments:
        # Build segment boundaries: [(start_sec, end_sec, lang), ...]
        segments = []
        for seg in lang_segments:
            start = seg["elapsed"]
            lang = seg["lang"]
            segments.append({"start": start, "lang": lang})
        # Sort by start time
        segments.sort(key=lambda x: x["start"])

        for i, seg in enumerate(segments):
            start_s = seg["start"]
            end_s = segments[i + 1]["start"] if i + 1 < len(segments) else duration
            lang = seg["lang"]
            label = seg.get("label", lang)
            start_sample = int(start_s * sr)
            end_sample = min(int(end_s * sr), len(audio))
            seg_audio = audio[start_sample:end_sample]

            if len(seg_audio) < sr * 0.5:  # skip very short segments
                print(f"  Segment [{start_s:.1f}s - {end_s:.1f}s] {label}: skipped (< 0.5s)")
                continue

            print(f"  Segment [{start_s:.1f}s - {end_s:.1f}s] {label}...")
            model_path, model_arch = get_model_for_language(lang)
            transcriber = Transcriber(model_path=model_path, model_arch=model_arch)
            transcript = transcriber.transcribe_without_streaming(seg_audio.tolist(), sr)
            transcriber.close()

            for line in transcript.lines:
                if line.text.strip():
                    abs_start = start_s + (line.start_time if line.start_time else 0.0)
                    lines.append({"text": line.text.strip(), "start": abs_start})
    else:
        # Single-pass transcription (backward compatible)
        model_path, model_arch = get_model_for_language(default_lang)
        transcriber = Transcriber(model_path=model_path, model_arch=model_arch)
        transcript = transcriber.transcribe_without_streaming(audio.tolist(), sr)
        transcriber.close()

        for line in transcript.lines:
            if line.text.strip():
                start_s = line.start_time if line.start_time else 0.0
                lines.append({"text": line.text.strip(), "start": start_s})

    lines.sort(key=lambda x: x["start"])
    print(f"Transcription lines: {len(lines)}")

    # Generate markdown
    md_path = os.path.join(folder, "meeting_report.md")
    with open(md_path, "w", encoding="utf-8") as f:
        title = meeting_name if meeting_name else start_time_str
        f.write(f"# 会議記録 - {title}\n\n")
        f.write(f"| 項目 | 値 |\n|---|---|\n")
        if meeting_name:
            f.write(f"| 会議名 | {meeting_name} |\n")
        f.write(f"| 開始時刻 | {start_time_str} |\n")
        f.write(f"| 録音時間 | {duration:.0f}s ({duration/60:.1f}min) |\n")
        f.write(f"| 画像数 | {len(img_entries)} |\n")
        f.write(f"| 音声ソース | {meta.get('AUDIO_SOURCE', '-')} |\n")
        f.write(f"| 言語 | {default_lang} |\n")
        if lang_segments:
            lang_summary = ", ".join(
                f"{s.get('label', s['lang'])} ({s['start']:.0f}s〜)" for s in lang_segments)
            f.write(f"| 言語切替 | {lang_summary} |\n")
        f.write("\n---\n\n")

        # Merge images and transcription by time
        li = 0  # line index
        ii = 0  # image index

        while li < len(lines) or ii < len(img_entries):
            line_time = lines[li]["start"] if li < len(lines) else float('inf')
            img_time = img_entries[ii]["time"] if ii < len(img_entries) else float('inf')

            if img_time <= line_time and ii < len(img_entries):
                ent = img_entries[ii]
                m, s = divmod(int(ent["time"]), 60)
                tag = "自動" if ent.get("type") == "auto" else "手動"
                f.write(f"### [{m:02d}:{s:02d}] 画面キャプチャ ({tag})\n\n")
                f.write(f"![{ent['file']}]({ent['file']})\n\n")
                ii += 1
            elif li < len(lines):
                ln = lines[li]
                m, s = divmod(int(ln["start"]), 60)
                f.write(f"**[{m:02d}:{s:02d}]** {ln['text']}\n\n")
                li += 1

        f.write("---\n\n")
        f.write("*Generated by SlideSnap v2*\n")

    print(f"\nDone! Markdown saved to: {md_path}")
    print(f"  - {len(lines)} transcription lines")
    print(f"  - {len(img_entries)} images")

    print(f"\nDone! Markdown saved to: {md_path}")
    print(f"  - {len(lines)} transcription lines")
    print(f"  - {len(images)} images")


# ======================================================================

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="SlideSnap v2")
    parser.add_argument("--post-process", metavar="FOLDER",
        help="指定フォルダの音声を高精度文字起こしし、スクリーンショットと統合したMarkdownを出力")
    args = parser.parse_args()

    if args.post_process:
        try:
            post_process_folder(args.post_process)
        except Exception as e:
            print(f"[ERROR] {e}")
            sys.exit(1)
    else:
        root = tk.Tk()
        app = MeetingRecorderGUI(root)

        def _on_close():
            if app.is_recording:
                app.is_recording = False
                app.stop_event.set()
                root.after(2000, root.destroy)
            else:
                root.destroy()

        root.protocol("WM_DELETE_WINDOW", _on_close)
        root.mainloop()
