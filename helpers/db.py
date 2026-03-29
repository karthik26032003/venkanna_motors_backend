import logging
import os

import asyncpg

logger = logging.getLogger("db")

_pool: asyncpg.Pool | None = None


def get_concurrency() -> int:
    return int(os.getenv("BATCH_CONCURRENCY", "3"))


async def init_pool() -> None:
    global _pool
    dsn = os.getenv("DATABASE_URL", "").strip()
    if not dsn:
        logger.warning("DATABASE_URL not set — DB features disabled")
        return
    _pool = await asyncpg.create_pool(dsn=dsn, min_size=2, max_size=10)
    logger.info("DB pool initialised")


async def close_pool() -> None:
    global _pool
    if _pool:
        await _pool.close()
        logger.info("DB pool closed")


def get_pool() -> asyncpg.Pool | None:
    return _pool


CREATE_BATCHES = """
CREATE TABLE IF NOT EXISTS batches (
    batch_id    TEXT        PRIMARY KEY,
    agent_id    TEXT        NOT NULL,
    from_number TEXT        NOT NULL,
    total       INT         NOT NULL DEFAULT 0,
    queued      INT         NOT NULL DEFAULT 0,
    active      INT         NOT NULL DEFAULT 0,
    succeeded   INT         NOT NULL DEFAULT 0,
    failed      INT         NOT NULL DEFAULT 0,
    status      TEXT        NOT NULL DEFAULT 'running',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""

CREATE_VENKANNA_CALLS = """
CREATE TABLE IF NOT EXISTS venkanna_calls (
    id            BIGSERIAL   PRIMARY KEY,
    batch_id      TEXT        NOT NULL REFERENCES batches(batch_id),
    phone_number  TEXT        NOT NULL,
    customer_name TEXT        NOT NULL DEFAULT '',
    vehicle       TEXT        NOT NULL DEFAULT '',
    call_id       TEXT,
    status        TEXT        NOT NULL DEFAULT 'queued',
    error         TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_venkanna_calls_batch_id ON venkanna_calls(batch_id);
CREATE INDEX IF NOT EXISTS idx_venkanna_calls_call_id  ON venkanna_calls(call_id);
CREATE INDEX IF NOT EXISTS idx_venkanna_calls_status   ON venkanna_calls(batch_id, status);
"""


async def create_tables() -> None:
    pool = get_pool()
    if not pool:
        return
    async with pool.acquire() as conn:
        await conn.execute(CREATE_BATCHES)
        await conn.execute(CREATE_VENKANNA_CALLS)
        await conn.execute(
            "ALTER TABLE batches ADD COLUMN IF NOT EXISTS name TEXT NOT NULL DEFAULT ''"
        )
        await conn.execute(
            "ALTER TABLE venkanna_calls ADD COLUMN IF NOT EXISTS customer_name TEXT NOT NULL DEFAULT ''"
        )
        await conn.execute(
            "ALTER TABLE venkanna_calls ADD COLUMN IF NOT EXISTS vehicle TEXT NOT NULL DEFAULT ''"
        )
        await conn.execute(
            "ALTER TABLE venkanna_calls ADD COLUMN IF NOT EXISTS transcript TEXT NOT NULL DEFAULT ''"
        )
        await conn.execute(
            "ALTER TABLE venkanna_calls ADD COLUMN IF NOT EXISTS sentiment TEXT NOT NULL DEFAULT ''"
        )
        await conn.execute(
            "ALTER TABLE venkanna_calls ADD COLUMN IF NOT EXISTS takeaway TEXT NOT NULL DEFAULT ''"
        )
        await conn.execute(
            "ALTER TABLE venkanna_calls ADD COLUMN IF NOT EXISTS callback BOOLEAN NOT NULL DEFAULT FALSE"
        )
    logger.info("DB tables ready")


async def create_batch(
    batch_id: str,
    agent_id: str,
    from_number: str,
    total: int,
    name: str = "",
) -> None:
    pool = get_pool()
    if not pool:
        return
    await pool.execute(
        """
        INSERT INTO batches (batch_id, agent_id, from_number, total, queued, status, name)
        VALUES ($1, $2, $3, $4, $4, 'running', $5)
        """,
        batch_id, agent_id, from_number, total, name,
    )


async def insert_batch_calls(
    batch_id: str,
    contacts: list[dict],  # each dict: {phone_number, name, vehicle}
) -> None:
    pool = get_pool()
    if not pool:
        return
    rows = [
        (batch_id, c["phone_number"], c.get("name", ""), c.get("vehicle", ""))
        for c in contacts
    ]
    await pool.executemany(
        """
        INSERT INTO venkanna_calls (batch_id, phone_number, customer_name, vehicle)
        VALUES ($1, $2, $3, $4)
        """,
        rows,
    )


async def pop_next_queued(batch_id: str) -> dict | None:
    """
    Returns {phone_number, customer_name, vehicle} for the next queued call,
    or None if the queue is empty.
    """
    pool = get_pool()
    if not pool:
        return None
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            UPDATE venkanna_calls
            SET    status     = 'initiated',
                   updated_at = NOW()
            WHERE  id = (
                SELECT id FROM venkanna_calls
                WHERE  batch_id = $1 AND status = 'queued'
                ORDER  BY id ASC
                LIMIT  1
                FOR UPDATE SKIP LOCKED
            )
            RETURNING id, phone_number, customer_name, vehicle
            """,
            batch_id,
        )
        if row:
            await conn.execute(
                """
                UPDATE batches
                SET    active     = active + 1,
                       queued     = queued - 1,
                       updated_at = NOW()
                WHERE  batch_id = $1
                """,
                batch_id,
            )
            return {
                "phone_number":  row["phone_number"],
                "customer_name": row["customer_name"],
                "vehicle":       row["vehicle"],
            }
        return None


async def update_call_status_by_phone(
    batch_id: str,
    phone_number: str,
    status: str,
    error: str | None = None,
) -> None:
    pool = get_pool()
    if not pool:
        return
    await pool.execute(
        """
        UPDATE venkanna_calls
        SET    status     = $1,
               error      = $2,
               updated_at = NOW()
        WHERE  batch_id     = $3
          AND  phone_number = $4
          AND  status       = 'initiated'
        """,
        status, error, batch_id, phone_number,
    )


async def set_call_id(batch_id: str, phone_number: str, call_id: str) -> None:
    pool = get_pool()
    if not pool:
        return
    await pool.execute(
        """
        UPDATE venkanna_calls
        SET    call_id    = $1,
               updated_at = NOW()
        WHERE  batch_id     = $2
          AND  phone_number = $3
          AND  status       = 'initiated'
        """,
        call_id, batch_id, phone_number,
    )


async def update_call_status(
    call_id: str,
    status: str,
    error: str | None = None,
) -> str | None:
    pool = get_pool()
    if not pool:
        return None
    row = await pool.fetchrow(
        """
        UPDATE venkanna_calls
        SET    status     = $1,
               error      = $2,
               updated_at = NOW()
        WHERE  call_id = $3
        RETURNING batch_id
        """,
        status, error, call_id,
    )
    return row["batch_id"] if row else None


async def close_call_on_batch(
    batch_id: str,
    succeeded: bool,
) -> dict:
    pool = get_pool()
    if not pool:
        return {}
    col = "succeeded" if succeeded else "failed"
    row = await pool.fetchrow(
        f"""
        UPDATE batches
        SET    active     = active - 1,
               {col}     = {col} + 1,
               updated_at = NOW()
        WHERE  batch_id = $1
        RETURNING *
        """,
        batch_id,
    )
    return dict(row) if row else {}


async def mark_batch_complete(batch_id: str) -> None:
    pool = get_pool()
    if not pool:
        return
    await pool.execute(
        """
        UPDATE batches
        SET    status     = 'completed',
               updated_at = NOW()
        WHERE  batch_id = $1
        """,
        batch_id,
    )


async def get_batch(batch_id: str) -> dict | None:
    pool = get_pool()
    if not pool:
        return None
    row = await pool.fetchrow(
        "SELECT * FROM batches WHERE batch_id = $1",
        batch_id,
    )
    return dict(row) if row else None


async def list_batches() -> list[dict]:
    pool = get_pool()
    if not pool:
        return []
    rows = await pool.fetch("SELECT * FROM batches ORDER BY created_at DESC")
    return [dict(r) for r in rows]


async def get_batch_calls(batch_id: str) -> list[dict]:
    pool = get_pool()
    if not pool:
        return []
    rows = await pool.fetch(
        """
        SELECT id, phone_number, customer_name, vehicle, call_id, status, error, created_at
        FROM   venkanna_calls
        WHERE  batch_id = $1
        ORDER  BY id ASC
        """,
        batch_id,
    )
    return [dict(r) for r in rows]


async def get_contact_info_by_call_ids(call_ids: list[str]) -> dict[str, dict]:
    """
    Returns enrichment data for each call_id found in our DB (batch calls only).
    Keys: customer_name, phone_number, vehicle, sentiment, takeaway, callback
    """
    pool = get_pool()
    if not pool or not call_ids:
        return {}
    rows = await pool.fetch(
        """
        SELECT call_id, phone_number, customer_name, vehicle,
               sentiment, takeaway, callback
        FROM   venkanna_calls
        WHERE  call_id = ANY($1::text[])
        """,
        call_ids,
    )
    return {
        r["call_id"]: {
            "phone_number":  r["phone_number"],
            "customer_name": r["customer_name"],
            "vehicle":       r["vehicle"],
            "sentiment":     r["sentiment"],
            "takeaway":      r["takeaway"],
            "callback":      r["callback"],
        }
        for r in rows
    }


async def save_call_analysis(
    call_id:   str,
    transcript: str,
    sentiment:  str,
    takeaway:   str,
    callback:   bool,
) -> None:
    """Persist GPT analysis + formatted transcript for a completed call."""
    pool = get_pool()
    if not pool:
        return
    await pool.execute(
        """
        UPDATE venkanna_calls
        SET    transcript = $1,
               sentiment  = $2,
               takeaway   = $3,
               callback   = $4,
               updated_at = NOW()
        WHERE  call_id = $5
        """,
        transcript, sentiment, takeaway, callback, call_id,
    )


async def delete_batch(batch_id: str) -> bool:
    pool = get_pool()
    if not pool:
        return False
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM venkanna_calls WHERE batch_id = $1", batch_id)
        result = await conn.execute("DELETE FROM batches WHERE batch_id = $1", batch_id)
    return result == "DELETE 1"


async def mark_failed_initiated_calls() -> None:
    pool = get_pool()
    if not pool:
        return
    await pool.execute(
        """
        UPDATE venkanna_calls
        SET    status = 'failed', error = 'server restart', updated_at = NOW()
        WHERE  batch_id IN (SELECT batch_id FROM batches WHERE status = 'running')
          AND  status = 'initiated'
        """,
    )
