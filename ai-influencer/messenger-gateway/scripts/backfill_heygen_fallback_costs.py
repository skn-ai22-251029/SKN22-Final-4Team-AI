import argparse
import asyncio
from decimal import Decimal

import asyncpg


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Backfill historical HeyGen fallback cost events that were recorded with an incorrect fixed USD value."
    )
    parser.add_argument("--db-host", default="postgres")
    parser.add_argument("--db-port", type=int, default=5432)
    parser.add_argument("--db-name", default="ai_influencer")
    parser.add_argument("--db-user", default="aiuser")
    parser.add_argument("--db-password", required=True)
    parser.add_argument("--old-cost", type=Decimal, default=Decimal("5.0"))
    parser.add_argument("--new-cost", type=Decimal, default=Decimal("0.8"))
    parser.add_argument("--apply", action="store_true", help="Apply the update. Without this flag, only show a dry-run summary.")
    return parser.parse_args()


async def summarize(conn: asyncpg.Connection, *, old_cost: Decimal) -> asyncpg.Record:
    return await conn.fetchrow(
        """
        SELECT
          COUNT(*)::int AS rows,
          COALESCE(SUM(cost_usd), 0)::numeric AS total_usd,
          MIN(created_at) AS first_at,
          MAX(created_at) AS last_at
        FROM cost_events
        WHERE process = 'heygen_generate'
          AND pricing_source = 'config_fallback'
          AND cost_usd = $1
        """,
        old_cost,
    )


async def apply_backfill(conn: asyncpg.Connection, *, old_cost: Decimal, new_cost: Decimal) -> list[asyncpg.Record]:
    return await conn.fetch(
        """
        UPDATE cost_events
        SET
          cost_usd = $2,
          cost_krw = CASE
            WHEN usd_krw_rate IS NULL THEN NULL
            ELSE ROUND(($2::numeric * usd_krw_rate)::numeric, 3)
          END
        WHERE process = 'heygen_generate'
          AND pricing_source = 'config_fallback'
          AND cost_usd = $1
        RETURNING id::text, job_id::text, created_at, cost_usd, cost_krw
        """,
        old_cost,
        new_cost,
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
        before = await summarize(conn, old_cost=args.old_cost)
        rows = int(before["rows"] or 0)
        print(
            f"[dry-run] matched_rows={rows} total_usd={before['total_usd']} "
            f"first_at={before['first_at']} last_at={before['last_at']}"
        )
        if not args.apply:
            return
        async with conn.transaction():
            updated = await apply_backfill(conn, old_cost=args.old_cost, new_cost=args.new_cost)
        print(
            f"[applied] updated_rows={len(updated)} old_cost={args.old_cost} new_cost={args.new_cost} "
            f"usd_delta={(args.new_cost - args.old_cost) * rows}"
        )
        after = await summarize(conn, old_cost=args.old_cost)
        print(
            f"[after] remaining_old_cost_rows={after['rows']} total_usd={after['total_usd']}"
        )
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
