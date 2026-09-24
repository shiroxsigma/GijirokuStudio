"""Shared application settings and terminology."""
import json
import os
from .paths import SETTINGS_PATH, GLOSSARY_PATH

def load_app_settings():
    """Read settings.json into a dict ({} if absent/invalid). Shared by GUI & CLI."""
    if not os.path.exists(SETTINGS_PATH):
        return {}
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
            s = json.load(f)
        return s if isinstance(s, dict) else {}
    except Exception as e:
        print(f"[設定読み込み警告] {e}")
        return {}


def load_glossary():
    """Load the term glossary CSV. Each row: form, reading, alias1, alias2, ...
    Returns list of {form, reading, aliases}. Empty list if the file is absent.
    Blank lines and '#'-prefixed lines are skipped.
    """
    if not os.path.exists(GLOSSARY_PATH):
        return []
    import csv
    terms = []
    try:
        with open(GLOSSARY_PATH, "r", encoding="utf-8-sig", newline="") as f:
            for row in csv.reader(f):
                row = [c.strip() for c in row]
                if not row or not row[0] or row[0].startswith("#"):
                    continue
                if row[0].lower() in ("正規形", "form", "用語", "term", "word", "name"):
                    continue  # header row
                form = row[0]
                reading = row[1] if len(row) > 1 else ""
                aliases = [a for a in row[2:] if a]
                terms.append({"form": form, "reading": reading, "aliases": aliases})
    except Exception as e:
        print(f"[用語辞書読み込み警告] {e}")
    return terms
