import asyncio
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI
from pydantic import BaseModel

from app import db
from app.agent import handle_message

HOLDING_REPLY = "Aapka message mil gaya hai, hum check kar rahe hain. Thodi der mein jawab denge."
SAFE_ERROR_REPLY = "Sorry, abhi system mein dikkat hai. Aapki complaint ops team ko bhej di hai."
STALE_AFTER_SECONDS = 60


class MessageIn(BaseModel):
    message_id: str
    rider_id: str
    text: str
    received_at: datetime


@asynccontextmanager
async def lifespan(app):
    await db.init_db()
    yield
    await db.pool.close()


app = FastAPI(lifespan=lifespan)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/messages")
async def post_message(msg: MessageIn):
    if not await claim_message(msg):
        reply = await wait_for_reply(msg.message_id)
        await db.add_trace(msg.rider_id, "decision", "duplicate_message_ignored",
                           {"message_id": msg.message_id}, {"reply": reply})
        return {"reply": reply}

    try:
        async with db.rider_lock(msg.rider_id) as lock_conn:
            await db.add_trace(msg.rider_id, "message_in", "rider_message", {
                "message_id": msg.message_id, "text": msg.text,
                "received_at": msg.received_at.isoformat(),
            })
            try:
                reply = await handle_message(msg)
            except Exception as e:
                await db.add_trace(msg.rider_id, "error", "agent_failed", None, {"error": repr(e)})
                reply = SAFE_ERROR_REPLY
            await finish_message(lock_conn, msg.message_id, reply)
            await db.add_trace(msg.rider_id, "reply", "reply_sent", None, reply)
    except TimeoutError as e:
        await db.add_trace(msg.rider_id, "error", "rider_lock_timeout", None, {"error": str(e)})
        reply = HOLDING_REPLY
        async with db.pool.connection() as conn:
            await finish_message(conn, msg.message_id, reply)
    return {"reply": reply}


async def claim_message(msg) -> bool:
    """True if this request owns the message and should process it.

    Wins if it inserts the message_id first, or if the existing row has been
    stuck in 'processing' for over a minute (the first attempt died). Both
    are single atomic statements, so only one request can win either way.
    """
    async with db.pool.connection() as conn:
        cur = await conn.execute(
            "INSERT INTO messages (message_id, rider_id, text, received_at) "
            "VALUES (%s, %s, %s, %s) ON CONFLICT (message_id) DO NOTHING RETURNING message_id",
            (msg.message_id, msg.rider_id, msg.text, msg.received_at),
        )
        if await cur.fetchone():
            return True
        cur = await conn.execute(
            "UPDATE messages SET created_at = now() "
            "WHERE message_id = %s AND status = 'processing' "
            "AND created_at < now() - make_interval(secs => %s) RETURNING message_id",
            (msg.message_id, STALE_AFTER_SECONDS),
        )
        taken_over = await cur.fetchone() is not None
    if taken_over:
        await db.add_trace(msg.rider_id, "decision", "stale_message_reprocessed",
                           {"message_id": msg.message_id}, None)
    return taken_over


async def finish_message(conn, message_id, reply):
    await conn.execute(
        "UPDATE messages SET status = 'done', reply = %s WHERE message_id = %s",
        (reply, message_id),
    )


async def wait_for_reply(message_id, max_wait=7.0):
    """A resend arrived. Wait for the original to finish and return its reply."""
    waited = 0.0
    while waited < max_wait:
        async with db.pool.connection() as conn:
            cur = await conn.execute(
                "SELECT status, reply FROM messages WHERE message_id = %s", (message_id,))
            row = await cur.fetchone()
        if row and row["status"] == "done":
            return row["reply"]
        await asyncio.sleep(0.3)
        waited += 0.3
    return HOLDING_REPLY
