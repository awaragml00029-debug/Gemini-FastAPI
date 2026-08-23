import asyncio
import contextlib
import random
from collections import deque

from loguru import logger

from app.utils import g_config
from app.utils.singleton import Singleton

from .client import GeminiClientWrapper

# First gap between startup init attempts; tripled on each further attempt (5s, 15s, 45s).
STARTUP_RETRY_BASE_SECONDS = 5.0


class ClientBusyError(RuntimeError):
    """A restart was refused because the client still has in-flight requests.

    Kept distinct from a genuine startup failure: being busy is transient and must
    not retire the client.
    """


class GeminiClientPool(metaclass=Singleton):
    """Pool of GeminiClient instances identified by unique ids."""

    def __init__(self) -> None:
        self._clients: list[GeminiClientWrapper] = []
        self._id_map: dict[str, GeminiClientWrapper] = {}
        self._round_robin: deque[GeminiClientWrapper] = deque()
        self._restart_locks: dict[str, asyncio.Lock] = {}
        # Clients given up on for this process's lifetime - see _retire().
        self._retired: set[str] = set()
        # client id -> consecutive failed background restarts, reset on success.
        self._restart_failures: dict[str, int] = {}
        # Raised when a client is known to need reviving, so the recovery task can act
        # at once instead of waiting out its poll interval.
        self._recovery_wanted = asyncio.Event()

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

    async def _init_one(self, client: GeminiClientWrapper) -> bool:
        """Initialize a single client; returns True on success."""
        return await self._init_attempt(client)

    async def _init_attempt(self, client: GeminiClientWrapper) -> bool:
        """Run library init; returns True on success."""
        try:
            await client.init()
            return True
        except Exception:
            return False

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

                await self._init_with_retries(client)

            if i < len(clients_to_init) - 1:
                delay = random.uniform(5, 30)
                logger.info(f"Staggering next initialization by {delay:.2f}s")
                await asyncio.sleep(delay)

        success_count = sum(self._client_ready(client) for client in self._clients)
        if success_count == 0:
            raise RuntimeError("Failed to initialize any Gemini clients")

    async def acquire(
        self, client_id: str | None = None, require_account: bool = False
    ) -> GeminiClientWrapper:
        """Return a healthy client by id or using round-robin.

        `require_account` excludes guest sessions, for requests they cannot serve at all - file
        uploads. Otherwise a guest is used only once no authenticated client is left.
        """
        if not self._round_robin:
            raise RuntimeError("No Gemini clients configured")

        if client_id:
            client = self._id_map.get(client_id)
            if not client:
                raise ValueError(f"Client id {client_id} not found")
            if self._client_ready(client):
                return client
            raise RuntimeError(f"Gemini client {client_id} is not currently available")

        # Authenticated clients first. A client whose cookies expired keeps answering text
        # prompts as a guest, so it stays usable and must not take the pool down, but it has no
        # history, no uploads and no model choice - traffic belongs elsewhere while it can.
        #
        # Selection is a pure, non-blocking readiness check on purpose. Restarting a dead client
        # here used to run three auth rounds (~7s) inside the request, and a permanently dead
        # account made every request that round-robined onto it pay that again. A client that
        # failed to start is retired instead - see _retire().
        for account_only in (True,) if require_account else (True, False):
            for _ in range(len(self._round_robin)):
                client = self._round_robin[0]
                self._round_robin.rotate(-1)
                if account_only and client.is_guest():
                    continue
                if self._client_ready(client):
                    return client

            if account_only and not require_account and any(c.is_guest() for c in self._clients):
                logger.warning(
                    "No authenticated Gemini client is available; falling back to a guest "
                    "session until cookies are refreshed."
                )

        if require_account:
            raise RuntimeError(
                "No authenticated Gemini client is available. This request needs a file upload, "
                "which a guest session cannot do - refresh the client cookies."
            )
        raise RuntimeError("No Gemini clients are currently available")

    @staticmethod
    def _client_ready(client: GeminiClientWrapper) -> bool:
        return client.running() and client.is_healthy()

    def _retire(self, client_id: str) -> None:
        """Take a client out of service for the rest of this process's life.

        A client that keeps failing to authenticate has credentials that are wrong, and
        those live in the container's environment: fixing them means editing the compose
        file and restarting the container, which starts a new process anyway. So there is
        nothing a further retry could pick up, and each one costs ~7s of auth attempts.
        """
        self._restart_failures.pop(client_id, None)
        if client_id in self._retired:
            return
        self._retired.add(client_id)
        logger.warning(
            f"Gemini client {client_id} is retired for the lifetime of this process. "
            "Fix its credentials and restart the container to bring it back."
        )

    async def _init_with_retries(self, client: GeminiClientWrapper) -> None:
        """Bring a client up at startup, spacing out retries before giving up on it.

        The library's own three auth attempts all land inside ~7s, so a proxy or network
        that is not up yet at container start fails every one of them. Spreading these
        retries out tells that apart from credentials that are simply wrong. Startup runs
        in the background, so the extra wait is not on any request.
        """
        attempts = g_config.gemini.startup_init_attempts
        for attempt in range(1, attempts + 1):
            try:
                await self._restart_client(client)
                return
            except ClientBusyError:
                # In-flight requests: nothing was restarted, and this is not a credential problem.
                logger.error(f"Failed to initialize client {client.id}: still serving requests")
                return
            except Exception:
                logger.error(
                    f"Failed to initialize client {client.id} (attempt {attempt}/{attempts})"
                )
                if attempt < attempts:
                    delay = STARTUP_RETRY_BASE_SECONDS * 3 ** (attempt - 1)
                    logger.info(f"Retrying client {client.id} in {delay:.0f}s")
                    await asyncio.sleep(delay)

        self._retire(client.id)

    async def recover_unavailable_clients(self) -> None:
        """Revive clients that dropped out of rotation; retire the ones that keep failing.

        Driven by the background recovery task, never by a request: a request is taken off
        a client on any error (see chat.py), and this is what puts it back. The multi-second
        auth retries a revival can cost belong here, off the critical path.
        """
        limit = g_config.gemini.restart_max_failures
        # Cleared up front: anything that goes wrong from here on re-raises the signal,
        # rather than being swallowed by this pass.
        self._recovery_wanted.clear()

        for client in self._clients:
            if client.id in self._retired or self._client_ready(client):
                continue

            lock = self._restart_locks.get(client.id)
            if lock is None:
                continue

            async with lock:
                # Re-checked under the lock: startup init holds it across its own retries,
                # and may have retired the client while this pass was queued behind it.
                if client.id in self._retired or self._client_ready(client):
                    continue

                try:
                    await self._restart_client(client)
                except ClientBusyError:
                    # Transient, and no restart was attempted - do not count it against the client.
                    continue
                except Exception:
                    failures = self._restart_failures.get(client.id, 0) + 1
                    self._restart_failures[client.id] = failures
                    logger.warning(
                        f"Failed to restart Gemini client {client.id} ({failures}/{limit})."
                    )
                    if failures >= limit:
                        self._retire(client.id)
                    continue

                logger.info(f"Restarted Gemini client {client.id} after it became unavailable.")
                self._restart_failures.pop(client.id, None)

    async def _restart_client(self, client: GeminiClientWrapper) -> None:
        if client.active_requests > 0:
            raise ClientBusyError(
                f"Gemini client {client.id} has {client.active_requests} active request(s)"
            )

        if client.running():
            await client.close()

        await asyncio.wait_for(client.init(), timeout=float(g_config.gemini.timeout))

    @property
    def clients(self) -> list[GeminiClientWrapper]:
        """Return managed clients."""
        return self._clients

    @property
    def retired_clients(self) -> list[str]:
        """Ids of clients given up on until the process restarts."""
        return sorted(self._retired)

    def mark_unavailable(self, client: GeminiClientWrapper) -> None:
        """Take a client out of rotation and ask for it to be revived promptly.

        Callers on the request path use this rather than client.mark_unavailable() so the
        recovery task starts within milliseconds instead of at its next poll: the client is
        out of rotation until it succeeds, and that gap is pure lost capacity.
        """
        client.mark_unavailable()
        self._recovery_wanted.set()

    async def wait_for_recovery_signal(self, timeout: float) -> None:
        """Block until a client needs reviving, or until `timeout` elapses."""
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._recovery_wanted.wait(), timeout=timeout)

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
