import logging
from typing import Any, Optional, Union

import asyncpg

from config import settings
from models.job import IncomingMessageRequest, ReportMessageRequest

logger = logging.getLogger(__name__)

_pool: Optional[asyncpg.Pool] = None


async def get_db_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        dsn = (
            f"postgresql://{settings.postgres_user}:{settings.postgres_password}"
            f"@{settings.postgres_host}:{settings.postgres_port}/{settings.postgres_db}"
        )
        _pool = await asyncpg.create_pool(
            dsn=dsn,
            min_size=2,
            max_size=10,
        )
        logger.info("DB pool created")
    return _pool


async def close_db_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
        logger.info("DB pool closed")


async def create_job(data: Union[IncomingMessageRequest, ReportMessageRequest]) -> dict[str, Any]:
    pool = await get_db_pool()
    if isinstance(data, ReportMessageRequest):
        concept_text = data.prompt
        ref_image_url = None
    else:
        concept_text = data.concept_text
        ref_image_url = data.ref_image_url
    row = await pool.fetchrow(
        """
        INSERT INTO jobs (
            id, user_id, character_id, concept_text, ref_image_url,
            messenger_source, messenger_user_id, messenger_channel_id, status
        ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, 'DRAFT')
        RETURNING *
        """,
        data.job_id,
        data.messenger_user_id,
        data.character_id,
        concept_text,
        ref_image_url,
        data.messenger_source.value,
        data.messenger_user_id,
        data.messenger_channel_id,
    )
    result = dict(row)
    logger.info("[%s] create_job job_id=%s", data.messenger_source.value, data.job_id)
    return result


async def get_job(job_id: str) -> Optional[dict[str, Any]]:
    pool = await get_db_pool()
    row = await pool.fetchrow("SELECT * FROM jobs WHERE id = $1", job_id)
    if row is None:
        return None
    return dict(row)


async def update_job(job_id: str, **kwargs: Any) -> dict[str, Any]:
    if not kwargs:
        return await get_job(job_id)

    set_clauses = []
    values = []
    for i, (key, value) in enumerate(kwargs.items(), start=1):
        set_clauses.append(f"{key} = ${i}")
        values.append(value)

    values.append(job_id)
    query = f"UPDATE jobs SET {', '.join(set_clauses)} WHERE id = ${len(values)} RETURNING *"

    pool = await get_db_pool()
    row = await pool.fetchrow(query, *values)
    logger.info("update_job job_id=%s fields=%s", job_id, list(kwargs.keys()))
    return dict(row)


async def transition_status(
    job_id: str,
    new_status: str,
    note: Optional[str] = None,
) -> dict[str, Any]:
    pool = await get_db_pool()
    row = await pool.fetchrow(
        "UPDATE jobs SET status = $1 WHERE id = $2 RETURNING *",
        new_status,
        job_id,
    )
    result = dict(row)
    logger.info("transition_status job_id=%s -> %s", job_id, new_status)
    return result
