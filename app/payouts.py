"""Turns a policy shortfall into money: dispute record, auto-pay gate,
PaySwift call, background settlement and ops approve/reject.
"""
import asyncio

from psycopg import errors

from app import db, payswift, policy

RETRY_DELAYS = [2, 5, 15, 30, 60, 120, 300]
_tasks = set()


async def pending_items(rider_id) -> set:
    """Items already being paid or waiting for ops; never open them twice."""
    async with db.pool.connection() as conn:
        cur = await conn.execute(
            "SELECT item_ids FROM disputes WHERE rider_id = %s "
            "AND status IN ('in_progress', 'awaiting_approval', 'escalated')",
            (rider_id,),
        )
        return {i for row in await cur.fetchall() for i in row["item_ids"]}


async def open_dispute(rider_id, day, items, today, issue_type, timeout) -> dict:
    """One dispute = all shortfalls for one rider on one IST day."""
    shortfalls = {i.item_id: i.shortfall for i in items if i.shortfall > 0}
    amount = sum(shortfalls.values())
    key = policy.dispute_key(rider_id, day, shortfalls)
    reference = payswift.make_reference(key, shortfalls)
    row = dict(dispute_key=key, rider_id=rider_id, issue_type=issue_type, issue_date=day,
               item_ids=sorted(shortfalls), amount=amount, reference=reference)

    async with db.pool.connection() as conn:
        cur = await conn.execute("SELECT * FROM disputes WHERE dispute_key = %s", (key,))
        existing = await cur.fetchone()
        if existing:
            return {"action": "already_" + existing["status"], "amount": existing["amount"],
                    "dispute_id": existing["id"]}

        dispute, auto = None, False
        if policy.gate(amount, auto_paid_today=False) == "auto_pay":
            try:
                # the unique index one_autopay_per_day is the once-per-day check
                dispute = await _insert_dispute(conn, row, "in_progress", auto_pay_day=today)
                auto = True
            except errors.UniqueViolation as e:
                if e.diag.constraint_name != "one_autopay_per_day":
                    raise
        if dispute is None:
            reason = (f"₹{amount} is over the ₹{policy.AUTO_PAY_LIMIT} auto-pay limit"
                      if amount > policy.AUTO_PAY_LIMIT
                      else "rider already had one auto-pay today")
            dispute = await _insert_dispute(conn, {**row, "reason": reason}, "awaiting_approval")
            await conn.execute(
                "INSERT INTO ops_items (rider_id, dispute_id, type, amount, reason) "
                "VALUES (%s, %s, 'approval', %s, %s)",
                (rider_id, dispute["id"], amount, f"{reason}. Items: {shortfalls}"),
            )

    await db.add_trace(rider_id, "decision", "auto_pay" if auto else "needs_approval",
                       {"day": day.isoformat(), "items": shortfalls, "amount": amount,
                        "dispute_key": key},
                       {"dispute_id": dispute["id"], "reason": dispute.get("reason")})
    if not auto:
        return {"action": "approval", "amount": amount, "dispute_id": dispute["id"],
                "reason": dispute["reason"]}
    state = await execute_payout(dispute, timeout)
    return {"action": {"processed": "paid", "rejected": "escalated"}.get(state, "processing"),
            "amount": amount, "dispute_id": dispute["id"]}


async def _insert_dispute(conn, row, status, auto_pay_day=None):
    async with conn.transaction():
        cur = await conn.execute(
            "INSERT INTO disputes (dispute_key, rider_id, issue_type, issue_date, item_ids, "
            "amount, reference, reason, status, auto_pay_day) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING *",
            (row["dispute_key"], row["rider_id"], row["issue_type"], row["issue_date"],
             row["item_ids"], row["amount"], row["reference"], row.get("reason"),
             status, auto_pay_day),
        )
        return await cur.fetchone()


async def execute_payout(dispute, timeout) -> str:
    """Call PaySwift with the dispute's fixed key and body; record the outcome."""
    result = await payswift.create_payout(dispute["rider_id"], dispute["amount"],
                                          dispute["dispute_key"], dispute["reference"], timeout)
    await db.add_trace(dispute["rider_id"], "tool_call", "payswift_create_payout",
                       {"amount": dispute["amount"], "idempotency_key": dispute["dispute_key"],
                        "reference": dispute["reference"]},
                       result.to_dict())
    if result.state == "processed":
        await _set_status(dispute["id"], "paid", result.payout_id)
    elif result.state == "rejected":
        await _set_status(dispute["id"], "escalated")
        await escalate(dispute["rider_id"], f"PaySwift rejected payout: {result.detail}",
                       dispute["id"])
    else:
        await _set_status(dispute["id"], "in_progress")
        settle_in_background(dispute["id"])
    return result.state


def settle_in_background(dispute_id):
    task = asyncio.create_task(_settle(dispute_id))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def _settle(dispute_id):
    """Finish a payout whose outcome was unknown. Same key, so it can't double-pay."""
    for delay in RETRY_DELAYS:
        await asyncio.sleep(delay)
        dispute = await _get_dispute(dispute_id)
        if dispute is None or dispute["status"] != "in_progress":
            return
        try:
            found = payswift.find_by_key(await payswift.list_payouts(dispute["rider_id"]),
                                         dispute["dispute_key"])
        except Exception:
            found = None
        if found:
            await db.add_trace(dispute["rider_id"], "tool_call", "payswift_confirm_payout",
                               {"idempotency_key": dispute["dispute_key"]}, found)
            await _set_status(dispute_id, "paid", found["payout_id"])
            return
        result = await payswift.create_payout(dispute["rider_id"], dispute["amount"],
                                              dispute["dispute_key"], dispute["reference"], 10)
        await db.add_trace(dispute["rider_id"], "tool_call", "payswift_retry_payout",
                           {"idempotency_key": dispute["dispute_key"]}, result.to_dict())
        if result.state == "processed":
            await _set_status(dispute_id, "paid", result.payout_id)
            return
        if result.state == "rejected":
            await _set_status(dispute_id, "escalated")
            await escalate(dispute["rider_id"], f"PaySwift rejected payout: {result.detail}",
                           dispute_id)
            return
    await _set_status(dispute_id, "escalated")
    await escalate(dispute["rider_id"], "payout outcome still unknown after retries", dispute_id)


async def resume_in_progress():
    """On startup, pick up payouts a previous process left unfinished."""
    async with db.pool.connection() as conn:
        cur = await conn.execute("SELECT id FROM disputes WHERE status = 'in_progress'")
        for row in await cur.fetchall():
            settle_in_background(row["id"])


async def escalate(rider_id, reason, dispute_id=None, amount=None):
    async with db.pool.connection() as conn:
        cur = await conn.execute(
            "SELECT id FROM ops_items WHERE rider_id = %s AND type = 'escalation' "
            "AND status = 'pending' AND reason = %s", (rider_id, reason))
        if await cur.fetchone():
            return
        await conn.execute(
            "INSERT INTO ops_items (rider_id, dispute_id, type, amount, reason) "
            "VALUES (%s, %s, 'escalation', %s, %s)", (rider_id, dispute_id, amount, reason))
    await db.add_trace(rider_id, "decision", "escalated_to_ops", {"dispute_id": dispute_id},
                       {"reason": reason})


async def resolve_ops_item(item_id, approve: bool) -> dict:
    async with db.pool.connection() as conn:
        cur = await conn.execute(
            "UPDATE ops_items SET status = %s, resolved_at = now() "
            "WHERE id = %s AND status = 'pending' RETURNING *",
            ("approved" if approve else "rejected", item_id))
        item = await cur.fetchone()
    if item is None:
        return {"ok": False, "error": "not found or already resolved"}
    await db.add_trace(item["rider_id"], "decision",
                       "ops_approved" if approve else "ops_rejected",
                       {"ops_item_id": item_id, "type": item["type"]}, {"reason": item["reason"]})
    result = {"ok": True, "item": item["id"]}
    if item["type"] == "approval" and item["dispute_id"]:
        if approve:
            await _set_status(item["dispute_id"], "in_progress")
            result["payout"] = await execute_payout(await _get_dispute(item["dispute_id"]), 10)
        else:
            await _set_status(item["dispute_id"], "rejected")
    return result


async def _get_dispute(dispute_id):
    async with db.pool.connection() as conn:
        cur = await conn.execute("SELECT * FROM disputes WHERE id = %s", (dispute_id,))
        return await cur.fetchone()


async def _set_status(dispute_id, status, payout_id=None):
    async with db.pool.connection() as conn:
        await conn.execute(
            "UPDATE disputes SET status = %s, payout_id = COALESCE(%s, payout_id) WHERE id = %s",
            (status, payout_id, dispute_id))
