"""Payout policy as plain code. The model never decides money; this does.

Everything here is a pure function of the data exports plus what PaySwift
says we already refunded, so the same claim always gives the same answer.
"""
import hashlib
from dataclasses import asdict, dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

BASE_FARE = Decimal(25)
FREE_KM = Decimal(2)
PER_KM = Decimal(6)
RIDER_CANCEL_PENALTY = -10
INCENTIVE_TRIPS = 12
INCENTIVE_AMOUNT = 150
DISPUTE_WINDOW_DAYS = 7
AUTO_PAY_LIMIT = 200


def trip_expected(trip) -> int:
    """What the policy says a trip should pay (negative for a rider cancel)."""
    if trip["status"] == "completed":
        km = Decimal(str(trip["distance_km"]))
        fare = (BASE_FARE + PER_KM * max(Decimal(0), km - FREE_KM)) * Decimal(str(trip["surge"]))
        return int(fare.quantize(Decimal(1), rounding=ROUND_HALF_UP))
    if trip["status"] == "cancelled_by_rider":
        return RIDER_CANCEL_PENALTY
    return 0  # cancelled by customer earns nothing


@dataclass
class Item:
    item_id: str        # trip_id, or INC-<date> for the daily incentive
    day: date
    kind: str           # trip | incentive
    expected: int       # per policy
    paid: int           # per our payout export
    refunded: int       # already sent by us through PaySwift for this item
    pending: bool       # already waiting in ops / being paid
    detail: dict = field(default_factory=dict)

    @property
    def shortfall(self) -> int:
        if self.pending:
            return 0
        return max(0, self.expected - self.paid - self.refunded)

    def to_dict(self):
        d = asdict(self)
        d["day"] = self.day.isoformat()
        d["shortfall"] = self.shortfall
        return d


@dataclass
class Check:
    outcome: str        # checked | need_info | outside_window | future_date |
                        # unknown_trip | not_rider_trip | unknown_rider
    items: list = field(default_factory=list)
    notes: list = field(default_factory=list)

    @property
    def shortfall(self) -> int:
        return sum(i.shortfall for i in self.items)

    def by_day(self) -> dict:
        days = {}
        for i in self.items:
            if i.shortfall > 0:
                days.setdefault(i.day, []).append(i)
        return days

    def to_dict(self):
        return {"outcome": self.outcome, "shortfall": self.shortfall,
                "items": [i.to_dict() for i in self.items], "notes": self.notes}


def check_claim(store, rider_id, claim, today, refunded=None, pending=None) -> Check:
    """Work out what the rider is owed for one claim.

    claim: {"date": "YYYY-MM-DD" | None, "trip_ids": [..]} as extracted by the model.
    refunded: item_id -> rupees PaySwift shows we already sent for it.
    pending: item_ids already waiting for ops approval or being paid.
    If trip_ids are given only those trips are checked; with only a date the
    whole day is scanned (trips, penalties and the incentive).
    """
    refunded = refunded or {}
    pending = pending or set()
    if rider_id not in store.riders:
        return Check("unknown_rider", notes=[f"{rider_id} is not in riders.csv"])

    trip_ids = [t.strip().upper() for t in claim.get("trip_ids") or [] if t]
    claim_day = date.fromisoformat(claim["date"]) if claim.get("date") else None

    trips = []
    for tid in trip_ids:
        trip = store.trips.get(tid)
        if trip is None:
            return Check("unknown_trip", notes=[f"{tid} not found in trips export"])
        if trip["rider_id"] != rider_id:
            # never reveal whose trip it is
            return Check("not_rider_trip", notes=[f"{tid} belongs to a different rider"])
        trips.append(trip)

    if not trips and claim_day is None:
        return Check("need_info", notes=["no date or trip id to check"])

    days = {t["day"] for t in trips} or {claim_day}
    for d in days:
        if d > today:
            return Check("future_date", notes=[f"{d} is after {today}"])
        if (today - d).days > DISPUTE_WINDOW_DAYS:
            return Check("outside_window",
                         notes=[f"{d} is {(today - d).days} days before {today}; window is {DISPUTE_WINDOW_DAYS}"])

    check = Check("checked")
    if not trips:  # date only: scan the whole day
        trips = store.trips_by_rider_day.get((rider_id, claim_day), [])
        check.items.append(_incentive_item(store, rider_id, claim_day, trips, refunded, pending))

    for trip in trips:
        lines = store.lines_by_trip.get(trip["trip_id"], [])
        check.items.append(Item(
            item_id=trip["trip_id"], day=trip["day"], kind="trip",
            expected=trip_expected(trip),
            paid=sum(l["amount"] for l in lines),
            refunded=refunded.get(trip["trip_id"], 0),
            pending=trip["trip_id"] in pending,
            detail={"status": trip["status"], "distance_km": trip["distance_km"],
                    "surge": trip["surge"], "paid_lines": [(l["line_type"], l["amount"]) for l in lines]},
        ))
        for fix in store.fixes.get(trip["trip_id"], []):
            check.notes.append(f"{trip['trip_id']}: {fix}")
    return check


def _incentive_item(store, rider_id, day, trips, refunded, pending) -> Item:
    completed = sum(1 for t in trips if t["status"] == "completed")
    item_id = f"INC-{day.isoformat()}"
    return Item(
        item_id=item_id, day=day, kind="incentive",
        expected=INCENTIVE_AMOUNT if completed >= INCENTIVE_TRIPS else 0,
        paid=store.incentives.get((rider_id, day), 0),
        refunded=refunded.get(item_id, 0),
        pending=item_id in pending,
        detail={"completed_trips": completed, "needed": INCENTIVE_TRIPS},
    )


def dispute_key(rider_id, day, item_ids) -> str:
    """Same dispute -> same key, whichever message or retry triggers it."""
    raw = f"{rider_id}|{day.isoformat()}|{','.join(sorted(item_ids))}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def gate(amount, auto_paid_today: bool) -> str:
    """Finance rule: auto-pay up to 200 per dispute, once per rider per day."""
    if amount <= 0:
        return "nothing_owed"
    if amount <= AUTO_PAY_LIMIT and not auto_paid_today:
        return "auto_pay"
    return "needs_approval"
