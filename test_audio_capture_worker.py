import io
import pickle
import queue
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

import audio_capture_worker as worker
from audio_pipeline import CapturePacket


class WorkerTests(unittest.TestCase):
    def test_recorder_restarts_after_worker_crash_and_keeps_packets(self):
        from main import MeetingRecorderGUI, _EOF
        gui = MeetingRecorderGUI.__new__(MeetingRecorderGUI)
        gui._queue_overflow_count = 0
        gui.is_recording = True
        active = threading.Event()
        active.set()
        logs = []
        def log(message):
            logs.append(message)
            if '代替機器へ切替' in message:
                gui.is_recording = False
                active.clear()
        gui._log = log
        gui.root = SimpleNamespace(after=lambda _delay, fn, *args: fn(*args))
        class Process:
            def __init__(self, messages):
                self.stdout = io.BytesIO(b''.join(pickle.dumps(m) for m in messages))
                self.stdin = SimpleNamespace(close=lambda: None)
            def wait(self, timeout=None):
                return -1
        first_packet = CapturePacket(12.0, 48000,
                                     np.zeros((10, 2), dtype=np.float32))
        processes = [Process([first_packet]),
                     Process([{'state': 'fallback', 'name': 'Built-in mic'}])]
        packets = queue.Queue()
        spec = dict(kind='mic', preferred_name='FreeClip',
                    preferred_api='WASAPI', fallback_name='Built-in mic')
        with patch('main.subprocess.Popen', side_effect=processes) as popen, \
             patch('main.time.sleep', return_value=None):
            gui._capture_audio_worker(active, spec, packets)
        self.assertEqual(popen.call_count, 2)
        self.assertIsInstance(packets.get_nowait(), CapturePacket)
        self.assertIs(packets.get_nowait(), _EOF)
        self.assertTrue(any('代替機器へ切替' in log for log in logs))

    def test_recorder_survives_missing_device_worker_and_stops(self):
        from main import MeetingRecorderGUI, _EOF
        gui = MeetingRecorderGUI.__new__(MeetingRecorderGUI)
        gui.root = SimpleNamespace(after=lambda _delay, fn, *args: fn(*args))
        logs = []
        gui._log = logs.append
        gui._queue_overflow_count = 0
        gui.is_recording = True
        active = threading.Event()
        active.set()
        packets = queue.Queue()
        spec = dict(kind='mic', preferred_name='Definitely Missing Input',
                    preferred_api='WASAPI', fallback_name='',
                    allow_default_fallback=False)
        thread = threading.Thread(target=gui._capture_audio_worker,
                                  args=(active, spec, packets))
        thread.start()
        deadline = time.monotonic() + 6
        while not logs and time.monotonic() < deadline:
            time.sleep(0.05)
        gui.is_recording = False
        active.clear()
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertTrue(any('待機' in log for log in logs), logs)
        self.assertIs(packets.get_nowait(), _EOF)

    def test_reconnected_bluetooth_number_matches_preferred(self):
        devices = [dict(name='Headset (4- FreeClip)', index=9, api='WASAPI')]
        selected, role = worker.choose_device(devices, 'Headset (3- FreeClip)',
                                              api='WASAPI')
        self.assertEqual((selected['index'], role), (9, 'preferred'))

    def test_stale_bluetooth_default_is_not_its_own_fallback(self):
        devices = [dict(name='FreeClip', index=1, api='WASAPI', is_default=True),
                   dict(name='Built-in mic', index=2, api='WASAPI')]
        chosen, role = worker.choose_device(devices, 'FreeClip',
            allow_default=True, skip_preferred=True)
        self.assertEqual((chosen['index'], role), (2, 'fallback'))

    def test_failed_preferred_mic_falls_back_without_stopping_timeline(self):
        devices = [
            dict(name='FreeClip', index=1, channels=1, rate=16000, api='WASAPI'),
            dict(name='Built-in mic', index=2, channels=1, rate=48000,
                 api='WASAPI', is_default=True),
        ]
        class Stop:
            checks = 0
            def is_set(self):
                self.checks += 1
                return self.checks >= 5
            def wait(self, _seconds):
                return self.is_set()
        stop = Stop()
        output = io.BytesIO()
        class Stream:
            def __init__(self, callback):
                self.callback = callback
            def start(self):
                self.callback(np.zeros((1024, 1), dtype=np.int16), 1024, {}, None)
            def stop(self):
                pass
            def close(self):
                pass
        def input_stream(**kwargs):
            if kwargs['device'] == 1:
                raise OSError('Bluetooth disconnected')
            return Stream(kwargs['callback'])
        config = dict(kind='mic', preferred_name='FreeClip',
                      fallback_name='Built-in mic', preferred_api='WASAPI',
                      allow_default_fallback=True)
        with patch.object(worker, 'enumerate_mics', return_value=devices), \
             patch.object(worker.sd, 'InputStream', side_effect=input_stream):
            worker.run(config, output=output, stop=stop)
        output.seek(0)
        messages = []
        try:
            while True:
                messages.append(pickle.load(output))
        except EOFError:
            pass
        self.assertEqual([m['state'] for m in messages if isinstance(m, dict)],
                         ['open_error', 'fallback'])
        self.assertTrue(any(isinstance(m, CapturePacket) for m in messages))


if __name__ == '__main__':
    unittest.main()
