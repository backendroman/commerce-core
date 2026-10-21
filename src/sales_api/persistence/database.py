from __future__ import annotations

import threading
from collections import deque
from collections.abc import Callable
from typing import TYPE_CHECKING

from .store import Store

if TYPE_CHECKING:
    from ..events.domain_event import DomainEvent


class Database:
    """In-memory DB behind a single RLock. Events are dispatched after the outermost write."""

    def __init__(self, store: Store | None = None):
        self.store = store or Store()
        self._lock = threading.RLock()
        self._pending: list[DomainEvent] = []
        self._depth = 0
        self._writer: int | None = None
        self.committed: list[Callable[[list[DomainEvent]], None]] = []

    def write[T](self, action: Callable[[Store], T]) -> T:
        committed: list[DomainEvent] | None = None
        with self._lock:
            self._depth += 1
            self._writer = threading.get_ident()
            try:
                result = action(self.store)
                if self._depth == 1 and self._pending:
                    committed = list(self._pending)
                    self._pending.clear()
            except BaseException:
                if self._depth == 1:
                    self._pending.clear()
                raise
            finally:
                self._depth -= 1
                if self._depth == 0:
                    self._writer = None

        if committed:
            for handler in list(self.committed):
                handler(committed)
        return result

    def read[T](self, query: Callable[[Store], T]) -> T:
        with self._lock:
            return query(self.store)

    def raise_event(self, domain_event: DomainEvent) -> None:
        if self._writer != threading.get_ident():
            raise RuntimeError("Events can only be raised inside a write.")
        self._pending.append(domain_event)


class JobQueue:
    def __init__(self) -> None:
        self._queue: deque[tuple[str, Callable[[], object]]] = deque()
        self._queued_names: set[str] = set()
        self._gate = threading.Lock()

    @property
    def count(self) -> int:
        with self._gate:
            return len(self._queue)

    def enqueue(self, name: str, work: Callable[[], object]) -> None:
        with self._gate:
            # dedupe by name
            if name in self._queued_names:
                return
            self._queued_names.add(name)
            self._queue.append((name, work))

    def run_pending(self) -> int:
        executed = 0
        while True:
            with self._gate:
                if not self._queue:
                    return executed
                name, work = self._queue.popleft()
                self._queued_names.discard(name)
            work()
            executed += 1
