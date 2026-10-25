"""Main application entry point and lifespan event setup."""

import asyncio
import contextlib
import mimetypes
from contextlib import asynccontextmanager

from fastapi import FastAPI
from loguru import logger

from .server.chat import refresh_available_models_cache
from .server.chat import router as chat_router
from .server.gemini import add_gemini_exception_handlers
from .server.gemini import router as gemini_router
from .server.gems import router as gems_router
from .server.health import router as health_router
from .server.media import router as media_router
from .server.middleware import (
    add_cors_middleware,
    add_exception_handler,
    add_request_size_limit_middleware,
    cleanup_expired_media,
)
from .services import GeminiClientPool, LMDBConversationStore
from .utils import g_config
from .utils.config import ChatMode

# Canonical audio MIME types: Python's platform mapping yields legacy "x-"
# types (audio/x-wav, audio/x-aac, audio/x-flac) that Google's upload
# endpoint does not classify as audio, so the attached file never reaches
# the model as an audible attachment. Registered at import time, before any upload.
for _ext, _mime in (
    (".wav", "audio/wav"),
    (".aac", "audio/aac"),
    (".flac", "audio/flac"),
    (".m4a", "audio/mp4"),
):
    with contextlib.suppress(ValueError):
        mimetypes.add_type(_mime, _ext)

RETENTION_CLEANUP_INTERVAL_SECONDS = 6 * 60 * 60  # Check every 6 hours


def _get_cleanup_interval() -> int:
    """Determine the retention cleanup check interval."""
    interval = RETENTION_CLEANUP_INTERVAL_SECONDS
    if g_config.gemini.chat_mode == ChatMode.AUTO_DELETE:
        interval = min(interval, max(60, g_config.gemini.auto_delete_ttl // 2))
    return interval


async def _run_retention_cleanup(stop_event: asyncio.Event) -> None:
    """Periodically enforce LMDB retention policy and remote chat deletion."""
    store = LMDBConversationStore()
    pool = GeminiClientPool()

    if (
        store.retention_days <= 0
        and g_config.gemini.chat_mode != ChatMode.AUTO_DELETE
        and not store.get_pending_deletions()
        and not store.has_auto_delete_records()
    ):
        logger.info("LMDB retention cleanup disabled; skipping scheduler.")
        return

    interval = _get_cleanup_interval()
    logger.info(
        f"Starting LMDB retention cleanup task (retention={store.retention_days} day(s), "
        f"auto_delete_ttl={g_config.gemini.auto_delete_ttl}s, interval={interval}s)."
    )

    while not stop_event.is_set():
        try:
            _, to_delete = store.cleanup_expired()
            if to_delete:
                for client_id, cid in to_delete:
                    if stop_event.is_set():
                        break
                    try:
                        success = await pool.delete_chat(client_id, cid)
                        if success:
                            store.remove_pending_deletion(cid)
                        else:
                            store.record_pending_deletion_failure(cid)
                    except Exception as e:
                        logger.warning(
                            f"Failed to delete remote chat {cid} for client {client_id}: {e}"
                        )
                        store.record_pending_deletion_failure(cid)
                    await asyncio.sleep(0.1)

            if g_config.gemini.chat_mode == ChatMode.AUTO_DELETE:
                cleanup_expired_media(ttl_seconds=g_config.gemini.auto_delete_ttl)
            else:
                cleanup_expired_media(retention_days=store.retention_days)
        except Exception:
            logger.exception("LMDB retention cleanup task failed.")

        try:
            current_interval = _get_cleanup_interval()
            await asyncio.wait_for(
                stop_event.wait(),
                timeout=current_interval,
            )
        except TimeoutError:
            continue

    logger.info("LMDB retention cleanup task stopped.")


async def _run_client_recovery(pool: GeminiClientPool, stop_event: asyncio.Event) -> None:
    """Periodically revive Gemini clients that dropped out of rotation until stop_event is set.

    A request is taken off its client on any error and acquire() only ever checks readiness, so
    without this nothing would put a client back. Keeping the revival here means the auth retries
    it can cost never land inside a request.

    The pool raises a signal the moment it takes a client out of rotation, so a revival normally
    starts within milliseconds; the interval is the backstop for a client that went unhealthy
    without anyone saying so, such as the library's watchdog changing its account status.
    """
    interval = g_config.gemini.restart_check_interval
    logger.info(f"Starting Gemini client recovery task (backstop interval={interval} seconds).")

    while not stop_event.is_set():
        await pool.wait_for_recovery_signal(timeout=interval)

        if stop_event.is_set():
            break

        try:
            await pool.recover_unavailable_clients()
        except Exception:
            logger.exception("Gemini client recovery task failed.")

    logger.info("Gemini client recovery task stopped.")


async def _run_pool_init_in_background(pool: GeminiClientPool) -> None:
    try:
        await pool.init()
        await refresh_available_models_cache(pool)
        healthy_clients = [c.id for c in pool.clients if c.running()]
        if healthy_clients:
            logger.info(f"Gemini clients initialized in background: {healthy_clients}.")
        else:
            logger.warning(
                "Gemini client background initialization finished with no running clients."
            )
    except asyncio.CancelledError:
        logger.debug("Gemini client background initialization task cancelled.")
        raise
    except Exception:
        logger.exception("Gemini client background initialization failed.")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage application startup and shutdown lifecycles."""
    cleanup_stop_event = asyncio.Event()
    recovery_stop_event = asyncio.Event()

    pool = GeminiClientPool()
    # Client init deliberately stays off the startup path -- see
    # _run_pool_init_in_background. Upstream blocks here instead, which turns a
    # slow or unreachable Gemini account into a server that never becomes ready.
    try:
        LMDBConversationStore().prune_stale_indexes()
    except Exception:
        logger.exception("Failed to prune stale LMDB indexes; continuing with startup.")

    cleanup_task = asyncio.create_task(_run_retention_cleanup(cleanup_stop_event))
    recovery_task = asyncio.create_task(_run_client_recovery(pool, recovery_stop_event))
    pool_init_task = asyncio.create_task(_run_pool_init_in_background(pool))

    # Give the tasks a chance to start and surface immediate failures.
    await asyncio.sleep(0)

    if cleanup_task.done():
        try:
            cleanup_task.result()
        except Exception:
            logger.exception("LMDB retention cleanup task failed to start.")
            raise

    if recovery_task.done():
        try:
            recovery_task.result()
        except Exception:
            logger.exception("Gemini client recovery task failed to start.")
            raise

    if pool_init_task.done():
        try:
            pool_init_task.result()
        except asyncio.CancelledError:
            logger.debug("Gemini client background initialization task cancelled at startup.")
        except Exception:
            logger.exception("Gemini client background initialization task failed to start.")

    logger.info(
        "Gemini API Server ready to serve requests; Gemini clients initialize in background."
    )

    try:
        yield
    finally:
        cleanup_stop_event.set()
        recovery_stop_event.set()

        if not pool_init_task.done():
            pool_init_task.cancel()

        try:
            await pool_init_task
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("Gemini client background initialization task ended with an error.")

        # Settled before pool.close(): a revival in flight is a client.init() that would
        # otherwise race the close. Signalling alone is not enough, since that init can sit
        # there for gemini.timeout seconds.
        if not recovery_task.done():
            recovery_task.cancel()

        try:
            await recovery_task
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("Gemini client recovery task ended with an error.")

        # Also settled before pool.close(): with auto_delete the cleanup task calls
        # pool.delete_chat(), which would otherwise race the close.
        cleanup_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await cleanup_task

        try:
            await pool.close()
        except Exception:
            logger.exception("Failed to close Gemini client pool gracefully.")


def create_app() -> FastAPI:
    """Create and configure the FastAPI application instance."""
    app = FastAPI(
        title="Gemini API Server",
        description="OpenAI-compatible API for Gemini Web",
        version="1.0.0",
        lifespan=lifespan,
    )

    # Order matters: the last middleware added is the outermost, so the body limit has to be
    # registered first for its 413 to still pass back out through CORS.
    add_request_size_limit_middleware(app)
    add_cors_middleware(app)
    add_exception_handler(app)
    add_gemini_exception_handlers(app)

    app.include_router(health_router, tags=["Health"])
    app.include_router(chat_router, tags=["Chat"])
    app.include_router(media_router, tags=["Media"])
    app.include_router(gems_router, tags=["Gems"])
    app.include_router(gemini_router, tags=["Gemini"])

    return app
