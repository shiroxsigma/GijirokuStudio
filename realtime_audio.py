"""Realtime echo cancellation helpers for role-separated transcription."""
from __future__ import annotations

from collections import deque
from difflib import SequenceMatcher
import re
import time
import unicodedata

import numpy as np
from scipy.signal import resample_poly


def _mono(pcm: bytes) -> np.ndarray:
    samples = np.frombuffer(pcm, dtype=np.int16)
    if samples.size < 2:
        return np.zeros(0, dtype=np.int16)
    return samples[:samples.size - samples.size % 2].reshape(-1, 2).mean(axis=1)


class EchoReferenceProcessor:
    """Align mic/render chunks and run WebRTC AEC3 in 10 ms frames.

    Input is aligned stereo int16 PCM from the recorder mixer. Output is
    16 kHz mono float32 ready for ASR. The far-end stream is left untouched;
    only the microphone stream is echo-cancelled.
    """

    sample_rate = 16000
    frame_samples = 160

    def __init__(self, input_rate: int, stream_delay_ms: int = 0):
        from pywebrtc_audio import AudioProcessor

        self.input_rate = int(input_rate)
        self.processor = AudioProcessor(
            sample_rate=self.sample_rate,
            echo_cancellation=True,
            noise_suppression=True,
            auto_gain_control=False,
            stream_delay_ms=max(0, int(stream_delay_ms)),
        )
        self._near = np.zeros(0, dtype=np.int16)
        self._far = np.zeros(0, dtype=np.int16)
        self._input_samples = 0
        self._resampled_samples = 0

    def process(self, mic_pcm: bytes, speaker_pcm: bytes):
        near = _mono(mic_pcm)
        far = _mono(speaker_pcm)
        count = min(near.size, far.size)
        if count == 0:
            return None
        near = near[:count]
        far = far[:count]
        if self.input_rate != self.sample_rate:
            self._input_samples += count
            target_total = round(
                self._input_samples * self.sample_rate / self.input_rate)
            needed = target_total - self._resampled_samples
            near = resample_poly(near, self.sample_rate, self.input_rate)[:needed]
            far = resample_poly(far, self.sample_rate, self.input_rate)[:needed]
            self._resampled_samples += min(near.size, far.size)
        near = np.clip(near, -32768, 32767).astype(np.int16)
        far = np.clip(far, -32768, 32767).astype(np.int16)
        self._near = np.concatenate((self._near, near))
        self._far = np.concatenate((self._far, far))
        usable = min(self._near.size, self._far.size)
        usable -= usable % self.frame_samples
        if usable <= 0:
            return None
        near = self._near[:usable]
        far = self._far[:usable]
        self._near = self._near[usable:]
        self._far = self._far[usable:]
        clean = self.processor.process(near, far)
        return {
            "self": clean.astype(np.float32) / 32768.0,
            "other": far.astype(np.float32) / 32768.0,
        }

    def reset(self):
        self._near = np.zeros(0, dtype=np.int16)
        self._far = np.zeros(0, dtype=np.int16)
        self._input_samples = 0
        self._resampled_samples = 0
        self.processor.reset()


def normalized_text(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "").casefold()
    return re.sub(r"[^\w\u3040-\u30ff\u3400-\u9fff]+", "", text)


def text_similarity(left: str, right: str) -> float:
    left = normalized_text(left)
    right = normalized_text(right)
    if not left or not right:
        return 0.0
    if left in right or right in left:
        return min(len(left), len(right)) / max(len(left), len(right))
    return SequenceMatcher(None, left, right, autojunk=False).ratio()


def events_overlap(left, right, tolerance_samples=16000) -> bool:
    return (left.start_sample < right.end_sample + tolerance_samples
            and left.end_sample + tolerance_samples > right.start_sample)


def remove_cross_role_duplicates(events, self_role: str, other_role: str,
                                 threshold: float = 0.72):
    """Prefer far-end events when an overlapping mic event says the same thing."""
    other = [e for e in events if e.speaker == other_role]
    kept = []
    for event in events:
        duplicate = (event.speaker == self_role and any(
            events_overlap(event, candidate)
            and text_similarity(event.text, candidate.text) >= threshold
            for candidate in other))
        if not duplicate:
            kept.append(event)
    return kept


def remove_cross_role_line_duplicates(lines, self_role: str, other_role: str,
                                      threshold: float = 0.72,
                                      tolerance_seconds: float = 3.0):
    """Equivalent leakage guard for post-processing rows with start seconds."""
    other = [line for line in lines if line.get("speaker") == other_role]
    kept = []
    for line in lines:
        duplicate = (line.get("speaker") == self_role and any(
            abs(float(line.get("start", 0)) - float(candidate.get("start", 0)))
            <= tolerance_seconds
            and text_similarity(line.get("text", ""), candidate.get("text", ""))
            >= threshold
            for candidate in other))
        if not duplicate:
            kept.append(line)
    return kept


class PartialLeakageGuard:
    """Short-lived far-end text cache used to hide leaked mic partials."""

    def __init__(self, lifetime=3.0, threshold=0.72):
        self.lifetime = float(lifetime)
        self.threshold = float(threshold)
        self._other = deque()

    def remember_other(self, text: str):
        self._prune()
        if normalized_text(text):
            self._other.append((time.monotonic(), text))

    def is_leakage(self, text: str) -> bool:
        self._prune()
        return any(text_similarity(text, other) >= self.threshold
                   for _, other in self._other)

    def _prune(self):
        cutoff = time.monotonic() - self.lifetime
        while self._other and self._other[0][0] < cutoff:
            self._other.popleft()
