import httpx
import time
import re

MIN_ODD = 1.12
MAX_SIGNALS = 3


def parse_current_score(set_score: str) -> int:
    try:
        parts = set_score.split(":")
        return int(parts[0]) + int(parts[1])
    except Exception:
        return 0


def get_target_under(total_goals: int) -> float:
    return total_goals + 0.5


def slugify(name: str) -> str:
    name = name.strip()
    name = re.sub(r"\s+", "_", name)
    name = re.sub(r"[^\w]", "", name)
    return name


def build_match_url(event_data: dict) -> str:
    category   = event_data.get("sport", {}).get("category", {})
    country    = slugify(category.get("name", "unknown"))
    tournament = slugify(category.get("tournament", {}).get("name", "unknown"))
    home       = slugify(event_data.get("homeTeamName", "home"))
    away       = slugify(event_data.get("awayTeamName", "away"))
    event_id   = event_data.get("eventId", "")
    return f"https://www.sportybet.com/ng/sport/football/live/{country}/{tournament}/{home}_vs_{away}/{event_id}"


def find_under_market(markets: list, target_under: float) -> dict | None:
    target_desc = f"Under {target_under:g}"

    for market in markets:
        if market.get("id") != "68":
            continue
        if market.get("desc") != "1st Half - Over/Under":
            continue
        if "|" in market.get("specifier", ""):
            continue

        for outcome in market.get("outcomes", []):
            if outcome.get("desc") != target_desc:
                continue

            is_active = outcome.get("isActive", 0)
            odds_str  = outcome.get("odds", "0")
            odds_val  = float(odds_str)

            return {
                "market_id":   market.get("id"),
                "specifier":   market.get("specifier", ""),
                "outcome_id":  outcome.get("id"),
                "desc":        outcome.get("desc"),
                "market_desc": market.get("desc"),
                "odds":        odds_str,
                "isActive":    is_active,
                "probability": outcome.get("probability"),
                "active_ok":   is_active == 1,
                "odds_ok":     odds_val >= MIN_ODD,
                "playable":    is_active == 1 and odds_val >= MIN_ODD,
            }

    return None


# Shared counter across all concurrent analyze() calls
_signal_count = 0


def reset_signal_count():
    global _signal_count
    _signal_count = 0


async def analyze(event_id: str, headers: dict, staker=None):
    global _signal_count

    if _signal_count >= MAX_SIGNALS:
        return

    timestamp = int(time.time() * 1000)
    url       = "https://www.sportybet.com/api/ng/factsCenter/event"
    params    = {
        "eventId":   event_id,
        "productId": 1,
        "_t":        timestamp,
    }

    request_headers = {
        **headers,
        "Referer": f"https://www.sportybet.com/ng/sport/football/live/{event_id}",
    }

    try:
        async with httpx.AsyncClient(http2=True, timeout=15.0) as client:
            response = await client.get(url, headers=request_headers, params=params)

        if response.status_code != 200:
            print(f"[analyzer] {event_id} -> HTTP {response.status_code}")
            return

        data       = response.json()
        event_data = data.get("data", {})
        match      = f"{event_data.get('homeTeamName', '?')} vs {event_data.get('awayTeamName', '?')}"
        set_score  = event_data.get("setScore", "0:0")
        markets    = event_data.get("markets", [])
        played     = event_data.get("playedSeconds", "?")

        total_goals  = parse_current_score(set_score)
        target_under = get_target_under(total_goals)
        result       = find_under_market(markets, target_under)

        if result is None:
            print(f"[analyzer] {event_id} -> {match} | {set_score} | Target: Under {target_under:g} | market not found")
            return

        if not result["playable"]:
            print(f"[analyzer] {event_id} -> {match} | {set_score} | Target: Under {target_under:g} | not playable (active={result['isActive']} odds={result['odds']})")
            return

        # Guard: check again after the async fetch in case others filled the slots
        if _signal_count >= MAX_SIGNALS:
            return

        _signal_count += 1

        match_url = build_match_url(event_data)

        print(f"\n[analyzer] {event_id} -> {match}")
        print(f"           Score     : {set_score}  (total goals: {total_goals})")
        print(f"           Target    : Under {target_under:g}")
        print(f"           Played    : {played}")
        print(f"           market_id : {result['market_id']}")
        print(f"           desc      : {result['desc']}")
        print(f"           odds      : {result['odds']}")
        print(f"           isActive  : {result['isActive']}")
        print(f"           PLAYABLE  : {result['desc']} @ {result['odds']}  [{_signal_count}/{MAX_SIGNALS}]")
        print(f"           URL       : {match_url}")
        print(f"           --desc \"{result['market_desc']}\" --market \"{result['desc']}\"")

        if staker:
            await staker.queue(match_url, result["market_desc"], result["desc"])

    except Exception as e:
        print(f"[analyzer] {event_id} -> error: {e}")