import argparse
import asyncio

import asyncpg


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Backfill job statuses so PUBLISHED only means final platform publication."
    )
    parser.add_argument("--db-host", default="postgres")
    parser.add_argument("--db-port", type=int, default=5432)
    parser.add_argument("--db-name", default="ai_influencer")
    parser.add_argument("--db-user", default="aiuser")
    parser.add_argument("--db-password", required=True)
    parser.add_argument("--apply", action="store_true", help="Apply updates. Without this flag, only show dry-run output.")
    parser.add_argument("--sample-limit", type=int, default=20)
    return parser.parse_args()


BACKFILL_CTE = """
WITH job_flags AS (
  SELECT
    j.id,
    j.status AS old_status,
    COALESCE(j.concept_text, '') AS concept_text,
    COALESCE(j.script_json->'wf13_auto'->>'state', '') AS wf13_state,
    EXISTS (
      SELECT 1 FROM platform_posts p
      WHERE p.job_id = j.id
        AND p.platform = 'youtube'
        AND COALESCE(p.status, '') = 'published'
    ) AS has_youtube_published,
    EXISTS (
      SELECT 1 FROM cost_events ce
      WHERE ce.job_id = j.id
        AND ce.stage = 'tts'
        AND ce.status = 'failed'
    ) AS has_tts_failed,
    EXISTS (
      SELECT 1 FROM cost_events ce
      WHERE ce.job_id = j.id
        AND ce.stage = 'video'
        AND ce.status = 'failed'
    ) AS has_video_failed,
    EXISTS (
      SELECT 1 FROM cost_events ce
      WHERE ce.job_id = j.id
        AND ce.stage = 'video'
        AND ce.process = 'heygen_generate'
        AND ce.status = 'success'
    ) AS has_heygen_success,
    j.updated_at
  FROM jobs j
  WHERE j.status IN ('PUBLISHED', 'WAITING_VIDEO_APPROVAL')
),
classified AS (
  SELECT
    *,
    CASE
      WHEN has_youtube_published THEN old_status
      WHEN wf13_state IN ('failed', 'blocked') OR has_tts_failed OR has_video_failed THEN 'PUBLISH_FAILED'
      WHEN has_heygen_success THEN 'WAITING_VIDEO_APPROVAL'
      ELSE 'REPORT_READY'
    END AS new_status
  FROM job_flags
),
changes AS (
  SELECT *
  FROM classified
  WHERE NOT has_youtube_published
    AND old_status <> new_status
)
"""


async def fetch_summary(conn: asyncpg.Connection) -> list[asyncpg.Record]:
    return await conn.fetch(
        BACKFILL_CTE
        + """
        SELECT old_status, new_status, COUNT(*)::int AS rows
        FROM changes
        GROUP BY old_status, new_status
        ORDER BY old_status, new_status
        """
    )


async def fetch_samples(conn: asyncpg.Connection, *, limit: int) -> list[asyncpg.Record]:
    return await conn.fetch(
        BACKFILL_CTE
        + """
        SELECT
          id::text AS job_id,
          old_status,
          new_status,
          wf13_state,
          has_tts_failed,
          has_video_failed,
          has_heygen_success,
          updated_at,
          LEFT(concept_text, 90) AS title
        FROM changes
        ORDER BY updated_at DESC, id
        LIMIT $1
        """,
        max(1, limit),
    )


async def apply_backfill(conn: asyncpg.Connection) -> list[asyncpg.Record]:
    return await conn.fetch(
        BACKFILL_CTE
        + """
        UPDATE jobs j
        SET status = changes.new_status,
            updated_at = NOW()
        FROM changes
        WHERE j.id = changes.id
        RETURNING
          j.id::text AS job_id,
          changes.old_status,
          changes.new_status,
          j.updated_at
        """
    )


async def main() -> None:
    args = parse_args()
    conn = await asyncpg.connect(
        user=args.db_user,
        password=args.db_password,
        database=args.db_name,
        host=args.db_host,
        port=args.db_port,
    )
    try:
        summary = await fetch_summary(conn)
        total = sum(int(row["rows"] or 0) for row in summary)
        print(f"[dry-run] matched_rows={total}")
        for row in summary:
            print(f"[dry-run] {row['old_status']} -> {row['new_status']}: {row['rows']}")
        samples = await fetch_samples(conn, limit=args.sample_limit)
        for row in samples:
            print(
                "[sample] "
                f"job={row['job_id'][:8]} {row['old_status']}->{row['new_status']} "
                f"wf13={row['wf13_state'] or '-'} tts_failed={row['has_tts_failed']} "
                f"video_failed={row['has_video_failed']} heygen_success={row['has_heygen_success']} "
                f"updated_at={row['updated_at']} title={row['title']}"
            )
        if not args.apply:
            return
        async with conn.transaction():
            updated = await apply_backfill(conn)
        print(f"[applied] updated_rows={len(updated)}")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
