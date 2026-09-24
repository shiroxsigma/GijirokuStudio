"""Paths and names shared by the GUI, setup tools, and report pipeline."""

import os
import re

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FFMPEG_PATH = os.path.join(BASE_DIR, "ffmpeg.exe")
SETTINGS_PATH = os.path.join(BASE_DIR, "settings.json")
GLOSSARY_PATH = os.path.join(BASE_DIR, "glossary.csv")
MODELS_DIR = os.path.join(BASE_DIR, "models", "fast_ja_en")

ROLE_SELF = "自分"
ROLE_OTHER = "相手"
ROLE_TRACK_SELF = "audio_self.mp3"
ROLE_TRACK_OTHER = "audio_other.mp3"


def sanitize_name(name):
    """Make a string safe to embed in a Windows folder or file name."""
    cleaned = re.sub(r'[\\/:*?"<>|]', "_", name)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned.rstrip(". ")
