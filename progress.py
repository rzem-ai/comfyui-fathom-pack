"""A cloud node's progress through ComfyUI's own progress bar, which ComfyUI sends as `progress` events.

The scale is 0 to 100 in stages, so Fathom names the stage from the value alone: below 10 submitting,
10 to 19 waiting in the provider's queue (19 at the front), 20 to 89 running, 90 to 100 downloading the
outputs. A provider gives no running fraction, so running creeps toward 85 with each poll.
"""

try:  # ComfyUI's own; absent when the pack is imported outside ComfyUI, as the tests do.
    from comfy.utils import ProgressBar
except Exception:  # noqa: BLE001
    ProgressBar = None

TOTAL = 100
SUBMITTING = 5
QUEUED = 10
RUNNING = 20
RUNNING_CEILING = 85
DOWNLOADING = 90


class Stages:
    """One node run's progress bar. Progress is never worth failing a Run for, so the bar's own errors are dropped."""

    def __init__(self):
        self.value = 0
        self.bar = None
        if ProgressBar is not None:
            try:
                self.bar = ProgressBar(TOTAL)
            except Exception:  # noqa: BLE001
                self.bar = None

    def at(self, value):
        self.value = max(0, min(TOTAL, int(value)))
        if self.bar is not None:
            try:
                self.bar.update_absolute(self.value, TOTAL)
            except Exception:  # noqa: BLE001
                pass

    def submitting(self):
        self.at(SUBMITTING)

    def queued(self, position=None):
        """Waiting in the provider's queue; a queue position moves the bar toward the front."""
        ahead = position if isinstance(position, int) and position >= 0 else RUNNING - 1 - QUEUED
        self.at(RUNNING - 1 - min(ahead, RUNNING - 1 - QUEUED))

    def running(self):
        """Running at the provider: the first poll enters the stage, each later one creeps toward the ceiling."""
        if self.value < RUNNING:
            self.at(RUNNING)
        else:
            self.at(min(RUNNING_CEILING, self.value + max(1, (RUNNING_CEILING - self.value) // 10)))

    def downloading(self, done, count):
        self.at(DOWNLOADING + (TOTAL - DOWNLOADING) * done // max(1, count))
