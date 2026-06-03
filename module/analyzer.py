import httpx
import random
import asyncio


#Config
HT_TOTAL_TYPE_ID = 161
HT_DC_TYPE_ID    = 9588

TIME_WINDOW_GAP = {
    (25, 30): 3,
    (30, 40): 2,
}

# HT DC: minimum goal lead required per window to qualify
HT_DC_LEAD_GAP = {
    (25, 30): 2,
    (30, 35): 1,
}

MIN_ODD     = 1.10
MAX_SIGNALS = 3

BETKING_HEADERS = {
    "User-Agent":      "Mozilla/5.0 (X11; Linux x86_64; rv:140.0) Gecko/20100101 Firefox/140.0",
    "Accept":          "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.5",
    "Origin":          "https://www.betking.com",
    "Referer":         "https://www.betking.com/",
    "Sec-Fetch-Dest":  "empty",
    "Sec-Fetch-Mode":  "cors",
    "Sec-Fetch-Site":  "same-site",
    "Sec-Gpc":         "1",
    "Priority":        "u=6",
    "Cache-Control":   "max-age=0",
    "Te":              "trailers",
}

_signal_count = 0
_signal_lock  = asyncio.Lock()


def reset_signal_count():
    global _signal_count
    _signal_count = 0


def _get_window_gap(match_time, windows):
    for (lo, hi), gap in windows.items():
        if lo <= match_time <= hi:
            return gap
    return None


def _parse_score(score_str):
    try:
        h, a = score_str.strip().split(":")
        return int(h), int(a)
    except Exception:
        return None


# --- Primary: 1st Half Under ---

def _find_ht_total_market(markets, target_line):
    target_str = str(target_line)
    for market in markets:
        if market.get("TypeId") != HT_TOTAL_TYPE_ID:
            continue
        if str(market.get("SpecialValue", "")) != target_str:
            continue
        return market
    return None


def _get_under_selection(market):
    for sel in market.get("Selections", []):
        if sel.get("Name", "").lower() != "under":
            continue
        odds_list = sel.get("Odds", [])
        if not odds_list:
            return None, "no odds data"
        odd_obj = odds_list[0]
        if odd_obj.get("Status") != 1:
            return None, "market suspended"
        value = odd_obj.get("Value", 0.0)
        if value < MIN_ODD:
            return None, f"odd {value} below minimum {MIN_ODD}"
        return sel, None
    return None, "under selection not found"


# --- Fallback: HT DC ---

def _find_ht_dc_market(markets):
    for market in markets:
        if market.get("TypeId") == HT_DC_TYPE_ID:
            return market
    return None


def _get_dc_selection(market, home_goals, away_goals):
    """
    If home is winning -> 1X (home team can't lose)
    If away is winning -> X2 (away team can't lose)
    Draw -> no signal (no lead, DC doesn't apply)
    """
    if home_goals > away_goals:
        target_name = "1X"
    elif away_goals > home_goals:
        target_name = "X2"
    else:
        return None, "draw - no lead for DC"

    for sel in market.get("Selections", []):
        if sel.get("Name") != target_name:
            continue
        odds_list = sel.get("Odds", [])
        if not odds_list:
            return None, "no odds data"
        odd_obj = odds_list[0]
        if odd_obj.get("Status") != 1:
            return None, f"{target_name} suspended"
        value = odd_obj.get("Value", 0.0)
        if value < MIN_ODD:
            return None, f"{target_name} odd {value} below minimum {MIN_ODD}"
        return sel, None

    return None, f"{target_name} selection not found"


async def fetch_event(event_id):
    url = f"https://sportsapicdn-desktop.betking.com/api/feeds/live/{event_id}/en"
    async with httpx.AsyncClient(http2=True, timeout=15.0) as client:
        resp = await client.get(url, headers=BETKING_HEADERS)
    if resp.status_code != 200:
        print(f"[analyzer] fetch failed {event_id}: HTTP {resp.status_code}")
        return None
    return resp.json()


def extract_event(data):
    try:
        return data["Tournaments"][0]["Events"][0]
    except (KeyError, IndexError):
        return None


async def analyze(event_id, match_time, staker=None):
    global _signal_count

    async with _signal_lock:
        if _signal_count >= MAX_SIGNALS:
            return

    raw = await fetch_event(event_id)
    if raw is None:
        return

    event = extract_event(raw)
    if event is None:
        return

    match_name    = event.get("Name", "?")
    score_str     = event.get("Score", "")
    markets       = event.get("Markets", [])
    tournament_id = event.get("TournamentId")
    tournament    = event.get("TournamentName", "?")
    category_id   = event.get("CategoryId")
    category_name = event.get("CategoryName", "")
    teams         = event.get("Teams", [])
    home_team     = next((t["Name"] for t in teams if t.get("ItemOrder") == 1), "Home")
    away_team     = next((t["Name"] for t in teams if t.get("ItemOrder") == 2), "Away")

    parsed = _parse_score(score_str)
    if parsed is None:
        print(f"[analyzer] {match_name} - bad score '{score_str}'")
        return

    home_goals, away_goals = parsed
    total_goals = home_goals + away_goals

    print(f"[analyzer] checking  : {match_name}")
    print(f"[analyzer] score     : {score_str} | time: {match_time}'")

    chosen_market = None
    chosen_sel    = None
    chosen_label  = None
    chosen_odd    = None
    special_value = ""

    # --- Primary: 1st Half Under ---
    under_gap = _get_window_gap(match_time, TIME_WINDOW_GAP)
    if under_gap is not None:
        target_line   = float(total_goals + under_gap) - 0.5
        special_value = str(target_line)
        print(f"[analyzer] primary   : Under {target_line} (gap={under_gap})")
        market = _find_ht_total_market(markets, target_line)
        if market is None:
            print(f"[analyzer] primary   : no market for Under {target_line}")
        else:
            under_sel, reason = _get_under_selection(market)
            if under_sel is not None:
                chosen_market = market
                chosen_sel    = under_sel
                chosen_label  = f"Under {target_line}"
                chosen_odd    = under_sel["Odds"][0]["Value"]
            else:
                print(f"[analyzer] primary   : {reason}")

    # --- Fallback: HT DC ---
    if chosen_sel is None:
        dc_gap = _get_window_gap(match_time, HT_DC_LEAD_GAP)
        if dc_gap is None:
            print(f"[analyzer] fallback  : time {match_time}' not in DC window")
        else:
            lead = abs(home_goals - away_goals)
            if lead < dc_gap:
                print(f"[analyzer] fallback  : lead {lead} below required {dc_gap} for DC")
            else:
                dc_market = _find_ht_dc_market(markets)
                if dc_market is None:
                    print(f"[analyzer] fallback  : HT DC market not found")
                else:
                    dc_sel, reason = _get_dc_selection(dc_market, home_goals, away_goals)
                    if dc_sel is not None:
                        chosen_market = dc_market
                        chosen_sel    = dc_sel
                        chosen_label  = dc_sel["Name"]
                        chosen_odd    = dc_sel["Odds"][0]["Value"]
                        special_value = ""
                    else:
                        print(f"[analyzer] fallback  : {reason}")

    if chosen_sel is None:
        print(f"[analyzer] no signal : {match_name}")
        print()
        return

    async with _signal_lock:
        if _signal_count >= MAX_SIGNALS:
            print()
            return
        _signal_count += 1

    print(f"[analyzer] signal    : {match_name} | {chosen_label} @ {chosen_odd}")
    print()

    if staker is None:
        return

    await staker.queue({
        "eventId":              event_id,
        "matchId":              event_id,
        "parentEventId":        event_id,
        "matchName":            match_name,
        "matchKMId":            event_id % 10_000_000,
        "tournamentId":         tournament_id,
        "tournamentName":       tournament,
        "categoryId":           category_id,
        "categoryName":         category_name,
        "marketId":             chosen_market["Id"],
        "marketName":           chosen_market["Name"],
        "marketTypeId":         chosen_market["TypeId"],
        "marketKMId":           chosen_market["Id"] % 10_000_000,
        "specialValue":         special_value,
        "selectionId":          chosen_sel["Id"],
        "selectionName":        chosen_label,
        "selectionKMId":        1_000_000_000 + chosen_sel["Id"] % 100_000_000,
        "selectionTypeId":      chosen_sel.get("TypeId"),
        "oddValue":             chosen_odd,
        "score":                score_str,
        "matchTime":            match_time,
        "targetLine":           chosen_label,
        "homeTeam":             home_team,
        "awayTeam":             away_team,
        "requestTransactionId": str(random.randint(10_000_000_000, 99_999_999_999)),
    })
