# Rider Payout Dispute Desk

An agent that handles QuickDrop rider payout complaints over WhatsApp: it works out what the rider means, recomputes what they are owed from the trip export, reconciles against PaySwift, then pays, explains, sends the payout for ops approval, or escalates. Python 3.12, FastAPI, Postgres, no agent framework.

## Run

```bash
cp .env.example .env            # optional: add GROQ_API_KEY; without it the rule-based extractor is used
docker compose up --build       # app :8000, Postgres, PaySwift sandbox :8081
python evals/run.py http://localhost:8000          # needs httpx; --payswift URL to override, -v for replies
```
Ops page: http://localhost:8000/ops (pending approvals and escalations, approve or reject, per-rider trace). CI (`.github/workflows/ci.yml`) brings up the stack and runs the evals on every push.

## Design: the model reads, code decides

| Step | Who | What |
|---|---|---|
| Understand the message | **LLM** (`openai/gpt-oss-20b` on Groq, JSON mode, 4s timeout) | intent, claims `{issue_type, date, trip_ids}`, impersonation, injection, "personal reason" flag |
| Fallback | code (`extract.py`) | regex for trip IDs, dates (`kal`, `20 tarikh`, `Sep 19`, `20 wala` only if we asked), issue keywords. Used on no key, error, timeout, or 429; trace records `llm_fallback: <why>` |
| Validate | code | drop trip IDs not literally in the text; rule safety flags are OR-ed with the model's |
| What is owed | code (`policy.py`, pure) | per item: `policy fare − paid in export − already refunded in PaySwift`, never below 0, never the rider's own number |
| Pay / approve / escalate | code (`payouts.py`) | ≤ ₹200 and first auto-pay today → pay; else ops approval. Unknown trips, other riders' trips, pushback after a recheck, personal reasons, injection, and PaySwift failures → escalate |
| Reply text | code templates | every number in a reply comes from the policy result, never from the model |

**Tools the agent uses:** `extract_claims`, `payswift_list_payouts` (reconciliation), `check_claim`, `payswift_create_payout`, `escalate`. Every step lands in `trace_steps`, served by `GET /trace/{rider_id}`. Conversation memory is one JSON row per rider: what we asked for, the last claim (for "dobara check karo"), and dispute statuses already reported, so the next reply can say "Update: aapka ₹15 bhej diya gaya hai".

**Money safety under the 10s vendor budget**
- `message_id` is the primary key. A resend waits up to 7s for the original and returns its stored reply. Rows stuck in `processing` for over 60s are taken over.
- A per-rider Postgres advisory lock processes one message per rider at a time. Waiters poll `pg_try_advisory_lock` and release their connection between tries, so they can't starve the pool.
- PaySwift `Idempotency-Key` = hash(rider, IST day, item ids), and the reference is `qd|<key>|T926334=25`. The same dispute always sends the same key and body, so no retry, resend or ops click can pay twice. We work out what was already refunded per trip from PaySwift alone.
- A unique partial index `(rider_id, auto_pay_day)` enforces "one auto-pay per rider per day" in the database.
- The PaySwift call gets what is left of a 9s budget (max 6s). On timeout, 504 or `request_in_progress`, the payout stays `in_progress` and a background task retries with the same key, after first checking the ledger. On startup we resume any unfinished payouts. A 429 backs off. 400 or "key reused" escalates.

## Assumptions
1. "Today" for the once-a-day auto-pay and for "kal" is the IST date of `received_at`. The 7-day window counts back from it.
2. One dispute = all shortfalls for one rider on one IST day (R033's two trips = one ₹176 dispute).
3. If the rider names trip IDs, only those trips are checked. With only a date, the whole day is scanned (trips, penalties, incentive).
4. We pay the computed shortfall, not the amount the rider quotes (R035 says 300, is owed 25).
5. Data cleaning, noted in the trace: `R7`/`r19` → `R007`/`R019`; duplicate trip rows counted once; distances > 100 are metres.
6. Incentive lines are dated by the IST day they belong to (trip lines match the IST day in all cases).

## Eval results (the 24 sample conversations from `data/`, duplicate deliveries sent concurrently)
- **Fallback only (no key, as in CI): 24/24.**
- **LLM path: 24/24.** 17 extractions by the model, 1 where rules filled a date the model dropped (`20 wala`), 11 fell back because Groq's free tier returned 429 during the burst. All replies under 10s.

## Skipped / next
- Replies are Hinglish templates only (no English variant, no LLM rephrasing).
- Overpaid trips are not netted against underpaid ones on the same day.
- The ops page has no login. Escalations have only resolve or reject, no notes.
- No unit test suite beyond the evals. Voice notes, sentiment-based queue priority, and proper per-rider rate limits are not done.
