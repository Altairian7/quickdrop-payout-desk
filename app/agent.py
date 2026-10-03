"""The conversation loop for one rider message.

Model decides: what the rider means (extract.py).
Code decides: what is owed, whether to pay, approve or escalate (policy.py, payouts.py),
and the reply wording (templates below), so no number in a reply comes from the model.
"""
import time
from datetime import date

from psycopg.types.json import Jsonb

from app import db, payouts, payswift
from app.data import IST, store
from app.extract import extract
from app.policy import INCENTIVE_TRIPS, check_claim

BUDGET_SECONDS = 9.0
ASK_DETAILS = "Kaunse din ya kaunse order ka payout galat laga? Date ya order ID (jaise T123456) bata dijiye."
ONLY_OWN_ACCOUNT = ("Main sirf is number se jude account ke baare mein madad kar sakta hoon. "
                    "Aapke account ki koi specific problem ho to date ya order ID bataiye.")
STATUS_WORDS = {"paid": "aapke account mein bhej diya gaya hai",
                "in_progress": "process ho raha hai, confirm hote hi bata denge",
                "awaiting_approval": "ops team ke approval ke liye pending hai, approve hote hi aa jayega",
                "rejected": "ops team ne reject kar diya hai",
                "escalated": "ops team dekh rahi hai"}


async def handle_message(msg) -> str:
    deadline = time.monotonic() + BUDGET_SECONDS
    rider = msg.rider_id
    today = msg.received_at.astimezone(IST).date()
    state = await _load_state(rider)
    lines = await _status_updates(rider, state)

    ext, source = await extract(msg.text, today, rider, state.get("asked_for"))
    await db.add_trace(rider, "tool_call", "extract_claims",
                       {"text": msg.text, "today": today.isoformat(), "asked_for": state.get("asked_for")},
                       {"source": source, **ext})

    if ext["injection"] or ext["other_rider_id"]:
        why = ("message tried to override rules" if ext["injection"]
               else f"sender referred to another rider ({ext['other_rider_id']})")
        await payouts.escalate(rider, f"Suspicious message: {why}")
        lines.append(ONLY_OWN_ACCOUNT)
        return await _finish(rider, state, lines, asked_for=None)

    claims = ext["claims"]
    if state.get("asked_for") and state.get("open_issue"):
        for c in claims:  # rider is answering our question about an earlier complaint
            if c["issue_type"] == "general":
                c["issue_type"] = state["open_issue"]
    checkable = [c for c in claims if c["date"] or c["trip_ids"]]

    if not checkable:
        if ext["intent"] == "pushback" and state.get("last_claim"):
            lines += await _recheck(rider, state, today, deadline)
            return await _finish(rider, state, lines, asked_for=None)
        if ext["intent"] in ("status", "pushback") and await _has_disputes(rider):
            lines.append(await _status_summary(rider))
            return await _finish(rider, state, lines, asked_for=None)
        if claims or ext["intent"] in ("complaint", "more_info"):
            await db.add_trace(rider, "decision", "ask_for_details", {"claims": claims},
                               {"reason": "no date or trip id to check"})
            lines.append(ASK_DETAILS)
            state["open_issue"] = claims[0]["issue_type"] if claims else "general"
            return await _finish(rider, state, lines, asked_for="date_or_trip")
        lines.append("Namaste! Payout mein koi problem ho to date ya order ID ke saath bataiye.")
        return await _finish(rider, state, lines, asked_for=None)

    for claim in checkable:
        lines += await _handle_claim(rider, claim, today, deadline, ext["needs_human"])
        state["last_claim"] = claim
    return await _finish(rider, state, lines, asked_for=None)


async def _handle_claim(rider, claim, today, deadline, needs_human, pushback=False) -> list:
    try:
        ledger = await payswift.list_payouts(rider)
        refunded = payswift.refunded_by_item(ledger)
        await db.add_trace(rider, "tool_call", "payswift_list_payouts", {"rider_id": rider},
                           {"count": len(ledger), "refunded_by_item": refunded})
    except Exception as e:
        await db.add_trace(rider, "error", "payswift_list_payouts", {"rider_id": rider}, {"error": repr(e)})
        await payouts.escalate(rider, f"Could not reconcile with PaySwift for claim {claim}")
        return ["Abhi payment system se confirm nahi ho pa raha. Aapki complaint ops team ko bhej di hai, woh check karke batayenge."]

    check = check_claim(store(), rider, claim, today, refunded, await payouts.pending_items(rider))
    await db.add_trace(rider, "tool_call", "check_claim", claim, check.to_dict())
    out = check.outcome

    if out == "unknown_rider":
        await payouts.escalate(rider, "Rider id not found in rider records")
        return ["Aapka account humare records mein nahi mila. Ops team ko bhej diya hai."]
    if out in ("unknown_trip", "not_rider_trip"):
        tid = ", ".join(claim["trip_ids"])
        if out == "not_rider_trip":
            await payouts.escalate(rider, f"Rider asked about {tid}, which belongs to another rider")
        return [f"Order {tid} aapke account pe nahi mila. Order ID dobara check karke bhejiye."]
    if out == "outside_window":
        return [f"Ye {_fmt_day_from(claim, check)} ka mamla hai. Hum sirf pichle 7 din ke payout disputes dekh sakte hain, "
                "isliye ise check nahi kar sakte."]
    if out == "future_date":
        return ["Ye date abhi aayi nahi hai. Sahi date ya order ID bataiye."]
    if out == "need_info":
        return [ASK_DETAILS]

    lines = []
    by_day = check.by_day()
    for day, items in by_day.items():
        facts = "; ".join(_describe(i) for i in items)
        remaining = deadline - time.monotonic() - 0.5
        result = await payouts.open_dispute(rider, day, items, today, claim["issue_type"],
                                            timeout=max(0.5, min(6.0, remaining)))
        lines.append(f"{day:%d %b}: {facts}. {_action_text(result)}")

    if not by_day:
        lines.append(_explain_no_shortfall(check, claim))
        if pushback:
            await payouts.escalate(rider, f"Rider disputes our record after recheck: {claim}")
            lines.append("Agar aapko lagta hai record galat hai, maine ise ops team ko bhej diya hai, woh check karenge.")
        elif needs_human:
            await payouts.escalate(rider, f"Rider gave a personal reason; ops may waive: {claim}")
            lines.append("Aapki baat ops team ko bhej di hai, woh dekhkar decide karenge.")
    return lines


async def _recheck(rider, state, today, deadline):
    claim = state["last_claim"]
    await db.add_trace(rider, "decision", "recheck_after_pushback", claim, None)
    lines = await _handle_claim(rider, claim, today, deadline, False, pushback=True)
    return ["Dobara check kiya:"] + lines


def _describe(i) -> str:
    if i.kind == "incentive":
        n = i.detail["completed_trips"]
        if i.expected:
            return f"aapke {n} completed trips the, ₹{i.expected} incentive banta tha, ₹{i.paid} mila"
        return f"aapke {n} completed trips the; incentive {INCENTIVE_TRIPS} trips pe milta hai"
    d = i.detail
    if d["status"] == "cancelled_by_rider":
        return f"order {i.item_id} aapne cancel kiya tha: ₹10 penalty banti hai, ₹{-i.paid} kata"
    if d["status"] == "cancelled_by_customer":
        return f"order {i.item_id} customer ne cancel kiya tha, is pe payment nahi banta"
    surge = f", {d['surge']}x surge" if d["surge"] > 1 else ""
    text = f"order {i.item_id} ({d['distance_km']} km{surge}): ₹{i.expected} banta tha, ₹{i.paid} mila"
    if i.refunded:
        text += f", ₹{i.refunded} pehle hi bhej diya"
    return text


def _explain_no_shortfall(check, claim) -> str:
    items = check.items
    pending = [i for i in items if i.pending]
    if pending:
        return f"{', '.join(i.item_id for i in pending)} ka payment pehle se process ya approval mein hai."
    if claim["trip_ids"]:
        return "Check kiya: " + "; ".join(_describe(i) for i in items) + ". Isliye koi farak nahi banta."
    day = items[0].day if items else None
    trips = [i for i in items if i.kind == "trip"]
    incentive = next((i for i in items if i.kind == "incentive"), None)
    text = f"{day:%d %b} ke {len(trips)} trips check kiye, sab ka payment sahi mila"
    if incentive:
        text += f". Us din {_describe(incentive)}"
    return text + "."


def _action_text(result) -> str:
    amt, action = result["amount"], result["action"]
    if action == "paid":
        return f"Farak ₹{amt} aapke account mein bhej diya hai."
    if action == "processing":
        return f"Farak ₹{amt} bhej rahe hain, payment process ho raha hai."
    if action == "approval":
        why = ("₹200 se zyada amount ops approve karti hai" if amt > 200
               else "ek din mein ek hi auto-payment ho sakta hai")
        return f"Farak ₹{amt} ops team ke approval ke liye bheja hai ({why}), approve hote hi aa jayega."
    if action == "escalated":
        return f"₹{amt} ka payment abhi nahi ho paya, ops team ko bhej diya hai."
    status = action.removeprefix("already_")
    return f"Is complaint ka ₹{amt} {STATUS_WORDS.get(status, status)}."


def _fmt_day_from(claim, check):
    if claim.get("date"):
        return f"{date.fromisoformat(claim['date']):%d %b}"
    return "order " + ", ".join(claim["trip_ids"])


async def _has_disputes(rider):
    async with db.pool.connection() as conn:
        cur = await conn.execute("SELECT 1 FROM disputes WHERE rider_id = %s LIMIT 1", (rider,))
        return await cur.fetchone() is not None


async def _status_summary(rider) -> str:
    async with db.pool.connection() as conn:
        cur = await conn.execute(
            "SELECT issue_date, amount, status FROM disputes WHERE rider_id = %s ORDER BY id", (rider,))
        rows = await cur.fetchall()
    parts = [f"{r['issue_date']:%d %b} ka ₹{r['amount']} {STATUS_WORDS.get(r['status'], r['status'])}"
             for r in rows]
    await db.add_trace(rider, "decision", "status_update", None, {"disputes": len(rows)})
    return "Aapke payments: " + "; ".join(parts) + "."


async def _status_updates(rider, state) -> list:
    """Tell the rider about payouts that finished since our last reply.
    We can only speak when they message us, so this rides on the next reply."""
    seen = state.get("seen", {})
    async with db.pool.connection() as conn:
        cur = await conn.execute("SELECT id, amount, status FROM disputes WHERE rider_id = %s", (rider,))
        rows = await cur.fetchall()
    lines = []
    for r in rows:
        before = seen.get(str(r["id"]))
        if before and before != r["status"] and r["status"] in ("paid", "rejected", "escalated"):
            lines.append(f"Update: aapka ₹{r['amount']} {STATUS_WORDS[r['status']]}.")
    if lines:
        await db.add_trace(rider, "decision", "notify_status_change", None, {"updates": lines})
    return lines


async def _load_state(rider) -> dict:
    async with db.pool.connection() as conn:
        cur = await conn.execute("SELECT state FROM conversations WHERE rider_id = %s", (rider,))
        row = await cur.fetchone()
    return row["state"] if row else {}


async def _finish(rider, state, lines, asked_for) -> str:
    state["asked_for"] = asked_for
    if not asked_for:
        state.pop("open_issue", None)
    async with db.pool.connection() as conn:
        cur = await conn.execute("SELECT id, status FROM disputes WHERE rider_id = %s", (rider,))
        state["seen"] = {str(r["id"]): r["status"] for r in await cur.fetchall()}
        await conn.execute(
            "INSERT INTO conversations (rider_id, state, updated_at) VALUES (%s, %s, now()) "
            "ON CONFLICT (rider_id) DO UPDATE SET state = EXCLUDED.state, updated_at = now()",
            (rider, Jsonb(state)))
    return "\n".join(lines)
