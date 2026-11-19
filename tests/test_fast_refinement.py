"""Fast ASR scheduling and replay without downloading recognition models."""
import io
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np

from gijiroku.asr.fast import FastASREvent, FastJapaneseEnglishASR


class FastRefinementTests(unittest.TestCase):
    def make_session(self):
        session = object.__new__(FastJapaneseEnglishASR)
        vad = Mock()
        vad.empty.return_value = True
        vad.is_speech_detected.return_value = False
        session._make_vad = lambda: vad
        session._init_session()
        self.addCleanup(session.close_deferred_refinements)
        return session

    def test_first_two_partial_captions_are_one_second_apart_in_both_languages(self):
        for lang in ("ja", "en"):
            with self.subTest(lang=lang):
                session = self.make_session()
                chunk = np.zeros(session.window_size, dtype=np.float32)
                session.vad.is_speech_detected.return_value = True
                session.vad.current_segment = SimpleNamespace(
                    samples=np.zeros(session.sample_rate, dtype=np.float32))
                session._decode_lang = Mock(return_value="caption")
                partial_times = []
                for i in range(65):
                    for event in session.accept(chunk, lang_hint=lang):
                        self.assertEqual((event.kind, event.language), ("partial", lang))
                        partial_times.append((i + 1) * len(chunk) / session.sample_rate)
                self.assertEqual(len(partial_times), 2)
                self.assertAlmostEqual(partial_times[0], 1, delta=len(chunk) / session.sample_rate)
                self.assertAlmostEqual(partial_times[1] - partial_times[0], 1,
                                       delta=len(chunk) / session.sample_rate)

    def test_deferred_recordings_keep_their_own_pcm_after_history_expires(self):
        recordings = []
        for lang, value in (("ja", 0.25), ("en", -0.5)):
            session = self.make_session()
            session.defer_refinement = True
            sr = session.sample_rate
            samples = np.full(3 * sr, value, dtype=np.float32)
            session._push_history(samples)
            start, end = 2 * sr, 3 * sr
            session._group = [(start, end, lang, "final")]
            session._decode_lang = Mock(return_value="corrected final")
            self.assertEqual(session.flush(), [])
            session._decode_lang.assert_not_called()
            recordings.append((session, lang, samples[sr:end], start, end))
            session._push_history(np.zeros(61 * sr, dtype=np.float32))
            session.defer_refinement = False

        for session, lang, expected_audio, start, end in reversed(recordings):
            events = list(session.drain_deferred_refinements())
            self.assertEqual(events, [FastASREvent("refine", "corrected final", lang, start, end)])
            session._decode_lang.assert_called_once()
            np.testing.assert_array_equal(session._decode_lang.call_args.args[0], expected_audio)
            self.assertEqual(list(session.drain_deferred_refinements()), [])

    def test_closing_discards_unprocessed_refinements_without_decoding(self):
        session = self.make_session()
        session.defer_refinement = True
        session._push_history(np.zeros(1600, dtype=np.float32))
        session._group = [(0, 1600, "en", "final")]
        session._decode_lang = Mock()
        self.assertEqual(session.flush(), [])
        session.close_deferred_refinements()
        self.assertEqual(list(session.drain_deferred_refinements()), [])
        session._decode_lang.assert_not_called()

    def test_stop_replays_before_final_export_without_retranslating(self):
        from gijiroku.gui import MeetingRecorderGUI, ROLE_OTHER

        session = self.make_session()
        sr = session.sample_rate
        session.defer_refinement = True
        session._push_history(np.zeros(2 * sr, dtype=np.float32))
        session._group = [(sr, 2 * sr, "en", "fifteen")]
        session._decode_lang = Mock(return_value="fifty dollars")
        self.assertEqual(session.flush(), [])
        with tempfile.TemporaryDirectory() as folder:
            gui = MeetingRecorderGUI.__new__(MeetingRecorderGUI)
            gui.TRANSCRIBE_RATE = sr
            gui.transcriber = {ROLE_OTHER: session}
            gui._transcribe_thread = None
            gui.root = Mock()
            gui.label_status = Mock()
            gui._translation_worker = Mock()
            gui._submit_translation = Mock()
            gui._meeting_name = "test"
            gui._recording_dir = folder
            gui.transcription_file = None
            gui.refined_transcription_file = io.StringIO()
            gui._fast_final_segments = [
                FastASREvent("final", "fifteen", "en", sr, 2 * sr, speaker=ROLE_OTHER)]
            gui._fast_refined_segments = []

            gui._transcription_stop()

            output = (Path(folder) / "transcription_final.txt").read_text(encoding="utf-8")
            self.assertEqual(output.strip(), f"[00:01] [{ROLE_OTHER}] [en] fifty dollars")
            self.assertEqual(session.pending_refinement_count, 0)
            gui._submit_translation.assert_not_called()


if __name__ == "__main__":
    unittest.main()
