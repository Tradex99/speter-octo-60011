import httpx
import asyncio


#Config
EVENT_URL = "https://m.betking.com/en-ng/sports/live/api/event?areaId=2&fixtureId={fixture_id}&_data=routes%2F%28%24locale%29.sports.live.api.event"

EVENT_HEADERS = {
    "User-Agent":      "Mozilla/5.0 (X11; Linux x86_64; rv:140.0) Gecko/20100101 Firefox/140.0",
    "Accept":          "*/*",
    "Accept-Language": "en-US,en;q=0.5",
    "Dnt":             "1",
    "Sec-Gpc":         "1",
    "Sec-Fetch-Dest":  "empty",
    "Sec-Fetch-Mode":  "cors",
    "Sec-Fetch-Site":  "same-origin",
    "Priority":        "u=4",
    "Te":              "trailers",
}

# Market typeId for "game - total points"
GAME_TOTAL_TYPE_ID = 10637
TARGET_LINES       = [15.5, 16.5]   # checked in order; first qualifying wins
TARGET_SELECTION   = "Over"
MIN_ODD            = 1.18

# Minimum loser score per set — loser must score >= 8 in each of the 3 sets
SET_THRESHOLDS = [8, 8, 8]


def _build_referer(event_id: int) -> str:
    return f"https://m.betking.com/en-ng/sports/live/{event_id}"


def _parse_period_scores(period_scores: str) -> list[tuple[int, int]]:
    """Parse '6:11 - 11:9 - 11:7' into [(6,11), (11,9), (11,7)]."""
    sets = []
    for part in period_scores.split(" - "):
        part = part.strip()
        if not part or ":" not in part:
            continue
        try:
            h, a = part.split(":")
            sets.append((int(h), int(a)))
        except ValueError:
            continue
    return sets


def _sets_qualify(sets: list[tuple[int, int]]) -> bool:
    """
    Check first 3 sets. In each set the loser must have scored
    at least the threshold for that set position.
    """
    if len(sets) < 3:
        return False
    for i, threshold in enumerate(SET_THRESHOLDS):
        h, a = sets[i]
        loser_score = min(h, a)
        if loser_score < threshold:
            return False
    return True


def _find_market(markets: list, line: float) -> dict | None:
    """Find the game total points market with the given lineValue."""
    for market in markets:
        if market.get("marketTypeId") != GAME_TOTAL_TYPE_ID:
            continue
        if market.get("lineValue") != line:
            continue
        return market
    return None


def _get_over_selection(market: dict) -> dict | None:
    """Return the Over selection if active and odd meets minimum."""
    for sel in market.get("selections", []):
        if sel.get("name") != TARGET_SELECTION:
            continue
        odd = sel.get("odd", {})
        if odd.get("statusId") != 1:
            return None
        if odd.get("value", 0.0) < MIN_ODD:
            return None
        return sel
    return None


def _pick_market(markets: list) -> tuple[dict, dict, float] | tuple[None, None, None]:
    """
    Try TARGET_LINES in order. Return (market, over_sel, line) for the
    first line whose Over selection exists and meets MIN_ODD, else (None, None, None).
    """
    for line in TARGET_LINES:
        market = _find_market(markets, line)
        if market is None:
            print(f"[analyzer] no market : game total {line} not found")
            continue
        over_sel = _get_over_selection(market)
        if over_sel is None:
            print(f"[analyzer] no signal : Over {line} suspended or odd below {MIN_ODD}")
            continue
        return market, over_sel, line
    return None, None, None


async def fetch_event(event_id: int) -> dict | None:
    url = EVENT_URL.format(fixture_id=event_id)

    try:
        import os, sys
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from supabase import create_client
        base   = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        config = {}
        with open(os.path.join(base, "db.txt")) as f:
            for line in f:
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    k, _, v = line.partition("=")
                    config[k.strip()] = v.strip().strip('"')
        sb     = create_client(config["SUPABASE_URL"], config["SUPABASE_KEY"])
        cookie = sb.table("betking").select("cookie").eq("id", 1).single().execute().data["cookie"]
    except Exception as e:
        print(f"[analyzer] cookie err {event_id}: {e}")
        cookie = ""

    headers = {
        **EVENT_HEADERS,
        "Referer": _build_referer(event_id),
        "Cookie":  cookie,
    }

    try:
        async with httpx.AsyncClient(http2=True, timeout=15.0) as client:
            resp = await client.get(url, headers=headers)
        if resp.status_code != 200:
            print(f"[analyzer] fetch failed {event_id}: HTTP {resp.status_code}")
            return None
        if not resp.text or not resp.text.strip():
            print(f"[analyzer] fetch failed {event_id}: empty response")
            return None
        data = resp.json()
        if not isinstance(data, dict) or "event" not in data:
            print(f"[analyzer] fetch failed {event_id}: unexpected response | keys={list(data.keys()) if isinstance(data, dict) else type(data)}")
            return None
        return data
    except Exception as e:
        print(f"[analyzer] fetch error {event_id}: {e}")
        return None


async def analyze(event_id: int, staker=None) -> None:
    raw = await fetch_event(event_id)
    if raw is None:
        return

    event   = raw.get("event", {})
    markets = raw.get("markets", [])

    match_name     = event.get("name", "?")
    period_scores  = event.get("periodScores", "")
    tournament_id  = event.get("tournamentId")
    tournament     = event.get("tournamentName", "?")
    category_id    = event.get("categoryId")
    category_name  = event.get("categoryName", "")
    event_date     = event.get("date", "")
    match_status   = event.get("matchStatus", "")

    print(f"[analyzer] checking  : {match_name}")
    print(f"[analyzer] sets      : {period_scores}")
    print(f"[analyzer] status    : {match_status}")

    sets = _parse_period_scores(period_scores)
    if not _sets_qualify(sets):
        details = " | ".join(
            f"set{i+1}: {h}:{a} (loser={min(h,a)}>={SET_THRESHOLDS[i]}?{'YES' if min(h,a)>=SET_THRESHOLDS[i] else 'NO'})"
            for i, (h, a) in enumerate(sets[:3])
        )
        print(f"[analyzer] no signal : sets did not qualify | {details}")
        print()
        return

    print(f"[analyzer] sets ok   : all 3 thresholds met")

    market, over_sel, chosen_line = _pick_market(markets)
    if market is None:
        print(f"[analyzer] no signal : no qualifying line found in {TARGET_LINES}")
        print()
        return

    odd_value    = over_sel["odd"]["value"]
    selection_id = over_sel["id"]

    print(f"[analyzer] signal    : {match_name} | Over {chosen_line} @ {odd_value}")
    print()

    payload = {
        "eventId":         event_id,
        "matchId":         event_id,
        "parentEventId":   event_id,
        "matchName":       match_name,
        "matchKMId":       event_id,
        "tournamentId":    tournament_id,
        "tournamentName":  tournament,
        "categoryId":      category_id,
        "categoryName":    category_name,
        "eventDate":       event_date,
        "marketId":        market["marketId"],
        "marketName":      market["name"],
        "marketTypeId":    market["marketTypeId"],
        "marketKMId":      market["marketId"] - 240_000_000,
        "specialValue":    str(market.get("specialValue", "")),
        "selectionId":     selection_id,
        "selectionName":   f"Over ({market.get('specialValue', '')})",
        "selectionKMId":   selection_id - 820_000_000,
        "selectionTypeId": over_sel.get("typeId"),
        "oddValue":        odd_value,
        "lineValue":       chosen_line,
        "periodScores":    period_scores,
    }

    if staker is not None:
        await staker.queue(payload)