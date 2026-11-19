import threading
from collections.abc import Generator
from contextlib import contextmanager


class _LockSlot:
    def __init__(self):
        self.lock = threading.Lock()
        self.users = 0


class KeyedLockPool:
    """Serialize requests sharing the same (namespace, key)."""

    def __init__(self):
        self._guard = threading.Lock()
        self._slots: dict[tuple[str, str], _LockSlot] = {}

    @contextmanager
    def hold(
        self,
        namespace: str,
        key: str,
    ) -> Generator[None, None, None]:
        identity = (namespace, key)

        # Count waiting threads so the shared lock remains available to them.
        with self._guard:
            slot = self._slots.get(identity)

            if slot is None:
                slot = _LockSlot()
                self._slots[identity] = slot

            slot.users += 1

        acquired = False

        # Serialize work for this namespace and cache key.
        try:
            slot.lock.acquire()
            acquired = True
            yield
        finally:
            if acquired:
                slot.lock.release()

            # Remove the lock only after its last user leaves.
            with self._guard:
                slot.users -= 1

                if slot.users == 0:
                    del self._slots[identity]
