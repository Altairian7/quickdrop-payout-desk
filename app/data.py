"""Read-only exports from QuickDrop's systems, loaded once at startup.

Cleans the known data problems and remembers what it fixed, so the
agent can put those fixes in the trace.
"""
import csv
import os
import re
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

IST = timezone(timedelta(hours=5, minutes=30))
DATA_DIR = Path(os.environ.get("DATA_DIR", Path(__file__).parent.parent / "data"))
METRES_THRESHOLD_KM = 100  # no delivery is 100 km; bigger numbers are metres


def normalize_rider_id(raw: str) -> str:
    m = re.fullmatch(r"\s*[Rr]0*(\d+)\s*", raw or "")
    return f"R{int(m.group(1)):03d}" if m else (raw or "").strip()


def ist_date(utc_iso: str) -> date:
    return datetime.fromisoformat(utc_iso.replace("Z", "+00:00")).astimezone(IST).date()


class Store:
    def __init__(self, data_dir: Path = DATA_DIR):
        self.riders = {}
        self.trips = {}                                  # trip_id -> trip dict
        self.trips_by_rider_day = defaultdict(list)      # (rider_id, date) -> [trip]
        self.lines_by_trip = defaultdict(list)           # trip_id -> [line]
        self.incentives = defaultdict(int)               # (rider_id, date) -> amount paid
        self.fixes = defaultdict(list)                   # trip_id -> ["what we cleaned"]

        with open(data_dir / "riders.csv") as f:
            for r in csv.DictReader(f):
                self.riders[normalize_rider_id(r["rider_id"])] = r

        with open(data_dir / "trips.csv") as f:
            for r in csv.DictReader(f):
                tid = r["trip_id"].strip()
                if tid in self.trips:
                    continue  # exact duplicate rows exist in the export; count once
                fixes = []
                rider = normalize_rider_id(r["rider_id"])
                if rider != r["rider_id"]:
                    fixes.append(f"rider_id {r['rider_id']!r} read as {rider}")
                km = float(r["distance_km"])
                if km > METRES_THRESHOLD_KM:
                    fixes.append(f"distance {r['distance_km']} looks like metres, read as {km / 1000} km")
                    km = km / 1000
                trip = {
                    "trip_id": tid,
                    "rider_id": rider,
                    "started_at": r["started_at"],
                    "day": ist_date(r["started_at"]),
                    "status": r["status"],
                    "distance_km": km,
                    "surge": float(r["surge_multiplier"] or 1.0),
                }
                self.trips[tid] = trip
                self.trips_by_rider_day[(rider, trip["day"])].append(trip)
                if fixes:
                    self.fixes[tid] = fixes

        with open(data_dir / "payout_lines.csv") as f:
            for r in csv.DictReader(f):
                line = {**r, "rider_id": normalize_rider_id(r["rider_id"]), "amount": int(r["amount"])}
                if line["line_type"] == "daily_incentive":
                    day = date.fromisoformat(r["payout_date"])
                    self.incentives[(line["rider_id"], day)] += line["amount"]
                elif r["trip_id"]:
                    self.lines_by_trip[r["trip_id"]].append(line)


_store = None


def store() -> Store:
    global _store
    if _store is None:
        _store = Store()
    return _store
