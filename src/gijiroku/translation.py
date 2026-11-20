"""Local English-to-Japanese translation, isolated from the ASR worker."""
from __future__ import annotations

from dataclasses import dataclass
import http.client
import json
from pathlib import Path
import queue
import re
import threading

from .paths import BASE_DIR


MARIAN_MODELS = [
    ("ElanMT-tiny（省電力）", "elan-tiny", "Mitsua/elan-mt-tiny-en-ja"),
    ("FuguMT（標準）", "fugumt", "staka/fugumt-en-ja"),
]
PLAMO_MODELS = [
    ("PLaMo 3 Translate 0.8B（Ollama）", "plamo-3-translate-0.8b", "pfnet/plamo-3-translate-0.8b-GGUF"),
    ("PLaMo 3 Translate 2B（Ollama）", "plamo-3-translate-2b", "pfnet/plamo-3-translate-2b-GGUF"),
]
TRANSLATION_MODELS = MARIAN_MODELS + PLAMO_MODELS
DEFAULT_TRANSLATION_MODEL = "elan-tiny"


def plamo_ollama_tag(name):
    for _, model_name, repository in PLAMO_MODELS:
        if model_name == name:
            return f"hf.co/{repository}:Q4_K_M"
    return None


def translation_model_dir(name):
    if name not in {item[1] for item in MARIAN_MODELS}:
        raise ValueError(f"不明な翻訳モデル: {name}")
    return Path(BASE_DIR) / "models" / "translation_en_ja" / name


class LocalEnglishJapaneseTranslator:
    """Load only local CT2/SentencePiece assets; never download at runtime."""

    def __init__(self, model_dir, threads=1):
        model_dir = Path(model_dir)
        required = ("model.bin", "config.json", "source.spm", "target.spm")
        if not all((model_dir / name).is_file() for name in required):
            option = f" --model {model_dir.name}" if model_dir.name in {
                item[1] for item in MARIAN_MODELS} else ""
            raise RuntimeError(
                f"翻訳モデル未導入。python src/setup_translation.py{option} を実行してください")
        import ctranslate2
        import sentencepiece as spm
        from sacremoses import MosesPunctNormalizer

        self.source = spm.SentencePieceProcessor(model_file=str(model_dir / "source.spm"))
        self.target = spm.SentencePieceProcessor(model_file=str(model_dir / "target.spm"))
        self._normalize = MosesPunctNormalizer(lang="en").normalize
        self.model = ctranslate2.Translator(
            str(model_dir), device="cpu", compute_type="int8",
            inter_threads=1, intra_threads=max(1, int(threads)))

    def translate(self, text):
        output = []
        # Respect Marian's position limit without silently truncating long speech.
        for sentence in re.split(r"(?<=[.!?])\s+|\n+", text.strip()):
            tokens = self.source.encode(self._normalize(sentence), out_type=str)
            for offset in range(0, len(tokens), 256):
                source = tokens[offset:offset + 256] + ["</s>"]
                result = self.model.translate_batch(
                    [source], beam_size=1, max_decoding_length=512,
                    return_scores=False)[0]
                pieces = [x for x in result.hypotheses[0] if x not in ("</s>", "<pad>")]
                translated = self.target.decode(pieces).strip()
                if translated:
                    output.append(translated)
        return " ".join(output)


class OllamaModelUnavailable(RuntimeError):
    """The local service or selected model needs to be installed or started."""


class PlamoTranslator:
    def __init__(self, model_tag, threads):
        self.model_tag = model_tag
        self.threads = threads

    def translate(self, text):
        payload = {
            "model": self.model_tag,
            "stream": False,
            "messages": [
                {"role": "system", "content": "[plamo-translate]\ntarget_lang=ja\nsource_lang=en"},
                {"role": "user", "content": text},
            ],
            "options": {"temperature": 0, "num_predict": 512, "num_thread": self.threads,
                        "stop": ["<|plamo:tag|>", "<|plamo:bos|>"]},
        }
        connection = http.client.HTTPConnection("127.0.0.1", 11434, timeout=60)
        try:
            connection.request("POST", "/api/chat", body=json.dumps(payload).encode("utf-8"),
                               headers={"Content-Type": "application/json"})
            response = connection.getresponse()
            if response.status == 404:
                raise OllamaModelUnavailable(
                    f"翻訳モデル未導入。ollama pull {self.model_tag} を実行してください")
            result = json.loads(response.read())
            if response.status != 200 or result.get("error"):
                raise RuntimeError(f"Ollama翻訳エラー: {result.get('error') or response.status}")
            if result.get("done_reason") == "length":
                raise RuntimeError("翻訳結果が出力上限に達しました。英文は保存されています")
            return result["message"]["content"].strip()
        except ConnectionRefusedError as exc:
            raise OllamaModelUnavailable("Ollamaに接続できません。Ollamaを起動してください") from exc
        except TimeoutError as exc:
            raise RuntimeError("Ollamaの翻訳がタイムアウトしました") from exc
        finally:
            connection.close()


def create_translation_model(name, threads=1):
    tag = plamo_ollama_tag(name)
    if tag:
        return PlamoTranslator(tag, threads)
    return LocalEnglishJapaneseTranslator(translation_model_dir(name), threads)


@dataclass(frozen=True)
class TranslationRequest:
    source: str
    seconds: float
    speaker: str = ""
    row_id: str = ""
    timestamp: str = ""


class EnglishTranslationWorker:
    """A bounded, single-consumer queue. Only finalized English reaches the model."""

    def __init__(self, model_factory, output_dir, on_result, on_error,
                 max_pending=64, model_name=DEFAULT_TRANSLATION_MODEL, on_skipped=None):
        self._factory = model_factory
        self._output_dir = Path(output_dir)
        self._on_result = on_result
        self._on_error = on_error
        self._model_name = model_name
        self._on_skipped = on_skipped or (lambda request, error: None)
        self._queue = queue.Queue(maxsize=max_pending)
        self._closing = threading.Event()
        self._lock = threading.Lock()
        self._failed = threading.Event()
        self._enabled = threading.Event()
        self._enabled.set()
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="english-japanese-translation")
        self._thread.start()

    def set_enabled(self, enabled):
        """Switch future inference on/off without blocking the GUI."""
        if enabled:
            self._enabled.set()
        else:
            self._enabled.clear()

    def submit(self, text, language, *, kind="final", seconds=0.0, speaker="",
               row_id="", timestamp=""):
        # Gate before any model load or tokenization, using this utterance's LID.
        if language != "en" or kind != "final" or not text.strip():
            return False
        request = TranslationRequest(text.strip(), seconds, speaker, row_id, timestamp)
        with self._lock:
            if not self._enabled.is_set():
                self._on_skipped(request, "英語の翻訳はOFFです")
                return False
            if self._closing.is_set() or self._failed.is_set():
                self._on_skipped(request, "翻訳を利用できません")
                return False
            try:
                self._queue.put_nowait(request)
            except queue.Full:
                self._on_skipped(request, "翻訳待ちの上限に達しました")
                self._on_error("翻訳待ちが上限に達したため訳を省略しました。英文は保存されています")
                return False
        return True

    def close(self):
        """Drain accepted English jobs before closing the recording."""
        with self._lock:
            self._closing.set()
        self._thread.join()

    def _run(self):
        model = None
        journal = transcript = None
        try:
            while not self._closing.is_set() or not self._queue.empty():
                try:
                    request = self._queue.get(timeout=0.1)
                except queue.Empty:
                    continue
                try:
                    if not self._enabled.is_set():
                        self._on_skipped(request, "英語の翻訳はOFFです")
                        continue
                    if self._failed.is_set():
                        self._on_skipped(request, "翻訳を利用できません")
                        continue
                    if model is None:
                        try:
                            model = self._factory()
                            journal = (self._output_dir / "translation_en_ja.jsonl").open(
                                "w", encoding="utf-8")
                            transcript = (self._output_dir / "translation_en_ja.txt").open(
                                "w", encoding="utf-8")
                        except Exception as exc:
                            self._failed.set()
                            self._on_error(str(exc))
                            self._on_skipped(request, "翻訳モデルを読み込めませんでした")
                            continue
                    if not self._enabled.is_set():
                        self._on_skipped(request, "英語の翻訳はOFFです")
                        continue
                    translated = model.translate(request.source)
                    if not translated:
                        raise RuntimeError("訳文が空でした。英文は保存されています")
                    journal.write(json.dumps({
                        "seconds": request.seconds, "speaker": request.speaker,
                        "language": "en", "target_language": "ja", "kind": "final",
                        "source": request.source, "translation": translated,
                        "model": self._model_name,
                        "row_id": request.row_id, "timestamp": request.timestamp,
                    }, ensure_ascii=False) + "\n")
                    journal.flush()
                    who = f" [{request.speaker}]" if request.speaker else ""
                    transcript.write(f"[{request.seconds:.1f}s]{who} [en] {request.source}\n"
                                     f"  → [ja] {translated}\n")
                    transcript.flush()
                    self._on_result(request, translated)
                except Exception as exc:
                    if isinstance(exc, OllamaModelUnavailable):
                        self._failed.set()
                    self._on_error(str(exc))
                    self._on_skipped(request, "翻訳できませんでした")
                finally:
                    self._queue.task_done()
        finally:
            if journal is not None:
                journal.close()
            if transcript is not None:
                transcript.close()
