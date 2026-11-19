from queue import Full
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import numpy as np

from gijiroku.asr.backlog import AudioBacklogQueue
from gijiroku.asr.fast import FastJapaneseEnglishASR
from gijiroku.gui import MeetingRecorderGUI


class AudioBacklogTests(unittest.TestCase):
    def test_pending_seconds_tracks_accepted_chunks_in_their_source_format(self):
        audio = AudioBacklogQueue(maxsize=2, pcm_rate=48000, mono_rate=16000)
        stereo = bytes(12000 * 4)
        mono = np.zeros(8000, dtype=np.float32)
        audio.put_nowait(stereo)
        audio.put_nowait(mono)
        with self.assertRaises(Full):
            audio.put_nowait(stereo)
        self.assertAlmostEqual(audio.pending_seconds, 0.75)
        self.assertIs(audio.get_nowait(), stereo)
        self.assertAlmostEqual(audio.pending_seconds, 0.5)
        self.assertIs(audio.get_nowait(), mono)
        self.assertEqual(audio.pending_seconds, 0.0)


class SubtitleLoadTests(unittest.TestCase):
    def setUp(self):
        self.gui = MeetingRecorderGUI.__new__(MeetingRecorderGUI)
        self.gui.root = Mock()
        self.gui._asr_load_level = 0
        self.gui._asr_backlog_check = None
        self.queues = {role: SimpleNamespace(pending_seconds=0) for role in ("self", "other")}
        self.sessions = {role: SimpleNamespace(
            defer_refinement=False, partials_enabled=True,
            partial_every=FastJapaneseEnglishASR.partial_every) for role in self.queues}

    def check_load(self, now, self_lag, other_lag, *, defer, partials):
        self.queues["self"].pending_seconds = self_lag
        self.queues["other"].pending_seconds = other_lag
        with patch("gijiroku.gui.time.monotonic", return_value=now):
            self.gui._update_asr_load(self.queues, self.sessions)
        for session in self.sessions.values():
            self.assertEqual(session.defer_refinement, defer)
            self.assertEqual(session.partials_enabled, partials)
            self.assertEqual(session.partial_every, 1.0)

    def test_worst_role_defers_first_then_pauses_and_recovers(self):
        # Simultaneous speaker tracks each represent the same meeting clock.
        self.check_load(10.0, 0.6, 0.6, defer=False, partials=True)
        self.check_load(11.0, 0.1, 3.5, defer=True, partials=True)
        self.check_load(11.1, 0.1, 3.5, defer=True, partials=True)
        self.check_load(12.2, 0.1, 4.0, defer=True, partials=False)
        self.check_load(13.2, 0.1, 1.2, defer=True, partials=True)
        self.check_load(14.2, 0.1, 0.4, defer=False, partials=True)

    def test_falling_backlog_keeps_partial_subtitles(self):
        self.check_load(20.0, 5.0, 0.1, defer=True, partials=True)
        self.check_load(21.2, 3.5, 0.1, defer=True, partials=True)
