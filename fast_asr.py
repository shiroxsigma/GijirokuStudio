"""Japanese/English streaming ASR adapted for GijirokuStudio.

Pipeline: Silero VAD -> early Whisper LID -> ReazonSpeech (ja) / Parakeet
(en), with partial drafts, Japanese punctuation, and an idle-time refine pass.
"""
from __future__ import annotations

import glob
import os
from dataclasses import dataclass

import numpy as np


REAZON_DIR = "sherpa-onnx-zipformer-ja-en-reazonspeech-2025-01-17"
PARAKEET_DIR = "sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8"
LID_DIR = "sherpa-onnx-whisper-tiny"


@dataclass
class FastASREvent:
    kind: str                 # partial | final | refine
    text: str
    language: str
    start_sample: int = 0
    end_sample: int = 0


class FastJapaneseEnglishASR:
    sample_rate = 16000
    window_size = 512
    partial_every = 0.5
    partial_window = 8.0
    refine_gap = 2.0
    refine_max = 25.0

    def __init__(self, models_dir: str, threads: int = 4,
                 min_silence: float = 0.35, max_speech: float = 12.0):
        try:
            import sherpa_onnx
        except ImportError as exc:
            raise RuntimeError("setup_fast_asr.py を実行してください") from exc
        self.sherpa = sherpa_onnx
        self.models_dir = models_dir
        self.threads = max(1, int(threads))
        self.reazon = self._build_reazon()
        self.parakeet = self._build_parakeet()
        self.lid = self._build_lid()
        from fast_punct import JapanesePunctuator
        self.punctuator = JapanesePunctuator(
            os.path.join(models_dir, "mojicast-punct-onnx"), self.threads)

        vad_path = self._required(os.path.join(models_dir, "silero_vad.onnx"), "VAD")
        cfg = sherpa_onnx.VadModelConfig(
            silero_vad=sherpa_onnx.SileroVadModelConfig(
                model=vad_path, min_silence_duration=min_silence,
                min_speech_duration=0.25, window_size=self.window_size,
                max_speech_duration=max_speech),
            sample_rate=self.sample_rate, num_threads=1)
        self.vad = sherpa_onnx.VoiceActivityDetector(cfg, buffer_size_in_seconds=30)
        self._pending = np.zeros(0, dtype=np.float32)
        self._history = np.zeros(0, dtype=np.float32)
        self._history_offset = 0
        self._audio_pos = 0
        self._last_partial = 0
        self._early_lang = None
        self._group = []
        self._last_lang = None

        silence = np.zeros(self.sample_rate, dtype=np.float32)
        self._identify(silence)
        self._decode(self.reazon, silence, "ja", punctuate=False)
        self._decode(self.parakeet, silence, "en", punctuate=False)

    @staticmethod
    def _find(folder: str, pattern: str) -> str:
        hits = glob.glob(os.path.join(folder, pattern))
        return hits[0] if hits else ""

    @staticmethod
    def _required(path: str, label: str) -> str:
        if not path or not os.path.isfile(path):
            raise RuntimeError(f"高速ASRの{label}がありません。setup_fast_asr.py を実行してください")
        return path

    def _build_reazon(self):
        folder = os.path.join(self.models_dir, REAZON_DIR)
        return self.sherpa.OfflineRecognizer.from_transducer(
            encoder=self._required(self._find(folder, "encoder-*.int8.onnx"), "日本語encoder"),
            decoder=self._required(self._find(folder, "decoder-*.int8.onnx"), "日本語decoder"),
            joiner=self._required(self._find(folder, "joiner-*.int8.onnx"), "日本語joiner"),
            tokens=self._required(os.path.join(folder, "tokens.txt"), "日本語tokens"),
            num_threads=self.threads, decoding_method="modified_beam_search",
            modeling_unit="cjkchar")

    def _build_parakeet(self):
        folder = os.path.join(self.models_dir, PARAKEET_DIR)
        return self.sherpa.OfflineRecognizer.from_transducer(
            encoder=self._required(self._find(folder, "encoder*.onnx"), "英語encoder"),
            decoder=self._required(self._find(folder, "decoder*.onnx"), "英語decoder"),
            joiner=self._required(self._find(folder, "joiner*.onnx"), "英語joiner"),
            tokens=self._required(os.path.join(folder, "tokens.txt"), "英語tokens"),
            num_threads=self.threads, model_type="nemo_transducer")

    def _build_lid(self):
        folder = os.path.join(self.models_dir, LID_DIR)
        cfg = self.sherpa.SpokenLanguageIdentificationWhisperConfig(
            encoder=self._required(os.path.join(folder, "tiny-encoder.int8.onnx"), "LID encoder"),
            decoder=self._required(os.path.join(folder, "tiny-decoder.int8.onnx"), "LID decoder"))
        return self.sherpa.SpokenLanguageIdentification(
            self.sherpa.SpokenLanguageIdentificationConfig(
                whisper=cfg, num_threads=self.threads))

    @staticmethod
    def _clean(text: str) -> str:
        for junk in ("［", "］", "〈", "〉"):
            text = text.replace(junk, "")
        return text.strip()

    def _identify(self, samples: np.ndarray) -> str:
        clip = samples[:4 * self.sample_rate]
        stream = self.lid.create_stream()
        stream.accept_waveform(self.sample_rate, clip)
        lang = self.lid.compute(stream)
        return "en" if "en" in lang.lower() else "ja"

    def _decode(self, recognizer, samples, lang, punctuate=True) -> str:
        stream = recognizer.create_stream()
        stream.accept_waveform(self.sample_rate, samples)
        recognizer.decode_stream(stream)
        text = self._clean(stream.result.text)
        if text and lang == "ja" and punctuate:
            text = self.punctuator.restore(text)
        return text

    def _decode_lang(self, samples, lang, punctuate=True):
        rec = self.parakeet if lang == "en" else self.reazon
        return self._decode(rec, samples, lang, punctuate)

    def _push_history(self, chunk):
        self._history = np.concatenate((self._history, chunk))
        keep = 60 * self.sample_rate
        if len(self._history) > keep:
            drop = len(self._history) - keep
            self._history = self._history[drop:]
            self._history_offset += drop

    def _history_slice(self, start, end):
        lo = max(start, self._history_offset) - self._history_offset
        hi = max(end, self._history_offset) - self._history_offset
        return self._history[lo:hi].copy()

    def _refine(self, force=False, allow_silence=True):
        if not self._group:
            return []
        last_end = self._group[-1][1]
        if not force:
            silence_due = (allow_silence
                           and self._audio_pos - last_end >= self.refine_gap * self.sample_rate)
            length_due = last_end - self._group[0][0] >= self.refine_max * self.sample_rate
            if not (silence_due or length_due):
                return []
        group = self._group
        self._group = []
        langs = [x[2] for x in group]
        lang = max(set(langs), key=langs.count)
        fast = " ".join(x[3] for x in group)
        # Do not destroy genuine code-switching by forcing a majority model.
        if len(set(langs)) > 1:
            text = fast
        else:
            samples = self._history_slice(max(0, group[0][0] - self.sample_rate), last_end)
            text = self._decode_lang(samples, lang)
            if len(text) < 0.7 * len(fast):
                text = fast
        return [FastASREvent("refine", text, lang, group[0][0], last_end)] if text else []

    def _drain(self, lang_hint=None):
        out = []
        while not self.vad.empty():
            segment = self.vad.front
            samples = np.asarray(segment.samples, dtype=np.float32)
            start, end = int(segment.start), int(segment.start + len(samples))
            self.vad.pop()
            lang = lang_hint if lang_hint in ("ja", "en") else self._early_lang
            if lang not in ("ja", "en"):
                lang = self._identify(samples)
            text = self._decode_lang(samples, lang)
            if text:
                out.append(FastASREvent("final", text, lang, start, end))
                self._group.append((start, end, lang, text))
                self._last_lang = lang
            self._early_lang = None
        return out

    def accept(self, samples: np.ndarray, lang_hint: str | None = None):
        events = []
        samples = np.asarray(samples, dtype=np.float32).reshape(-1)
        if len(samples):
            self._pending = np.concatenate((self._pending, samples))
        while len(self._pending) >= self.window_size:
            chunk = self._pending[:self.window_size]
            self._pending = self._pending[self.window_size:]
            self.vad.accept_waveform(chunk)
            self._push_history(chunk)
            self._audio_pos += len(chunk)
            if self.vad.is_speech_detected():
                cur = np.asarray(self.vad.current_segment.samples, dtype=np.float32)
                if lang_hint not in ("ja", "en") and self._early_lang is None \
                        and len(cur) >= 2 * self.sample_rate:
                    self._early_lang = self._identify(cur)
                if (self._audio_pos - self._last_partial >= self.partial_every * self.sample_rate
                        and len(cur) >= self.sample_rate // 2):
                    self._last_partial = self._audio_pos
                    draft = cur[-int(self.partial_window * self.sample_rate):]
                    lang = lang_hint if lang_hint in ("ja", "en") else (self._early_lang or self._last_lang or "ja")
                    text = self._decode_lang(draft, lang, punctuate=False)
                    if text:
                        events.append(FastASREvent("partial", text, lang))
            events.extend(self._drain(lang_hint))
            # Long narration may never contain a two-second pause. Refine a
            # bounded group as soon as finalized spans reach refine_max;
            # otherwise use the normal idle-gap trigger.
            events.extend(self._refine(
                allow_silence=not self.vad.is_speech_detected()))
        return events

    def flush(self, lang_hint: str | None = None):
        events = []
        if len(self._pending):
            padded = np.pad(self._pending, (0, self.window_size - len(self._pending)))
            self.vad.accept_waveform(padded)
            self._push_history(padded)
            self._audio_pos += len(padded)
            self._pending = np.zeros(0, dtype=np.float32)
        self.vad.flush()
        events.extend(self._drain(lang_hint))
        events.extend(self._refine(force=True))
        return events
