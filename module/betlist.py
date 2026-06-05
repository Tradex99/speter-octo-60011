import httpx
import asyncio
from datetime import datetime, timezone


#Config
MAX_OPEN_BETS = 10
MAX_PLAYED    = 20
TRACKER_NAME  = "chukwuebuka"

OPEN_URL    = "https://m.betking.com/en-ng/my-bets/sports/open?_data=routes%2F%28%24locale%29.my-bets.sports.%24betsType"
SETTLED_URL = "https://m.betking.com/en-ng/my-bets/sports/settled?_data=routes%2F%28%24locale%29.my-bets.sports.%24betsType"

MYBETS_HEADERS = {
    "User-Agent":      "Mozilla/5.0 (X11; Linux x86_64; rv:140.0) Gecko/20100101 Firefox/140.0",
    "Accept":          "*/*",
    "Accept-Language": "en-US,en;q=0.5",
    "Dnt":             "1",
    "Sec-Gpc":         "1",
    "Sec-Fetch-Dest":  "empty",
    "Sec-Fetch-Mode":  "cors",
    "Sec-Fetch-Site":  "same-origin",
    "Priority":        "u=0",
    "Te":              "trailers",
}


def _get_supabase():
    import os
    from supabase import create_client
    base   = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    config = {}
    with open(os.path.join(base, "db.txt"), "r") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, _, value = line.partition("=")
                config[key.strip()] = value.strip().strip('"')
    return create_client(config["SUPABASE_URL"], config["SUPABASE_KEY"])


def _get_cookie():
    sb  = _get_supabase()
    row = sb.table("betking").select("cookie").eq("id", 1).single().execute()
    return row.data["cookie"]


def _today_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _day_label() -> str:
    today = _today_str()
    y, m, d = today.split("-")
    return f"{int(d)}/{int(m)}/{y}"


async def _fetch_open(headers: dict) -> dict:
    try:
        async with httpx.AsyncClient(http2=True, timeout=15.0, follow_redirects=False) as client:
            resp = await client.get(
                OPEN_URL,
                headers={**headers, "Referer": "https://m.betking.com/en-ng/my-bets/sports/settled"},
            )
            if resp.status_code in (401, 301):
                return {"error": str(resp.status_code)}
            if not resp.text or not resp.text.strip():
                return {"error": "session_expired"}
            data = resp.json()
            return data if isinstance(data, dict) else {"error": "session_expired"}
    except Exception as e:
        return {"error": f"exception:{e}"}


async def _fetch_settled(headers: dict) -> dict:
    try:
        async with httpx.AsyncClient(http2=True, timeout=15.0, follow_redirects=False) as client:
            resp = await client.get(
                SETTLED_URL,
                headers={**headers, "Referer": "https://m.betking.com/en-ng/my-bets/sports/open"},
            )
            if resp.status_code in (401, 301):
                return {"error": str(resp.status_code)}
            if not resp.text or not resp.text.strip():
                return {"error": "session_expired"}
            data = resp.json()
            return data if isinstance(data, dict) else {"error": "session_expired"}
    except Exception as e:
        return {"error": f"exception:{e}"}


def _count_open(data: dict) -> int:
    coupons = data.get("couponsData", {}).get("coupons", [])
    return sum(1 for c in coupons if c.get("couponCode"))


def _parse_settled_today(data: dict) -> tuple[int, int]:
    """
    Parse today's settled bets, deduplicating by selectionId set.
    5 tickets of same match = 1 win or 1 loss.
    Returns (wins, losses) as unique match counts.
    """
    today   = _today_str()
    coupons = data.get("couponsData", {}).get("coupons", [])

    # Group by frozenset of selectionIds — same set = same match/bet
    seen_keys = {}   # key -> betFinalState (first seen wins)
    for c in coupons:
        settled = c.get("settledDate", "")
        if not settled.startswith(today):
            continue
        sel_key = frozenset(
            s["selectionId"] for s in c.get("selectionResults", [])
        )
        if not sel_key:
            continue
        # Only record first occurrence — all tickets for same match share same state
        if sel_key not in seen_keys:
            seen_keys[sel_key] = c.get("betFinalState")

    wins   = 0
    losses = 0
    for state in seen_keys.values():
        if state in (1, 5):
            wins += 1
        elif state == 3:
            losses += 1

    return wins, losses


def _update_tracker(wins: int, losses: int, api_played: int):
    """
    Update sp_tracker using increment logic:
    - wins and losses come from API (deduplicated) — these are always the source of truth
    - played = wins + losses (derived, always accurate)
    - max is capped at MAX_PLAYED (20)
    - If the row is from a previous day, reset it fresh for today
    """
    today   = _today_str()
    day_str = _day_label()
    played  = wins + losses

    sb  = _get_supabase()
    row = sb.table("sp_tracker").select("*").eq("name", TRACKER_NAME).execute()

    if row.data:
        existing = row.data[0]
        # If existing row is from a previous day, reset everything
        if existing.get("day") != day_str:
            sb.table("sp_tracker").update({
                "win":    wins,
                "lost":   losses,
                "played": played,
                "max":    MAX_PLAYED,
                "day":    day_str,
            }).eq("name", TRACKER_NAME).execute()
        else:
            # Same day — wins/losses from API are deduplicated source of truth
            # played is always wins + losses
            sb.table("sp_tracker").update({
                "win":    wins,
                "lost":   losses,
                "played": played,
                "max":    MAX_PLAYED,
            }).eq("name", TRACKER_NAME).execute()
    else:
        sb.table("sp_tracker").insert({
            "name":   TRACKER_NAME,
            "max":    MAX_PLAYED,
            "played": played,
            "win":    wins,
            "lost":   losses,
            "day":    day_str,
        }).execute()


async def check_can_bet() -> bool:
    """
    Returns True if betting is allowed, False if blocked.
    Fires open + settled requests in parallel, updates sp_tracker.
    """
    try:
        cookie = _get_cookie()
    except Exception as e:
        print(f"[betlist] cookie err : {e}")
        return False

    headers = {**MYBETS_HEADERS, "Cookie": cookie}

    print(f"[betlist] checking   : open bets + settled")

    open_task, settled_task = await asyncio.gather(
        _fetch_open(headers),
        _fetch_settled(headers),
    )

    open_err    = open_task.get("error")
    settled_err = settled_task.get("error")

    if open_err or settled_err:
        err = open_err or settled_err
        if err in ("401", "301", "session_expired"):
            print(f"[betlist] session    : expired - attempting re-login")
            import sys, os
            sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            from login import login
            await login()
        else:
            print(f"[betlist] fetch err  : {err}")
        return False

    open_count   = _count_open(open_task)
    wins, losses = _parse_settled_today(settled_task)
    played       = wins + losses

    print(f"[betlist] open bets  : {open_count} / {MAX_OPEN_BETS}")
    print(f"[betlist] today      : played={played} | win={wins} | lost={losses} | max={MAX_PLAYED}")

    try:
        _update_tracker(wins, losses, played)
        print(f"[betlist] tracker    : updated")
    except Exception as e:
        print(f"[betlist] tracker err: {e}")

    if open_count >= MAX_OPEN_BETS:
        print(f"[betlist] blocked    : max open bets reached ({open_count})")
        return False

    if played >= MAX_PLAYED:
        print(f"[betlist] blocked    : daily max played reached ({played}/{MAX_PLAYED})")
        return False

    return True


async def increment_played():
    """
    Called by staker immediately after a successful bet placement.
    Increments the played count in DB by 1 without waiting for API sync.
    This ensures accurate count between betlist cycles.
    """
    try:
        today   = _today_str()
        day_str = _day_label()
        sb      = _get_supabase()
        row     = sb.table("sp_tracker").select("*").eq("name", TRACKER_NAME).execute()

        if row.data:
            existing = row.data[0]
            if existing.get("day") == day_str:
                current = existing.get("played", 0)
                sb.table("sp_tracker").update({
                    "played": current + 1,
                }).eq("name", TRACKER_NAME).execute()
            # If different day, don't increment — betlist full sync will fix it
        # If no row yet, also skip — betlist full sync will create it
    except Exception as e:
        print(f"[betlist] increment err: {e}")
