"""Timestamp-based microphone gating, including buffered capture packets."""
import math
import threading
import time

import numpy as np


class MicrophoneGate:
    def __init__(self, enabled=True):
        self._lock = threading.Lock()
        self._changes = [(-math.inf, bool(enabled))]

    def set_enabled(self, enabled, at=None):
        at = time.monotonic() if at is None else at
        with self._lock:
            if bool(enabled) != self._changes[-1][1]:
                self._changes.append((at, bool(enabled)))

    def mask(self, start, rate, count):
        with self._lock:
            changes = tuple(self._changes)
        enabled = changes[0][1]
        result = np.empty(count, dtype=np.bool_)
        offset = 0
        for at, state in changes[1:]:
            if at <= start:
                enabled = state
                continue
            boundary = min(count, max(0, math.ceil((at - start) * rate - 1e-7)))
            result[offset:boundary] = enabled
            offset, enabled = boundary, state
            if offset == count:
                break
        result[offset:] = enabled
        return result

    def filter(self, samples, start, rate):
        mask = self.mask(start, rate, len(samples))
        return samples if mask.all() else samples * mask[:, None]
