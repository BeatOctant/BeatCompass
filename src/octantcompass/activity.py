"""Recent OctantCompass events for the OctantCompass window. Never log tokens, secrets or pairing codes."""

import logging
from collections import deque
from datetime import datetime, timezone

log = logging.getLogger("octantcompass")
log.setLevel(logging.INFO)


class RecentLog(logging.Handler):
    def __init__(self, lines):
        super().__init__(logging.INFO)
        self.lines = deque(maxlen=lines)

    def emit(self, record):
        at = datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        self.lines.append({"at": at, "level": record.levelname.lower(), "message": record.getMessage()})

    def read(self):
        with self.lock:
            return list(self.lines)

    def resize(self, lines):
        with self.lock:
            if lines != self.lines.maxlen:
                self.lines = deque(self.lines, maxlen=lines)
