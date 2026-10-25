"""auto_delete's remote deletion must go through this fork's client lifecycle, not around it.

Upstream's pool.delete_chat() restarts a stopped client inline via `_ensure_client_ready`,
a method this fork's pool does not have: reviving a client is the recovery task's job
alone, behind its lock, busy check and retirement. Merged as-is, the call raised
AttributeError, which the surrounding `except Exception` turned into a deletion that
silently failed on every pass.
"""

import asyncio
from collections import deque
from contextlib import asynccontextmanager

from app.services.pool import GeminiClientPool


class FakeClient:
    def __init__(self, client_id: str, *, ready: bool = True):
        self.id = client_id
        self.ready = ready
        self.in_scope = False
        self.deleted: list[tuple[str, bool]] = []
        self.latest_chat_cid: str | None = None

    def running(self) -> bool:
        return self.ready

    def is_healthy(self) -> bool:
        return self.ready

    def is_guest(self) -> bool:
        return False

    @asynccontextmanager
    async def request_scope(self):
        self.in_scope = True
        try:
            yield
        finally:
            self.in_scope = False

    async def delete_chat(self, cid: str) -> None:
        self.deleted.append((cid, self.in_scope))


class NoRestartPool(GeminiClientPool):
    restarts = 0

    async def _restart_client(self, client) -> None:
        self.restarts += 1


def build_pool(*clients) -> NoRestartPool:
    pool = object.__new__(NoRestartPool)
    pool._clients = list(clients)
    pool._id_map = {c.id: c for c in clients}
    pool._round_robin = deque(clients)
    pool._restart_locks = {c.id: asyncio.Lock() for c in clients}
    pool._retired = set()
    return pool


def test_a_ready_client_deletes_inside_a_request_scope():
    """Held like a request, so a background restart or auto-close cannot interrupt it."""
    client = FakeClient("acc-1")
    client.latest_chat_cid = "c_1"
    pool = build_pool(client)

    assert asyncio.run(pool.delete_chat("acc-1", "c_1")) is True
    assert client.deleted == [("c_1", True)]
    assert client.latest_chat_cid is None


def test_an_unready_client_costs_one_attempt_and_is_never_restarted_inline():
    client = FakeClient("acc-1", ready=False)
    pool = build_pool(client)

    assert asyncio.run(pool.delete_chat("acc-1", "c_1")) is False
    assert client.deleted == []
    assert pool.restarts == 0


def test_a_retired_client_is_not_used_for_deletion():
    client = FakeClient("acc-1")
    pool = build_pool(client)
    pool._retired.add("acc-1")

    assert asyncio.run(pool.delete_chat("acc-1", "c_1")) is False
    assert client.deleted == []
