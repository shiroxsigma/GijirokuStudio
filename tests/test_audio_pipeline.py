import queue
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
from scipy.signal import butter, sosfilt

from gijiroku.recording import (CaptureClock, CapturePacket, TimelineSource,
                            DuplicateMixer, RecordingProcessor, RATE, FRAMES)


class CaptureTests(unittest.TestCase):
    def test_adc_timestamps_ignore_callback_jitter(self):
        clock = CaptureClock(48000, 1)
        raw = bytes(960)
        first = clock.packet(raw, {'current_time': 10, 'input_buffer_adc_time': 9.99}, 100)
        second = clock.packet(raw, {'current_time': 10.01, 'input_buffer_adc_time': 10}, 100.04)
        self.assertAlmostEqual(second.start - first.start, .01)
        self.assertEqual(first.samples.shape, (480, 2))

    def test_missing_timestamps_preserve_idle_gap(self):
        clock = CaptureClock(48000, 2)
        a = clock.packet(bytes(1920), {}, 10)
        b = clock.packet(bytes(1920), {}, 11)
        self.assertAlmostEqual(b.start - a.start, 1)

    def test_native_rate_fractional_phase_and_boundary(self):
        source = TimelineSource()
        rate = 44100
        # A ramp should be exactly interpolated, even at callback boundaries.
        samples = np.arange(5000, dtype=np.float32) / 5000
        for i in range(0, len(samples), 1024):
            source.add(CapturePacket(10 + i / rate, rate,
                np.repeat(samples[i:i+1024, None], 2, axis=1)))
        actual = np.concatenate([source.read(10 + i / RATE)[:, 0]
                                 for i in range(0, 4800, FRAMES)])
        expected = np.arange(4800) * rate / RATE / 5000
        np.testing.assert_allclose(actual, expected, atol=1e-6)

    def test_gap_is_silence_not_concatenation(self):
        source = TimelineSource()
        block = np.ones((480, 2), np.float32) * .2
        source.add(CapturePacket(10, RATE, block))
        source.add(CapturePacket(10.1, RATE, block))
        np.testing.assert_allclose(source.read(10.05), 0)
        np.testing.assert_allclose(source.read(10.1), .2)

    def test_high_rate_input_is_filtered(self):
        source = TimelineSource()
        x = np.sin(2 * np.pi * 35000 * np.arange(9600) / 96000).astype(np.float32)
        for i in range(0, len(x), 960):
            source.add(CapturePacket(10 + i / 96000, 96000,
                                    np.repeat(x[i:i+960, None], 2, axis=1)))
        self.assertLess(np.std(source.read(10.05)), .03)


class DuplicateTests(unittest.TestCase):
    def run_mix(self, signals, threshold=.86):
        mixer = DuplicateMixer(len(signals), threshold)
        output = []
        for i in range(0, len(signals[0]), FRAMES):
            output.append(mixer.process([np.repeat(s[i:i+FRAMES, None], 2, axis=1)
                                         for s in signals]))
        return mixer, np.concatenate(output)[:, 0]

    def test_delayed_mirrored_inputs_are_not_summed(self):
        rng = np.random.default_rng(4)
        x = rng.normal(0, .1, RATE * 2).astype(np.float32)
        delay = 1440  # 30 ms
        y = np.r_[np.zeros(delay), x[:-delay]].astype(np.float32)
        mixer, output = self.run_mix([x, y, x * .6])
        self.assertEqual(mixer.duplicates, 2)
        np.testing.assert_allclose(output[-RATE:], x[-RATE:], atol=1e-5)

    def test_independent_simultaneous_voices_remain(self):
        rng = np.random.default_rng(8)
        x, y = rng.normal(0, .05, (2, RATE)).astype(np.float32)
        mixer, output = self.run_mix([x, y])
        self.assertEqual(mixer.duplicates, 0)
        np.testing.assert_allclose(output, x + y, atol=1e-6)

    def test_suppressed_source_recovers_when_content_changes(self):
        rng = np.random.default_rng(9)
        x, y = rng.normal(0, .05, (2, RATE * 2)).astype(np.float32)
        y[:RATE] = x[:RATE]
        mixer, output = self.run_mix([x, y])
        self.assertEqual(mixer.duplicates, 0)
        np.testing.assert_allclose(output[-4800:], (x+y)[-4800:], atol=1e-5)


class ProcessingTests(unittest.TestCase):
    def test_independent_aec_instances_and_gain_after_processing(self):
        seen = []
        class Stub:
            def __init__(self, **kwargs):
                seen.append(self)
            def reset(self):
                pass
            def process(self, near, far):
                self.near = near.copy()
                self.far = far.copy()
                return near * .1
        processor = RecordingProcessor(['mic', 'mic', 'speaker'], 2, processor_factory=Stub)
        mic = np.full((480, 2), .7, np.float32)
        far = np.full((480, 2), .2, np.float32)
        mix, own, other = processor.process([mic, mic, far])
        self.assertEqual(len(seen), 2)
        for instance in seen:
            np.testing.assert_allclose(instance.near, .7)
        self.assertEqual(len(mix), 1920)
        self.assertLess(np.max(np.frombuffer(own, np.int16)), 10000)
        self.assertGreater(np.max(np.frombuffer(other, np.int16)), 10000)

    def test_speaker_only_does_not_require_aec(self):
        def unavailable(**kwargs):
            raise RuntimeError('not installed')
        processor = RecordingProcessor(['speaker'], processor_factory=unavailable)
        x = np.full((480, 2), .1, np.float32)
        mix, own, other = processor.process([x])
        self.assertEqual(mix, other)
        self.assertEqual(own, bytes(1920))

    def test_idle_speakers_do_not_add_processing_stages(self):
        instances = []
        class Stub:
            def __init__(self, **kwargs):
                self.calls = 0
                instances.append(self)
            def reset(self):
                pass
            def process(self, near, far):
                self.calls += 1
                return near
        processor = RecordingProcessor(['mic'] + ['speaker'] * 8,
                                       processor_factory=Stub)
        zero = np.zeros((480, 2), np.float32)
        mic = np.full((480, 2), .1, np.float32)
        processor.process([mic] + [zero] * 8)
        self.assertEqual(sum(x.calls for x in instances), 1)

    def test_limiter_preserves_role_sum(self):
        processor = RecordingProcessor(['mic', 'mic'], gain=5)
        x = np.full((480, 2), .8, np.float32)
        mix, own, other = processor.process([x, x])
        self.assertEqual(mix, own)
        self.assertEqual(other, bytes(1920))
        self.assertLessEqual(np.max(np.frombuffer(mix, np.int16)), 31130)

    def test_real_aec_reduces_delayed_echo(self):
        rng = np.random.default_rng(12)
        far = sosfilt(butter(3, 4000, fs=RATE, output='sos'),
                      rng.normal(0, .15, RATE * 6)).astype(np.float32)
        near = np.r_[np.zeros(1440), far[:-1440] * .6].astype(np.float32)
        processor = RecordingProcessor(['mic', 'speaker'])
        output = []
        for i in range(0, len(far), FRAMES):
            _, own, _ = processor.process([
                np.repeat(near[i:i+FRAMES, None], 2, axis=1),
                np.repeat(far[i:i+FRAMES, None], 2, axis=1)])
            output.append(np.frombuffer(own, np.int16).reshape(-1, 2)[:, 0] / 32768)
        clean = np.concatenate(output)
        reduction = 20 * np.log10(np.std(near[-RATE:]) / max(np.std(clean[-RATE:]), 1e-9))
        print(f'\nSynthetic delayed echo reduction: {reduction:.1f} dB')
        self.assertGreater(reduction, 12)

    def test_independent_speaker_paths_and_double_talk(self):
        rng = np.random.default_rng(42)
        length = RATE * 8
        sos = butter(3, 3500, fs=RATE, output='sos')
        a, b, voice = [sosfilt(sos, x).astype(np.float32)
                       for x in rng.normal(0, .15, (3, length))]
        # A voiced, syllabically modulated probe. Stationary white noise would
        # correctly be removed by NS and is not a speech-preservation test.
        t = np.arange(length) / RATE
        phase = 2 * np.pi * (150 * t + 2 * np.sin(2 * np.pi * 2 * t))
        voice = (.12 * (np.sin(phase) + .5 * np.sin(2 * phase)
                         + .25 * np.sin(3 * phase))
                 * (.5 + .5 * np.sin(2 * np.pi * 3.7 * t))).astype(np.float32)
        echo = np.r_[np.zeros(960), a[:-960] * .7]
        echo += np.r_[np.zeros(2880), b[:-2880] * .4]
        voice[:RATE * 6] = 0
        processor = RecordingProcessor(['mic', 'speaker', 'speaker'])
        output = []
        for i in range(0, length, FRAMES):
            blocks = [np.repeat(x[i:i+FRAMES, None], 2, axis=1).astype(np.float32)
                      for x in (echo + voice, a, b)]
            _, own, _ = processor.process(blocks)
            output.append(np.frombuffer(own, np.int16).reshape(-1, 2)[:, 0] / 32768)
        clean = np.concatenate(output)
        section = slice(RATE * 5, RATE * 6)
        reduction = 20 * np.log10(np.std(echo[section]) / max(np.std(clean[section]), 1e-9))
        retained = np.std(clean[-RATE:]) / np.std(voice[-RATE:])
        print(f'\nTwo speaker paths: {reduction:.1f} dB echo reduction; double-talk RMS ratio {retained:.2f}')
        self.assertGreater(reduction, 12)
        self.assertGreater(retained, .4)
        self.assertLess(retained, 1.5)


class IntegrationTests(unittest.TestCase):
    def test_identically_named_physical_mics_are_preserved(self):
        from gijiroku.gui import MeetingRecorderGUI
        gui = MeetingRecorderGUI.__new__(MeetingRecorderGUI)
        devices = [dict(name='USB Microphone', index=i, max_input_channels=1,
                        default_samplerate=48000, hostapi=0 if i < 2 else 1)
                   for i in range(4)]
        pa = SimpleNamespace(get_loopback_device_info_generator=lambda: [],
                             terminate=lambda: None)
        with patch('gijiroku.gui.pyaudio.PyAudio', return_value=pa), \
             patch('gijiroku.gui.sd.query_hostapis', return_value=[{'name': 'MME'}, {'name': 'Windows WASAPI'}]), \
             patch('gijiroku.gui.sd.query_devices', return_value=devices):
            gui._enumerate_audio_devices()
        self.assertEqual([x['native_idx'] for x in gui._audio_devices], [2, 3])

    def test_asr_role_queues_drop_together(self):
        from gijiroku.gui import MeetingRecorderGUI, ROLE_SELF, ROLE_OTHER
        gui = MeetingRecorderGUI.__new__(MeetingRecorderGUI)
        own, other = queue.Queue(maxsize=1), queue.Queue(maxsize=1)
        own.put(b'previous')
        gui.transcribe_role_queues = {ROLE_SELF: own, ROLE_OTHER: other}
        gui._queue_overflow_count = 0
        gui._push_transcribe(bytes(1920), {ROLE_SELF: bytes(1920), ROLE_OTHER: bytes(1920)})
        self.assertTrue(other.empty())
        self.assertEqual(gui._queue_overflow_count, 1)

    def test_fast_asr_drains_after_recording_stops(self):
        from gijiroku.gui import MeetingRecorderGUI
        gui = MeetingRecorderGUI.__new__(MeetingRecorderGUI)
        accepted = []
        gui.transcriber = SimpleNamespace(
            accept=lambda pcm, lang: accepted.append(len(pcm)) or [],
            flush=lambda lang: [])
        gui.transcribe_queue = queue.Queue()
        gui.transcribe_queue.put(bytes(4800 * 4))
        gui._audio_finished = threading.Event()
        gui._audio_finished.set()
        gui.is_recording = False
        gui.stop_event = threading.Event()
        gui.stop_event.set()
        gui.TARGET_RATE = RATE
        gui.TRANSCRIBE_RATE = 16000
        gui._rt_lang = 'ja'
        gui._record_fast_results = lambda *args: None
        gui._update_asr_load = lambda *args: None
        gui._transcribe_fast_loop()
        self.assertEqual(accepted, [1600])

    def test_stop_flushes_parallel_sources_on_one_timeline(self):
        from gijiroku.gui import MeetingRecorderGUI, _EOF, ROLE_SELF, ROLE_OTHER
        gui = MeetingRecorderGUI.__new__(MeetingRecorderGUI)
        gui._audio_origin = 10
        gui._audio_stop_time = 10.05
        gui._audio_error = None
        gui._audio_packets = 0
        gui._audio_finished = threading.Event()
        gui.TARGET_RATE = RATE
        gui.stop_event = threading.Event()
        gui.stop_event.set()
        gui._recording_processor = RecordingProcessor(['mic', 'mic'])
        gui._queue_overflow_count = 0
        gui.root = SimpleNamespace(after=lambda *args: None)
        gui._log = lambda *args: None
        gui._update_level = lambda *args: None
        written, forwarded = [], []
        gui._write_audio = lambda raw, roles: written.append((raw, roles))
        gui._push_transcribe = lambda raw, roles: forwarded.append((raw, roles))
        queues = [queue.Queue(), queue.Queue()]
        for q in queues:
            q.put(CapturePacket(10, RATE, np.ones((2400, 2), np.float32) * .1))
            q.put(_EOF)
        active = threading.Event()
        gui._mixer_loop_n(active, queues)
        self.assertIsNone(gui._audio_error)
        self.assertEqual(sum(len(raw) for raw, _ in written), 2400 * 4)
        self.assertEqual(sum(len(raw) for raw, _ in forwarded), 2400 * 4)
        for raw, roles in written:
            self.assertEqual(len(roles[ROLE_SELF]), len(raw))
            self.assertEqual(roles[ROLE_OTHER], bytes(len(raw)))


if __name__ == '__main__':
    unittest.main()
