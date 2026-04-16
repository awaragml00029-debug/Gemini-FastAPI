import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from loguru import logger

from .server.chat import router as chat_router
from .server.health import router as health_router
from .server.media import router as media_router
from .server.middleware import (
    add_cors_middleware,
    add_exception_handler,
    cleanup_expired_media,
)
from .services import GeminiClientPool, LMDBConversationStore
from .utils import g_config

RETENTION_CLEANUP_INTERVAL_SECONDS = 6 * 60 * 60  # Check every 6 hours


async def _run_retention_cleanup(stop_event: asyncio.Event) -> None:
    """
    Periodically enforce LMDB retention policy until the stop_event is set.
    """
    store = LMDBConversationStore()
    if store.retention_days <= 0:
        logger.info("LMDB retention cleanup disabled; skipping scheduler.")
        return

    logger.info(
        f"Starting LMDB retention cleanup task (retention={store.retention_days} day(s), interval={RETENTION_CLEANUP_INTERVAL_SECONDS} seconds)."
    )

    while not stop_event.is_set():
        try:
            store.cleanup_expired()
            cleanup_expired_media(store.retention_days)
        except Exception:
            logger.exception("LMDB retention cleanup task failed.")

        try:
            await asyncio.wait_for(
                stop_event.wait(),
                timeout=RETENTION_CLEANUP_INTERVAL_SECONDS,
            )
        except TimeoutError:
            continue

    logger.info("LMDB retention cleanup task stopped.")


async def _run_pool_watchdog(stop_event: asyncio.Event) -> None:
    """
    Periodically check for dead clients in the pool and revive them.
    """
    pool = GeminiClientPool()
    interval = g_config.gemini.pool_watchdog_interval
    logger.info(f"Starting Gemini pool watchdog task (interval={interval} seconds).")

    while not stop_event.is_set():
        try:
            await pool.revive_dead_clients()
        except Exception:
            logger.exception("Gemini pool watchdog task encountered an error.")

        try:
            await asyncio.wait_for(
                stop_event.wait(),
                timeout=interval,
            )
        except TimeoutError:
            continue

    logger.info("Gemini pool watchdog task stopped.")


@asynccontextmanager
async def lifespan(app: FastAPI):
    cleanup_stop_event = asyncio.Event()

    pool = GeminiClientPool()
    try:
        await pool.init()
    except Exception as e:
        logger.exception(f"Failed to initialize Gemini clients: {e}")
        raise

    cleanup_task = asyncio.create_task(_run_retention_cleanup(cleanup_stop_event))
    watchdog_task = asyncio.create_task(_run_pool_watchdog(cleanup_stop_event))

    # Give the tasks a chance to start and surface immediate failures.
    await asyncio.sleep(0)

    for task, name in [(cleanup_task, "LMDB retention cleanup"), (watchdog_task, "Pool watchdog")]:
        if task.done():
            try:
                task.result()
            except Exception:
                logger.exception(f"{name} task failed to start.")
                raise

    logger.info(f"Gemini clients initialized: {[c.id for c in pool.clients]}.")
    logger.info("Gemini API Server ready to serve requests.")

    try:
        yield
    finally:
        cleanup_stop_event.set()
        try:
            await pool.close()
        except Exception:
            logger.exception("Failed to close Gemini client pool gracefully.")

        try:
            await asyncio.gather(cleanup_task, watchdog_task)
        except asyncio.CancelledError:
            logger.debug("Background tasks cancelled during shutdown.")
        except Exception:
            logger.exception(
                "One or more background tasks terminated with an unexpected error during shutdown."
            )


def create_app() -> FastAPI:
    app = FastAPI(
        title="Gemini API Server",
        description="OpenAI-compatible API for Gemini Web",
        version="1.0.0",
        lifespan=lifespan,
    )

    add_cors_middleware(app)
    add_exception_handler(app)

    app.include_router(health_router, tags=["Health"])
    app.include_router(chat_router, tags=["Chat"])
    app.include_router(media_router, tags=["Media"])

    return app
