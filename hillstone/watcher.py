#!/usr/bin/env python3
"""Hillstone Phoenix availability watcher + auto-booker.

Polls Wisely's public inventory endpoint. When a slot opens in your window it
tries to book the earliest one (book.py, headless browser) and pushes the
result via ntfy.sh. If booking fails it still pushes the opening with a
one-tap link so you can grab it by hand.

Stdlib only; Playwright is imported lazily, only when a slot is found.

Env:
  DATES         comma list, YYYY-MM-DD. Default: the next 2 Fridays (Phoenix)
  PARTY_SIZE    default 2
  WINDOW_START  HH:MM local Phoenix time, default 17:00
  WINDOW_END    HH:MM local Phoenix time, default 18:30
  AUTO_BOOK     1 to book automatically (default 1)
  DRY_RUN       1 to walk the booking flow but stop before submitting
  NTFY_TOPIC    ntfy topic name (alerts skipped if unset)
  MERCHANT_ID   default 278170 (Hillstone Phoenix)
  STATE_DIR     where seen.json / booked.json live, default ./state
"""
import json
import os
import sys
import urllib.request
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

TZ = ZoneInfo("America/Phoenix")
API = "https://loyaltyapi.wisely.io/v2/web/reservations/inventory"
BOOK_URL = "https://reservations.getwisely.com/hillstone-phoenix?g=hillstone-phoenix"

MERCHANT_ID = os.getenv("MERCHANT_ID") or "278170"
PARTY_SIZE = int(os.getenv("PARTY_SIZE") or "2")
WINDOW_START = os.getenv("WINDOW_START") or "17:00"
WINDOW_END = os.getenv("WINDOW_END") or "18:30"
AUTO_BOOK = (os.getenv("AUTO_BOOK") or "1") == "1"
DRY_RUN = (os.getenv("DRY_RUN") or "0") == "1"
NTFY_TOPIC = os.getenv("NTFY_TOPIC")
STATE_DIR = Path(os.getenv("STATE_DIR") or "state")
SEEN_FILE = STATE_DIR / "seen.json"
BOOKED_FILE = STATE_DIR / "booked.json"


def next_fridays(n=2):
    today = datetime.now(TZ).date()
    first = today + timedelta(days=(4 - today.weekday()) % 7)
    return [first + timedelta(weeks=i) for i in range(n)]


def target_dates():
    raw = [d.strip() for d in (os.getenv("DATES") or "").split(",") if d.strip()]
    if not raw:
        return next_fridays()
    return [datetime.strptime(d, "%Y-%m-%d").date() for d in raw]


def hm(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


def fetch(day):
    start = datetime.combine(day, hm(WINDOW_START), TZ)
    qs = (
        f"merchant_id={MERCHANT_ID}&party_size={PARTY_SIZE}"
        f"&search_ts={int(start.timestamp() * 1000)}"
        "&show_reservation_types=1&limit=50"
    )
    req = urllib.request.Request(
        f"{API}?{qs}",
        headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def iter_times(node):
    """Yield every slot dict under any 'times' list, wherever it's nested."""
    if isinstance(node, dict):
        if isinstance(node.get("times"), list):
            yield from node["times"]
        for v in node.values():
            yield from iter_times(v)
    elif isinstance(node, list):
        for v in node:
            yield from iter_times(v)


def open_slots(day, data) -> dict:
    lo = datetime.combine(day, hm(WINDOW_START), TZ)
    hi = datetime.combine(day, hm(WINDOW_END), TZ)
    out = {}
    for t in iter_times(data):
        if not t.get("is_available") or "reserved_ts" not in t:
            continue
        if not (t.get("min_party_size", 1) <= PARTY_SIZE <= t.get("max_party_size", 99)):
            continue
        ts = datetime.fromtimestamp(t["reserved_ts"] / 1000, TZ)
        if ts.date() == day and lo <= ts <= hi:
            out[ts.isoformat()] = t.get("display_time") or ts.strftime("%-I:%M %p")
    return out


def notify(title, lines, priority="high", tags="fork_and_knife"):
    body = "\n".join(lines)
    print(f"ALERT: {title}\n{body}")
    if not NTFY_TOPIC:
        print("NTFY_TOPIC not set; skipping push")
        return
    req = urllib.request.Request(
        f"https://ntfy.sh/{NTFY_TOPIC}",
        data=body.encode(),
        headers={"Title": title, "Click": BOOK_URL, "Priority": priority, "Tags": tags},
    )
    try:
        urllib.request.urlopen(req, timeout=20)
    except Exception as e:
        print(f"ntfy push failed: {e}")


def label(iso, display):
    return f"{datetime.fromisoformat(iso):%a %b %-d} {display}"


def try_book(current: dict) -> bool:
    """Book the earliest open slot. Returns True once a reservation is made."""
    from book import book  # lazy: needs playwright

    for iso in sorted(current):
        when = datetime.fromisoformat(iso)
        print(f"Attempting to book {label(iso, current[iso])} ...")
        try:
            ok, detail = book(when, PARTY_SIZE, dry_run=DRY_RUN)
        except Exception as e:
            ok, detail = False, f"{type(e).__name__}: {e}"
        print(f"  -> ok={ok} {detail}")
        if ok and not DRY_RUN:
            BOOKED_FILE.write_text(json.dumps({"slot": iso, "detail": detail}))
            notify(
                f"BOOKED Hillstone {label(iso, current[iso])}",
                [f"Party of {PARTY_SIZE}. {detail}", "Check your email/text for the confirmation."],
                priority="urgent",
                tags="white_check_mark,fork_and_knife",
            )
            return True
        if ok and DRY_RUN:
            notify("Hillstone dry run OK", [f"Would book {label(iso, current[iso])}", detail], priority="default")
            return False
    return False


def main():
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    if BOOKED_FILE.exists() and not DRY_RUN:
        print(f"Already booked: {BOOKED_FILE.read_text()} -- nothing to do.")
        return 0

    seen = set(json.loads(SEEN_FILE.read_text())) if SEEN_FILE.exists() else set()
    today = datetime.now(TZ).date()
    current = {}
    dates = [d for d in target_dates() if d >= today]
    print(f"Checking {', '.join(map(str, dates))} {WINDOW_START}-{WINDOW_END}, party {PARTY_SIZE}")

    for day in dates:
        try:
            slots = open_slots(day, fetch(day))
            print(f"{day}: {len(slots)} open in window {sorted(slots.values())}")
            current.update(slots)
        except Exception as e:  # keep checking other dates
            print(f"{day}: error {e}")

    booked = False
    if current and (AUTO_BOOK or DRY_RUN):
        booked = try_book(current)

    new = sorted(k for k in current if k not in seen)
    if new and not booked:
        notify(
            f"Hillstone opening (party of {PARTY_SIZE})"
            + (" - auto-book FAILED, tap to book" if AUTO_BOOK and not DRY_RUN else ""),
            [label(k, current[k]) for k in new],
        )
    elif not current:
        print("No openings in window.")

    # Store only what's open now, so a slot that closes and reopens re-alerts.
    SEEN_FILE.write_text(json.dumps(sorted(current)))
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).parent))
    sys.exit(main())
