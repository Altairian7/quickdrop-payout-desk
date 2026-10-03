# Rider Payout Dispute Desk

ProcureYard take-home for the ML/AI Engineer Intern role. Start with `BRIEF.md`.

## Design notes: money safety under the 10s vendor budget

- **Deterministic PaySwift key.** `Idempotency-Key = sha256(rider_id | IST date | issue_type | sorted trip_ids)`. A retry from anywhere (vendor resend, background job, ops approval) reuses it, so PaySwift never pays twice. If we ever send a different amount under the same key, PaySwift answers 409 `idempotency_key_reused`, and we escalate instead of retrying.
- **Adaptive timeout.** The PaySwift call gets whatever is left of a ~9s budget (max 6s). On timeout, 503 or 504 the payout is marked `in_progress`, the rider is told it is being processed, and a background retry with the same key finishes it, confirmed via `GET /v1/payouts`. A startup sweeper resumes any `in_progress` rows.
- **429 / account_blocked.** Back off, never retry in a loop; hand off to the background retry with a long delay.
- **Duplicate wamid.** `message_id` is unique. A resend waits up to ~7s for the original to finish and returns its stored reply, else a short holding reply. Nothing is processed twice.
- **One rider at a time.** A per-rider Postgres advisory lock serialises a rider's messages, which also makes the once-per-day auto-pay check race-free.

## Assumptions

- A1: "today" for the once-per-day auto-pay is the IST date of `received_at`.
- A2: the 7-day dispute window is counted back from `received_at`, inclusive.
- A3: one dispute = one issue type on one IST day (several trips can be in one dispute).
- A4: clearly fixable data issues are normalised in code and noted in the trace: rider IDs like `R7`/`r19`, duplicate trip rows, distances above 100 treated as metres.
