"""Charge each delivery until its handler and settlement have both finished."""

from collections.abc import Callable

from .config import DeliveryLimits
from .errors import ConsumerOverloadedError
from .frames import MessageFrame


class Completion:
    """An idempotent capability to finish one owner's part of a reservation."""

    def __init__(self, finish: Callable[[], None]) -> None:
        self._finish = finish
        self._done = False

    @property
    def done(self) -> bool:
        return self._done

    def finish(self) -> None:
        if not self._done:
            self._done = True
            self._finish()


class Reservation:
    """Give the handler and settlement independent, single-use completions."""

    def __init__(self, size: int, release: Callable[["Reservation"], None]) -> None:
        self._size = size
        self._handled = Completion(self._try_release)
        self._settled = Completion(self._try_release)
        self._release = release

    @property
    def size(self) -> int:
        return self._size

    @property
    def handled(self) -> Completion:
        return self._handled

    @property
    def settled(self) -> Completion:
        return self._settled

    def _try_release(self) -> None:
        if self.handled.done and self.settled.done:
            self._release(self)


class Capacity:
    def __init__(self, limits: DeliveryLimits) -> None:
        self._limits = limits
        self._reservations: set[Reservation] = set()
        self._bytes = 0

    @property
    def pending_messages(self) -> int:
        return len(self._reservations)

    @property
    def pending_bytes(self) -> int:
        return self._bytes

    def reserve(self, frame: MessageFrame) -> Reservation:
        size = len(frame.body) + sum(
            len(key.encode()) + len(str(value).encode()) for key, value in frame.headers.items()
        )
        messages_limit = self._limits.pending_messages
        bytes_limit = self._limits.pending_bytes
        messages_full = isinstance(messages_limit, int) and len(self._reservations) >= messages_limit
        bytes_full = isinstance(bytes_limit, int) and self._bytes + size > bytes_limit
        if messages_full or bytes_full:
            raise ConsumerOverloadedError(
                max_pending_messages=self._limits.pending_messages, max_pending_bytes=self._limits.pending_bytes
            )
        reservation = Reservation(size, self._release)
        self._reservations.add(reservation)
        self._bytes += size
        return reservation

    def _release(self, reservation: Reservation) -> None:
        self._reservations.remove(reservation)
        self._bytes -= reservation.size
