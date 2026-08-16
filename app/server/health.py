from fastapi import APIRouter, Response, status
from loguru import logger

from app.models import HealthCheckResponse
from app.services import GeminiClientPool, LMDBConversationStore

router = APIRouter()


@router.get("/health", response_model=HealthCheckResponse)
async def health_check(response: Response):
    pool = GeminiClientPool()
    db = LMDBConversationStore()
    client_status = pool.status()
    stat = db.stats()

    if not all(client_status.values()):
        down_clients = [client_id for client_id, status in client_status.items() if not status]
        logger.warning(f"One or more Gemini clients are unhealthy: {', '.join(down_clients)}")

    if not stat:
        logger.error("Failed to retrieve LMDB conversation store stats")
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return HealthCheckResponse(
            ok=False, error="LMDB conversation store unavailable", clients=client_status
        )

    if not any(client_status.values()):
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return HealthCheckResponse(
            ok=False,
            error="No usable Gemini client is available",
            storage=stat,
            clients=client_status,
        )

    return HealthCheckResponse(ok=True, storage=stat, clients=client_status)
