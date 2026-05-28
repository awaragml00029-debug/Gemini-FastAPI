import asyncio
import random
from collections import deque

from loguru import logger

from app.utils import g_config
from app.utils.singleton import Singleton

from .client import GeminiClientWrapper


class GeminiClientPool(metaclass=Singleton):
    """Pool of GeminiClient instances identified by unique ids."""

    def __init__(self) -> None:
        self._clients: list[GeminiClientWrapper] = []
        self._id_map: dict[str, GeminiClientWrapper] = {}
        self._round_robin: deque[GeminiClientWrapper] = deque()
        self._restart_locks: dict[str, asyncio.Lock] = {}

        if len(g_config.gemini.clients) == 0:
            raise ValueError("No Gemini clients configured")

        for c in g_config.gemini.clients:
            client = GeminiClientWrapper(
                client_id=c.id,
                **c.model_dump(exclude={"id"}),
            )
            self._clients.append(client)
            self._id_map[c.id] = client
            self._round_robin.append(client)
            self._restart_locks[c.id] = asyncio.Lock()

    async def init(self) -> None:
        """Initialize all clients in the pool with staggered start times."""
        clients_to_init = [c for c in self._clients if not self._client_ready(c)]
        for i, client in enumerate(clients_to_init):
            lock = self._restart_locks.get(client.id)
            if lock is None:
                logger.error(f"Restart lock missing for client {client.id}")
                continue

            async with lock:
                if self._client_ready(client):
                    continue

                try:
                    await self._restart_client(client)
                except Exception:
                    logger.error(f"Failed to initialize client {client.id}")

            if i < len(clients_to_init) - 1:
                delay = random.uniform(5, 30)
                logger.info(f"Staggering next initialization by {delay:.2f}s")
                await asyncio.sleep(delay)

        success_count = sum(self._client_ready(client) for client in self._clients)
        if success_count == 0:
            raise RuntimeError("Failed to initialize any Gemini clients")

    async def acquire(self, client_id: str | None = None) -> GeminiClientWrapper:
        """Return a healthy client by id or using round-robin."""
        if not self._round_robin:
            raise RuntimeError("No Gemini clients configured")

        if client_id:
            client = self._id_map.get(client_id)
            if not client:
                raise ValueError(f"Client id {client_id} not found")
            if await self._ensure_client_ready(client):
                return client
            raise RuntimeError(
                f"Gemini client {client_id} is not running and could not be restarted"
            )

        for _ in range(len(self._round_robin)):
            client = self._round_robin[0]
            self._round_robin.rotate(-1)
            if await self._ensure_client_ready(client):
                return client

        raise RuntimeError("No Gemini clients are currently available")

    @staticmethod
    def _client_ready(client: GeminiClientWrapper) -> bool:
        return client.running() and client.is_healthy()

    async def _restart_client(self, client: GeminiClientWrapper) -> None:
        if client.active_requests > 0:
            raise RuntimeError(
                f"Gemini client {client.id} has {client.active_requests} active request(s)"
            )

        if client.running():
            await client.close()

        await asyncio.wait_for(client.init(), timeout=float(g_config.gemini.timeout))

    async def _ensure_client_ready(self, client: GeminiClientWrapper) -> bool:
        """Make sure the client is usable, attempting a restart if needed."""
        if self._client_ready(client):
            return True

        lock = self._restart_locks.get(client.id)
        if lock is None:
            return False

        async with lock:
            if self._client_ready(client):
                return True

            try:
                await self._restart_client(client)
                logger.info(f"Restarted Gemini client {client.id} after it became unavailable.")
                return self._client_ready(client)
            except Exception:
                logger.exception(f"Failed to restart Gemini client {client.id}")
                return False

    @property
    def clients(self) -> list[GeminiClientWrapper]:
        """Return managed clients."""
        return self._clients

    async def close(self) -> None:
        """Close all clients in the pool."""
        if not self._clients:
            return

        logger.info(f"Closing {len(self._clients)} Gemini clients...")
        await asyncio.gather(
            *(client.close() for client in self._clients if client.running()),
            return_exceptions=True,
        )
        logger.info("All Gemini clients closed.")

    def status(self) -> dict[str, bool]:
        """Return healthy status for each client."""
        return {client.id: client.is_healthy() for client in self._clients}
