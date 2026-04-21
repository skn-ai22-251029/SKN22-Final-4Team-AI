import json
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Optional

from config import settings
from services import job_service


KST = timezone(timedelta(hours=9))
PRICING_KINDS = {"actual", "estimated", "fixed", "missing"}
SUBJECT_TYPES = {"job", "operation"}
JOB_SUMMARY_SORT_FIELDS = {"updated_at", "created_at", "main_cost_usd", "estimated_cost_usd"}
JOB_SUMMARY_SORT_DIRECTIONS = {"asc", "desc"}
EPOCH_UTC = datetime.fromtimestamp(0, timezone.utc)
FIXED_INFRA_PROCESS = "daily_fixed_allocation"
FIXED_INFRA_PROVIDERS = {"aws_fixed", "runpod_fixed"}
DAILY_ESTIMATE_TARGET_VIDEO_COUNT = 3
DAILY_ESTIMATE_SAMPLE_LIMIT = 10


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


def _normalize_job_summary_sort_by(value: str) -> str:
    normalized = str(value or "").strip().lower()
    return normalized if normalized in JOB_SUMMARY_SORT_FIELDS else "updated_at"


def _normalize_job_summary_sort_dir(value: str) -> str:
    normalized = str(value or "").strip().lower()
    return normalized if normalized in JOB_SUMMARY_SORT_DIRECTIONS else "desc"


def _sort_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return EPOCH_UTC


def _sort_number(value: Any) -> float:
    coerced = _safe_float(value)
    return coerced if coerced is not None else 0.0


def _sort_job_summary_items(items: list[dict[str, Any]], *, sort_by: str, sort_dir: str) -> list[dict[str, Any]]:
    sorted_items = list(items)
    sorted_items.sort(key=lambda item: str(item.get("subject_key") or ""))
    sorted_items.sort(key=lambda item: _sort_datetime(item.get("created_at")), reverse=True)
    sorted_items.sort(key=lambda item: _sort_datetime(item.get("updated_at")), reverse=True)
    reverse = sort_dir == "desc"
    if sort_by in {"main_cost_usd", "estimated_cost_usd"}:
        sorted_items.sort(key=lambda item: _sort_number(item.get(sort_by)), reverse=reverse)
    else:
        sorted_items.sort(key=lambda item: _sort_datetime(item.get(sort_by)), reverse=reverse)
    return sorted_items


def _is_fixed_infra_event(event: dict[str, Any]) -> bool:
    process = str(event.get("process") or "").strip()
    provider = str(event.get("provider") or "").strip()
    return process == FIXED_INFRA_PROCESS or provider in FIXED_INFRA_PROVIDERS


def _is_ignored_missing_event(event: dict[str, Any]) -> bool:
    provider = str(event.get("provider") or "").strip().lower()
    status = str(event.get("status") or "").strip().lower()
    pricing_kind = str(event.get("pricing_kind") or "").strip().lower() or "missing"
    pricing_source = str(event.get("pricing_source") or "").strip().lower() or "unavailable"
    return (
        provider == "runpod_tts"
        and status == "failed"
        and pricing_kind == "missing"
        and pricing_source == "unavailable"
    )


def _visible_cost_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [event for event in events if not _is_fixed_infra_event(event)]


def _daily_estimate_payload(*, sample_count: int, average_variable_cost_usd: float) -> dict[str, Any]:
    target_video_count = DAILY_ESTIMATE_TARGET_VIDEO_COUNT
    aws_daily_fixed_usd = float(settings.aws_daily_fixed_usd or 0.0)
    estimated_daily_cost_usd = (
        round((average_variable_cost_usd * target_video_count) + aws_daily_fixed_usd, 6) if sample_count > 0 else 0.0
    )
    average_variable_cost_krw = _cost_krw(average_variable_cost_usd) or 0.0
    aws_daily_fixed_krw = _cost_krw(aws_daily_fixed_usd) or 0.0
    estimated_daily_cost_krw = _cost_krw(estimated_daily_cost_usd) or 0.0
    return {
        "sample_count": sample_count,
        "sample_limit": DAILY_ESTIMATE_SAMPLE_LIMIT,
        "target_video_count": target_video_count,
        "average_variable_cost_usd": round(average_variable_cost_usd, 6),
        "average_variable_cost_krw": round(average_variable_cost_krw, 3),
        "aws_daily_fixed_usd": round(aws_daily_fixed_usd, 6),
        "aws_daily_fixed_krw": round(aws_daily_fixed_krw, 3),
        "estimated_daily_cost_usd": estimated_daily_cost_usd,
        "estimated_daily_cost_krw": round(estimated_daily_cost_krw, 3),
        "basis": (
            f"최근 {sample_count}개 평균 × {target_video_count} + AWS 고정비"
            if sample_count > 0
            else "표본 없음"
        ),
    }


def _normalize_pricing_kind(
    pricing_kind: str,
    *,
    pricing_source: str,
    provider: str,
    process: str,
    cost_usd: Optional[float],
) -> str:
    normalized = str(pricing_kind or "").strip().lower()
    if normalized in PRICING_KINDS:
        return normalized
    if provider in {"aws_fixed", "runpod_fixed"} or process == "daily_fixed_allocation":
        return "fixed"
    if str(pricing_source or "").strip().lower() == "provider_actual":
        return "actual"
    if cost_usd is None:
        return "missing"
    return "estimated"


def _normalize_pricing_source(pricing_source: str, *, pricing_kind: str, provider: str, process: str) -> str:
    normalized = str(pricing_source or "").strip().lower()
    if normalized:
        return normalized
    if pricing_kind == "fixed" or provider in {"aws_fixed", "runpod_fixed"} or process == "daily_fixed_allocation":
        return "fixed_allocation"
    if pricing_kind == "actual":
        return "provider_actual"
    if pricing_kind == "estimated":
        return "provider_usage_estimate"
    return "unavailable"


def _normalize_api_key_family(api_key_family: str, *, process: str, provider: str) -> str:
    normalized = str(api_key_family or "").strip()
    if normalized:
        return normalized
    if process in {"tts_script_rewrite", "subtitle_script_rewrite"}:
        return "rewrite"
    if process == "generate_tts_audio":
        return "tts_generation"
    if process == "heygen_generate":
        return "heygen"
    if process == "hardburn_subtitle":
        return "hardburn_subtitle"
    if provider in {"aws_fixed", "runpod_fixed"} or process == "daily_fixed_allocation":
        return "infra_fixed"
    return str(provider or "unknown").strip() or "unknown"


def _normalize_subject_type(subject_type: str, *, job_id: str) -> str:
    normalized = str(subject_type or "").strip().lower()
    if normalized in SUBJECT_TYPES:
        return normalized
    return "job" if str(job_id or "").strip() else "operation"


def _normalize_subject_key(subject_key: str, *, subject_type: str, job_id: str, process: str, provider: str) -> str:
    normalized = str(subject_key or "").strip()
    if normalized:
        return normalized
    if subject_type == "job" and str(job_id or "").strip():
        return str(job_id).strip()
    return f"operation:{process or 'unknown'}:{provider or 'unknown'}"


def _normalize_subject_label(
    subject_label: str,
    *,
    subject_type: str,
    topic_text: str,
    subject_key: str,
    process: str,
    provider: str,
) -> str:
    normalized = str(subject_label or "").strip()
    if normalized:
        return normalized
    if subject_type == "job":
        topic = str(topic_text or "").strip()
        return topic or subject_key
    return f"{process or 'operation'} / {provider or 'unknown'}"


def _kst_day_range(target_date: date) -> tuple[datetime, datetime]:
    start_kst = datetime.combine(target_date, time(0, 0, 0), tzinfo=KST)
    end_kst = start_kst + timedelta(days=1)
    return start_kst.astimezone(timezone.utc), end_kst.astimezone(timezone.utc)


def _bucket_add(bucket: dict[str, dict[str, float | int]], key: str, cost_usd: Optional[float], cost_krw: Optional[float]) -> None:
    if not key:
        key = "(empty)"
    item = bucket.setdefault(key, {"cost_usd": 0.0, "cost_krw": 0.0, "count": 0})
    item["count"] = int(item.get("count") or 0) + 1
    item["cost_usd"] = round(float(item.get("cost_usd") or 0.0) + float(cost_usd or 0.0), 6)
    item["cost_krw"] = round(float(item.get("cost_krw") or 0.0) + float(cost_krw or 0.0), 3)


def _summarize_events(events: list[dict[str, Any]]) -> dict[str, Any]:
    summary = {
        "total_events": len(events),
        "total_cost_usd": 0.0,
        "total_cost_krw": 0.0,
        "actual_cost_usd": 0.0,
        "estimated_cost_usd": 0.0,
        "fixed_cost_usd": 0.0,
        "missing_cost_event_count": 0,
        "main_cost_usd": 0.0,
        "by_stage": {},
        "by_process": {},
        "by_provider": {},
        "by_api_key_family": {},
        "by_subject_type": {},
        "by_pricing_kind": {},
    }
    for event in events:
        cost_usd = _safe_float(event.get("cost_usd")) or 0.0
        cost_krw = _safe_float(event.get("cost_krw")) or 0.0
        pricing_kind = str(event.get("pricing_kind") or "").strip().lower() or "missing"
        ignored_missing = _is_ignored_missing_event(event)
        summary["total_cost_usd"] = round(float(summary["total_cost_usd"]) + cost_usd, 6)
        summary["total_cost_krw"] = round(float(summary["total_cost_krw"]) + cost_krw, 3)
        if pricing_kind == "actual":
            summary["actual_cost_usd"] = round(float(summary["actual_cost_usd"]) + cost_usd, 6)
        elif pricing_kind == "estimated":
            summary["estimated_cost_usd"] = round(float(summary["estimated_cost_usd"]) + cost_usd, 6)
        elif pricing_kind == "fixed":
            summary["fixed_cost_usd"] = round(float(summary["fixed_cost_usd"]) + cost_usd, 6)
        elif not ignored_missing:
            summary["missing_cost_event_count"] = int(summary["missing_cost_event_count"]) + 1
        _bucket_add(summary["by_stage"], str(event.get("stage") or "").strip(), cost_usd, cost_krw)
        _bucket_add(summary["by_process"], str(event.get("process") or "").strip(), cost_usd, cost_krw)
        _bucket_add(summary["by_provider"], str(event.get("provider") or "").strip(), cost_usd, cost_krw)
        _bucket_add(summary["by_api_key_family"], str(event.get("api_key_family") or "").strip(), cost_usd, cost_krw)
        _bucket_add(summary["by_subject_type"], str(event.get("subject_type") or "").strip(), cost_usd, cost_krw)
        if not ignored_missing:
            _bucket_add(summary["by_pricing_kind"], pricing_kind, cost_usd, cost_krw)
    summary["main_cost_usd"] = round(float(summary["actual_cost_usd"]) + float(summary["fixed_cost_usd"]), 6)
    return summary


async def record_event(
    *,
    job_id: str = "",
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
    pricing_kind: str = "",
    pricing_source: str = "",
    api_key_family: str = "",
    subject_type: str = "",
    subject_key: str = "",
    subject_label: str = "",
    error_type: str = "",
    error_message: str = "",
    idempotency_key: str = "",
) -> bool:
    pool = await job_service.get_db_pool()
    started = _coerce_dt(started_at)
    ended = _coerce_dt(ended_at)
    duration_ms = int(max(0.0, (ended - started).total_seconds()) * 1000)
    normalized_job_id = str(job_id or "").strip()
    normalized_cost = _safe_float(cost_usd)
    normalized_pricing_kind = _normalize_pricing_kind(
        pricing_kind,
        pricing_source=pricing_source,
        provider=provider,
        process=process,
        cost_usd=normalized_cost,
    )
    normalized_pricing_source = _normalize_pricing_source(
        pricing_source,
        pricing_kind=normalized_pricing_kind,
        provider=provider,
        process=process,
    )
    normalized_api_key_family = _normalize_api_key_family(api_key_family, process=process, provider=provider)
    normalized_subject_type = _normalize_subject_type(subject_type, job_id=normalized_job_id)
    normalized_subject_key = _normalize_subject_key(
        subject_key,
        subject_type=normalized_subject_type,
        job_id=normalized_job_id,
        process=process,
        provider=provider,
    )
    normalized_subject_label = _normalize_subject_label(
        subject_label,
        subject_type=normalized_subject_type,
        topic_text=topic_text,
        subject_key=normalized_subject_key,
        process=process,
        provider=provider,
    )
    rate = float(settings.cost_usd_krw_rate)
    async with pool.acquire() as conn:
        result = await conn.execute(
            """
            INSERT INTO cost_events (
                job_id, topic_text, stage, process, provider, attempt_no, status,
                started_at, ended_at, duration_ms, usage_json, raw_response_json,
                cost_usd, pricing_kind, pricing_source, api_key_family,
                subject_type, subject_key, subject_label,
                usd_krw_rate, cost_krw, error_type, error_message, idempotency_key
            ) VALUES (
                NULLIF($1, '')::uuid, $2, $3, $4, $5, $6, $7,
                $8, $9, $10, $11::jsonb, $12::jsonb,
                $13, $14, $15, $16,
                $17, $18, $19,
                $20, $21, $22, $23, $24
            )
            ON CONFLICT (idempotency_key)
            DO NOTHING
            """,
            normalized_job_id,
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
            normalized_pricing_kind,
            normalized_pricing_source,
            normalized_api_key_family,
            normalized_subject_type,
            normalized_subject_key,
            normalized_subject_label[:500],
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
        pricing_kind=str(payload.get("pricing_kind") or "").strip(),
        pricing_source=str(payload.get("pricing_source") or "").strip(),
        api_key_family=str(payload.get("api_key_family") or "").strip(),
        subject_type=str(payload.get("subject_type") or "").strip(),
        subject_key=str(payload.get("subject_key") or "").strip(),
        subject_label=str(payload.get("subject_label") or "").strip(),
        error_type=str(payload.get("error_type") or "").strip(),
        error_message=str(payload.get("error_message") or "").strip(),
        idempotency_key=str(payload.get("idempotency_key") or "").strip(),
    )


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
              AND job_id IS NOT NULL
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
                pricing_kind="fixed",
                pricing_source="fixed_allocation",
                api_key_family="infra_fixed",
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
                pricing_kind="fixed",
                pricing_source="fixed_allocation",
                api_key_family="infra_fixed",
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


async def _fetch_subject_events(
    conn: Any,
    *,
    subject_type: str,
    subject_keys: list[str],
) -> dict[tuple[str, str], list[dict[str, Any]]]:
    if not subject_keys:
        return {}
    rows = await conn.fetch(
        """
        SELECT
          id::text AS event_id,
          COALESCE(job_id::text, '') AS job_id,
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
          pricing_kind,
          pricing_source,
          api_key_family,
          subject_type,
          subject_key,
          subject_label,
          cost_krw,
          error_type,
          error_message,
          created_at
        FROM cost_events
        WHERE subject_type = $1
          AND subject_key = ANY($2::text[])
        ORDER BY created_at ASC
        """,
        subject_type,
        subject_keys,
    )
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows:
        record = dict(row)
        grouped.setdefault((str(record.get("subject_type") or ""), str(record.get("subject_key") or "")), []).append(record)
    return grouped


async def _fetch_daily_estimate(
    conn: Any,
    *,
    from_date: Optional[date],
    to_date: Optional[date],
    search: str,
    status: str,
) -> dict[str, Any]:
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
    if search:
        params.append(f"%{search}%")
        where.append(f"(j.id::text ILIKE ${len(params)} OR COALESCE(j.concept_text,'') ILIKE ${len(params)})")
    if status.strip():
        params.append(status.strip())
        where.append(f"j.status = ${len(params)}")
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    params.append(DAILY_ESTIMATE_SAMPLE_LIMIT)
    row = await conn.fetchrow(
        f"""
        WITH filtered_jobs AS (
          SELECT j.id
          FROM jobs j
          {where_sql}
          {"AND" if where_sql else "WHERE"} j.status IN ('WAITING_VIDEO_APPROVAL', 'PUBLISHED')
        ),
        heygen_success AS (
          SELECT ce.job_id, MAX(ce.created_at) AS video_ready_at
          FROM cost_events ce
          JOIN filtered_jobs fj ON fj.id = ce.job_id
          WHERE ce.stage = 'video'
            AND ce.process = 'heygen_generate'
            AND ce.status = 'success'
            AND ce.job_id IS NOT NULL
          GROUP BY ce.job_id
        ),
        picked AS (
          SELECT job_id, video_ready_at
          FROM heygen_success
          ORDER BY video_ready_at DESC
          LIMIT ${len(params)}
        ),
        per_job AS (
          SELECT
            p.job_id,
            COALESCE(SUM(
              CASE
                WHEN ce.process = 'daily_fixed_allocation' OR ce.provider IN ('aws_fixed', 'runpod_fixed') THEN 0
                ELSE COALESCE(ce.cost_usd, 0)
              END
            ), 0) AS variable_cost_usd
          FROM picked p
          LEFT JOIN cost_events ce ON ce.job_id = p.job_id
          GROUP BY p.job_id
        )
        SELECT
          COUNT(*)::int AS sample_count,
          COALESCE(AVG(variable_cost_usd), 0)::float AS average_variable_cost_usd
        FROM per_job
        """,
        *params,
    )
    sample_count = int(row["sample_count"] if row else 0)
    average_variable_cost_usd = float(row["average_variable_cost_usd"] if row else 0.0)
    return _daily_estimate_payload(sample_count=sample_count, average_variable_cost_usd=average_variable_cost_usd)


async def list_jobs_summary(
    *,
    from_date: Optional[date],
    to_date: Optional[date],
    q: str,
    status: str,
    limit: int,
    offset: int,
    subject_type: str = "all",
    sort_by: str = "updated_at",
    sort_dir: str = "desc",
) -> dict[str, Any]:
    pool = await job_service.get_db_pool()
    normalized_subject_type = (subject_type or "all").strip().lower()
    normalized_sort_by = _normalize_job_summary_sort_by(sort_by)
    normalized_sort_dir = _normalize_job_summary_sort_dir(sort_dir)
    has_status_filter = bool(status.strip())
    include_jobs = normalized_subject_type in {"all", "job"}
    include_operations = normalized_subject_type in {"all", "operation"} and not has_status_filter
    search = (q or "").strip()
    limit_value = max(1, int(limit))
    offset_value = max(0, int(offset))
    items: list[dict[str, Any]] = []
    total_jobs = 0
    total_operations = 0
    daily_estimate: dict[str, Any] = _daily_estimate_payload(sample_count=0, average_variable_cost_usd=0.0)

    async with pool.acquire() as conn:
        daily_estimate = await _fetch_daily_estimate(
            conn,
            from_date=from_date,
            to_date=to_date,
            search=search,
            status=status,
        )
        if include_jobs:
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
            if search:
                params.append(f"%{search}%")
                where.append(f"(j.id::text ILIKE ${len(params)} OR COALESCE(j.concept_text,'') ILIKE ${len(params)})")
            if status.strip():
                params.append(status.strip())
                where.append(f"j.status = ${len(params)}")
            where_sql = ("WHERE " + " AND ".join(where)) if where else ""
            count_row = await conn.fetchrow(f"SELECT COUNT(*)::int AS cnt FROM jobs j {where_sql}", *params)
            total_jobs = int(count_row["cnt"] if count_row else 0)
            rows = await conn.fetch(
                f"""
                SELECT
                  'job' AS subject_type,
                  j.id::text AS subject_key,
                  COALESCE(j.concept_text, '') AS subject_label,
                  j.id::text AS job_id,
                  COALESCE(j.concept_text, '') AS topic_text,
                  j.status,
                  j.created_at,
                  j.updated_at
                FROM jobs j
                {where_sql}
                ORDER BY j.created_at DESC
                """,
                *params,
            )
            items.extend(dict(row) for row in rows)

        if include_operations:
            where = [
                "subject_type = 'operation'",
                "NOT (process = 'daily_fixed_allocation' OR provider IN ('aws_fixed', 'runpod_fixed'))",
            ]
            params = []
            if from_date:
                start_utc, _ = _kst_day_range(from_date)
                params.append(start_utc)
                where.append(f"created_at >= ${len(params)}")
            if to_date:
                _, end_utc = _kst_day_range(to_date)
                params.append(end_utc)
                where.append(f"created_at < ${len(params)}")
            if search:
                params.append(f"%{search}%")
                where.append(
                    f"(subject_key ILIKE ${len(params)} OR COALESCE(subject_label,'') ILIKE ${len(params)} OR COALESCE(topic_text,'') ILIKE ${len(params)})"
                )
            where_sql = "WHERE " + " AND ".join(where)
            count_row = await conn.fetchrow(
                f"SELECT COUNT(DISTINCT subject_key)::int AS cnt FROM cost_events {where_sql}",
                *params,
            )
            total_operations = int(count_row["cnt"] if count_row else 0)
            rows = await conn.fetch(
                f"""
                SELECT
                  'operation' AS subject_type,
                  subject_key,
                  COALESCE(MAX(NULLIF(subject_label, '')), MAX(NULLIF(topic_text, '')), subject_key) AS subject_label,
                  '' AS job_id,
                  COALESCE(MAX(NULLIF(topic_text, '')), MAX(NULLIF(subject_label, '')), subject_key) AS topic_text,
                  '' AS status,
                  MAX(created_at) AS created_at,
                  MAX(created_at) AS updated_at
                FROM cost_events
                {where_sql}
                GROUP BY subject_key
                ORDER BY MAX(created_at) DESC
                """,
                *params,
            )
            items.extend(dict(row) for row in rows)

        job_keys = [str(item["subject_key"]) for item in items if item.get("subject_type") == "job"]
        operation_keys = [str(item["subject_key"]) for item in items if item.get("subject_type") == "operation"]
        event_map: dict[tuple[str, str], list[dict[str, Any]]] = {}
        event_map.update(await _fetch_subject_events(conn, subject_type="job", subject_keys=job_keys))
        event_map.update(await _fetch_subject_events(conn, subject_type="operation", subject_keys=operation_keys))

    enriched: list[dict[str, Any]] = []
    for item in items:
        subject_key = str(item.get("subject_key") or "")
        subject_type_value = str(item.get("subject_type") or "")
        events = event_map.get((subject_type_value, subject_key), [])
        visible_events = _visible_cost_events(events)
        summary = _summarize_events(visible_events)
        stage_counts = {
            "script_success": sum(1 for event in visible_events if event.get("stage") == "script" and event.get("status") == "success"),
            "script_failed": sum(1 for event in visible_events if event.get("stage") == "script" and event.get("status") == "failed"),
            "tts_success": sum(1 for event in visible_events if event.get("stage") == "tts" and event.get("status") == "success"),
            "tts_failed": sum(1 for event in visible_events if event.get("stage") == "tts" and event.get("status") == "failed"),
            "video_success": sum(1 for event in visible_events if event.get("stage") == "video" and event.get("status") == "success"),
            "video_failed": sum(1 for event in visible_events if event.get("stage") == "video" and event.get("status") == "failed"),
        }
        enriched.append(
            {
                **item,
                **stage_counts,
                **summary,
            }
        )
    sorted_items = _sort_job_summary_items(enriched, sort_by=normalized_sort_by, sort_dir=normalized_sort_dir)
    selected = sorted_items[offset_value : offset_value + limit_value]

    return {
        "total": total_jobs + total_operations,
        "limit": limit_value,
        "offset": offset_value,
        "subject_type": normalized_subject_type,
        "sort_by": normalized_sort_by,
        "sort_dir": normalized_sort_dir,
        "daily_estimate": daily_estimate,
        "items": selected,
    }


async def get_job_detail(subject_key: str) -> dict[str, Any]:
    pool = await job_service.get_db_pool()
    async with pool.acquire() as conn:
        job = await conn.fetchrow(
            """
            SELECT
              'job' AS subject_type,
              id::text AS subject_key,
              COALESCE(concept_text, '') AS subject_label,
              id::text AS job_id,
              status,
              COALESCE(concept_text, '') AS topic_text,
              created_at,
              updated_at
            FROM jobs
            WHERE id::text = $1
            """,
            subject_key,
        )
        subject_row = job
        if job is None:
            subject_row = await conn.fetchrow(
                """
                SELECT
                  'operation' AS subject_type,
                  subject_key,
                  COALESCE(MAX(NULLIF(subject_label, '')), MAX(NULLIF(topic_text, '')), subject_key) AS subject_label,
                  '' AS job_id,
                  '' AS status,
                  COALESCE(MAX(NULLIF(topic_text, '')), MAX(NULLIF(subject_label, '')), subject_key) AS topic_text,
                  MIN(created_at) AS created_at,
                  MAX(created_at) AS updated_at
                FROM cost_events
                WHERE subject_type = 'operation'
                  AND subject_key = $1
                GROUP BY subject_key
                """,
                subject_key,
            )
        if subject_row is None:
            raise RuntimeError("subject not found")
        subject_dict = dict(subject_row)
        subject_type_value = str(subject_dict.get("subject_type") or "")
        events = await conn.fetch(
            """
            SELECT
              id::text AS event_id,
              COALESCE(job_id::text, '') AS job_id,
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
              pricing_kind,
              pricing_source,
              api_key_family,
              subject_type,
              subject_key,
              subject_label,
              cost_krw,
              error_type,
              error_message,
              created_at
            FROM cost_events
            WHERE subject_type = $1
              AND subject_key = $2
            ORDER BY created_at ASC
            """,
            subject_type_value,
            subject_key,
        )
    event_dicts = _visible_cost_events([dict(event) for event in events])
    return {
        "subject": subject_dict,
        "summary": _summarize_events(event_dicts),
        "events": event_dicts,
    }


async def export_payload(
    *,
    job_id: str = "",
    from_date: Optional[date] = None,
    to_date: Optional[date] = None,
    subject_type: str = "all",
) -> dict[str, Any]:
    if job_id.strip():
        detail = await get_job_detail(job_id.strip())
        return {
            "meta": {
                "mode": "subject",
                "subject_key": job_id.strip(),
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
        subject_type=subject_type,
    )
    return {
        "meta": {
            "mode": "range",
            "from_date": from_date.isoformat() if from_date else "",
            "to_date": to_date.isoformat() if to_date else "",
            "subject_type": subject_type,
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "usd_krw_rate": settings.cost_usd_krw_rate,
        },
        "data": summary,
    }
