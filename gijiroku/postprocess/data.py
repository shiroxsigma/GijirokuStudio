"""Load recordings and related meeting data."""
import datetime
import json
import os
from ..paths import ROLE_SELF, ROLE_OTHER, ROLE_TRACK_SELF, ROLE_TRACK_OTHER

# ------------------------------------------------------- Meeting data loading

def _read_jsonl(path):
    """Read a .jsonl log, tolerating a truncated or corrupt trailing line."""
    rows = []
    if not os.path.exists(path):
        return rows
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                print(f"[ログ警告] 壊れた行を無視: {os.path.basename(path)}")
    return rows


def _read_metadata(folder):
    meta = {}
    path = os.path.join(folder, "metadata.txt")
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                if "=" in line:
                    k, v = line.strip().split("=", 1)
                    meta[k] = v
    return meta


def _image_time_from_name(fname, start_time_str):
    """Elapsed seconds from a snapshot_HHMMSS_mmm.jpg name (pre-jsonl folders)."""
    if not start_time_str:
        return None
    try:
        hhmmss = os.path.splitext(fname)[0].split("_")[1]
        h, m, s = int(hhmmss[:2]), int(hhmmss[2:4]), int(hhmmss[4:6])
        st = datetime.datetime.strptime(start_time_str, "%Y-%m-%d %H:%M:%S")
        return (st.replace(hour=h, minute=m, second=s) - st).total_seconds()
    except Exception:
        return None


def collect_meeting_data(folder):
    """Gather everything a recording folder holds into one structure.

    Every renderer (markdown / HTML / DOCX) consumes this dict. None of them
    parses another renderer's output — that coupling would break the moment a
    heading or a table changes.
    """
    if not os.path.isdir(folder):
        raise FileNotFoundError(f"Folder not found: {folder}")

    audio_file = None
    for name in ("audio_main.mp3", "audio_main.wav"):
        path = os.path.join(folder, name)
        if os.path.exists(path):
            audio_file = path
            break
    if audio_file is None:
        raise RuntimeError(f"No audio file found in: {folder}")

    meta = _read_metadata(folder)

    # Per-role tracks, written only when both a mic and a speaker were captured.
    role_tracks = {}
    for role, fname in ((ROLE_SELF, ROLE_TRACK_SELF), (ROLE_OTHER, ROLE_TRACK_OTHER)):
        path = os.path.join(folder, fname)
        if os.path.exists(path) and os.path.getsize(path) > 0:
            role_tracks[role] = path

    # Image entries. snapshots.jsonl carries the exact timings, but the folder
    # is the source of truth for which images exist: any picture on disk that
    # the log missed still belongs in the report, timed from its filename.
    img_entries = []
    logged = set()
    for entry in _read_jsonl(os.path.join(folder, "snapshots.jsonl")):
        img_entries.append({
            "file": entry["file"],
            "time": entry.get("elapsed", 0.0),
            "type": entry.get("type", "auto"),
        })
        logged.add(entry["file"])
    start_str = meta.get("START_TIME_STR")
    missing = 0
    for fname in sorted(f for f in os.listdir(folder)
                        if f.lower().endswith((".jpg", ".jpeg", ".png"))):
        if fname in logged:
            continue
        t = _image_time_from_name(fname, start_str)
        if t is not None:
            img_entries.append({"file": fname, "time": t,
                                "type": "manual" if fname.startswith("manual_")
                                        else "auto"})
            missing += 1
    if missing and logged:
        print(f"[画像] ログに無い画像 {missing}枚をファイル名から復元しました")
    img_entries.sort(key=lambda x: x["time"])

    # OCR text is cached so re-running post-processing never redoes it
    ocr_by_file = {e.get("file"): e.get("text", "")
                   for e in _read_jsonl(os.path.join(folder, "ocr.jsonl"))}
    for ent in img_entries:
        ent["ocr"] = ocr_by_file.get(ent["file"], "")

    lang = meta.get("LANGUAGE", "ja")
    return {
        "folder": folder,
        "meta": meta,
        "meeting_name": meta.get("MEETING_NAME", ""),
        "start_time_str": meta.get("START_TIME_STR", "Unknown"),
        "audio_source": meta.get("AUDIO_SOURCE", "-"),
        "language": lang,
        "auto_mode": lang == "auto",
        "audio_file": audio_file,
        "audio_name": os.path.basename(audio_file),
        "role_tracks": role_tracks,
        "lang_segments": _read_jsonl(os.path.join(folder, "language_segments.jsonl")),
        "images": img_entries,
        "markers": sorted(_read_jsonl(os.path.join(folder, "markers.jsonl")),
                          key=lambda m: m.get("elapsed", 0.0)),
        "duration": 0.0,
        "detected_langs": [],
        "lines": [],
        "summary": None,
    }
