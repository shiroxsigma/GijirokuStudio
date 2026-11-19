"""Install the Windows FFmpeg executable used by the recorder."""
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import urllib.request
import zipfile

from gijiroku.paths import FFMPEG_PATH


VERSION = "9.0.2"
BUILD = f"ffmpeg-{VERSION}-essentials_build"
URL = f"https://github.com/GyanD/codexffmpeg/releases/download/{VERSION}/{BUILD}.zip"
# Gyan.FFmpeg.Essentials installer SHA-256 from microsoft/winget-pkgs.
SHA256 = "60f467265b1e312373dbcd92200c2618a74850f98d3d078e94296bb3fa2047ba"


def check_executable(path):
    subprocess.run([str(path), "-version"], check=True, timeout=15,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def install():
    target = Path(FFMPEG_PATH)
    if target.is_file():
        try:
            check_executable(target)
        except (OSError, subprocess.SubprocessError):
            pass
        else:
            print("導入済みの ffmpeg.exe を使用します。")
            return

    print(f"FFmpeg {VERSION} をダウンロードしています...", flush=True)
    with tempfile.TemporaryDirectory(dir=target.parent) as temp:
        archive = Path(temp) / "ffmpeg.zip"
        with urllib.request.urlopen(URL, timeout=90) as response, archive.open("wb") as output:
            shutil.copyfileobj(response, output)
        with archive.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != SHA256:
                raise ValueError("FFmpeg のダウンロードファイルの SHA-256 が一致しません。")
        executable = Path(temp) / "ffmpeg.exe"
        with zipfile.ZipFile(archive) as package:
            with package.open(f"{BUILD}/bin/ffmpeg.exe") as source, executable.open("wb") as output:
                shutil.copyfileobj(source, output)
        check_executable(executable)
        os.replace(executable, target)
    print(f"FFmpeg を導入しました: {target}")


if __name__ == "__main__":
    install()
