"""Install the optional CPU-only Japanese/English realtime ASR assets."""
import os
import subprocess
import sys
import tarfile
import tempfile
import urllib.request

from fast_asr import LID_DIR, PARAKEET_DIR, REAZON_DIR

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MODELS_DIR = os.path.join(BASE_DIR, "models", "fast_ja_en")
TAG = "asr-models"
RELEASES = "https://github.com/k2-fsa/sherpa-onnx/releases/download"


def download(url, path, label):
    if os.path.isfile(path) and os.path.getsize(path) > 0:
        print(f"{label}: 導入済み")
        return
    print(f"{label} をダウンロード中...")
    urllib.request.urlretrieve(url, path)


def download_archive(dirname, label):
    target = os.path.join(MODELS_DIR, dirname)
    if os.path.isdir(target) and os.listdir(target):
        print(f"{label}: 導入済み")
        return
    with tempfile.TemporaryDirectory() as tmp:
        archive = os.path.join(tmp, "model.tar.bz2")
        download(f"{RELEASES}/{TAG}/{dirname}.tar.bz2", archive, label)
        print(f"{label} を展開中...")
        with tarfile.open(archive, "r:bz2") as tf:
            tf.extractall(MODELS_DIR, filter="data")


def main():
    subprocess.check_call([sys.executable, "-m", "pip", "install", "sherpa-onnx==1.13.6"])
    os.makedirs(MODELS_DIR, exist_ok=True)
    download_archive(REAZON_DIR, "ReazonSpeech日本語 INT8")
    download_archive(PARAKEET_DIR, "Parakeet英語 INT8")
    download_archive(LID_DIR, "Whisper-tiny日英言語判定")
    download(f"{RELEASES}/{TAG}/silero_vad.onnx",
             os.path.join(MODELS_DIR, "silero_vad.onnx"), "Silero VAD")
    punct = os.path.join(MODELS_DIR, "mojicast-punct-onnx")
    os.makedirs(punct, exist_ok=True)
    hf = "https://huggingface.co/ishiki-emo/mojicast-punct-onnx/resolve/main"
    download(f"{hf}/punct_bert.onnx", os.path.join(punct, "punct_bert.onnx"),
             "日本語句読点モデル")
    download(f"{hf}/vocab.txt", os.path.join(punct, "vocab.txt"),
             "日本語句読点語彙")
    print(f"完了: {MODELS_DIR}")


if __name__ == "__main__":
    main()
