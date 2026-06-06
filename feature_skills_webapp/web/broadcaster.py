import asyncio


class Broadcaster:
    """In-process SSE fan-out. One asyncio.Queue per connected client.

    Single-process only: a multi-worker deployment would leave clients on
    one worker deaf to changes detected on another.
    """

    def __init__(self) -> None:
        self._clients: set[asyncio.Queue[str]] = set()

    def register(self) -> asyncio.Queue[str]:
        q: asyncio.Queue[str] = asyncio.Queue()
        self._clients.add(q)
        return q

    def unregister(self, q: asyncio.Queue[str]) -> None:
        self._clients.discard(q)

    def broadcast(self, message: str = "changed") -> None:
        for q in self._clients:
            q.put_nowait(message)

    @property
    def client_count(self) -> int:
        return len(self._clients)
