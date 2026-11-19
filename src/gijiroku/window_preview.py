"""Live preview of one Windows Graphics Capture window in Tk."""
from collections import deque
import ctypes
from ctypes import wintypes
import os
import time
import tkinter as tk
from tkinter import ttk

from PIL import Image, ImageTk


def pick_window(root):
    """Intercept one click over the virtual desktop and resolve the underlying HWND."""
    user32 = ctypes.windll.user32
    user32.WindowFromPoint.argtypes = (wintypes.POINT,)
    user32.WindowFromPoint.restype = wintypes.HWND
    user32.GetAncestor.argtypes = (wintypes.HWND, wintypes.UINT)
    user32.GetAncestor.restype = wintypes.HWND
    state = root.state()
    overlay = tk.Toplevel(root)
    overlay.withdraw()
    selection = None

    def select(event):
        nonlocal selection
        point = wintypes.POINT(event.x_root, event.y_root)
        overlay.withdraw()
        hwnd = user32.GetAncestor(user32.WindowFromPoint(point), 2)  # GA_ROOT
        if hwnd:
            handle = wintypes.HWND(hwnd)
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(handle, ctypes.byref(pid))
            title = ctypes.create_unicode_buffer(user32.GetWindowTextLengthW(handle) + 1)
            cls = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(handle, cls, len(cls))
            if (pid.value != os.getpid() and cls.value not in
                    ("Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd")
                    and user32.GetWindowTextW(handle, title, len(title))):
                selection = (title.value, hwnd)
        overlay.destroy()

    try:
        root.withdraw()
        x, y, width, height = (user32.GetSystemMetrics(i) for i in range(76, 80))
        overlay.overrideredirect(True)
        overlay.geometry(f"{width}x{height}+{x}+{y}")
        overlay.configure(bg="black", cursor="crosshair")
        overlay.attributes("-alpha", 0.18, "-topmost", True)
        overlay.bind("<ButtonRelease-1>", select)
        overlay.bind("<Escape>", lambda _: overlay.destroy())
        overlay.deiconify()
        overlay.lift()
        overlay.focus_force()
        root.wait_window(overlay)
    finally:
        if overlay.winfo_exists():
            overlay.destroy()
        root.state(state)
        root.lift()
        root.focus_force()
    return selection


class WindowPreview(ttk.Frame):
    def __init__(self, parent, choose_window):
        super().__init__(parent, padding=6)
        self.control = None
        self.target = None
        self.latest_image = None
        self.timer = None
        self.photo = None
        bar = ttk.Frame(self)
        bar.pack(fill="x", pady=(0, 6))
        self.pick_button = ttk.Button(bar, text="ウィンドウを選ぶ", command=choose_window)
        self.pick_button.pack(side="left")
        self.reconnect_button = ttk.Button(bar, text="再接続", command=self.reconnect,
                                           state="disabled")
        self.reconnect_button.pack(side="left", padx=6)
        self.stop_button = ttk.Button(bar, text="停止", width=5, command=self.stop,
                                      state="disabled")
        self.stop_button.pack(side="right")
        self.canvas = tk.Canvas(self, width=240, height=160, bg="#18212b", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self.status = ttk.Label(self, text="取り込むウィンドウをクリックしてください（Esc でキャンセル）",
                                wraplength=440)
        self.status.pack(anchor="w", pady=(4, 0))
        self.bind("<Destroy>", self._destroy, add="+")

    def reconnect(self):
        if self.target is not None:
            self.start_window(*self.target, retry=True)

    def start_window(self, title, hwnd, retry=False):
        self.stop()
        self.target = (title, hwnd)
        self.reconnect_button.configure(state="normal")
        frames = deque(maxlen=1)
        closed = deque(maxlen=1)
        try:
            from windows_capture import WindowsCapture
            capture = WindowsCapture(window_hwnd=hwnd, cursor_capture=None)

            @capture.event
            def on_frame_arrived(frame, control):
                if frame.width and frame.height:
                    # Release the native mapped surface on the capture thread.
                    frames.append(frame.frame_buffer.copy())

            @capture.event
            def on_closed():
                closed.append(True)

            self.control = capture.start_free_threaded()
        except Exception as exc:
            self.status.configure(text=f"プレビューを開始できません: {exc}")
            return

        if not self.pick_button.instate(["disabled"]):
            self.stop_button.configure(state="normal")
        self.status.configure(text=f"映像を待っています（最小化を解除してください）: {title}")
        source = None
        last_size = None
        needs_draw = False
        last_arrival = time.monotonic()
        # A static window can legitimately stop emitting frames. Re-arm the
        # watchdog only after the replacement session delivers ongoing updates.
        first_arrival = None
        watchdog_armed = not retry

        def draw():
            nonlocal source, last_size, needs_draw
            nonlocal last_arrival, first_arrival, watchdog_armed
            self.timer = None
            if closed or self.control.is_finished():
                self.stop()
                self.status.configure(text="ウィンドウのキャプチャが終了しました")
                return
            size = (max(1, self.canvas.winfo_width()), max(1, self.canvas.winfo_height()))
            # Keep polling even after a transient image conversion or Tk error.
            self.timer = self.after(100, draw)
            if frames:
                last_arrival = time.monotonic()
                if first_arrival is None:
                    first_arrival = last_arrival
                elif last_arrival - first_arrival >= 1:
                    watchdog_armed = True
                if source is None:
                    self.status.configure(text=title)
                pixels = frames.pop()
                height, width = pixels.shape[:2]
                source = Image.frombytes("RGB", (width, height), pixels, "raw", "BGRX")
                self.latest_image = source
                needs_draw = True
            elif (watchdog_armed and time.monotonic() - last_arrival > 12
                  and not ctypes.windll.user32.IsIconic(wintypes.HWND(hwnd))):
                self.reconnect()
                return
            if source is not None and (needs_draw or size != last_size) and self.canvas.winfo_ismapped():
                scale = min(size[0] / source.width, size[1] / source.height)
                target = (max(1, round(source.width * scale)), max(1, round(source.height * scale)))
                self.photo = ImageTk.PhotoImage(source.resize(target, Image.Resampling.BILINEAR))
                self.canvas.delete("frame")
                self.canvas.create_image(size[0]//2, size[1]//2, image=self.photo, tags="frame")
                last_size = size
                needs_draw = False

        draw()

    def stop(self):
        self.target = None
        self.reconnect_button.configure(state="disabled")
        if self.timer is not None:
            self.after_cancel(self.timer)
            self.timer = None
        if self.control is not None:
            control, self.control = self.control, None
            if not control.is_finished():
                control.stop()
        self.canvas.delete("frame")
        self.latest_image = None
        self.photo = None
        self.stop_button.configure(state="disabled")
        self.status.configure(text="プレビュー停止中")

    def _destroy(self, event):
        if event.widget is self and self.control is not None:
            if not self.control.is_finished():
                self.control.stop()
            self.control = None
