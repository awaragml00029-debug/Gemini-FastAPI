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


async def _run_pool_init_in_background(pool: GeminiClientPool) -> None:
    try:
        await pool.init()
        await refresh_available_models_cache(pool)
        healthy_clients = [c.id for c in pool.clients if c.running()]
        if healthy_clients:
            logger.info(f"Gemini clients initialized in background: {healthy_clients}.")
        else:
            logger.warning("Gemini client background initialization finished with no running clients.")
    except asyncio.CancelledError:
        logger.debug("Gemini client background initialization task cancelled.")
        raise
    except Exception:
        logger.exception("Gemini client background initialization failed.")


@asynccontextmanager
async def lifespan(app: FastAPI):
    cleanup_stop_event = asyncio.Event()

    pool = GeminiClientPool()
    # Client init deliberately stays off the startup path -- see
    # _run_pool_init_in_background. Upstream blocks here instead, which turns a
    # slow or unreachable Gemini account into a server that never becomes ready.
    try:
        LMDBConversationStore().prune_stale_indexes()
    except Exception:
        logger.exception("Failed to prune stale LMDB indexes; continuing with startup.")

    cleanup_task = asyncio.create_task(_run_retention_cleanup(cleanup_stop_event))
    pool_init_task = asyncio.create_task(_run_pool_init_in_background(pool))

    # Give the tasks a chance to start and surface immediate failures.
    await asyncio.sleep(0)

    if cleanup_task.done():
        try:
            cleanup_task.result()
        except Exception:
            logger.exception("LMDB retention cleanup task failed to start.")
            raise

    if pool_init_task.done():
        try:
            pool_init_task.result()
        except asyncio.CancelledError:
            logger.debug("Gemini client background initialization task cancelled at startup.")
        except Exception:
            logger.exception("Gemini client background initialization task failed to start.")

    logger.info("Gemini API Server ready to serve requests; Gemini clients initialize in background.")

    try:
        yield
    finally:
        cleanup_stop_event.set()

        if not pool_init_task.done():
            pool_init_task.cancel()

        try:
            await pool_init_task
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("Gemini client background initialization task ended with an error.")

        try:
            await pool.close()
        except Exception:
            logger.exception("Failed to close Gemini client pool gracefully.")

        try:
            await cleanup_task
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
