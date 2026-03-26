from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")


def build_media_basename(job_id: str, now: datetime | None = None) -> str:
    if not job_id:
        raise ValueError("job_id is required")
    current = now or datetime.now(tz=KST)
    if current.tzinfo is None:
        current = current.replace(tzinfo=KST)
    kst_now = current.astimezone(KST)
    return f"{kst_now.strftime('%Y%m%d')}-{job_id}"


def build_filename(job_id: str, ext: str, now: datetime | None = None) -> str:
    normalized_ext = (ext or "").strip().lstrip(".").lower()
    if not normalized_ext:
        raise ValueError("ext is required")
    return f"{build_media_basename(job_id, now)}.{normalized_ext}"


def normalize_or_fallback(
    job_id: str,
    ext: str,
    candidate_filename: str | None = None,
    now: datetime | None = None,
) -> str:
    expected = build_filename(job_id, ext, now)
    if not candidate_filename:
        return expected
    if candidate_filename == expected:
        return expected
    return expected

