"""Protect explicit recovery from live requests in the enforced single-worker app.

Database locks/constraints still serialize business writes. This registry only
distinguishes a live execution from an abandoned running row; time is not proof
that a request has stopped. The API startup lease prohibits another worker.
"""
from contextlib import contextmanager
from threading import Lock

from ticketmind.core.errors import AppError

_lock = Lock()
_active: dict[str, int] = {}
_recovering: set[str] = set()


@contextmanager
def ticket_activity(ticket_id, *, recovery=False):
    key = str(ticket_id)
    with _lock:
        if key in _recovering or (recovery and _active.get(key, 0)):
            raise AppError(409, "execution_in_progress", "该工单仍有执行中的请求，请待请求结束后恢复")
        if recovery:
            _recovering.add(key)
        else:
            _active[key] = _active.get(key, 0) + 1
    try:
        yield
    finally:
        with _lock:
            if recovery:
                _recovering.remove(key)
            elif _active[key] == 1:
                del _active[key]
            else:
                _active[key] -= 1
