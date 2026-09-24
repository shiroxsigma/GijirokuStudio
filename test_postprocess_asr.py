"""Focused checks for the fast post-processing adapter."""

import unittest
from types import SimpleNamespace

import numpy as np

from fast_asr import FastASREvent
from main import _FastPostEngine, _Reporter, PostProcessCancelled


class _Session:
    def __init__(self):
        self.calls = 0
        self.partials_enabled = True

    def accept(self, _samples, lang_hint=None):
        self.calls += 1
        if self.calls == 1:
            return [FastASREvent("final", "速報", "ja", 3200, 8000)]
        return [FastASREvent("refine", "補正結果", "ja", 3200, 8000)]

    def flush(self, lang_hint=None):
        return []


class FastPostEngineTests(unittest.TestCase):
    def test_refinement_replaces_draft_and_preserves_timestamp(self):
        session = _Session()
        engine = object.__new__(_FastPostEngine)
        engine.model = SimpleNamespace(clone_session=lambda: session)
        engine.report = _Reporter()

        result = list(engine.transcribe(np.zeros(32000, dtype=np.float32), None, 16000))

        self.assertEqual(result, [(0.2, "補正結果", "ja")])
        self.assertFalse(session.partials_enabled)

    def test_cancellation_is_checked_between_audio_chunks(self):
        engine = object.__new__(_FastPostEngine)
        engine.model = SimpleNamespace(clone_session=_Session)
        engine.report = _Reporter(cancel=lambda: True)

        with self.assertRaises(PostProcessCancelled):
            list(engine.transcribe(np.zeros(32000, dtype=np.float32), "ja", 16000))


if __name__ == "__main__":
    unittest.main()
