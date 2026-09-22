#!/usr/bin/env python3
"""
Tennis.az (Skedda) court-availability watcher.

Polls the venue's booking feed, works out which court/hour slots are free
inside your window, and Telegrams you when a slot that was BOOKED on the
previous run has become FREE (i.e. someone cancelled).

State lives in state.json next to this script.
"""

import argparse
import datetime as dt
import http.cookiejar
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from zoneinfo import ZoneInfo

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

SUBDOMAIN = "tennisaz"

# Azerbaijan has been on a permanent UTC+4 with no DST since 2016, so the fixed
# offset is exact — it's only a fallback because ZoneInfo is the safer choice if
# that ever changes. Windows has no system tzdata, hence the guard.
try:
    TZ = ZoneInfo("Asia/Baku")
except Exception:  # noqa: BLE001
    TZ = dt.timezone(dt.timedelta(hours=4), "Asia/Baku")

COURTS = {
    "985726": "Glasstech",
    "1260597": "Mobitek",
    "1260598": "Indoor",
    "1474132": "Gardashlar Mebel",
    "1474133": "Ozio",
}

# Hours you care about. 18 -> an 18:00-19:00 slot. 21 is the last one (21:00-22:00).
HOUR_FROM = int(os.environ.get("HOUR_FROM", 18))
HOUR_TO = int(os.environ.get("HOUR_TO", 22))       # exclusive end of the window
DAYS_AHEAD = int(os.environ.get("DAYS_AHEAD", 14))
MIN_LEAD_HOURS = int(os.environ.get("MIN_LEAD_HOURS", 2))  # ignore slots starting sooner

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")

TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")

BOOKING_URL = f"https://{SUBDOMAIN}.skedda.com/booking"

HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": BOOKING_URL,
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "X-Requested-With": "XMLHttpRequest",
}


# --------------------------------------------------------------------------
# Fetch
# --------------------------------------------------------------------------

TOKEN_PATTERNS = [
    r'name="__RequestVerificationToken"[^>]*value="([^"]+)"',
    r'value="([^"]+)"[^>]*name="__RequestVerificationToken"',
    r'["\']?requestVerificationToken["\']?\s*[:=]\s*["\']([^"\']+)["\']',
    r'<meta[^>]+name="[^"]*erificationToken[^"]*"[^>]+content="([^"]+)"',
    r'<meta[^>]+content="([^"]+)"[^>]+name="[^"]*erificationToken[^"]*"',
    # Last resort: ASP.NET Core data-protected blobs all start with CfDJ8.
    r'(CfDJ8[A-Za-z0-9_\-]{60,})',
]


def open_session():
    """Load the booking page to pick up the antiforgery cookie AND the matching
    token. Skedda validates the two together — cookie alone gives a 422."""
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    req = urllib.request.Request(BOOKING_URL, headers={
        "User-Agent": HEADERS["User-Agent"],
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": HEADERS["Accept-Language"],
    })
    with opener.open(req, timeout=45) as resp:
        html = resp.read().decode("utf-8", "replace")

    token = None
    for pattern in TOKEN_PATTERNS:
        m = re.search(pattern, html, re.IGNORECASE)
        if m:
            token = m.group(1)
            break
    return opener, token, html, [c.name for c in jar]


def fetch_bookings(start: dt.datetime, end: dt.datetime) -> list:
    opener, token, html, cookies = open_session()
    if not token:
        raise RuntimeError(
            "Could not find an antiforgery token on the booking page.\n"
            f"Cookies received: {cookies or 'none'}\n"
            "Run with --diag to dump what the page actually contains."
        )

    qs = urllib.parse.urlencode({
        "start": start.strftime("%Y-%m-%dT%H:%M:%S"),
        "end": end.strftime("%Y-%m-%dT%H:%M:%S.999"),
    })
    url = f"https://{SUBDOMAIN}.skedda.com/bookingslists?{qs}"

    headers = dict(HEADERS)
    # Skedda's client has used both spellings across versions; sending both is safe.
    headers["RequestVerificationToken"] = token
    headers["X-Skedda-RequestVerificationToken"] = token

    try:
        with opener.open(urllib.request.Request(url, headers=headers), timeout=45) as resp:
            body = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:800]
        raise RuntimeError(
            f"HTTP {exc.code} from {url}\n"
            f"token used: {token[:25]}... ({len(token)} chars)\n"
            f"cookies: {cookies}\n"
            f"--- server said ---\n{detail or '(empty body)'}"
        ) from None

    if not body.lstrip().startswith("{"):
        raise RuntimeError(
            "Expected JSON but got something else (login wall / bot check?).\n"
            f"URL: {url}\nFirst 300 chars:\n{body[:300]}"
        )
    data = json.loads(body)
    if "bookings" not in data:
        raise RuntimeError(f"No 'bookings' key in response. Keys: {list(data)}")
    return data["bookings"]


def diagnose() -> None:
    """Dump everything token-shaped on the booking page."""
    opener, token, html, cookies = open_session()
    print(f"page length : {len(html)}")
    print(f"cookies     : {cookies or 'none'}")
    print(f"token found : {token[:40] + '...' if token else 'NONE'}")
    print("\n-- which pattern matched --")
    for i, pattern in enumerate(TOKEN_PATTERNS, 1):
        m = re.search(pattern, html, re.IGNORECASE)
        print(f"  {i}. {'HIT  ' + m.group(1)[:45] + '...' if m else 'miss'}")
    print("\n-- lines mentioning 'token' or 'verification' --")
    shown = 0
    for line in html.splitlines():
        if re.search(r"token|verification|antiforgery", line, re.IGNORECASE):
            print("  " + line.strip()[:220])
            shown += 1
            if shown >= 12:
                break
    if not shown:
        print("  (none)")


# --------------------------------------------------------------------------
# Occupancy
# --------------------------------------------------------------------------

def occupied_slots(bookings: list) -> set:
    """Return a set of (date, court_id, hour) that are taken.

    Skedda gives each booking an `occurrenceHash`: per-year, per-month 32-bit
    masks where bit N means "this booking occurs on day N+1". That already has
    the RRULE expanded and EXDATEs removed, so we don't touch the ical string.
    """
    taken = set()
    for b in bookings:
        try:
            s = dt.datetime.fromisoformat(b["start"])
            e = dt.datetime.fromisoformat(b["end"])
        except (KeyError, ValueError):
            continue

        end_hour = e.hour if e.hour > s.hour else 24
        hours = list(range(s.hour, end_hour))
        spaces = b.get("spaces") or []
        hashes = b.get("occurrenceHash") or {}

        dates = []
        for year, months in hashes.items():
            for month_idx, mask in enumerate(months or []):
                if not mask:
                    continue
                for day_idx in range(31):
                    if mask >> day_idx & 1:
                        try:
                            dates.append(dt.date(int(year), month_idx + 1, day_idx + 1))
                        except ValueError:
                            pass

        # Non-recurring bookings without a usable hash: fall back to the start date.
        if not dates:
            dates = [s.date()]

        for d in dates:
            for court in spaces:
                for h in hours:
                    taken.add((d, court, h))
    return taken


def free_slots(taken: set, now: dt.datetime) -> set:
    """Every slot in the window that is NOT taken, as 'YYYY-MM-DD|courtid|HH'."""
    cutoff = now + dt.timedelta(hours=MIN_LEAD_HOURS)
    out = set()
    for offset in range(DAYS_AHEAD + 1):
        day = now.date() + dt.timedelta(days=offset)
        for hour in range(HOUR_FROM, HOUR_TO):
            slot_start = dt.datetime.combine(day, dt.time(hour), tzinfo=TZ)
            if slot_start < cutoff:
                continue
            for court in COURTS:
                if (day, court, hour) not in taken:
                    out.add(f"{day.isoformat()}|{court}|{hour:02d}")
    return out


# --------------------------------------------------------------------------
# State
# --------------------------------------------------------------------------

def load_state() -> set | None:
    if not os.path.exists(STATE_FILE):
        return None
    try:
        with open(STATE_FILE, encoding="utf-8") as fh:
            return set(json.load(fh).get("free", []))
    except (json.JSONDecodeError, OSError):
        return None


def save_state(free: set, now: dt.datetime) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as fh:
        json.dump(
            {"checked_at": now.isoformat(timespec="seconds"), "free": sorted(free)},
            fh,
            indent=1,
        )


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------

def describe(slots) -> str:
    """Group slots into 'Fri 25 Sep — Ozio 18:00, 19:00' lines."""
    by_day = defaultdict(lambda: defaultdict(list))
    for slot in sorted(slots):
        day_s, court, hour = slot.split("|")
        day = dt.date.fromisoformat(day_s)
        by_day[day][COURTS.get(court, court)].append(int(hour))

    lines = []
    for day in sorted(by_day):
        lines.append(f"*{day.strftime('%a %d %b')}*")
        for court in sorted(by_day[day]):
            hrs = ", ".join(f"{h}:00" for h in sorted(by_day[day][court]))
            lines.append(f"  {court} — {hrs}")
    return "\n".join(lines)


def telegram(text: str) -> None:
    if not TG_TOKEN or not TG_CHAT:
        print("[telegram] credentials missing, printing instead:\n" + text)
        return
    payload = json.dumps({
        "chat_id": TG_CHAT,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        resp.read()


# --------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dump", action="store_true",
                    help="print the full current availability grid and exit")
    ap.add_argument("--reset", action="store_true",
                    help="rewrite state from this run without alerting")
    ap.add_argument("--diag", action="store_true",
                    help="dump antiforgery-token diagnostics and exit")
    args = ap.parse_args()

    if args.diag:
        diagnose()
        return 0

    now = dt.datetime.now(TZ)

    # The browser always asks for whole Mon-Sun weeks (its capture ran
    # Mon 31 Aug -> Sat 14 Nov). Skedda may reject ranges that don't line up
    # with a grid view, so we snap outward to week boundaries and simply
    # request a bit more data than we need.
    monday = now.date() - dt.timedelta(days=now.weekday())
    weeks = -(-(now.weekday() + DAYS_AHEAD + 1) // 7)   # ceil division
    window_start = dt.datetime.combine(monday, dt.time(0))
    window_end = dt.datetime.combine(monday + dt.timedelta(days=weeks * 7 - 1),
                                     dt.time(23, 59, 59))

    bookings = fetch_bookings(window_start, window_end)
    print(f"[info] {len(bookings)} bookings fetched at {now:%Y-%m-%d %H:%M} Baku time")

    current = free_slots(occupied_slots(bookings), now)

    if args.dump:
        print(describe(current) or "Nothing free in the window.")
        return 0

    previous = load_state()
    save_state(current, now)

    if previous is None or args.reset:
        print(f"[info] baseline saved ({len(current)} free slots). No alert sent.")
        return 0

    opened = current - previous
    if not opened:
        print("[info] nothing new opened up.")
        return 0

    print(f"[info] {len(opened)} slot(s) freed up")
    telegram("🎾 *Court(s) just opened up*\n\n" + describe(opened) + f"\n\n{BOOKING_URL}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001
        print(f"[error] {exc}", file=sys.stderr)
        sys.exit(1)
