import asyncio
import os
from contextlib import asynccontextmanager
from pathlib import Path

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://qd:qd@localhost:5433/qd")

pool = AsyncConnectionPool(
    DATABASE_URL,
    min_size=2,
    max_size=20,
    open=False,
    kwargs={"row_factory": dict_row, "autocommit": True},
)


async def init_db():
    await pool.open()
    schema = (Path(__file__).parent / "schema.sql").read_text()
    async with pool.connection() as conn:
        await conn.execute(schema)


async def add_trace(rider_id, type_, name, input_=None, output=None):
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO trace_steps (rider_id, type, name, input, output) "
            "VALUES (%s, %s, %s, %s, %s)",
            (rider_id, type_, name, Jsonb(input_), Jsonb(output)),
        )


@asynccontextmanager
async def rider_lock(rider_id, max_wait=20.0):
    """Session-level advisory lock, one rider at a time.

    Waiters poll with pg_try_advisory_lock and give their connection back
    between tries, so a queue of waiters can never starve the lock holder
    of pool connections. Raises TimeoutError after max_wait.
    """
    waited = 0.0
    while True:
        conn = await pool.getconn()
        cur = await conn.execute("SELECT pg_try_advisory_lock(hashtext(%s)) AS ok", (rider_id,))
        if (await cur.fetchone())["ok"]:
            break
        await pool.putconn(conn)
        if waited >= max_wait:
            raise TimeoutError(f"rider lock busy for {rider_id}")
        await asyncio.sleep(0.2)
        waited += 0.2
    try:
        yield conn
    finally:
        try:
            await conn.execute("SELECT pg_advisory_unlock(hashtext(%s))", (rider_id,))
        finally:
            await pool.putconn(conn)
