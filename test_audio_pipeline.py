import queue
import os
import subprocess
import tempfile
import threading
import unittest
import wave
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
from scipy.signal import butter, sosfilt

from audio_pipeline import (CaptureClock, CapturePacket, TimelineSource,
                            DuplicateMixer, RecordingProcessor, RATE, FRAMES)


class CaptureTests(unittest.TestCase):
    def test_adc_timestamps_ignore_callback_jitter(self):
        clock = CaptureClock(48000, 1)
        raw = bytes(960)
        first = clock.packet(raw, {'current_time': 10, 'input_buffer_adc_time': 9.99}, 100)
        second = clock.packet(raw, {'current_time': 10.01, 'input_buffer_adc_time': 10}, 100.04)
        self.assertAlmostEqual(second.start - first.start, .01)
        self.assertEqual(first.samples.shape, (480, 2))

    def test_adc_timestamp_jitter_does_not_insert_audio_gaps(self):
        clock = CaptureClock(48000, 1)
        raw = bytes(960)
        first = clock.packet(raw, {'current_time': 10, 'input_buffer_adc_time': 9.99}, 100)
        second = clock.packet(raw, {'current_time': 10.03,
                                    'input_buffer_adc_time': 10.015}, 100.03)
        self.assertAlmostEqual(second.start - first.start, .01, delta=1 / 48000)

    def test_jittered_adc_packets_render_without_short_silences(self):
        clock = CaptureClock(RATE, 1)
        source = TimelineSource()
        raw = np.full(1024, 6554, dtype=np.int16).tobytes()
        duration = 1024 / RATE
        first_start = None
        for i in range(50):
            t = 10 + i * duration
            jitter = (0, .012, -.009)[i % 3]
            packet = clock.packet(raw, {
                'current_time': t + .01,
                'input_buffer_adc_time': t - .01 + jitter,
            }, 100 + i * duration + .01)
            if first_start is None:
                first_start = packet.start
            source.add(packet)
        audio = np.concatenate([source.read(first_start + i * FRAMES / RATE)[:, 0]
                                for i in range(100)])
        self.assertGreater(np.min(audio[100:-100]), .19)

    def test_native_16k_mic_packets_resample_without_gaps(self):
        clock = CaptureClock(16000, 1)
        source = TimelineSource()
        raw = np.full(1024, 6554, dtype=np.int16).tobytes()
        start = None
        for i in range(20):
            t = 10 + i * 1024 / 16000
            packet = clock.packet(raw, {
                'current_time': t + .01,
                'input_buffer_adc_time': t - .01 + (0, .018, -.012)[i % 3],
            }, 100 + i * 1024 / 16000 + .01)
            if start is None:
                start = packet.start
            source.add(packet)
        audio = np.concatenate([source.read(start + i * FRAMES / RATE)[:, 0]
                                for i in range(100)])
        self.assertGreater(np.min(audio[100:-100]), .19)

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

    def test_silent_speakers_leave_microphone_unprocessed(self):
        instances = []
        class Stub:
            def __init__(self, **kwargs):
                self.calls = 0
                instances.append(self)
            def reset(self):
                pass
            def process(self, near, far):
                self.calls += 1
                return near * .1
        processor = RecordingProcessor(['mic'] + ['speaker'] * 8,
                                       processor_factory=Stub)
        zero = np.full((480, 2), 3e-5, np.float32)
        mic = np.full((480, 2), .1, np.float32)
        _, own, _ = processor.process([mic] + [zero] * 8)
        self.assertEqual(sum(x.calls for x in instances), 0)
        np.testing.assert_allclose(np.frombuffer(own, np.int16),
                                   np.full(FRAMES * 2, 3276), atol=1)

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
    def test_capture_tries_device_native_rate_first(self):
        from main import MeetingRecorderGUI
        candidates = MeetingRecorderGUI._format_candidates(1, 16000)
        self.assertEqual(candidates[0], (1, 16000))
        self.assertIn((1, 48000), candidates)

    def test_role_mp3_keeps_full_rate_and_does_not_boost_mono(self):
        from main import _PcmWriter, FFMPEG_PATH
        if not os.path.exists(FFMPEG_PATH):
            self.skipTest('ffmpeg.exe is unavailable')
        t = np.arange(RATE, dtype=np.float32) / RATE
        tone = (.25 * np.sin(2 * np.pi * 1000 * t))
        stereo = np.repeat(tone[:, None], 2, axis=1)
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, 'role.mp3')
            writer = _PcmWriter(path, RATE, transcription_only=True)
            writer.write((stereo * 32767).astype(np.int16).tobytes())
            writer.close()
            decoded = subprocess.run([FFMPEG_PATH, '-v', 'error', '-i', path,
                '-f', 'f32le', '-'], capture_output=True, check=True).stdout
        samples = np.frombuffer(decoded, dtype='<f4')
        self.assertGreater(len(samples), RATE - 1000)
        self.assertLess(len(samples), RATE + 1000)
        self.assertLess(np.max(np.abs(samples)), .30)

    def test_normal_audio_selection_keeps_speakers_and_headset_mic(self):
        from main import MeetingRecorderGUI
        gui = MeetingRecorderGUI.__new__(MeetingRecorderGUI)
        class Listbox:
            def __init__(self):
                self.selected = set()
                self.items = []
            def delete(self, *args):
                self.items.clear()
                self.selected.clear()
            def insert(self, *args):
                self.items.append(args[-1])
            def selection_set(self, idx):
                self.selected.add(idx)
            def curselection(self):
                return tuple(sorted(self.selected))
        gui.listbox_audio = Listbox()
        loopbacks = [
            {'index': 10, 'name': 'Headphones (3- FreeClip) [Loopback]', 'maxInputChannels': 2,
             'defaultSampleRate': 48000},
            {'index': 11, 'name': 'Speakers [Loopback]', 'maxInputChannels': 2,
             'defaultSampleRate': 48000},
        ]
        inputs = [
            {'index': 0, 'name': 'Built-in Microphone', 'max_input_channels': 1,
             'default_samplerate': 48000, 'hostapi': 0},
            {'index': 1, 'name': 'Built-in Microphone', 'max_input_channels': 1,
             'default_samplerate': 48000, 'hostapi': 1},
            {'index': 3, 'name': 'Headset (3- FreeClip)', 'max_input_channels': 1,
             'default_samplerate': 48000, 'hostapi': 1},
            {'index': 2, 'name': 'Old Bluetooth Hands-Free', 'max_input_channels': 1,
             'default_samplerate': 48000, 'hostapi': 2},
        ]
        pa = SimpleNamespace(
            get_default_output_device_info=lambda: {'name': 'Speakers'},
            get_loopback_device_info_generator=lambda: loopbacks,
            terminate=lambda: None)
        with patch('main.pyaudio.PyAudio', return_value=pa), \
             patch('main.sd.query_hostapis', return_value=[
                 {'name': 'MME'}, {'name': 'Windows WASAPI'},
                 {'name': 'Windows WDM-KS'}]), \
             patch('main.sd.query_devices', return_value=inputs), \
             patch('main.sd.default', SimpleNamespace(device=(0, None))):
            gui._enumerate_audio_devices()
        gui._populate_audio_listbox()
        self.assertEqual([d['name'] for d in gui._audio_devices],
                         ['Headphones (3- FreeClip) [Loopback]', 'Speakers [Loopback]',
                          'Built-in Microphone', 'Headset (3- FreeClip)'])
        self.assertEqual(gui.listbox_audio.selected, {0, 1, 3})
        gui.listbox_audio.selected = set(range(len(gui._audio_devices)))
        gui.is_recording = False
        gui._log = lambda *args: None
        gui._normalize_all_audio_selection()
        self.assertEqual(gui.listbox_audio.selected, {0, 1, 3})

    def test_recording_revalidates_disconnected_headset(self):
        from main import MeetingRecorderGUI
        gui = MeetingRecorderGUI.__new__(MeetingRecorderGUI)
        class Listbox:
            def __init__(self):
                self.selected = {0, 1, 2}
            def curselection(self):
                return tuple(sorted(self.selected))
            def selection_clear(self, *args):
                self.selected.clear()
            def selection_set(self, idx):
                self.selected.add(idx)
        gui.listbox_audio = Listbox()
        gui._audio_devices = [
            dict(kind='speaker', name='FreeClip [Loopback]', api='WASAPI', native_idx=7),
            dict(kind='speaker', name='Speakers [Loopback]', api='WASAPI', native_idx=8),
            dict(kind='mic', name='FreeClip mic', api='WASAPI', native_idx=9),
        ]
        def enumerate_fresh():
            gui._audio_devices = [
                dict(kind='speaker', name='Speakers [Loopback]', api='WASAPI', native_idx=4),
                dict(kind='mic', name='Built-in mic', api='WASAPI', native_idx=5),
            ]
        gui._enumerate_audio_devices = enumerate_fresh
        gui._populate_audio_listbox = lambda: None
        missing = gui._revalidate_audio_selection()
        self.assertEqual([d['name'] for d in missing],
                         ['FreeClip [Loopback]', 'FreeClip mic'])
        self.assertEqual(gui.listbox_audio.selected, {0})
        self.assertEqual(gui._selected_audio_devices()[0]['native_idx'], 4)

    def test_two_loopbacks_share_one_portaudio_instance(self):
        from main import MeetingRecorderGUI, _EOF
        gui = MeetingRecorderGUI.__new__(MeetingRecorderGUI)
        gui._open_lock = threading.Lock()
        gui._queue_overflow_count = 0
        gui.is_recording = False
        gui.root = SimpleNamespace(after=lambda *args: None)
        events = []
        class Stream:
            def start_stream(self):
                events.append('start')
            def stop_stream(self):
                events.append('stop')
            def close(self):
                events.append('close')
        class PortAudio:
            def __init__(self):
                events.append('init')
            def open(self, **kwargs):
                events.append(('open', kwargs['input_device_index']))
                return Stream()
            def terminate(self):
                events.append('terminate')
        tasks = [
            (dict(name=f'Speaker {i}', native_idx=i, channels=2, rate=48000), queue.Queue())
            for i in (1, 2)
        ]
        with patch('main.pyaudio.PyAudio', PortAudio), \
             patch('main._com_initialize', return_value=False):
            gui._capture_speakers(threading.Event(), tasks)
        self.assertEqual(events.count('init'), 1)
        self.assertEqual(events.count('terminate'), 1)
        self.assertEqual([e for e in events if isinstance(e, tuple)],
                         [('open', 1), ('open', 2)])
        self.assertTrue(all(q.get_nowait() is _EOF for _, q in tasks))

    def test_identically_named_physical_mics_are_preserved(self):
        from main import MeetingRecorderGUI
        gui = MeetingRecorderGUI.__new__(MeetingRecorderGUI)
        devices = [dict(name='USB Microphone', index=i, max_input_channels=1,
                        default_samplerate=48000, hostapi=0 if i < 2 else 1)
                   for i in range(4)]
        pa = SimpleNamespace(get_loopback_device_info_generator=lambda: [],
                             terminate=lambda: None)
        with patch('main.pyaudio.PyAudio', return_value=pa), \
             patch('main.sd.query_hostapis', return_value=[{'name': 'MME'}, {'name': 'Windows WASAPI'}]), \
             patch('main.sd.query_devices', return_value=devices):
            gui._enumerate_audio_devices()
        self.assertEqual([x['native_idx'] for x in gui._audio_devices], [2, 3])

    def test_asr_role_queues_drop_together(self):
        from main import MeetingRecorderGUI, ROLE_SELF, ROLE_OTHER
        gui = MeetingRecorderGUI.__new__(MeetingRecorderGUI)
        own, other = queue.Queue(maxsize=1), queue.Queue(maxsize=1)
        own.put(b'previous')
        gui.transcribe_role_queues = {ROLE_SELF: own, ROLE_OTHER: other}
        gui._queue_overflow_count = 0
        gui._push_transcribe(bytes(1920), {ROLE_SELF: bytes(1920), ROLE_OTHER: bytes(1920)})
        self.assertTrue(other.empty())
        self.assertEqual(gui._queue_overflow_count, 1)

    def test_fast_asr_drains_after_recording_stops(self):
        from main import MeetingRecorderGUI
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
        from main import MeetingRecorderGUI, _EOF, ROLE_SELF, ROLE_OTHER
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
        gui.AUDIO_DIAGNOSTIC_SECONDS = 1
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
        with tempfile.TemporaryDirectory() as folder:
            gui._recording_dir = folder
            gui._mixer_loop_n(active, queues,
                              [{'kind': 'mic'}, {'kind': 'mic'}])
            with wave.open(os.path.join(folder, 'audio_input_00_mic.wav'), 'rb') as recorded:
                self.assertEqual(recorded.getframerate(), RATE)
                self.assertEqual(recorded.getnframes(), 2400)
            with wave.open(os.path.join(folder, 'audio_capture_00_mic.wav'), 'rb') as captured:
                self.assertEqual(captured.getframerate(), RATE)
                self.assertEqual(captured.getnframes(), 2400)
        self.assertIsNone(gui._audio_error)
        self.assertEqual(sum(len(raw) for raw, _ in written), 2400 * 4)
        self.assertEqual(sum(len(raw) for raw, _ in forwarded), 2400 * 4)
        for raw, roles in written:
            self.assertEqual(len(roles[ROLE_SELF]), len(raw))
            self.assertEqual(roles[ROLE_OTHER], bytes(len(raw)))


if __name__ == '__main__':
    unittest.main()
