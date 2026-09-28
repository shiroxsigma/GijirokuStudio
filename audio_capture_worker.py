"""Isolated, reconnecting Windows audio capture worker.

One process owns one input slot. A native PortAudio crash therefore ends only
this slot; the recorder can restart it without losing its MP3 timeline.
Messages on stdout are pickled CapturePacket objects or status dictionaries.
"""
import base64
import json
import os
import pickle
import queue
import re
import subprocess
import sys
import threading
import time

import pyaudiowpatch as pyaudio
import sounddevice as sd

from capture_clock import CaptureClock


def identity(name):
    """Bluetooth's connection number may change after pairing again."""
    return re.sub(r"\(\s*\d+\s*-\s*", "(", name).casefold().strip()


def choose_device(devices, preferred, fallback="", api="", allow_default=False,
                  skip_preferred=False):
    def matching(name):
        matches = [d for d in devices if identity(d['name']) == identity(name)]
        return next((d for d in matches if d.get('api') == api),
                    matches[0] if matches else None)

    chosen = None if skip_preferred else matching(preferred)
    if chosen is not None:
        return chosen, 'preferred'
    if fallback:
        chosen = matching(fallback)
        if chosen is not None:
            return chosen, 'fallback'
    alternatives = [d for d in devices if identity(d['name']) != identity(preferred)]
    if allow_default and alternatives:
        return next((d for d in alternatives if d.get('is_default')),
                    alternatives[0]), 'fallback'
    return None, 'unavailable'


def enumerate_speakers():
    pa = pyaudio.PyAudio()
    try:
        try:
            default = identity(pa.get_default_output_device_info()['name'])
        except Exception:
            default = ''
        return [dict(name=d['name'], index=d['index'],
                     channels=int(d['maxInputChannels']),
                     rate=int(d['defaultSampleRate']), api='WASAPI',
                     is_default=identity(d['name'].removesuffix(' [Loopback]')) == default)
                for d in pa.get_loopback_device_info_generator()
                if d.get('maxInputChannels', 0) > 0]
    finally:
        pa.terminate()


def enumerate_mics():
    hostapis = list(sd.query_hostapis())
    devices = []
    try:
        default_index = int(sd.default.device[0])
    except (TypeError, ValueError, IndexError):
        default_index = -1
    for d in sd.query_devices():
        api_index = d.get('hostapi', -1)
        api = hostapis[api_index]['name'] if 0 <= api_index < len(hostapis) else ''
        if (d.get('max_input_channels', 0) <= 0 or 'Loopback' in d['name']
                or api == 'Windows WDM-KS'):
            continue
        devices.append(dict(name=d['name'], index=d['index'],
                            channels=int(d['max_input_channels']),
                            rate=int(d['default_samplerate']),
                            is_default=d['index'] == default_index,
                            api={'Windows WASAPI': 'WASAPI',
                                 'Windows DirectSound': 'DSound',
                                 'MME': 'MME'}.get(api, api)))
    return devices


def formats(channels, rate):
    seen = set()
    for ch in (min(channels, 2), 1, channels):
        for sr in (rate, 48000, 44100):
            candidate = (int(ch), int(sr))
            if candidate not in seen:
                seen.add(candidate)
                yield candidate


def run(config, output=None, stop=None):
    output = output or sys.stdout.buffer
    stop = stop or threading.Event()
    kind = config['kind']
    enumerate_devices = enumerate_speakers if kind == 'speaker' else enumerate_mics
    last_state = None
    preferred_retry_after = 0.0

    def candidate(devices):
        return choose_device(devices, config['preferred_name'],
            config.get('fallback_name', ''), config.get('preferred_api', ''),
            config.get('allow_default_fallback', False),
            skip_preferred=time.monotonic() < preferred_retry_after)

    def send(message):
        pickle.dump(message, output, protocol=pickle.HIGHEST_PROTOCOL)
        output.flush()

    while not stop.is_set():
        try:
            device, role = candidate(enumerate_devices())
        except Exception as exc:
            send({'state': 'scan_error', 'detail': str(exc)})
            stop.wait(1)
            continue
        if device is None:
            if last_state != 'unavailable':
                send({'state': 'unavailable'})
                last_state = 'unavailable'
            else:
                send({'state': 'heartbeat'})
            stop.wait(1)
            continue

        packets = queue.Queue(maxsize=80)
        last_packet = [time.monotonic()]
        stream = None
        pa = None
        error = None
        try:
            if kind == 'speaker':
                pa = pyaudio.PyAudio()
            for channels, rate in formats(device['channels'], device['rate']):
                clock = CaptureClock(rate, channels)

                def put(packet):
                    last_packet[0] = time.monotonic()
                    try:
                        packets.put_nowait(packet)
                    except queue.Full:
                        try:
                            packets.get_nowait()
                            packets.put_nowait(packet)
                        except queue.Empty:
                            pass

                try:
                    if kind == 'speaker':
                        def callback(data, _count, timing, _status):
                            try:
                                put(clock.packet(data, timing))
                            except Exception:
                                pass
                            return None, pyaudio.paContinue
                        stream = pa.open(format=pyaudio.paInt16, channels=channels,
                            rate=rate, input=True, input_device_index=device['index'],
                            frames_per_buffer=1024, start=False,
                            stream_callback=callback)
                        stream.start_stream()
                    else:
                        def callback(data, _count, timing, _status):
                            try:
                                put(clock.packet(data.tobytes(), timing))
                            except Exception:
                                pass
                        stream = sd.InputStream(device=device['index'],
                            channels=channels, samplerate=rate, dtype='int16',
                            blocksize=1024, callback=callback)
                        stream.start()
                    break
                except Exception as exc:
                    error = str(exc)
                    if stream is not None:
                        try:
                            stream.close()
                        except Exception:
                            pass
                        stream = None
            if stream is None:
                send({'state': 'open_error', 'name': device['name'], 'detail': error})
                if role == 'preferred':
                    preferred_retry_after = time.monotonic() + 3
                stop.wait(1)
                continue

            last_packet[0] = time.monotonic()

            state = (role, device['name'])
            if state != last_state:
                send({'state': role, 'name': device['name'],
                      'rate': rate, 'channels': channels})
                last_state = state
            next_scan = time.monotonic() + 2
            next_probe = time.monotonic() + 4
            next_heartbeat = time.monotonic() + 0.5
            probe_thread = None
            probe_result = queue.Queue(maxsize=1)

            def probe_preferred():
                encoded = base64.b64encode(json.dumps(
                    config, ensure_ascii=False).encode('utf-8')).decode('ascii')
                try:
                    probe = subprocess.run(
                        [sys.executable, os.path.abspath(__file__), encoded, '--probe'],
                        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL, timeout=5,
                        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                    probe_result.put_nowait(probe.returncode)
                except (OSError, subprocess.TimeoutExpired):
                    probe_result.put_nowait(1)

            while not stop.is_set():
                try:
                    send(packets.get(timeout=0.1))
                except queue.Empty:
                    pass
                now = time.monotonic()
                if now >= next_heartbeat:
                    send({'state': 'heartbeat'})
                    next_heartbeat = now + 0.5
                # WASAPI loopback may pause callbacks while no application is
                # playing sound. That silence must not trigger a restart.
                try:
                    stream_active = (stream.is_active() if kind == 'speaker'
                                     else stream.active)
                except Exception:
                    stream_active = False
                if now - last_packet[0] > 2 and (kind == 'mic' or not stream_active):
                    send({'state': 'stalled', 'name': device['name']})
                    if role == 'preferred':
                        preferred_retry_after = time.monotonic() + 3
                    break
                if now >= next_scan:
                    next_scan = now + 2
                    try:
                        current, current_role = candidate(enumerate_devices())
                        if (current is None or current_role != role
                                or current['index'] != device['index']):
                            break
                    except Exception:
                        break
                if role == 'fallback':
                    try:
                        found = probe_result.get_nowait() == 0
                    except queue.Empty:
                        found = False
                    if found:
                        send({'state': 'reconnecting'})
                        return  # Fresh process gets a fresh PortAudio device list.
                    if now >= next_probe and (probe_thread is None or not probe_thread.is_alive()):
                        next_probe = now + 4
                        probe_thread = threading.Thread(target=probe_preferred, daemon=True)
                        probe_thread.start()
        finally:
            if stream is not None:
                try:
                    if kind == 'speaker':
                        stream.stop_stream()
                    else:
                        stream.stop()
                except Exception:
                    pass
                try:
                    stream.close()
                except Exception:
                    pass
            if pa is not None:
                try:
                    pa.terminate()
                except Exception:
                    pass


def main():
    config = json.loads(base64.b64decode(sys.argv[1]).decode('utf-8'))
    if '--probe' in sys.argv[2:]:
        enumerate_devices = (enumerate_speakers if config['kind'] == 'speaker'
                             else enumerate_mics)
        try:
            device, _ = choose_device(enumerate_devices(),
                config['preferred_name'], api=config.get('preferred_api', ''))
        except Exception:
            device = None
        sys.exit(0 if device is not None else 1)
    stop = threading.Event()
    threading.Thread(target=lambda: (sys.stdin.buffer.read(1), stop.set()),
                     daemon=True).start()
    try:
        run(config, stop=stop)
    except (BrokenPipeError, OSError):
        pass


if __name__ == '__main__':
    main()
