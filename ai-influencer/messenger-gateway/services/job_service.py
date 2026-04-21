import json
import logging
from typing import Any, Optional, Union

import asyncpg

from config import settings
from models.job import IncomingMessageRequest, ReportMessageRequest

logger = logging.getLogger(__name__)

_pool: Optional[asyncpg.Pool] = None


def _normalize_job_row(row: asyncpg.Record | None) -> Optional[dict[str, Any]]:
    if row is None:
        return None
    result = dict(row)
    job_id = result.get("id")
    if job_id is not None:
        result["id"] = str(job_id)
    return result


async def _ensure_schema(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS pricing_snapshots (
                id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                provider            TEXT NOT NULL,
                product             TEXT NOT NULL,
                model               TEXT NOT NULL DEFAULT '',
                unit_type           TEXT NOT NULL,
                unit_price_usd      NUMERIC,
                effective_from      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                effective_to        TIMESTAMPTZ,
                source_note         TEXT,
                created_at          TIMESTAMPTZ DEFAULT NOW()
            )
            """
        )
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS daily_fixed_cost_pool (
                cost_date_kst           DATE PRIMARY KEY,
                aws_fixed_usd           NUMERIC NOT NULL DEFAULT 0,
                runpod_fixed_usd        NUMERIC NOT NULL DEFAULT 0,
                usd_krw_rate            NUMERIC NOT NULL DEFAULT 0,
                eligible_job_count      INT NOT NULL DEFAULT 0,
                allocated_per_job_usd   NUMERIC NOT NULL DEFAULT 0,
                created_at              TIMESTAMPTZ DEFAULT NOW(),
                updated_at              TIMESTAMPTZ DEFAULT NOW()
            )
            """
        )
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS cost_events (
                id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                job_id               UUID REFERENCES jobs(id) ON DELETE CASCADE,
                topic_text           TEXT,
                stage                TEXT NOT NULL,
                process              TEXT NOT NULL,
                provider             TEXT NOT NULL,
                attempt_no           INT NOT NULL DEFAULT 1,
                status               TEXT NOT NULL CHECK (status IN ('success','failed')),
                started_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                ended_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                duration_ms          INT NOT NULL DEFAULT 0,
                usage_json           JSONB NOT NULL DEFAULT '{}'::jsonb,
                raw_response_json    JSONB NOT NULL DEFAULT '{}'::jsonb,
                cost_usd             NUMERIC,
                pricing_kind         TEXT NOT NULL DEFAULT '',
                pricing_source       TEXT NOT NULL DEFAULT '',
                api_key_family       TEXT NOT NULL DEFAULT '',
                subject_type         TEXT NOT NULL DEFAULT '',
                subject_key          TEXT NOT NULL DEFAULT '',
                subject_label        TEXT NOT NULL DEFAULT '',
                usd_krw_rate         NUMERIC,
                cost_krw             NUMERIC,
                error_type           TEXT,
                error_message        TEXT,
                idempotency_key      TEXT NOT NULL,
                created_at           TIMESTAMPTZ DEFAULT NOW(),
                UNIQUE (idempotency_key)
            )
            """
        )
        await conn.execute("ALTER TABLE cost_events ADD COLUMN IF NOT EXISTS pricing_kind TEXT NOT NULL DEFAULT ''")
        await conn.execute("ALTER TABLE cost_events ADD COLUMN IF NOT EXISTS pricing_source TEXT NOT NULL DEFAULT ''")
        await conn.execute("ALTER TABLE cost_events ADD COLUMN IF NOT EXISTS api_key_family TEXT NOT NULL DEFAULT ''")
        await conn.execute("ALTER TABLE cost_events ADD COLUMN IF NOT EXISTS subject_type TEXT NOT NULL DEFAULT ''")
        await conn.execute("ALTER TABLE cost_events ADD COLUMN IF NOT EXISTS subject_key TEXT NOT NULL DEFAULT ''")
        await conn.execute("ALTER TABLE cost_events ADD COLUMN IF NOT EXISTS subject_label TEXT NOT NULL DEFAULT ''")
        await conn.execute(
            """
            UPDATE cost_events
            SET pricing_kind = CASE
                WHEN COALESCE(pricing_kind, '') <> '' THEN pricing_kind
                WHEN provider IN ('aws_fixed', 'runpod_fixed') OR process = 'daily_fixed_allocation' THEN 'fixed'
                WHEN cost_usd IS NULL THEN 'missing'
                ELSE 'estimated'
            END
            """
        )
        await conn.execute(
            """
            UPDATE cost_events
            SET pricing_source = CASE
                WHEN COALESCE(pricing_source, '') <> '' THEN pricing_source
                WHEN provider IN ('aws_fixed', 'runpod_fixed') OR process = 'daily_fixed_allocation' THEN 'fixed_allocation'
                WHEN provider = 'heygen' AND cost_usd IS NOT NULL THEN 'config_fallback'
                WHEN cost_usd IS NULL THEN 'unavailable'
                ELSE 'legacy_backfill'
            END
            """
        )
        await conn.execute(
            """
            UPDATE cost_events
            SET api_key_family = CASE
                WHEN COALESCE(api_key_family, '') <> '' THEN api_key_family
                WHEN process IN ('tts_script_rewrite', 'subtitle_script_rewrite') THEN 'rewrite'
                WHEN process = 'generate_tts_audio' THEN 'tts_generation'
                WHEN process = 'heygen_generate' THEN 'heygen'
                WHEN process = 'hardburn_subtitle' THEN 'hardburn_subtitle'
                WHEN provider IN ('aws_fixed', 'runpod_fixed') OR process = 'daily_fixed_allocation' THEN 'infra_fixed'
                ELSE provider
            END
            """
        )
        await conn.execute(
            """
            UPDATE cost_events
            SET subject_type = CASE
                WHEN COALESCE(subject_type, '') <> '' THEN subject_type
                WHEN job_id IS NULL THEN 'operation'
                ELSE 'job'
            END
            """
        )
        await conn.execute(
            """
            UPDATE cost_events
            SET subject_key = CASE
                WHEN COALESCE(subject_key, '') <> '' THEN subject_key
                WHEN job_id IS NOT NULL THEN job_id::text
                ELSE CONCAT('legacy:', process, ':', provider, ':', id::text)
            END
            """
        )
        await conn.execute(
            """
            UPDATE cost_events
            SET subject_label = CASE
                WHEN COALESCE(subject_label, '') <> '' THEN subject_label
                WHEN job_id IS NOT NULL THEN COALESCE(NULLIF(topic_text, ''), job_id::text)
                ELSE CONCAT('legacy operation: ', process, ' / ', provider)
            END
            """
        )
        await conn.execute("CREATE INDEX IF NOT EXISTS cost_events_job_created_idx ON cost_events(job_id, created_at DESC)")
        await conn.execute("CREATE INDEX IF NOT EXISTS cost_events_stage_status_created_idx ON cost_events(stage, status, created_at DESC)")
        await conn.execute("CREATE INDEX IF NOT EXISTS cost_events_provider_created_idx ON cost_events(provider, created_at DESC)")
        await conn.execute("CREATE INDEX IF NOT EXISTS cost_events_subject_created_idx ON cost_events(subject_type, subject_key, created_at DESC)")
        await conn.execute("CREATE INDEX IF NOT EXISTS cost_events_api_family_created_idx ON cost_events(api_key_family, created_at DESC)")
        await conn.execute("ALTER TABLE characters ADD COLUMN IF NOT EXISTS heygen_avatar_id TEXT")
        await conn.execute("ALTER TABLE platform_posts ADD COLUMN IF NOT EXISTS status TEXT")
        await conn.execute("ALTER TABLE platform_posts ADD COLUMN IF NOT EXISTS platform_post_url TEXT")
        await conn.execute("ALTER TABLE platform_posts ADD COLUMN IF NOT EXISTS error_message TEXT")
        await conn.execute("ALTER TABLE platform_posts ADD COLUMN IF NOT EXISTS request_json JSONB DEFAULT '{}'::jsonb")
        await conn.execute("ALTER TABLE platform_posts ADD COLUMN IF NOT EXISTS response_json JSONB DEFAULT '{}'::jsonb")
        await conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS platform_posts_job_platform_uidx ON platform_posts (job_id, platform)"
        )
        await conn.execute("ALTER TABLE jobs DROP CONSTRAINT IF EXISTS jobs_status_check")
        await conn.execute(
            """
            ALTER TABLE jobs
            ADD CONSTRAINT jobs_status_check CHECK (
                status IN (
                    'DRAFT','SCRIPTING','GENERATING',
                    'WAITING_APPROVAL','REVISION_REQUESTED',
                    'APPROVED','REPORT_READY','PUBLISHING','PUBLISHED',
                    'PARTIALLY_PUBLISHED','PUBLISH_FAILED',
                    'ANALYTICS_COLLECTED','FAILED',
                    'WAITING_VIDEO_APPROVAL'
                )
            )
            """
        )
        await conn.execute("ALTER TABLE platform_posts DROP CONSTRAINT IF EXISTS platform_posts_platform_check")
        await conn.execute(
            """
            ALTER TABLE platform_posts
            ADD CONSTRAINT platform_posts_platform_check CHECK (platform IN ('youtube','instagram','tiktok'))
            """
        )
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS seed_lab_runs (
                run_id                   TEXT PRIMARY KEY,
                status                   TEXT NOT NULL DEFAULT 'queued',
                messenger_source         TEXT NOT NULL DEFAULT 'discord',
                discord_user_id          TEXT NOT NULL,
                discord_channel_id       TEXT NOT NULL,
                dataset_path             TEXT,
                seed_list_raw            TEXT,
                dup_mode                 BOOLEAN NOT NULL DEFAULT FALSE,
                samples                  INT NOT NULL DEFAULT 30,
                takes_per_seed           INT NOT NULL DEFAULT 1,
                concurrency              INT NOT NULL DEFAULT 2,
                run_dir                  TEXT,
                signed_link_expires_at   TIMESTAMPTZ,
                last_error               TEXT,
                created_at               TIMESTAMPTZ DEFAULT NOW(),
                updated_at               TIMESTAMPTZ DEFAULT NOW()
            )
            """
        )
        await conn.execute("ALTER TABLE seed_lab_runs ADD COLUMN IF NOT EXISTS progress_message_id TEXT")
        await conn.execute("ALTER TABLE seed_lab_runs ADD COLUMN IF NOT EXISTS progress_last_stage TEXT")
        await conn.execute("ALTER TABLE seed_lab_runs ADD COLUMN IF NOT EXISTS progress_last_generated_count INT NOT NULL DEFAULT 0")
        await conn.execute("ALTER TABLE seed_lab_runs ADD COLUMN IF NOT EXISTS progress_last_evaluated_count INT NOT NULL DEFAULT 0")
        await conn.execute("ALTER TABLE seed_lab_runs ADD COLUMN IF NOT EXISTS progress_last_failed_count INT NOT NULL DEFAULT 0")
        await conn.execute("ALTER TABLE seed_lab_runs ADD COLUMN IF NOT EXISTS progress_last_total_count INT NOT NULL DEFAULT 0")
        await conn.execute("CREATE INDEX IF NOT EXISTS seed_lab_runs_user_created_idx ON seed_lab_runs(discord_user_id, created_at DESC)")


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
        await _ensure_schema(_pool)
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
    result = _normalize_job_row(row)
    logger.info("[%s] create_job job_id=%s", data.messenger_source.value, data.job_id)
    return result


async def get_job(job_id: str) -> Optional[dict[str, Any]]:
    pool = await get_db_pool()
    row = await pool.fetchrow("SELECT * FROM jobs WHERE id::text = $1", job_id)
    return _normalize_job_row(row)


async def list_platform_posts(job_id: str) -> list[dict[str, Any]]:
    pool = await get_db_pool()
    rows = await pool.fetch(
        """
        SELECT
            id::text AS id,
            job_id::text AS job_id,
            platform,
            platform_post_id,
            platform_post_url,
            status,
            error_message,
            request_json,
            response_json,
            published_at,
            created_at
        FROM platform_posts
        WHERE job_id::text = $1
        ORDER BY created_at DESC
        """,
        job_id,
    )
    return [dict(row) for row in rows]


async def get_character(character_id: str) -> Optional[dict[str, Any]]:
    pool = await get_db_pool()
    row = await pool.fetchrow("SELECT * FROM characters WHERE id = $1", character_id)
    return dict(row) if row is not None else None


async def update_character_avatar(character_id: str, avatar_id: str) -> Optional[dict[str, Any]]:
    pool = await get_db_pool()
    normalized_avatar_id = (avatar_id or "").strip() or None
    row = await pool.fetchrow(
        "UPDATE characters SET heygen_avatar_id = $1 WHERE id = $2 RETURNING *",
        normalized_avatar_id,
        character_id,
    )
    if row is None:
        return None
    logger.info("update_character_avatar character_id=%s avatar_id=%s", character_id, normalized_avatar_id or "")
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
    return _normalize_job_row(row)


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
    result = _normalize_job_row(row)
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


async def list_recent_jobs_in_channel(
    messenger_channel_id: str,
    *,
    limit: int = 5,
    require_script: bool = False,
    require_audio: bool = False,
) -> list[dict[str, Any]]:
    pool = await get_db_pool()
    safe_limit = max(1, min(limit, 10))

    where_clauses = ["messenger_channel_id = $1"]
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
            created_at,
            updated_at,
            COALESCE(script_json->>'script_text', script_json->>'script', '') AS script_text,
            COALESCE(audio_url, '') AS audio_url
        FROM jobs
        WHERE {' AND '.join(where_clauses)}
        ORDER BY created_at DESC
        LIMIT $2
    """
    rows = await pool.fetch(query, messenger_channel_id, safe_limit)
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


async def find_jobs_by_prefix_in_channel(
    job_prefix: str,
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
        "messenger_channel_id = $1",
        "id::text ILIKE $2",
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
        LIMIT $3
    """
    rows = await pool.fetch(
        query,
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


async def get_latest_job_in_channel(
    messenger_channel_id: str,
    *,
    require_script: bool = False,
    require_audio: bool = False,
) -> Optional[dict[str, Any]]:
    rows = await list_recent_jobs_in_channel(
        messenger_channel_id,
        limit=1,
        require_script=require_script,
        require_audio=require_audio,
    )
    if not rows:
        return None
    return rows[0]


async def find_existing_auto_report_job(
    *,
    channel_id: str,
    notebook_url: str,
) -> Optional[dict[str, Any]]:
    pool = await get_db_pool()
    row = await pool.fetchrow(
        """
        SELECT *
        FROM jobs
        WHERE messenger_user_id = 'system:auto-report'
          AND COALESCE(script_json->>'auto_report_channel_id', '') = $1
          AND COALESCE(script_json->>'auto_report_notebook_url', '') = $2
          AND status <> 'FAILED'
        ORDER BY created_at DESC
        LIMIT 1
        """,
        channel_id,
        notebook_url,
    )
    return _normalize_job_row(row)


async def get_latest_auto_report_job(
    *,
    channel_id: str,
    notebook_url: str,
) -> Optional[dict[str, Any]]:
    pool = await get_db_pool()
    row = await pool.fetchrow(
        """
        SELECT *
        FROM jobs
        WHERE messenger_user_id = 'system:auto-report'
          AND COALESCE(script_json->>'auto_report_channel_id', '') = $1
          AND COALESCE(script_json->>'auto_report_notebook_url', '') = $2
        ORDER BY created_at DESC
        LIMIT 1
        """,
        channel_id,
        notebook_url,
    )
    return _normalize_job_row(row)


async def count_auto_report_attempts_today(
    *,
    channel_id: str,
    notebook_url: str,
) -> int:
    pool = await get_db_pool()
    count = await pool.fetchval(
        """
        SELECT COUNT(*)
        FROM jobs
        WHERE messenger_user_id = 'system:auto-report'
          AND COALESCE(script_json->>'auto_report_channel_id', '') = $1
          AND COALESCE(script_json->>'auto_report_notebook_url', '') = $2
          AND created_at >= (date_trunc('day', now() AT TIME ZONE 'Asia/Seoul') AT TIME ZONE 'Asia/Seoul')
          AND created_at < ((date_trunc('day', now() AT TIME ZONE 'Asia/Seoul') + interval '1 day') AT TIME ZONE 'Asia/Seoul')
        """,
        channel_id,
        notebook_url,
    )
    return int(count or 0)


async def create_seed_lab_run(
    *,
    run_id: str,
    discord_user_id: str,
    discord_channel_id: str,
    dataset_path: str,
    seed_list_raw: str,
    dup_mode: bool,
    samples: int,
    takes_per_seed: int,
    concurrency: int,
    run_dir: str = "",
    signed_link_expires_at: Any = None,
) -> dict[str, Any]:
    pool = await get_db_pool()
    row = await pool.fetchrow(
        """
        INSERT INTO seed_lab_runs (
            run_id, status, messenger_source, discord_user_id, discord_channel_id,
            dataset_path, seed_list_raw, dup_mode, samples, takes_per_seed, concurrency,
            run_dir, signed_link_expires_at
        ) VALUES (
            $1, 'queued', 'discord', $2, $3,
            $4, $5, $6, $7, $8, $9,
            $10, $11
        )
        RETURNING *
        """,
        run_id,
        discord_user_id,
        discord_channel_id,
        dataset_path,
        seed_list_raw,
        dup_mode,
        samples,
        takes_per_seed,
        concurrency,
        run_dir,
        signed_link_expires_at,
    )
    return dict(row)


async def get_seed_lab_run(run_id: str) -> Optional[dict[str, Any]]:
    pool = await get_db_pool()
    row = await pool.fetchrow("SELECT * FROM seed_lab_runs WHERE run_id = $1", run_id)
    return dict(row) if row is not None else None


async def update_seed_lab_run(run_id: str, **kwargs: Any) -> Optional[dict[str, Any]]:
    if not kwargs:
        return await get_seed_lab_run(run_id)
    set_clauses = []
    values = []
    for i, (key, value) in enumerate(kwargs.items(), start=1):
        set_clauses.append(f"{key} = ${i}")
        values.append(value)
    values.append(run_id)
    query = (
        f"UPDATE seed_lab_runs SET {', '.join(set_clauses)}, updated_at = NOW() "
        f"WHERE run_id = ${len(values)} RETURNING *"
    )
    pool = await get_db_pool()
    row = await pool.fetchrow(query, *values)
    return dict(row) if row is not None else None
