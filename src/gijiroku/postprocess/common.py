"""Progress and cancellation used throughout report generation."""

class PostProcessCancelled(Exception):
    """Raised at a checkpoint when the caller asked to stop post-processing."""


class _Reporter:
    """Progress reporting plus cancellation, threaded through post-processing.

    Calling it announces a phase; `check()` is the cheap cancel-only probe for
    tight loops where a log line per iteration would be noise.
    """

    def __init__(self, progress=None, cancel=None):
        self._progress = progress
        self._cancel = cancel

    def __call__(self, frac, msg):
        print(msg)
        if self._progress is not None:
            try:
                self._progress(frac, msg)
            except Exception as e:
                print(f"[進捗通知警告] {e}")
        self.check()

    def check(self):
        if self._cancel is not None and self._cancel():
            raise PostProcessCancelled()
