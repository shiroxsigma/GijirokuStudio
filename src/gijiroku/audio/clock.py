"""Lightweight timestamp conversion shared by recorder and capture workers."""
from dataclasses import dataclass
import math
import time

import numpy as np


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
            data = np.repeat(data.mean(axis=1, keepdims=True), 2, axis=1)
        duration = len(data) / self.rate

        def value(snake, camel):
            if isinstance(timing, dict):
                return float(timing.get(snake, 0))
            return float(getattr(timing, camel, 0))
        current = value('current_time', 'currentTime')
        adc = value('input_buffer_adc_time', 'inputBufferAdcTime')
        valid_adc = (math.isfinite(current) and math.isfinite(adc)
                     and current > 0 and adc > 0 and 0 <= current - adc < 2)
        if valid_adc:
            if self.offset is None:
                self.offset = now - current
            estimate = adc + self.offset
        else:
            estimate = now - duration
        if self.next_start is not None and abs(estimate - self.next_start) < 0.05:
            start = self.next_start
        else:
            start = estimate
        self.next_start = start + duration
        return CapturePacket(start, self.rate, data)
