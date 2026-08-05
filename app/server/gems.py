from fastapi import APIRouter, Depends, HTTPException, status
from loguru import logger

from app.models.models import GemCreateRequest, GemListResponse, GemModel, GemUpdateRequest
from app.server.middleware import verify_api_key
from app.services import GeminiClientPool

router = APIRouter()


@router.get("/v1/gems", response_model=GemListResponse)
async def list_gems(
    predefined: bool | None = None,
    api_key: str = Depends(verify_api_key),
):
    pool = GeminiClientPool()
    try:
        client = await pool.acquire()
        async with client.request_scope():
            gem_jar = await client.fetch_gems()
    except Exception as e:
        logger.exception("Failed to fetch gems")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Failed to fetch gems",
        ) from e

    if predefined is not None:
        gem_jar = gem_jar.filter(predefined=predefined)

    return GemListResponse(data=[GemModel(**gem.model_dump()) for gem in gem_jar])


@router.get("/v1/gems/{gem_id}", response_model=GemModel)
async def get_gem(
    gem_id: str,
    api_key: str = Depends(verify_api_key),
):
    pool = GeminiClientPool()
    try:
        client = await pool.acquire()
        async with client.request_scope():
            gem_jar = await client.fetch_gems()
    except Exception as e:
        logger.exception("Failed to fetch gems")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Failed to fetch gems",
        ) from e

    gem = gem_jar.get(id=gem_id)
    if gem is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Gem '{gem_id}' not found."
        )

    return GemModel(**gem.model_dump())


@router.post("/v1/gems", response_model=GemModel, status_code=status.HTTP_201_CREATED)
async def create_gem(
    body: GemCreateRequest,
    api_key: str = Depends(verify_api_key),
):
    pool = GeminiClientPool()
    try:
        client = await pool.acquire()
        async with client.request_scope():
            gem = await client.create_gem(
                name=body.name, prompt=body.prompt, description=body.description
            )
    except Exception as e:
        logger.exception("Failed to create gem")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Failed to create gem",
        ) from e

    return GemModel(**gem.model_dump())


@router.put("/v1/gems/{gem_id}", response_model=GemModel)
async def update_gem(
    gem_id: str,
    body: GemUpdateRequest,
    api_key: str = Depends(verify_api_key),
):
    pool = GeminiClientPool()
    try:
        client = await pool.acquire()
        async with client.request_scope():
            gem = await client.update_gem(
                gem=gem_id, name=body.name, prompt=body.prompt, description=body.description
            )
    except Exception as e:
        logger.exception("Failed to update gem")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Failed to update gem",
        ) from e

    return GemModel(**gem.model_dump())


@router.delete("/v1/gems/{gem_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_gem(
    gem_id: str,
    api_key: str = Depends(verify_api_key),
):
    pool = GeminiClientPool()
    try:
        client = await pool.acquire()
        async with client.request_scope():
            await client.delete_gem(gem=gem_id)
    except Exception as e:
        logger.exception("Failed to delete gem")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Failed to delete gem",
        ) from e
