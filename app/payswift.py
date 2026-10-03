"""Thin PaySwift client that turns every HTTP outcome into one of four states.

processed  money definitely sent
unknown    may or may not have been sent (timeout, 504, in progress); retry with the same key
blocked    rate limited / account blocked; retry later, slowly
rejected   will never succeed as-is (400, idempotency key reused); a human must look
"""
import asyncio
import os
import time
from dataclasses import asdict, dataclass

import httpx

BASE_URL = os.environ.get("PAYSWIFT_BASE_URL", "http://localhost:8081")
_client = httpx.AsyncClient(base_url=BASE_URL)


@dataclass
class PayoutResult:
    state: str
    payout_id: str | None = None
    http_status: int | None = None
    detail: str = ""

    def to_dict(self):
        return asdict(self)


def make_reference(key, items) -> str:
    """items: {item_id: rupees}. Lets us reconcile per item from PaySwift alone."""
    return f"qd|{key}|" + ",".join(f"{k}={v}" for k, v in sorted(items.items()))


def parse_reference(ref):
    """-> (key, {item_id: rupees}) or None for payouts we didn't make."""
    parts = (ref or "").split("|")
    if len(parts) != 3 or parts[0] != "qd":
        return None
    items = {}
    for pair in filter(None, parts[2].split(",")):
        k, _, v = pair.partition("=")
        items[k] = int(v)
    return parts[1], items


async def create_payout(rider_id, amount, key, reference, timeout) -> PayoutResult:
    deadline = time.monotonic() + timeout
    body = {"rider_id": rider_id, "amount": amount, "reference": reference}
    for _ in range(3):
        remaining = deadline - time.monotonic()
        if remaining < 0.3:
            return PayoutResult("unknown", detail="out of time budget")
        try:
            r = await _client.post("/v1/payouts", json=body,
                                   headers={"Idempotency-Key": key}, timeout=remaining)
        except httpx.TimeoutException:
            return PayoutResult("unknown", detail="timed out; payout may exist")
        except httpx.TransportError as e:
            return PayoutResult("unknown", detail=f"connection problem: {e!r}")

        error = _error_code(r)
        if r.status_code in (200, 201):
            data = r.json()
            return PayoutResult("processed", data.get("payout_id"), r.status_code, data.get("status", ""))
        if r.status_code == 503:
            await asyncio.sleep(0.3)  # not created; safe to retry with the same key
            continue
        if r.status_code == 409 and error == "request_in_progress":
            return PayoutResult("unknown", http_status=409, detail=error)
        if r.status_code == 409 or r.status_code == 400:
            return PayoutResult("rejected", http_status=r.status_code, detail=error or r.text[:200])
        if r.status_code == 429:
            return PayoutResult("blocked", http_status=429, detail=error or "rate limited")
        return PayoutResult("unknown", http_status=r.status_code, detail=error or r.text[:200])
    return PayoutResult("unknown", http_status=503, detail="service unavailable after retries")


async def list_payouts(rider_id, timeout=3.0) -> list:
    r = await _client.get("/v1/payouts", params={"rider_id": rider_id}, timeout=timeout)
    r.raise_for_status()
    return r.json().get("data", [])


def refunded_by_item(payouts) -> dict:
    """What PaySwift says we already sent, per trip / incentive item."""
    out = {}
    for p in payouts:
        if p.get("status") != "processed":
            continue
        parsed = parse_reference(p.get("reference"))
        if parsed:
            for item, amount in parsed[1].items():
                out[item] = out.get(item, 0) + amount
    return out


def find_by_key(payouts, key):
    for p in payouts:
        parsed = parse_reference(p.get("reference"))
        if parsed and parsed[0] == key and p.get("status") == "processed":
            return p
    return None


def _error_code(r):
    try:
        return r.json().get("error")
    except Exception:
        return None
