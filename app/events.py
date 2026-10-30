"""Tiny in-process publish/subscribe broker used for Server-Sent Events.

The development server is single-process and multi-threaded, so plain
thread-safe queues are enough:

* ``publish(channel, event)``  – deliver an event to every live listener.
* ``subscribe(channel)``       – get a Queue for a channel.
* ``unsubscribe(channel, q)``  – remove it again when a stream closes.
"""

from queue import Empty, Queue
from threading import Lock

_channels: dict[str, list[Queue]] = {}
_lock = Lock()


def subscribe(channel: str) -> Queue:
    """Register a new listener queue for *channel*."""
    q: Queue = Queue()
    with _lock:
        _channels.setdefault(channel, []).append(q)
    return q


def unsubscribe(channel: str, q: Queue) -> None:
    """Remove a listener queue (safe to call twice)."""
    with _lock:
        listeners = _channels.get(channel)
        if not listeners:
            return
        try:
            listeners.remove(q)
        except ValueError:
            pass
        if not listeners:
            _channels.pop(channel, None)


def publish(channel: str, event: dict) -> int:
    """Send *event* to every listener of *channel*. Returns listener count."""
    with _lock:
        listeners = list(_channels.get(channel, ()))
    for q in listeners:
        q.put(event)
    return len(listeners)


def channel_for_exam(exam_id: int) -> str:
    return f"exam:{exam_id}"


def channel_for_attempt(attempt_id: int) -> str:
    return f"attempt:{attempt_id}"
