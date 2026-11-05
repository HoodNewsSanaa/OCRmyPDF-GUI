"""OCRmyPDF plugin that reports progress as machine-readable lines on stderr.

The GUI launches ``python -m ocrmypdf --plugin gui_progress_plugin.py ...`` and
parses lines that start with ``@@OCRGUI`` to drive its progress bar and ETA.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time

from ocrmypdf import hookimpl

MARKER = "@@OCRGUI "
_lock = threading.Lock()


def _emit(**event) -> None:
    event["t"] = time.time()
    line = MARKER + json.dumps(event) + "\n"
    with _lock:
        try:
            # Write straight to fd 2 so nothing (rich, logging) can swallow it.
            os.write(sys.stderr.fileno(), line.encode("utf-8"))
        except OSError:
            pass


class GuiProgressBar:
    """Implements OCRmyPDF's ProgressBar protocol.

    The ``disable`` flag is deliberately ignored: OCRmyPDF disables progress
    bars when stderr is not a terminal, but the GUI always wants the events.
    """

    def __init__(self, *, total=None, desc=None, unit=None, disable=False, **kwargs):
        self.total = total
        self.desc = desc or ""
        self.unit = unit or ""
        self.current = 0.0

    def __enter__(self):
        _emit(event="start", desc=self.desc, total=self.total, unit=self.unit)
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        _emit(event="end", desc=self.desc, ok=exc_type is None)
        return False

    def update(self, n=1, *, completed=None):
        if completed is not None:
            self.current = completed
        else:
            self.current += n
        _emit(event="update", desc=self.desc, current=self.current, total=self.total)


@hookimpl
def get_progressbar_class():
    return GuiProgressBar
