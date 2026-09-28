"""Video import and scene capture."""
import datetime
import json
import os
import shutil
import subprocess
import time
from PIL import Image
import imagehash
from ..paths import BASE_DIR, FFMPEG_PATH, sanitize_name
from .common import PostProcessCancelled

# --------------------------------------------------------------- Entry point

def _extract_video_snapshots(video_path, folder, interval=5.0, threshold=10,
                             max_edge=1600, jpeg_quality=85,
                             progress=None, cancel=None):
    """Sample a video and keep frames whose dHash changed significantly."""
    frame_dir = os.path.join(folder, ".video_frames")
    os.makedirs(frame_dir)
    pattern = os.path.join(frame_dir, "frame_%06d.jpg")
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    args = [
        FFMPEG_PATH, "-y", "-loglevel", "error", "-i", video_path,
        "-vf", f"fps=1/{max(0.1, float(interval))}", "-q:v", "3", pattern,
    ]
    proc = subprocess.Popen(
        args, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        creationflags=flags)
    while proc.poll() is None:
        if cancel is not None and cancel():
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
            raise PostProcessCancelled()
        time.sleep(0.2)
    stderr = proc.stderr.read().decode("utf-8", errors="replace").strip()
    if proc.returncode != 0:
        detail = stderr.splitlines()[-1] if stderr else "フレーム抽出に失敗しました"
        raise RuntimeError(f"動画の画像を抽出できませんでした: {detail}")

    frames = sorted(os.path.join(frame_dir, name) for name in os.listdir(frame_dir)
                    if name.lower().endswith(".jpg"))
    kept = []
    last_hash = None
    try:
        for index, source in enumerate(frames):
            if cancel is not None and cancel():
                raise PostProcessCancelled()
            with Image.open(source) as opened:
                image = opened.convert("RGB")
                current_hash = imagehash.dhash(image)
                diff = 0 if last_hash is None else int(current_hash - last_hash)
                if last_hash is not None and diff <= threshold:
                    continue
                last_hash = current_hash
                if max_edge and max(image.size) > max_edge:
                    scale = max_edge / max(image.size)
                    image = image.resize(
                        (max(1, round(image.width * scale)),
                         max(1, round(image.height * scale))), Image.LANCZOS)
                elapsed = index * float(interval)
                filename = f"snapshot_video_{round(elapsed * 1000):09d}.jpg"
                image.save(os.path.join(folder, filename), "JPEG", quality=jpeg_quality)
            kept.append({
                "file": filename, "elapsed": round(elapsed, 3),
                "type": "video", "diff": diff,
            })
            if progress is not None and len(kept) % 10 == 0:
                progress(None, f"動画の画面変化を抽出中: {len(kept)}枚保存")
        if kept:
            with open(os.path.join(folder, "snapshots.jsonl"), "w", encoding="utf-8") as f:
                for entry in kept:
                    f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    finally:
        shutil.rmtree(frame_dir, ignore_errors=True)
    return len(kept)


def import_video_file(video_path, language="auto", progress=None, cancel=None,
                      capture_scenes=False, scene_interval=5.0,
                      scene_threshold=10, max_edge=1600, jpeg_quality=85):
    """Create a meeting folder from a video without modifying the source file.

    The video stream itself is not copied. ffmpeg extracts a standard
    ``audio_main.mp3`` so the existing transcription and report pipeline can be
    reused without special cases in renderers.
    """
    video_path = os.path.abspath(video_path)
    if not os.path.isfile(video_path):
        raise FileNotFoundError(f"動画ファイルが見つかりません: {video_path}")
    if not os.path.isfile(FFMPEG_PATH):
        raise FileNotFoundError(f"ffmpeg.exe が見つかりません: {FFMPEG_PATH}")
    if cancel is not None and cancel():
        raise PostProcessCancelled()

    def _progress(frac, message):
        print(message)
        if progress is not None:
            progress(frac, message)

    stem = os.path.splitext(os.path.basename(video_path))[0]
    safe = sanitize_name(stem) or "Video"
    now = datetime.datetime.now()
    base_name = f"{now.strftime('%Y%m%d_%H%M%S')}_{safe}"
    folder = os.path.join(BASE_DIR, base_name)
    suffix = 2
    while os.path.exists(folder):
        folder = os.path.join(BASE_DIR, f"{base_name}_{suffix}")
        suffix += 1
    os.makedirs(folder)

    audio_path = os.path.join(folder, "audio_main.mp3")
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    _progress(0.0, f"動画を読み込み中: {os.path.basename(video_path)}")
    try:
        completed = subprocess.run(
            [FFMPEG_PATH, "-y", "-i", video_path, "-vn", "-map", "0:a:0",
             "-c:a", "libmp3lame", "-b:a", "192k", audio_path],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            creationflags=flags)
        if completed.returncode != 0 or not os.path.exists(audio_path) \
                or os.path.getsize(audio_path) == 0:
            detail = completed.stderr.decode("utf-8", errors="replace").strip()
            detail = detail.splitlines()[-1] if detail else "音声トラックがありません"
            raise RuntimeError(f"動画から音声を抽出できませんでした: {detail}")
        if cancel is not None and cancel():
            raise PostProcessCancelled()

        snapshot_count = 0
        if capture_scenes:
            _progress(0.02, "動画の画面変化を検出中...")
            snapshot_count = _extract_video_snapshots(
                video_path, folder, interval=scene_interval,
                threshold=scene_threshold, max_edge=max_edge,
                jpeg_quality=jpeg_quality, progress=progress, cancel=cancel)
            _progress(0.03, f"動画から画像を保存しました: {snapshot_count}枚")

        with open(os.path.join(folder, "metadata.txt"), "w", encoding="utf-8") as f:
            f.write(f"MEETING_NAME={stem}\n")
            f.write(f"START_TIME_EPOCH={now.timestamp()}\n")
            f.write(f"START_TIME_STR={now.strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write("AUDIO_SOURCE=動画ファイル\n")
            f.write("MODE=video_import\n")
            f.write(f"SNAPSHOT_COUNT={snapshot_count}\n")
            f.write("MARKER_COUNT=0\n")
            f.write("AUDIO_FILE=audio_main.mp3\n")
            f.write(f"LANGUAGE={language}\n")
            f.write(f"SOURCE_VIDEO={video_path}\n")
    except Exception:
        shutil.rmtree(folder, ignore_errors=True)
        raise

    _progress(0.03, f"動画の音声抽出完了: {os.path.basename(audio_path)}")
    return folder
