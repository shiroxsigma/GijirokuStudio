"""Measure audio waiting for a live recognizer in seconds of speech timeline."""
from queue import Queue

import numpy as np


class AudioBacklogQueue(Queue):
    def __init__(self, maxsize, pcm_rate, mono_rate):
        super().__init__(maxsize)
        self._pcm_rate = pcm_rate
        self._mono_rate = mono_rate
        self._pending_seconds = 0.0

    @property
    def pending_seconds(self):
        with self.mutex:
            return self._pending_seconds

    # Queue calls both hooks under its mutex, including put/get_nowait.
    def _put(self, audio):
        if isinstance(audio, np.ndarray):
            seconds = len(audio) / self._mono_rate
        else:
            seconds = (len(audio) // 4) / self._pcm_rate  # stereo int16
        super()._put((audio, seconds))
        self._pending_seconds += seconds

    def _get(self):
        audio, seconds = super()._get()
        self._pending_seconds = self._pending_seconds - seconds if self.queue else 0.0
        return audio
