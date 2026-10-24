"""Per-environment signal history, held only while the host runs; see OctantSpecs octantcompass/law.md 2.

Kept in memory (2026-10-22): OctantPath holds the record, and sends it whole whenever it meets a host it has not sent
to yet (`/v1/health` tells the host's `instanceId` and how many events it holds), then only what it adds. Nothing
is written to disk, so there is no cache to clear and nothing for another record under the same token to collide
with: the record that connects last is the one in use."""

import secrets
from threading import Lock


class SignalCache:
    def __init__(self):
        # Tells this run of the host from the last one; OctantPath sends everything again when it changes.
        self.instance = secrets.token_hex(8)
        self.lock = Lock()
        self._events = {}  # environment id -> [(event as sent, rejection reason or None)]

    def count(self, environment):
        return len(self._events.get(environment, ()))

    def history(self, environment):
        return [lowercase_hash(event) for event, reason in self._events.get(environment, ()) if not reason]

    def receive(self, environment, events, *, replace=False):
        """Receive API-validated (original event, rejection reason) entries, after or instead of what is held.

        A rejected event is kept as sent, so the count matches what OctantPath sent, but stays out of the history."""
        rejected = [{"index": index, "reason": reason} for index, (_, reason) in enumerate(events) if reason]
        with self.lock:
            held = [] if replace else self._events.get(environment, [])
            self._events[environment] = held + list(events)
            return {"count": len(self._events[environment]), "rejected": rejected}

    def delete(self, environment):
        with self.lock:
            self._events.pop(environment, None)


def lowercase_hash(event):
    """The cache keeps the event as sent; hashes are compared in lowercase (OctantSpecs octantpath/law.md 1.2)."""
    if isinstance(event, dict) and isinstance(event.get("hash"), str):
        return {**event, "hash": event["hash"].lower()}
    return event
