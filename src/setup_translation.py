"""Download and convert the optional local English-to-Japanese model once."""
import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import venv
from pathlib import Path

from gijiroku.translation import (TRANSLATION_MODELS, DEFAULT_TRANSLATION_MODEL,
                                 translation_model_dir)

CONVERSION_VERSION = 2


def convert_model(snapshot, converted):
    from ctranslate2.converters import TransformersConverter
    from ctranslate2.converters.transformers import MarianMTLoader, BartLoader, _MODEL_LOADERS

    class PreserveDecoderStartLoader(MarianMTLoader):
        # The standard converter assumes the Marian start/pad embedding is zero.
        # ElanMT learned a nonzero vector: preserve it and its token explicitly.
        def get_model_spec(self, model):
            model.config.normalize_before = False
            model.config.normalize_embedding = False
            return BartLoader.get_model_spec(self, model)

        def get_vocabulary(self, model, tokenizer):
            return BartLoader.get_vocabulary(self, model, tokenizer)

        def set_config(self, config, model, tokenizer):
            config.eos_token = tokenizer.eos_token
            config.unk_token = tokenizer.unk_token
            config.decoder_start_token = tokenizer.convert_ids_to_tokens(
                model.config.decoder_start_token_id)

        def set_decoder(self, spec, decoder):
            spec.start_from_zero_embedding = False
            BartLoader.set_decoder(self, spec, decoder)

    original_loader = _MODEL_LOADERS["MarianConfig"]
    try:
        _MODEL_LOADERS["MarianConfig"] = PreserveDecoderStartLoader()
        TransformersConverter(str(snapshot)).convert(str(converted), quantization="int8")
    finally:
        _MODEL_LOADERS["MarianConfig"] = original_loader


def download_and_convert(model, converted):
    """Run inside a disposable environment containing conversion dependencies."""
    from huggingface_hub import snapshot_download

    repo = next(repo for _, name, repo in TRANSLATION_MODELS if name == model)
    print(f"翻訳モデルを導入中: {repo}")
    snapshot = Path(snapshot_download(repo, allow_patterns=[
        "*.json", "*.spm", "*.safetensors", "pytorch_model.bin", "README.md"]))
    converted = Path(converted)
    convert_model(snapshot, converted)
    for name in ("source.spm", "target.spm", "README.md"):
        shutil.copy2(snapshot / name, converted / name)
    (converted / "installation.json").write_text(json.dumps({
        "repository": repo, "revision": snapshot.name, "quantization": "int8",
        "conversion_version": CONVERSION_VERSION,
    }, indent=2), encoding="utf-8")


def convert_in_temporary_environment(model, converted):
    # Never install Torch/Transformers into the application's runtime environment.
    # TemporaryDirectory also cleans up on download/conversion failures.
    with tempfile.TemporaryDirectory(prefix="gijiroku-conversion-") as folder:
        environment = Path(folder) / "venv"
        venv.EnvBuilder(with_pip=True).create(environment)
        python = environment / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
        subprocess.check_call([str(python), "-m", "pip", "install", "torch>=2.6",
                               "--index-url", "https://download.pytorch.org/whl/cpu"])
        subprocess.check_call([str(python), "-m", "pip", "install",
                               "ctranslate2==4.8.2", "transformers>=4.40,<5",
                               "sentencepiece>=0.2", "sacremoses"])
        subprocess.check_call([str(python), str(Path(__file__).resolve()),
                               "--_convert", model, str(converted)])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", choices=[x[1] for x in TRANSLATION_MODELS],
                        default=DEFAULT_TRANSLATION_MODEL)
    parser.add_argument("--_convert", nargs=2, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args._convert is not None:
        download_and_convert(*args._convert)
        return
    destination = translation_model_dir(args.model)
    manifest = destination / "installation.json"
    installed_version = 0
    if manifest.is_file():
        installed_version = json.loads(manifest.read_text(encoding="utf-8")).get("conversion_version", 0)
    if installed_version == CONVERSION_VERSION and all((destination / name).is_file() for name in
           ("model.bin", "config.json", "source.spm", "target.spm")):
        print(f"翻訳モデル導入済み: {destination}")
        return

    subprocess.check_call([sys.executable, "-m", "pip", "install",
                           "ctranslate2==4.8.2", "sentencepiece>=0.2", "sacremoses"])
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=destination.parent) as staging:
        converted = Path(staging) / "converted"
        convert_in_temporary_environment(args.model, converted)
        from gijiroku.translation import LocalEnglishJapaneseTranslator
        smoke_model = LocalEnglishJapaneseTranslator(converted)
        smoke_text = smoke_model.translate("We will review the budget tomorrow.")
        if not smoke_text or "⁇" in smoke_text or "<unk>" in smoke_text:
            raise RuntimeError("翻訳モデルの検証に失敗しました。既存モデルは更新しません")
        del smoke_model
        # Preserve any existing folder on a failed download/conversion.
        shutil.copytree(converted, destination, dirs_exist_ok=True)
    print(f"完了: {destination}（会議中はネット接続不要）")


if __name__ == "__main__":
    main()
