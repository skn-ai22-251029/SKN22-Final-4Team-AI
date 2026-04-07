import json
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Optional

from config import settings
from services import job_service


KST = timezone(timedelta(hours=9))


def _to_json(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False)


def _coerce_dt(value: Optional[datetime]) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        return current.replace(tzinfo=timezone.utc)
    return current


def _cost_krw(cost_usd: Optional[float]) -> Optional[float]:
    if cost_usd is None:
        return None
    return float(cost_usd) * float(settings.cost_usd_krw_rate)


def _safe_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except Exception:
        return None


async def record_event(
    *,
    job_id: str,
    topic_text: str = "",
    stage: str,
    process: str,
    provider: str,
    attempt_no: int = 1,
    status: str,
    started_at: Optional[datetime] = None,
    ended_at: Optional[datetime] = None,
    usage_json: Optional[dict[str, Any]] = None,
    raw_response_json: Optional[dict[str, Any]] = None,
    cost_usd: Optional[float] = None,
    error_type: str = "",
    error_message: str = "",
    idempotency_key: str = "",
) -> bool:
    pool = await job_service.get_db_pool()
    started = _coerce_dt(started_at)
    ended = _coerce_dt(ended_at)
    duration_ms = int(max(0.0, (ended - started).total_seconds()) * 1000)
    rate = float(settings.cost_usd_krw_rate)
    normalized_cost = _safe_float(cost_usd)
    async with pool.acquire() as conn:
        result = await conn.execute(
            """
            INSERT INTO cost_events (
                job_id, topic_text, stage, process, provider, attempt_no, status,
                started_at, ended_at, duration_ms, usage_json, raw_response_json,
                cost_usd, usd_krw_rate, cost_krw, error_type, error_message, idempotency_key
            ) VALUES (
                $1::uuid, $2, $3, $4, $5, $6, $7,
                $8, $9, $10, $11::jsonb, $12::jsonb,
                $13, $14, $15, $16, $17, $18
            )
            ON CONFLICT (idempotency_key)
            DO NOTHING
            """,
            job_id,
            (topic_text or "").strip(),
            stage,
            process,
            provider,
            int(max(1, attempt_no)),
            status,
            started,
            ended,
            duration_ms,
            _to_json(usage_json),
            _to_json(raw_response_json),
            normalized_cost,
            rate,
            _cost_krw(normalized_cost),
            (error_type or "").strip(),
            (error_message or "")[:1000],
            (idempotency_key or "").strip(),
        )
    return str(result).upper().startswith("INSERT 0 1")


async def ingest_event(payload: dict[str, Any]) -> bool:
    return await record_event(
        job_id=str(payload.get("job_id") or "").strip(),
        topic_text=str(payload.get("topic_text") or "").strip(),
        stage=str(payload.get("stage") or "").strip(),
        process=str(payload.get("process") or "").strip(),
        provider=str(payload.get("provider") or "").strip(),
        attempt_no=int(payload.get("attempt_no") or 1),
        status=str(payload.get("status") or "").strip(),
        started_at=payload.get("started_at"),
        ended_at=payload.get("ended_at"),
        usage_json=payload.get("usage_json") if isinstance(payload.get("usage_json"), dict) else {},
        raw_response_json=payload.get("raw_response_json") if isinstance(payload.get("raw_response_json"), dict) else {},
        cost_usd=_safe_float(payload.get("cost_usd")),
        error_type=str(payload.get("error_type") or "").strip(),
        error_message=str(payload.get("error_message") or "").strip(),
        idempotency_key=str(payload.get("idempotency_key") or "").strip(),
    )


def _kst_day_range(target_date: date) -> tuple[datetime, datetime]:
    start_kst = datetime.combine(target_date, time(0, 0, 0), tzinfo=KST)
    end_kst = start_kst + timedelta(days=1)
    return start_kst.astimezone(timezone.utc), end_kst.astimezone(timezone.utc)


async def allocate_daily_fixed_cost(*, target_date: date) -> dict[str, Any]:
    pool = await job_service.get_db_pool()
    start_utc, end_utc = _kst_day_range(target_date)
    aws_fixed = float(settings.aws_daily_fixed_usd)
    runpod_fixed = float(settings.runpod_daily_fixed_usd)
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT DISTINCT job_id::text AS job_id
            FROM cost_events
            WHERE stage='video'
              AND status='success'
              AND created_at >= $1
              AND created_at < $2
            ORDER BY job_id
            """,
            start_utc,
            end_utc,
        )
        job_ids = [str(row["job_id"]) for row in rows]
        eligible_count = len(job_ids)
        total_fixed = aws_fixed + runpod_fixed
        allocated_per_job = (total_fixed / eligible_count) if eligible_count > 0 else 0.0
        await conn.execute(
            """
            INSERT INTO daily_fixed_cost_pool (
                cost_date_kst, aws_fixed_usd, runpod_fixed_usd, usd_krw_rate,
                eligible_job_count, allocated_per_job_usd, updated_at
            ) VALUES ($1, $2, $3, $4, $5, $6, NOW())
            ON CONFLICT (cost_date_kst)
            DO UPDATE SET
              aws_fixed_usd = EXCLUDED.aws_fixed_usd,
              runpod_fixed_usd = EXCLUDED.runpod_fixed_usd,
              usd_krw_rate = EXCLUDED.usd_krw_rate,
              eligible_job_count = EXCLUDED.eligible_job_count,
              allocated_per_job_usd = EXCLUDED.allocated_per_job_usd,
              updated_at = NOW()
            """,
            target_date,
            aws_fixed,
            runpod_fixed,
            float(settings.cost_usd_krw_rate),
            eligible_count,
            allocated_per_job,
        )
    for job_id in job_ids:
        if aws_fixed > 0:
            await record_event(
                job_id=job_id,
                stage="infra",
                process="daily_fixed_allocation",
                provider="aws_fixed",
                status="success",
                cost_usd=(aws_fixed / eligible_count) if eligible_count > 0 else 0.0,
                usage_json={"cost_date_kst": target_date.isoformat()},
                raw_response_json={"allocation_method": "daily_even_split"},
                idempotency_key=f"infra:aws:{target_date.isoformat()}:{job_id}",
            )
        if runpod_fixed > 0:
            await record_event(
                job_id=job_id,
                stage="infra",
                process="daily_fixed_allocation",
                provider="runpod_fixed",
                status="success",
                cost_usd=(runpod_fixed / eligible_count) if eligible_count > 0 else 0.0,
                usage_json={"cost_date_kst": target_date.isoformat()},
                raw_response_json={"allocation_method": "daily_even_split"},
                idempotency_key=f"infra:runpod:{target_date.isoformat()}:{job_id}",
            )
    return {
        "cost_date_kst": target_date.isoformat(),
        "eligible_job_count": eligible_count,
        "aws_fixed_usd": aws_fixed,
        "runpod_fixed_usd": runpod_fixed,
        "allocated_per_job_usd": allocated_per_job,
    }


async def list_jobs_summary(
    *,
    from_date: Optional[date],
    to_date: Optional[date],
    q: str,
    status: str,
    limit: int,
    offset: int,
) -> dict[str, Any]:
    pool = await job_service.get_db_pool()
    where = []
    params: list[Any] = []
    if from_date:
        start_utc, _ = _kst_day_range(from_date)
        params.append(start_utc)
        where.append(f"j.created_at >= ${len(params)}")
    if to_date:
        _, end_utc = _kst_day_range(to_date)
        params.append(end_utc)
        where.append(f"j.created_at < ${len(params)}")
    if q.strip():
        params.append(f"%{q.strip()}%")
        where.append(f"(j.id::text ILIKE ${len(params)} OR COALESCE(j.concept_text,'') ILIKE ${len(params)})")
    if status.strip():
        params.append(status.strip())
        where.append(f"j.status = ${len(params)}")
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    params.extend([max(1, limit), max(0, offset)])
    async with pool.acquire() as conn:
        count_row = await conn.fetchrow(
            f"SELECT COUNT(*)::int AS cnt FROM jobs j {where_sql}",
            *params[:-2],
        )
        rows = await conn.fetch(
            f"""
            SELECT
              j.id::text AS job_id,
              COALESCE(j.concept_text, '') AS topic_text,
              j.status,
              j.created_at,
              j.updated_at,
              COALESCE(a.total_cost_usd, 0) AS total_cost_usd,
              COALESCE(a.total_cost_krw, 0) AS total_cost_krw,
              COALESCE(a.script_success, 0) AS script_success,
              COALESCE(a.script_failed, 0) AS script_failed,
              COALESCE(a.tts_success, 0) AS tts_success,
              COALESCE(a.tts_failed, 0) AS tts_failed,
              COALESCE(a.video_success, 0) AS video_success,
              COALESCE(a.video_failed, 0) AS video_failed
            FROM jobs j
            LEFT JOIN (
              SELECT
                job_id,
                SUM(COALESCE(cost_usd, 0)) AS total_cost_usd,
                SUM(COALESCE(cost_krw, 0)) AS total_cost_krw,
                COUNT(*) FILTER (WHERE stage='script' AND status='success') AS script_success,
                COUNT(*) FILTER (WHERE stage='script' AND status='failed') AS script_failed,
                COUNT(*) FILTER (WHERE stage='tts' AND status='success') AS tts_success,
                COUNT(*) FILTER (WHERE stage='tts' AND status='failed') AS tts_failed,
                COUNT(*) FILTER (WHERE stage='video' AND status='success') AS video_success,
                COUNT(*) FILTER (WHERE stage='video' AND status='failed') AS video_failed
              FROM cost_events
              GROUP BY job_id
            ) a ON a.job_id = j.id
            {where_sql}
            ORDER BY j.created_at DESC
            LIMIT ${len(params)-1} OFFSET ${len(params)}
            """,
            *params,
        )
    return {
        "total": int(count_row["cnt"] if count_row else 0),
        "limit": max(1, limit),
        "offset": max(0, offset),
        "items": [dict(row) for row in rows],
    }


async def get_job_detail(job_id: str) -> dict[str, Any]:
    pool = await job_service.get_db_pool()
    async with pool.acquire() as conn:
        job = await conn.fetchrow(
            """
            SELECT id::text AS job_id, status, COALESCE(concept_text,'') AS topic_text, created_at, updated_at
            FROM jobs
            WHERE id::text = $1
            """,
            job_id,
        )
        if job is None:
            raise RuntimeError("job not found")
        events = await conn.fetch(
            """
            SELECT
              id::text AS event_id,
              job_id::text AS job_id,
              stage,
              process,
              provider,
              attempt_no,
              status,
              started_at,
              ended_at,
              duration_ms,
              usage_json,
              raw_response_json,
              cost_usd,
              cost_krw,
              error_type,
              error_message,
              created_at
            FROM cost_events
            WHERE job_id::text = $1
            ORDER BY created_at ASC
            """,
            job_id,
        )
    total_cost_usd = sum(float(event["cost_usd"] or 0) for event in events)
    total_cost_krw = sum(float(event["cost_krw"] or 0) for event in events)
    return {
        "job": dict(job),
        "summary": {
            "total_events": len(events),
            "total_cost_usd": total_cost_usd,
            "total_cost_krw": total_cost_krw,
        },
        "events": [dict(event) for event in events],
    }


async def export_payload(*, job_id: str = "", from_date: Optional[date] = None, to_date: Optional[date] = None) -> dict[str, Any]:
    if job_id.strip():
        detail = await get_job_detail(job_id.strip())
        return {
            "meta": {
                "mode": "job",
                "job_id": job_id.strip(),
                "exported_at": datetime.now(timezone.utc).isoformat(),
                "usd_krw_rate": settings.cost_usd_krw_rate,
            },
            "data": detail,
        }
    summary = await list_jobs_summary(
        from_date=from_date,
        to_date=to_date,
        q="",
        status="",
        limit=max(settings.cost_max_list_limit, 5000),
        offset=0,
    )
    return {
        "meta": {
            "mode": "range",
            "from_date": from_date.isoformat() if from_date else "",
            "to_date": to_date.isoformat() if to_date else "",
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "usd_krw_rate": settings.cost_usd_krw_rate,
        },
        "data": summary,
    }
