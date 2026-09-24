"""Timestamped capture, per-microphone AEC and duplicate-aware recording.

All DSP runs on the mixer thread. Capture callbacks only copy native PCM and
map PortAudio timestamps onto the monotonic clock; no gain or clipping precedes
AEC. The output contract is exactly 10 ms of 48 kHz stereo per call.
"""
from collections import deque
from dataclasses import dataclass
import math
import time

import numpy as np
from scipy.signal import butter, correlate, sosfilt

RATE = 48000
FRAMES = RATE // 100


@dataclass
class CapturePacket:
    start: float
    rate: int
    samples: np.ndarray


class CaptureClock:
    """Map a stream's ADC clock to monotonic time without callback jitter."""

    def __init__(self, rate, channels):
        self.rate = int(rate)
        self.channels = int(channels)
        self.offset = None
        self.next_start = None

    def packet(self, raw, timing, now=None):
        now = time.monotonic() if now is None else now
        data = np.frombuffer(raw, dtype=np.int16)
        data = data[:data.size - data.size % self.channels]
        data = data.reshape(-1, self.channels).astype(np.float32) / 32768.0
        if self.channels == 1:
            data = np.repeat(data, 2, axis=1)
        elif self.channels > 2:
            # Capture is requested as stereo first; retain every channel if a
            # device only supports a multichannel fallback.
            data = np.repeat(data.mean(axis=1, keepdims=True), 2, axis=1)
        duration = len(data) / self.rate
        def value(snake, camel):
            if isinstance(timing, dict):
                return float(timing.get(snake, 0))
            return float(getattr(timing, camel, 0))
        current = value('current_time', 'currentTime')
        adc = value('input_buffer_adc_time', 'inputBufferAdcTime')
        if (math.isfinite(current) and math.isfinite(adc)
                and current > 0 and adc > 0 and 0 <= current - adc < 2):
            if self.offset is None:
                self.offset = now - current
            start = adc + self.offset
        else:
            estimate = now - duration
            # Some loopback drivers provide zero ADC timestamps. Preserve the
            # sample clock between callbacks, but leave real idle gaps intact.
            start = (self.next_start if self.next_start is not None
                     and abs(estimate - self.next_start) < 0.05 else estimate)
        self.next_start = start + duration
        return CapturePacket(start, self.rate, data)


class TimelineSource:
    """Bounded native-rate history sampled on a shared output clock.

    Absolute sample coordinates retain fractional resampling phase across
    callbacks and follow ADC clock drift. Missing intervals remain silence.
    """

    def __init__(self):
        self.packets = deque()
        self.end = 0.0
        self.dropped = 0
        self.filter_rate = None
        self.sos = self.zi = None

    def add(self, packet):
        if not len(packet.samples):
            return
        if packet.rate != self.filter_rate:
            self.filter_rate = packet.rate
            self.sos = (butter(8, RATE * 0.45, fs=packet.rate, output='sos')
                        if packet.rate > RATE else None)
            self.zi = (np.zeros((len(self.sos), 2, 2))
                       if self.sos is not None else None)
        if self.sos is not None:
            if packet.start - self.end > 0.05:
                self.zi.fill(0)
            data, self.zi = sosfilt(self.sos, packet.samples, axis=0, zi=self.zi)
            packet = CapturePacket(packet.start, packet.rate, data)
        self.packets.append(packet)
        self.end = packet.start + len(packet.samples) / packet.rate
        while self.packets and self.packets[0].start < self.end - 2.0:
            self.packets.popleft()
            self.dropped += 1

    def read(self, start, frames=FRAMES):
        positions = start + np.arange(frames) / RATE
        result = np.zeros((frames, 2), dtype=np.float32)
        while self.packets:
            p = self.packets[0]
            if p.start + len(p.samples) / p.rate < start - 1 / p.rate:
                self.packets.popleft()
            else:
                break
        packets = list(self.packets)
        for i, p in enumerate(packets):
            if p.start > positions[-1]:
                break
            end = p.start + len(p.samples) / p.rate
            mask = (positions >= p.start) & (positions < end)
            if not mask.any():
                continue
            data = p.samples
            # Include the next sample across a contiguous packet boundary.
            if i + 1 < len(packets):
                nxt = packets[i + 1]
                if nxt.rate == p.rate and abs(nxt.start - end) < 1.5 / p.rate:
                    data = np.vstack((data, nxt.samples[:1]))
            x = (positions[mask] - p.start) * p.rate
            for channel in range(2):
                result[mask, channel] = np.interp(x, np.arange(len(data)), data[:, channel])
        return result


class DuplicateMixer:
    """Keep one representative of highly correlated, possibly delayed inputs.

    Independent sources are summed, not gated by a single winning microphone.
    Decisions use 200 ms history at 8 kHz, allow 80 ms path delay, and are
    refreshed every 50 ms. Hysteresis and gain ramps avoid rapid switching.
    """

    def __init__(self, count, threshold=0.86):
        self.count = count
        self.threshold = threshold
        self.history = np.zeros((count, 1600), dtype=np.float32)
        self.weights = np.ones(count, dtype=np.float32)
        self.targets = np.ones(count, dtype=np.float32)
        self.tick = 0
        self.duplicates = 0

    @staticmethod
    def similarity(a, b):
        # Normalize by overlapping energy at each lag, not total energy.
        lag = 640
        n = len(a)
        corr = correlate(a, b, mode='full', method='fft')[n - 1 - lag:n + lag]
        lags = np.arange(-lag, lag + 1)
        aa = np.r_[0, np.cumsum(a.astype(np.float64) ** 2)]
        bb = np.r_[0, np.cumsum(b.astype(np.float64) ** 2)]
        ea = aa[np.minimum(n, n + lags)] - aa[np.maximum(0, lags)]
        eb = bb[np.minimum(n, n - lags)] - bb[np.maximum(0, -lags)]
        valid = (ea > 1e-5) & (eb > 1e-5)
        values = np.zeros_like(ea)
        values[valid] = np.abs(corr[valid]) / np.sqrt(ea[valid] * eb[valid])
        return float(np.max(values))

    def process(self, blocks):
        if not blocks:
            return np.zeros((FRAMES, 2), dtype=np.float32)
        if len(blocks) == 1:
            return blocks[0]
        # Decimation is for correlation only, not the recorded signal.
        for i, block in enumerate(blocks):
            mono = block.mean(axis=1).reshape(-1, 6).mean(axis=1)
            self.history[i] = np.roll(self.history[i], -len(mono))
            self.history[i, -len(mono):] = mono
        self.tick += 1
        if self.tick >= 10 and self.tick % 5 == 0:
            energy = np.mean(self.history ** 2, axis=1)
            parent = list(range(self.count))
            def root(i):
                while parent[i] != i:
                    i = parent[i]
                return i
            for i in range(self.count):
                for j in range(i):
                    if min(energy[i], energy[j]) > 1e-7:
                        if self.similarity(self.history[i], self.history[j]) >= self.threshold:
                            parent[root(i)] = root(j)
            targets = np.zeros(self.count)
            for group in {root(i) for i in range(self.count)}:
                members = [i for i in range(self.count) if root(i) == group]
                best = max(members, key=lambda i: energy[i])
                previous = [i for i in members if self.targets[i] > 0]
                if previous:
                    old = max(previous, key=lambda i: energy[i])
                    if energy[old] * 4 >= energy[best]:
                        best = old
                targets[best] = 1
            self.targets = targets
            self.duplicates = self.count - int(sum(targets))
        mixed = np.zeros_like(blocks[0])
        for i, block in enumerate(blocks):
            end = self.weights[i] + 0.5 * (self.targets[i] - self.weights[i])
            weights = np.linspace(self.weights[i], end, len(block), dtype=np.float32)
            mixed += block * weights[:, None]
            self.weights[i] = end
        return mixed


class RecordingProcessor:
    """AEC before recording/ASR, independently for each selected microphone."""

    def __init__(self, kinds, gain=1.0, delay_ms=0, processor_factory=None):
        self.kinds = list(kinds)
        self.gain = float(gain)
        self.mics = [i for i, kind in enumerate(kinds) if kind == 'mic']
        self.speakers = [i for i, kind in enumerate(kinds) if kind == 'speaker']
        self.processors = {}
        self.render_tail = np.zeros(len(self.speakers), dtype=np.int32)
        self.render_running = np.zeros(len(self.speakers), dtype=bool)
        if self.mics and self.speakers:
            if processor_factory is None:
                from pywebrtc_audio import AudioProcessor
                processor_factory = AudioProcessor
            for i in self.mics:
                # Different endpoints have different acoustic paths. A single
                # summed reference cannot model those independently. Each stage
                # removes one endpoint; only the last applies noise suppression.
                self.processors[i] = [processor_factory(
                    sample_rate=RATE, num_channels=2, echo_cancellation=True,
                    noise_suppression=j == len(self.speakers) - 1,
                    auto_gain_control=False,
                    stream_delay_ms=max(0, int(delay_ms)))
                    for j in range(len(self.speakers))]
        self.mic_mixer = DuplicateMixer(len(self.mics), threshold=0.82)
        self.speaker_mixer = DuplicateMixer(len(self.speakers), threshold=0.92)
        self.limiter = 1.0

    def process(self, blocks):
        speaker_blocks = [blocks[i] for i in self.speakers]
        # AEC must see every physical render path, including mirrored speakers.
        # Deduplication changes only the saved mix, never the echo reference.
        active = []
        for j, reference in enumerate(speaker_blocks):
            if np.max(np.abs(reference)) > 1e-5:
                self.render_tail[j] = 100  # retain one second of acoustic tail
            else:
                self.render_tail[j] = max(0, self.render_tail[j] - 1)
            # Retain the final stage for its single noise-suppression pass.
            running = self.render_tail[j] > 0 or j == len(speaker_blocks) - 1
            if running and not self.render_running[j]:
                for processors in self.processors.values():
                    processors[j].reset()
            self.render_running[j] = running
            active.append(running)
        microphones = []
        for i in self.mics:
            near = blocks[i]
            if i in self.processors:
                for j, (processor, reference) in enumerate(zip(self.processors[i], speaker_blocks)):
                    if not active[j]:
                        continue
                    near = processor.process(
                        np.ascontiguousarray(near.reshape(-1)),
                        np.ascontiguousarray(reference.reshape(-1))).reshape(FRAMES, 2)
                if not np.isfinite(near).all():
                    raise RuntimeError('AEC returned non-finite audio')
            microphones.append(near)
        own = self.mic_mixer.process(microphones)
        other = self.speaker_mixer.process(speaker_blocks)
        mix = own + other
        peak = max(float(np.max(np.abs(x))) for x in (mix, own, other)) * self.gain
        required = min(1.0, 0.95 / max(peak, 1e-9))
        self.limiter = min(required, self.limiter + 0.005)
        gain = self.gain * self.limiter
        def pcm(x):
            return np.clip(x * gain * 32768, -32768, 32767).astype(np.int16).tobytes()
        return pcm(mix), pcm(own), pcm(other)
