import json
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
    row = await pool.fetchrow("SELECT * FROM jobs WHERE id::text = $1", job_id)
    if row is None:
        return None
    return dict(row)


async def update_job(job_id: str, **kwargs: Any) -> dict[str, Any]:
    if not kwargs:
        return await get_job(job_id)

    set_clauses = []
    values = []
    for i, (key, value) in enumerate(kwargs.items(), start=1):
        if key == "script_json":
            if isinstance(value, (dict, list)):
                value = json.dumps(value, ensure_ascii=False)
            set_clauses.append(f"{key} = ${i}::jsonb")
        else:
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


async def list_recent_jobs(
    messenger_user_id: str,
    messenger_channel_id: str,
    *,
    limit: int = 5,
    require_script: bool = False,
    require_audio: bool = False,
) -> list[dict[str, Any]]:
    pool = await get_db_pool()
    safe_limit = max(1, min(limit, 10))

    where_clauses = ["messenger_user_id = $1", "messenger_channel_id = $2"]
    if require_script:
        where_clauses.append("COALESCE(script_json->>'script_text', script_json->>'script', '') <> ''")
    if require_audio:
        where_clauses.append("COALESCE(audio_url, '') <> ''")

    query = f"""
        SELECT
            id::text AS id,
            status,
            created_at,
            updated_at,
            COALESCE(script_json->>'script_text', script_json->>'script', '') AS script_text,
            COALESCE(audio_url, '') AS audio_url
        FROM jobs
        WHERE {' AND '.join(where_clauses)}
        ORDER BY created_at DESC
        LIMIT $3
    """
    rows = await pool.fetch(query, messenger_user_id, messenger_channel_id, safe_limit)
    return [dict(row) for row in rows]


async def find_jobs_by_prefix(
    job_prefix: str,
    messenger_user_id: str,
    messenger_channel_id: str,
    *,
    require_script: bool = False,
    require_audio: bool = False,
    limit: int = 6,
) -> list[dict[str, Any]]:
    pool = await get_db_pool()
    normalized_prefix = (job_prefix or "").strip()
    if not normalized_prefix:
        return []

    safe_limit = max(1, min(limit, 10))
    where_clauses = [
        "messenger_user_id = $1",
        "messenger_channel_id = $2",
        "id::text ILIKE $3",
    ]
    if require_script:
        where_clauses.append("COALESCE(script_json->>'script_text', script_json->>'script', '') <> ''")
    if require_audio:
        where_clauses.append("COALESCE(audio_url, '') <> ''")

    query = f"""
        SELECT
            id::text AS id,
            status,
            messenger_user_id,
            messenger_channel_id,
            COALESCE(script_json->>'script_text', script_json->>'script', '') AS script_text,
            COALESCE(audio_url, '') AS audio_url
        FROM jobs
        WHERE {' AND '.join(where_clauses)}
        ORDER BY created_at DESC
        LIMIT $4
    """
    rows = await pool.fetch(
        query,
        messenger_user_id,
        messenger_channel_id,
        f"{normalized_prefix}%",
        safe_limit,
    )
    return [dict(row) for row in rows]


async def get_latest_job(
    messenger_user_id: str,
    messenger_channel_id: str,
    *,
    require_script: bool = False,
    require_audio: bool = False,
) -> Optional[dict[str, Any]]:
    rows = await list_recent_jobs(
        messenger_user_id,
        messenger_channel_id,
        limit=1,
        require_script=require_script,
        require_audio=require_audio,
    )
    if not rows:
        return None
    return rows[0]
