"""Run test conversations against a dispute-desk service and print a report.

Usage: python evals/run.py http://localhost:8000 [--payswift http://localhost:8081] [--cases FILE]

Uses only the public interface (POST /messages, GET /trace, GET /ops/pending)
plus PaySwift, so it can be pointed at any implementation.

Payout is measured as the change in the rider's PaySwift total during the
conversation. A rider who already appeared earlier in the run may get 0 new
money for the same dispute; that counts as correct if their total since the
start of the run equals what this conversation expects (no double payment).
Run against a fresh system for exact results.
"""
import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
SETTLE_SECONDS = 15  # how long to wait for in-progress payouts to land


def payswift_total(ps, rider):
    r = ps.get("/v1/payouts", params={"rider_id": rider})
    r.raise_for_status()
    return sum(p["amount"] for p in r.json()["data"] if p.get("status") == "processed")


def pending(svc):
    r = svc.get("/ops/pending")
    r.raise_for_status()
    return r.json()


def send(svc, rider, turn):
    body = {"message_id": turn["message_id"], "rider_id": rider,
            "text": turn["text"], "received_at": turn["received_at"]}
    t0 = time.monotonic()
    r = svc.post("/messages", json=body, timeout=30)
    return r, time.monotonic() - t0


def run_case(svc, ps, case, run_start_totals):
    rider = case["rider_id"]
    exp = case["expected"]
    before_total = payswift_total(ps, rider)
    before_ids = {p["id"] for p in pending(svc)}
    replies, problems, slow = [], [], []

    turns = [t for t in case["turns"] if t["from"] == "rider"]
    i = 0
    while i < len(turns):
        # back-to-back identical message ids are sent concurrently, like a vendor resend
        group = [turns[i]]
        while i + len(group) < len(turns) and turns[i + len(group)]["message_id"] == turns[i]["message_id"]:
            group.append(turns[i + len(group)])
        with ThreadPoolExecutor(len(group)) as pool:
            results = list(pool.map(lambda t: send(svc, rider, t), group))
        for r, secs in results:
            if r.status_code // 100 != 2:
                problems.append(f"HTTP {r.status_code} on {group[0]['message_id']}")
                continue
            replies.append(r.json().get("reply", ""))
            if secs > 10:
                slow.append(round(secs, 1))
        if len(group) > 1 and len({rep for rep in replies[-len(group):]}) > 1:
            problems.append("duplicate message got different replies")
        i += len(group)

    # wait for background payouts to land
    want_any = [o.get("payout", 0) for o in exp.get("one_of", [exp])]
    deadline = time.monotonic() + SETTLE_SECONDS
    while True:
        paid = payswift_total(ps, rider) - before_total
        if paid in want_any or time.monotonic() > deadline:
            break
        time.sleep(1)
    since_start = payswift_total(ps, rider) - run_start_totals.setdefault(rider, before_total)

    new_items = [p for p in pending(svc) if p["id"] not in before_ids and p["rider_id"] == rider]
    approval = sum(p.get("amount") or 0 for p in new_items if p["type"] == "approval") or None
    escalated = any(p["type"] == "escalation" for p in new_items)

    outcomes = exp.get("one_of", [{"payout": exp.get("payout", 0), "approval": exp.get("approval")}])
    money_ok = any((paid == o["payout"] or since_start == o["payout"]) and approval == o["approval"]
                   for o in outcomes)
    if not money_ok:
        problems.append(f"money: paid {paid} (run total {since_start}), approval {approval}; "
                        f"expected {[(o['payout'], o['approval']) for o in outcomes]}")
    esc = exp.get("escalation", "no")
    if esc == "required" and not escalated:
        problems.append("escalation required but none created")
    if esc == "no" and escalated:
        problems.append("unexpected escalation")
    all_text = " ".join(replies)
    missing = [m for m in exp.get("reply_mentions", []) if m.lower() not in all_text.lower()]
    if missing:
        problems.append(f"reply does not mention {missing}")
    if slow:
        problems.append(f"replies slower than 10s: {slow}")

    trace = svc.get(f"/trace/{rider}").json()
    if not trace or any(k not in trace[0] for k in ("at", "type", "name", "input", "output")):
        problems.append("trace missing or wrong shape")
    return problems, replies


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("service_url")
    ap.add_argument("--payswift", default="http://localhost:8081")
    ap.add_argument("--cases", default=str(ROOT / "evals" / "cases.json"))
    ap.add_argument("-v", "--verbose", action="store_true", help="print every reply")
    args = ap.parse_args()

    cases = json.loads(Path(args.cases).read_text())
    svc = httpx.Client(base_url=args.service_url.rstrip("/"), timeout=30)
    ps = httpx.Client(base_url=args.payswift.rstrip("/"), timeout=10)

    run_start_totals, failures = {}, 0
    print(f"Running {len(cases)} conversations against {args.service_url}\n")
    for n, case in enumerate(cases, 1):
        try:
            problems, replies = run_case(svc, ps, case, run_start_totals)
        except Exception as e:
            problems, replies = [f"crashed: {e!r}"], []
        failures += bool(problems)
        mark = "PASS" if not problems else "FAIL"
        print(f"{mark}  {n:2}. {case['rider_id']}  {case['scenario']}")
        for p in problems:
            print(f"        - {p}")
        if args.verbose or problems:
            for rep in replies:
                print(f"        > {rep.replace(chr(10), ' | ')}")
    passed = len(cases) - failures
    print(f"\n{passed}/{len(cases)} passed")
    sys.exit(0 if failures == 0 else 1)


if __name__ == "__main__":
    main()
