import unittest
import numpy as np
from gijiroku.audio.microphone import MicrophoneGate
from gijiroku.audio.pipeline import RecordingProcessor, FRAMES


class MicrophoneTests(unittest.TestCase):
    def test_buffered_packet_keeps_muted_interval_after_reenable(self):
        gate = MicrophoneGate()
        gate.set_enabled(False, at=10.01)
        gate.set_enabled(True, at=10.03)
        samples = np.ones((2400, 2), np.float32)
        filtered = gate.filter(samples, 10, 48000)
        np.testing.assert_array_equal(filtered[:480], 1)
        np.testing.assert_array_equal(filtered[480:1440], 0)
        np.testing.assert_array_equal(filtered[1440:], 1)
        np.testing.assert_array_equal(samples, 1)

    def test_initial_off_and_exact_boundary(self):
        gate = MicrophoneGate(False)
        gate.set_enabled(True, at=1.01)
        np.testing.assert_array_equal(gate.mask(1, 48000, 480), False)
        np.testing.assert_array_equal(gate.mask(1.01, 48000, 480), True)

    def test_post_aec_mute_preserves_speaker_and_removes_residual(self):
        class ResidualAEC:
            def __init__(self, **kwargs): pass
            def reset(self): pass
            def process(self, near, reference):
                return np.ones_like(near) * .1
        processor = RecordingProcessor(['mic', 'speaker'], processor_factory=ResidualAEC)
        mic = np.zeros((FRAMES, 2), np.float32)
        speaker = np.ones_like(mic) * .2
        raw, own, other = processor.process([mic, speaker],
            microphone_mask=np.zeros(FRAMES, dtype=bool))
        self.assertEqual(own, bytes(FRAMES * 4))
        self.assertEqual(raw, other)
        self.assertTrue(np.frombuffer(other, np.int16).all())

    def test_gui_toggle_updates_gate_and_persists(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        from gijiroku.gui import MeetingRecorderGUI
        gui = MeetingRecorderGUI.__new__(MeetingRecorderGUI)
        gui._record_microphone_var = SimpleNamespace(get=lambda: False)
        gui._microphone_gate = MicrophoneGate()
        gui._save_settings = Mock()
        gui._log = Mock()
        gui._on_microphone_toggle()
        self.assertFalse(gui.RECORD_MICROPHONE)
        gui._save_settings.assert_called_once()
        import time
        self.assertFalse(gui._microphone_gate.mask(time.monotonic(), 48000, 480).any())

    def test_muted_audio_is_zero_in_saved_asr_and_diagnostic_outputs(self):
        import queue
        import threading
        import tempfile
        import wave
        from pathlib import Path
        from types import SimpleNamespace
        from gijiroku.audio.clock import CapturePacket
        from gijiroku.gui import MeetingRecorderGUI, _EOF, ROLE_SELF
        gui = MeetingRecorderGUI.__new__(MeetingRecorderGUI)
        gui._audio_origin, gui._audio_stop_time = 10, 10.05
        gui._audio_error, gui._audio_packets = None, 0
        gui._queue_overflow_count = 0
        gui._audio_finished = threading.Event()
        gui.TARGET_RATE = 48000
        gui.stop_event = threading.Event()
        gui.stop_event.set()
        gui._recording_processor = RecordingProcessor(['mic'])
        gui._microphone_gate = MicrophoneGate(False)
        gui.AUDIO_DIAGNOSTIC_SECONDS = 1
        gui.root = SimpleNamespace(after=lambda *args: None)
        gui._log = gui._update_level = lambda *args: None
        saved, asr = [], []
        gui._write_audio = lambda raw, roles: saved.append((raw, roles))
        gui._push_transcribe = lambda raw, roles: asr.append((raw, roles))
        packets = queue.Queue()
        packets.put(CapturePacket(10, 48000, np.ones((2400, 2), np.float32) * .1))
        packets.put(_EOF)
        with tempfile.TemporaryDirectory() as folder:
            gui._recording_dir = folder
            gui._mixer_loop_n(threading.Event(), [packets], [{'kind': 'mic'}])
            for filename in ('audio_capture_00_mic.wav', 'audio_input_00_mic.wav'):
                with wave.open(str(Path(folder) / filename), 'rb') as audio:
                    self.assertEqual(audio.getnframes(), 2400)
                    self.assertEqual(audio.readframes(2400), bytes(2400 * 4))
        self.assertIsNone(gui._audio_error)
        for outputs in (saved, asr):
            self.assertEqual(sum(len(raw) for raw, _ in outputs), 2400 * 4)
            for raw, roles in outputs:
                self.assertEqual(raw, bytes(len(raw)))
                self.assertEqual(roles[ROLE_SELF], raw)
