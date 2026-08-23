import asyncio
import contextlib
import random
import time
from collections import deque

from loguru import logger

from app.utils import g_config
from app.utils.singleton import Singleton

from .client import GeminiClientWrapper

# First gap between startup init attempts; tripled on each further attempt (5s, 15s, 45s).
STARTUP_RETRY_BASE_SECONDS = 5.0

# How long to leave a client alone after a restart that returned cleanly but left it still
# unusable. That is not the same as a restart that raised: nothing went wrong mechanically,
# the account simply is not coming back by being re-initialized - typically an expired cookie,
# where the library reports a successful init and then reports the session UNAUTHENTICATED.
# Retrying that once a minute forever is pure waste (~54 pointless auth round-trips an hour
# against Google, on an account already prone to 429s), so each futile round pushes the next
# attempt further out. It is deliberately a backoff and not a retirement: `auto_refresh` can
# still rotate the cookies and bring the account back on its own, and a retired client never
# returns for the life of the process.
FUTILE_RESTART_BACKOFF_SECONDS = (60.0, 300.0, 1800.0)


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
        # client id -> consecutive restarts that returned but left the client unusable.
        self._futile_restarts: dict[str, int] = {}
        # client id -> monotonic time before which the recovery task leaves it alone.
        self._restart_not_before: dict[str, float] = {}
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

        `require_account` narrows the search to clients that can serve an upload, which a guest
        session cannot. It does not otherwise widen it: a guest is never handed out. Google
        gives a guest no history, no uploads and no model choice, so serving one silently would
        answer a request with something quietly worse than what was asked for. When every
        account's cookies have expired the caller is told exactly that instead.
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

        # Selection is a pure, non-blocking readiness check on purpose. Restarting a dead client
        # here used to run three auth rounds (~7s) inside the request, and a permanently dead
        # account made every request that round-robined onto it pay that again. Reviving is the
        # background task's job - see recover_unavailable_clients().
        #
        # A guest can never satisfy `_client_ready`: readiness requires AccountStatus.AVAILABLE
        # and a guest is by definition UNAUTHENTICATED. That is the intended behaviour, so the
        # loop no longer runs a second pass pretending otherwise - the previous one logged that
        # it was falling back to a guest and then failed anyway, one line later.
        for _ in range(len(self._round_robin)):
            client = self._round_robin[0]
            self._round_robin.rotate(-1)
            if self._client_ready(client):
                return client

        raise RuntimeError(self._unavailable_reason(require_account))

    def _unavailable_reason(self, require_account: bool) -> str:
        """Say which way the pool is down, so the caller is not left guessing.

        Expired cookies and a transient outage need opposite responses - one wants a human
        editing the container's credentials, the other wants a retry - and they are worth
        telling apart in the message rather than in the logs only.
        """
        live = [c for c in self._clients if c.id not in self._retired]
        if live and all(c.is_guest() for c in live):
            logger.error(
                "Every Gemini account is unauthenticated: their cookies have expired. "
                "Requests fail until SECURE_1PSID / SECURE_1PSIDTS are updated."
            )
            return (
                "Every Gemini account is unauthenticated - the cookies have expired. Update "
                "SECURE_1PSID / SECURE_1PSIDTS for the configured clients and restart the "
                "container. Serving these requests from a guest session is not possible: "
                "Google gives a guest no history, no uploads and no model choice."
            )
        if self._retired and len(self._retired) == len(self._clients):
            return (
                "Every Gemini client has been retired after repeated failures. Fix their "
                "credentials and restart the container."
            )
        if require_account:
            return (
                "No authenticated Gemini client is available. This request needs a file "
                "upload, which a guest session cannot do - refresh the client cookies."
            )
        return "No Gemini clients are currently available"

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
        now = time.monotonic()
        # Cleared up front: anything that goes wrong from here on re-raises the signal,
        # rather than being swallowed by this pass.
        self._recovery_wanted.clear()

        for client in self._clients:
            if client.id in self._retired:
                continue

            if self._client_ready(client):
                # Back in rotation, whether this task did it or the library's own cookie
                # refresh did - either way its failure history no longer applies.
                self._clear_restart_state(client.id)
                continue

            if now < self._restart_not_before.get(client.id, 0.0):
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

                if not self._client_ready(client):
                    # `init()` returned without raising, so nothing here failed in the sense
                    # the counter above measures - the account came back still unusable. Left
                    # on the poll interval this repeats forever, because the failure it is
                    # waiting for never arrives.
                    self._defer_futile_restart(client.id)
                    continue

                logger.info(f"Restarted Gemini client {client.id} after it became unavailable.")
                self._clear_restart_state(client.id)

    def _clear_restart_state(self, client_id: str) -> None:
        """Forget a client's failure history once it is usable again."""
        self._restart_failures.pop(client_id, None)
        self._futile_restarts.pop(client_id, None)
        self._restart_not_before.pop(client_id, None)

    def _defer_futile_restart(self, client_id: str) -> None:
        """Push the next restart attempt out after one that changed nothing."""
        rounds = self._futile_restarts.get(client_id, 0) + 1
        self._futile_restarts[client_id] = rounds
        delay = FUTILE_RESTART_BACKOFF_SECONDS[min(rounds, len(FUTILE_RESTART_BACKOFF_SECONDS)) - 1]
        self._restart_not_before[client_id] = time.monotonic() + delay
        logger.info(
            f"Gemini client {client_id} restarted but is still unusable "
            f"(round {rounds}); next attempt in {delay:.0f}s."
        )

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
