import asyncio
import json
import math
import os
import random
import re
import time
import urllib.parse

import httpx

from common import norm

# ---------------------------------------------------------------------------
# Canonical name helper (strips parentheses suffixes)
# ---------------------------------------------------------------------------
_PAREN_SUFFIX_RE = re.compile(r"\([^)]*\)")

def canon_name(name: str) -> tuple:
    name = _PAREN_SUFFIX_RE.sub("", name or "")
    name = name.replace(",", " ")
    tokens = [norm(tok) for tok in name.split() if tok.strip()]
    return tuple(sorted(tokens))

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
INTERVAL_SECONDS       = 5
WORKER_POLL_SECONDS    = 7
MAX_WORKERS            = 10
REQUEST_TIMEOUT        = 10.0

MIN_ARB_PCT            = 5.0   # only log to console if margin >= this

MIN_STAKE_ARB_PCT      = 5.0  # only stake if margin >= this
RECOVERY_MIN_ARB_PCT   = 5.0   # minimum margin accepted during recovery retries
RECOVERY_REFRESHES     = 6

EXCLUDED_MARKET_TYPES  = set()

TOTAL_STAKE     = 40
ROUND_INCREMENT = max(1, 10 ** (len(str(int(TOTAL_STAKE))) - 2))
MIN_STAKE       = 10

B9_POLL_INTERVAL       = 1.0
B9_POLL_MAX_ATTEMPTS   = 8

BETKING_ACCOUNT_NAME   = "Chukwuebuka"
BET9JA_ACCOUNT_NAME    = "wixnation"

# ---------------------------------------------------------------------------
# Stake sizing helpers
# ---------------------------------------------------------------------------
def calculate_stakes(odd_a: float, odd_b: float, total: float) -> tuple[float, float]:
    stake_a = total * odd_b / (odd_a + odd_b)
    stake_b = total - stake_a
    return round(stake_a, 2), round(stake_b, 2)

def round_stakes(odd_a: float, odd_b: float, total: float, increment: float = ROUND_INCREMENT) -> tuple[float, float]:
    min_a = total / odd_a
    max_a = total - total / odd_b

    if min_a > max_a:
        return calculate_stakes(odd_a, odd_b, total)

    lo = math.ceil(min_a / increment) * increment
    hi = math.floor(max_a / increment) * increment

    valid = []
    val = lo
    while val <= hi + 1e-9:
        stake_a = round(val, 2)
        stake_b = round(total - stake_a, 2)
        profit_a = round(stake_a * odd_a - total, 4)
        profit_b = round(stake_b * odd_b - total, 4)
        if profit_a >= 0 and profit_b >= 0 and stake_a >= MIN_STAKE and stake_b >= MIN_STAKE:
            valid.append((stake_a, stake_b))
        val = round(val + increment, 2)

    if not valid:
        print("[TN_live stake] rounding skipped — no valid rounded pair, using exact split")
        return calculate_stakes(odd_a, odd_b, total)

    return random.choice(valid)

# ---------------------------------------------------------------------------
# Cookie helpers (using "cookies" table)
# ---------------------------------------------------------------------------
def _load_db_config() -> dict:
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    config = {}
    with open(os.path.join(base, "db.txt"), "r") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                k, _, v = line.partition("=")
                config[k.strip()] = v.strip().strip('"')
    return config

def _get_supabase():
    from supabase import create_client
    config = _load_db_config()
    return create_client(config["SUPABASE_URL"], config["SUPABASE_KEY"])

def get_cookie(account_name: str) -> str:
    try:
        sb = _get_supabase()
        row = (
            sb.table("cookies")
            .select("cookie")
            .eq("name", account_name)
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )
        if row.data:
            return row.data[0].get("cookie") or ""
        print(f"[TN_live] no cookie row found for '{account_name}'")
        return ""
    except Exception as e:
        print(f"[TN_live] cookie lookup failed for '{account_name}': {e}")
        return ""

# ---------------------------------------------------------------------------
# BetKing API (sportId = 5 for tennis)
# ---------------------------------------------------------------------------
SPORT_ID_BK = 5

BETKING_LIVE_URL = (
    f"https://m.betking.com/en-ng/sports/live/api/overview/{SPORT_ID_BK}"
    "?_data=routes%2F%28%24locale%29.sports.live.api.overview.%24sportId"
)
BETKING_BET_URL = (
    "https://m.betking.com/en-ng/sports/action/placebet"
    "?_data=routes%2F%28%24locale%29.sports.action.placebet"
)
BETKING_LIVE_HEADERS = {
    "User-Agent":      "Mozilla/5.0 (X11; Linux x86_64; rv:140.0) Gecko/20100101 Firefox/140.0",
    "Accept":          "*/*",
    "Accept-Language": "en-US,en;q=0.5",
    "Referer":         "https://m.betking.com/en-ng/sports/live/tennis/5",
}

_BK_GLOBAL_VAR = {
    "currencyId": -1,
    "defaultStakeGross": 100,
    "isVirtualsInstallation": False,
    "maxBetStake": 75000000,
    "maxCombinationBetWin": 75000000,
    "maxCombinationsByGrouping": 10000,
    "maxCouponCombinations": 10000,
    "maxGroupingsBetStake": 41641682,
    "maxMultipleBetWin": 75000000,
    "maxNoOfEvents": 40,
    "maxNoOfSelections": 40,
    "maxSingleBetWin": 75000000,
    "minBetStake": 10,
    "minBonusOdd": 1.35,
    "minFlexiCutOdds": 1.05,
    "minFlexiCutSelections": 5,
    "minGroupingsBetStake": 5,
    "stakeInnerMod0Combination": 0.01,
    "stakeMod0Multiple": 0,
    "stakeMod0Single": 0,
    "stakeThresholdMultiple": 75000,
    "stakeThresholdSingle": 7500,
    "flexiCutGlobalVariable": {
        "parameters": {
            "formulaId": 1,
            "minOddThreshold": 1.05,
            "minWinningSelections": 2,
        }
    },
}

async def fetch_betking_live(client: httpx.AsyncClient) -> dict:
    headers = dict(BETKING_LIVE_HEADERS)
    cookie = get_cookie(BETKING_ACCOUNT_NAME)
    if cookie:
        headers["Cookie"] = cookie
    resp = await client.get(BETKING_LIVE_URL, headers=headers, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    return resp.json()

async def fetch_betking_event(client: httpx.AsyncClient, fixture_id: int, area_id: int = 1) -> dict:
    url = (
        f"https://m.betking.com/en-ng/sports/live/api/event"
        f"?areaId={area_id}&fixtureId={fixture_id}"
        f"&_data=routes%2F%28%24locale%29.sports.live.api.event"
    )
    headers = dict(BETKING_LIVE_HEADERS)
    cookie = get_cookie(BETKING_ACCOUNT_NAME)
    if cookie:
        headers["Cookie"] = cookie
    resp = await client.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    return resp.json()

def parse_betking_live(bk: dict) -> dict:
    sport_data = bk.get("sportData") or []
    if not sport_data:
        return {}
    matches = {}
    for tournament in sport_data[0].get("tournaments", []):
        for ev in tournament.get("events", []):
            home = ev.get("homeTeam")
            away = ev.get("awayTeam")
            if not home or not away:
                teams = ev.get("teams", [])
                if len(teams) == 2:
                    home, away = teams[0].get("name"), teams[1].get("name")
            if not home or not away:
                continue
            key = (canon_name(home), canon_name(away))
            matches[key] = {
                "home": home,
                "away": away,
                "event_id": ev.get("id"),
                "tournament": tournament.get("name"),
                "status_label": ev.get("matchStatusLabel"),
                "score": ev.get("score"),
            }
    return matches

def _flatten_betking_markets(raw_markets: list) -> list:
    seen = set()
    out = []
    for m in raw_markets:
        mid = m.get("marketId")
        if mid not in seen:
            seen.add(mid)
            out.append(m)
        for sub in m.get("spreadMarkets", []) or []:
            smid = sub.get("marketId")
            if smid not in seen:
                seen.add(smid)
                out.append(sub)
    return out

def parse_betking_match_info(full_match_raw: dict) -> dict:
    """Extract match time, score, etc. from the BetKing full match event."""
    ev = full_match_raw.get("event")
    if not ev:
        return {}
    return {
        "time": ev.get("matchTime"),
        "score": ev.get("score"),
        "home_score": ev.get("homeScore"),
        "away_score": ev.get("awayScore"),
        "set_scores": ev.get("setScores"),
        "period_scores": ev.get("periodScores"),
        "status_id": ev.get("matchStatusId"),
        "status_label": ev.get("matchStatusLabel"),
    }

def parse_betking_event(markets: list, bk_raw: dict = None) -> dict:
    """
    Parse tennis markets from BetKing event.
    ONLY Set Winner markets are kept.
    """
    ev_meta = {}
    if bk_raw:
        event = bk_raw.get("event") or {}
        ev_meta = {
            "matchId":        event.get("id") or event.get("fixtureId"),
            "matchName":      event.get("name", ""),
            "eventId":        event.get("categoryId"),
            "eventName":      event.get("categoryName", "International"),
            "tournamentId":   event.get("tournamentId"),
            "tournamentName": event.get("tournamentName", ""),
        }

    flat = _flatten_betking_markets(markets)
    out = {}

    for m in flat:
        market_type_id = m.get("marketTypeId")
        name = m.get("name", "")
        specifiers = m.get("specifiers", {})
        selections = m.get("selections") or []
        if len(selections) < 2:
            continue

        odd_by_name = {}
        sel_by_name = {}
        for s in selections:
            sel_name = s.get("name")
            status = s.get("status")
            odd_val = s.get("odd", {}).get("value")
            if sel_name is not None and status == "VALID" and odd_val is not None and odd_val > 0:
                odd_by_name[sel_name] = float(odd_val)
                sel_by_name[sel_name] = s

        def _sel_meta(sel_name):
            s = sel_by_name.get(sel_name) or {}
            sid = s.get("id")
            return {
                "selectionId":     sid,
                "selectionName":   s.get("name"),
                "IDSelectionType": s.get("typeId"),
                "selectionKMId":   (sid - 820_000_000) if sid is not None else None,
                "oddValue":        odd_by_name.get(sel_name),
            }

        market_id = m.get("marketId")
        match_id  = ev_meta.get("matchId")
        bk_meta_base = {
            "marketId":     market_id,
            "marketTypeId": market_type_id,
            "specialValue": m.get("specialValue"),
            **ev_meta,
            "marketKMId": (market_id - 240_000_000) if market_id is not None else None,
            "matchKMId":  (match_id - 30_000_000) if match_id is not None else None,
        }

        # ----- ONLY Set Winner (typeId 9389) is kept -----
        if market_type_id == 9389 and "Set {line} - Winner" in name:
            set_num = specifiers.get("line")
            if set_num is None:
                set_num = m.get("specialValue")
            if set_num is None:
                continue
            try:
                set_num = int(set_num)
            except (TypeError, ValueError):
                continue
            if "1" in odd_by_name and "2" in odd_by_name:
                key = ("set_winner", set_num)
                out[key] = {
                    "market_type": "set_winner",
                    "label": f"Set {set_num} Winner",
                    "outcomes": {"1": odd_by_name["1"], "2": odd_by_name["2"]},
                    "line": set_num,
                    "set_num": set_num,
                    "game_num": None,
                    "bk_meta": {**bk_meta_base, "marketName": f"Set {set_num} - Winner", "sel": {"1": _sel_meta("1"), "2": _sel_meta("2")}},
                }

    return out

def _betking_match_ended(full_match_raw: dict) -> bool:
    return full_match_raw.get("event") is None and not full_match_raw.get("markets")

# ---------------------------------------------------------------------------
# Bet9ja API (sportId = 3000005 for tennis)
# ---------------------------------------------------------------------------
BET9JA_SPORT_ID  = "3000005"
BET9JA_VERSION   = "1.318.4.243"
BET9JA_LIVE_URL  = (
    "https://sports.bet9ja.com/desktop/feapi/PalimpsestLiveAjax/GetLiveEventsV3"
    f"?SID={BET9JA_SPORT_ID}&v_cache_version={BET9JA_VERSION}"
)
BET9JA_EVENT_URL_TEMPLATE = (
    "https://sports.bet9ja.com/desktop/feapi/PalimpsestLiveAjax/GetLiveEventV2"
    f"?ISMKT=1&v_cache_version={BET9JA_VERSION}&EVENTID={{event_id}}"
)
BET9JA_BET_URL = (
    "https://apigw.bet9ja.com/sportsbook/placebet/PlacebetV2"
    f"?source=desktop&v_cache_version={BET9JA_VERSION}"
)
BET9JA_PENDING_URL = "https://apigw.bet9ja.com/sportsbook/placebet/GetPendingCouponState"
BET9JA_HEADERS = {
    "User-Agent":      "Mozilla/5.0 (X11; Linux x86_64; rv:140.0) Gecko/20100101 Firefox/140.0",
    "Accept":          "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.5",
    "Referer":         "https://sports.bet9ja.com/",
}

async def fetch_bet9ja_live(client: httpx.AsyncClient) -> dict:
    headers = dict(BET9JA_HEADERS)
    cookie = get_cookie(BET9JA_ACCOUNT_NAME)
    if cookie:
        headers["Cookie"] = cookie
    resp = await client.get(BET9JA_LIVE_URL, headers=headers, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    return resp.json()

async def fetch_bet9ja_event(client: httpx.AsyncClient, event_id: str) -> dict:
    url = BET9JA_EVENT_URL_TEMPLATE.format(event_id=event_id)
    headers = dict(BET9JA_HEADERS)
    headers["Referer"] = f"https://sports.bet9ja.com/liveEvent/{event_id}"
    cookie = get_cookie(BET9JA_ACCOUNT_NAME)
    if cookie:
        headers["Cookie"] = cookie
    resp = await client.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    return resp.json()

def parse_bet9ja_live(b9: dict) -> dict:
    try:
        E = b9["D"]["E"]
    except (KeyError, TypeError):
        return {}
    events_iter = E.items() if isinstance(E, dict) else (
        (str(ev.get("ID", i)), ev) for i, ev in enumerate(E)
    )
    matches = {}
    for eid, ev in events_iter:
        if ev.get("SID") != int(BET9JA_SPORT_ID):
            continue
        ds = ev.get("DS", "")
        if " - " in ds:
            home, away = ds.split(" - ", 1)
        elif " v " in ds:
            home, away = ds.split(" v ", 1)
        else:
            continue
        home, away = home.strip(), away.strip()
        aod = ev.get("EOD", {}).get("A", {})
        r   = aod.get("R", {})
        key = (canon_name(home), canon_name(away))
        matches[key] = {
            "home": home,
            "away": away,
            "event_id": str(ev["ID"]),
            "status_label": aod.get("ES", ""),
            "score": r.get("S", ""),
        }
    return matches

def parse_bet9ja_match_info(b9_raw: dict) -> dict:
    """Extract match time, score, etc. from Bet9ja D.A object."""
    try:
        A = b9_raw["D"]["A"]
    except (KeyError, TypeError):
        return {}
    r = A.get("R", {})
    time_str = A.get("T2", "")
    minutes = None
    if time_str and "'" in time_str:
        try:
            minutes = int(time_str.replace("'", ""))
        except ValueError:
            pass
    return {
        "time": minutes,
        "time_str": time_str,
        "score": r.get("S"),
        "set_scores": r.get("SS"),
        "status_label": A.get("ES"),
    }

def parse_bet9ja_event(b9_raw: dict) -> dict:
    """
    Parse tennis markets from Bet9ja GetLiveEventV2 response.
    ONLY Set Winner markets are kept.
    """
    try:
        D = b9_raw["D"]
    except (KeyError, TypeError):
        return {}

    odds = D.get("O") or {}
    aa = D.get("AA") or {}
    event_id = str(aa.get("ID", ""))
    out = {}

    # ----- ONLY Set Winner (LIVET_12P{set}_1 / _2) is kept -----
    for key, val in odds.items():
        if key.startswith("LIVET_12P") and "_" in key:
            try:
                rest = key.replace("LIVET_12P", "").split("_", 1)
                if len(rest) != 2:
                    continue
                set_str, side = rest
                set_num = int(set_str)
            except (ValueError, IndexError):
                continue
            if side not in ("1", "2"):
                continue
            odd = val.get("v")
            if odd is None or odd <= 1:
                continue
            mkey = ("set_winner", set_num)
            if mkey not in out:
                out[mkey] = {
                    "market_type": "set_winner",
                    "label": f"Set {set_num} Winner",
                    "outcomes": {},
                    "line": set_num,
                    "set_num": set_num,
                    "game_num": None,
                    "b9_meta": {"event_id": event_id, "sel": {}},
                }
            out[mkey]["outcomes"][side] = float(odd)
            out[mkey]["b9_meta"]["sel"][side] = key
    # Keep only those with both 1 and 2
    for mkey in list(out.keys()):
        if mkey[0] == "set_winner" and len(out[mkey]["outcomes"]) != 2:
            del out[mkey]

    return out

def _bet9ja_match_ended(b9_raw: dict) -> bool:
    if b9_raw.get("D") is False:
        return True
    try:
        return b9_raw["D"]["AA"].get("ST") != 1
    except (KeyError, TypeError):
        return True

# ---------------------------------------------------------------------------
# Stake payload builders
# ---------------------------------------------------------------------------
def build_bk_payload(market_entry: dict, outcome_name: str, stake: float, allow_odd_changes: bool = False) -> str:
    meta = market_entry["bk_meta"]
    sel  = meta["sel"][outcome_name]
    odd  = sel["oddValue"]
    win  = round(stake * odd, 10)

    selection = {
        "IDSelectionType":    sel.get("IDSelectionType"),
        "IDSport":            SPORT_ID_BK,
        "allowFixed":         True,
        "compatibilityLevel": 0,
        "eventCategory":      "L",
        "eventId":            meta.get("eventId"),
        "eventName":          meta.get("eventName", "International"),
        "fixed":              False,
        "gamePlay":           1,
        "incompatibleEvents": [],
        "isExpired":          False,
        "isLocked":           False,
        "isBetBuilder":       False,
        "marketId":           meta["marketId"],
        "marketName":         meta["marketName"],
        "marketTag":          0,
        "marketTypeId":       meta["marketTypeId"],
        "matchId":            meta.get("matchId"),
        "matchName":          meta.get("matchName", ""),
        "oddValue":           odd,
        "parentEventId":      meta.get("matchId"),
        "selectionId":        sel["selectionId"],
        "selectionName":      sel["selectionName"],
        "smartCode":          0,
        "specialValue":       meta.get("specialValue"),
        "sportName":          "Tennis",
        "tournamentId":       meta.get("tournamentId"),
        "tournamentName":     meta.get("tournamentName", ""),
        "selectionKMId":      sel.get("selectionKMId"),
        "matchKMId":          meta.get("matchKMId"),
        "marketKMId":         meta.get("marketKMId"),
        "isTransitioned":     False,
    }

    grouping = {
        "grouping": 1, "combinations": 1,
        "minWin": win, "minWinNet": win, "netStakeMinWin": win,
        "maxWin": win, "maxWinNet": win, "netStakeMaxWin": win,
        "minBonus": 0, "maxBonus": 0,
        "minPercentageBonus": 0, "maxPercentageBonus": 0,
        "stake": stake, "netStake": stake, "selected": True,
    }

    bet_coupon = {
        "isClientSideCoupon": True,
        "couponTypeId": 1,
        "minWin": win, "minWinNet": win, "netStakeMinWin": win,
        "maxWin": win, "maxWinNet": win, "netStakeMaxWin": win,
        "minBonus": 0, "maxBonus": 0,
        "minPercentageBonus": 0, "maxPercentageBonus": 0,
        "minOdd": odd, "maxOdd": odd, "totalOdds": odd,
        "stake": stake,
        "useGroupsStake": False,
        "stakeGross": stake,
        "stakeTaxed": 0, "taxPercentage": 0, "tax": 0,
        "minWithholdingTax": 0, "maxWithholdingTax": 0, "turnoverTax": 0,
        "totalCombinations": 1,
        "odds": [selection],
        "groupings": [grouping],
        "possibleMissingGroupings": [],
        "currencyId": -1,
        "isLive": True,
        "isVirtual": False,
        "currentEvalMotivation": 0,
        "betCouponGlobalVariable": _BK_GLOBAL_VAR,
        "language": "en",
        "hasLive": True,
        "couponType": 1,
        "allGroupings": [grouping],
    }

    data_obj = {
        "betCoupon": bet_coupon,
        "allowOddChanges": allow_odd_changes,
        "allowStakeReduction": False,
        "requestTransactionId": str(random.randint(100000000000, 999999999999)),
        "transferStakeFromAgent": False,
        "trackingData": {
            "category": "tennis",
            "product": "sportsbook-live",
            "is_quick_slip": True,
            "bet_type": "Singles",
        },
    }

    return urllib.parse.urlencode({
        "data":      json.dumps(data_obj, separators=(",", ":")),
        "adjustIds": json.dumps({"adjustId": "", "adjustIdfa": "", "gpsAdId": ""}),
    })

def build_b9_payload(market_entry: dict, outcome_name: str, stake: float, accept_odds_changes: int = 0) -> str:
    meta     = market_entry["b9_meta"]
    od_key   = meta["sel"][outcome_name]
    sel_key  = f"{meta['event_id']}${od_key}"
    odd      = market_entry["outcomes"][outcome_name]
    odd_str  = str(odd)
    pot_win  = round(stake * odd, 10)

    betslip = {
        "BETS": [{
            "BSTYPE":    3,
            "TAB":       3,
            "NUMLINES":  1,
            "COMB":      1,
            "TYPE":      1,
            "STAKE":     stake,
            "POTWINMIN": pot_win,
            "POTWINMAX": pot_win,
            "BONUSMIN":  "0",
            "BONUSMAX":  "0",
            "ODDMIN":    odd_str,
            "ODDMAX":    odd_str,
            "ODDS":      {sel_key: odd_str},
            "FIXED":     {},
        }],
        "IMPERSONIZE": 0,
    }
    return urllib.parse.urlencode({
        "BETSLIP":             json.dumps(betslip, separators=(",", ":")),
        "BONUS":               "0",
        "ACCEPT_ODDS_CHANGES": str(accept_odds_changes),
        "IS_PASSBET":          "0",
        "IS_FIREBETS":         "0",
        "IS_CUT1":             "0",
    })

# ---------------------------------------------------------------------------
# Bet placement
# ---------------------------------------------------------------------------
_stake_lock   = asyncio.Lock()
_staked_arbs: set = set()

async def _place_bk_bet(client: httpx.AsyncClient, payload: str) -> dict:
    headers = {
        "User-Agent":      "Mozilla/5.0 (X11; Linux x86_64; rv:140.0) Gecko/20100101 Firefox/140.0",
        "Accept":          "*/*",
        "Accept-Language": "en-US,en;q=0.5",
        "Referer":         "https://m.betking.com/en-ng",
        "Content-Type":    "application/x-www-form-urlencoded;charset=UTF-8",
        "Origin":          "https://m.betking.com",
        "Dnt":             "1",
        "Sec-Gpc":         "1",
        "Sec-Fetch-Dest":  "empty",
        "Sec-Fetch-Mode":  "cors",
        "Sec-Fetch-Site":  "same-origin",
        "Priority":        "u=4",
        "Te":              "trailers",
        "Cookie":          get_cookie(BETKING_ACCOUNT_NAME),
    }
    resp = await client.put(BETKING_BET_URL, content=payload, headers=headers, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    return resp.json()

async def _place_b9_bet(client: httpx.AsyncClient, payload: str, event_id: str) -> dict:
    headers = {
        **BET9JA_HEADERS,
        "Referer":        f"https://sports.bet9ja.com/liveEvent/{event_id}",
        "Content-Type":   "application/x-www-form-urlencoded",
        "Origin":         "https://sports.bet9ja.com",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-site",
        "Cookie":         get_cookie(BET9JA_ACCOUNT_NAME),
    }
    resp = await client.post(BET9JA_BET_URL, content=payload, headers=headers, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    return resp.json()

async def _poll_b9_pending(client: httpx.AsyncClient, pending_id: int) -> tuple[int, dict]:
    url = f"{BET9JA_PENDING_URL}?CID={pending_id}&source=desktop&v_cache_version={BET9JA_VERSION}"
    headers = {
        **BET9JA_HEADERS,
        "Origin":         "https://sports.bet9ja.com",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-site",
        "Cookie":         get_cookie(BET9JA_ACCOUNT_NAME),
    }
    last_resp = {}
    for attempt in range(B9_POLL_MAX_ATTEMPTS):
        await asyncio.sleep(B9_POLL_INTERVAL)
        try:
            resp = await client.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            data = resp.json()
            last_resp = data
            top_status   = data.get("status")
            inner_status = (data.get("data") or {}).get("status") if isinstance(data.get("data"), dict) else None
            final = inner_status if inner_status is not None else top_status
            if attempt >= 2 and final in (1, 5):
                return final, last_resp
        except Exception:
            pass
    top_status   = last_resp.get("status")
    inner_status = (last_resp.get("data") or {}).get("status") if isinstance(last_resp.get("data"), dict) else None
    final = inner_status if inner_status is not None else top_status
    return (final if final in (1, 5) else -1), last_resp

# ---------------------------------------------------------------------------
# Arbitrage calculation and logging
# ---------------------------------------------------------------------------
def _arb_margin_pct(odds: list) -> float:
    valid = [o for o in odds if o > 0]
    if len(valid) < 2:
        return 100.0
    inv_sum = sum(1.0 / o for o in valid)
    return (1.0 - inv_sum) * 100.0

def _log_arb(match_label: str, mkey: tuple, bk_entry: dict, bj_entry: dict,
             trigger: str, match_info: dict = None):
    bk_out = bk_entry.get("outcomes", {})
    bj_out = bj_entry.get("outcomes", {})
    all_sel = set(bk_out.keys()) | set(bj_out.keys())
    best = {}
    source = {}
    for sel in all_sel:
        odds = []
        sources = []
        if sel in bk_out and bk_out[sel] > 0:
            odds.append(bk_out[sel])
            sources.append("BetKing")
        if sel in bj_out and bj_out[sel] > 0:
            odds.append(bj_out[sel])
            sources.append("Bet9ja")
        if odds:
            max_odd = max(odds)
            best[sel] = max_odd
            idx = odds.index(max_odd)
            source[sel] = sources[idx]

    if len(best) < 2:
        return None

    margin = _arb_margin_pct(list(best.values()))
    if margin >= MIN_ARB_PCT:
        label = bk_entry.get("label") or bj_entry.get("label") or str(mkey)
        extra = ""
        if mkey[0] == "total_games" and len(mkey) > 1:
            extra = f" line={mkey[1]}"
        elif mkey[0] == "set_winner" and len(mkey) > 1:
            extra = f" set={mkey[1]}"
        elif mkey[0] == "game_winner" and len(mkey) > 2:
            extra = f" set={mkey[1]} game={mkey[2]}"
        odds_info = {sel: (best[sel], source[sel]) for sel in best}

        context = ""
        if match_info:
            time_str = match_info.get("time")
            if time_str is not None:
                context += f" {time_str}'"
            score = match_info.get("score")
            if score:
                context += f" {score}"
            status = match_info.get("status_label")
            if status:
                context += f" [{status}]"

        print(
            f"[ARB] {match_label}{context} | {label}{extra} | "
            f"margin={margin:.2f}% | best_odds={odds_info} | (triggered by {trigger})"
        )

    return best, source, margin

async def _check_and_stake(
    client: httpx.AsyncClient,
    match_label: str,
    mkey: tuple,
    bk_entry: dict,
    bj_entry: dict,
    best: dict,
    source: dict,
    margin: float,
    bk_fixture_id: int,
    bj_event_id: str,
    min_margin: float = MIN_STAKE_ARB_PCT,
):
    if margin < min_margin or len(best) != 2:
        return

    outcomes = list(best.keys())
    books    = {source[o] for o in outcomes}
    if len(books) < 2:
        return

    bk_outcome = next(o for o in outcomes if source[o] == "BetKing")
    bj_outcome = next(o for o in outcomes if source[o] == "Bet9ja")
    stake_id = (match_label, mkey, tuple(sorted(books)))

    if stake_id in _staked_arbs:
        return
    if _stake_lock.locked():
        return

    async with _stake_lock:
        if stake_id in _staked_arbs:
            return
        _staked_arbs.add(stake_id)
        try:
            await execute_arb(
                client, match_label, mkey, bk_entry, bj_entry,
                bk_outcome, bj_outcome, best, source, margin,
                bk_fixture_id, bj_event_id,
            )
        except Exception as e:
            print(f"[TN_live stake] execute_arb error: {match_label} | {mkey} | {e}")

def _prune_stale_candidates(match_label: str, candidates: dict, cycle_num: int):
    for mkey in [k for k, c in candidates.items() if cycle_num - c["cycle"] > 1]:
        print(f"[TN_live stake] candidate expired (not confirmed on next poll): {match_label} | {mkey}")
        del candidates[mkey]

def _process_candidate(match_label: str, mkey: tuple, margin: float, candidates: dict, cycle_num: int) -> bool:
    cand = candidates.get(mkey)

    if margin < RECOVERY_MIN_ARB_PCT:
        if cand:
            print(f"[TN_live stake] candidate dropped — margin fell to {margin:.2f}% (< {RECOVERY_MIN_ARB_PCT}% floor): {match_label} | {mkey}")
            del candidates[mkey]
        return False

    if cand and cand["cycle"] == cycle_num - 1:
        del candidates[mkey]
        if margin < MIN_STAKE_ARB_PCT:
            print(
                f"[TN_live stake] candidate CONFIRMED at reduced margin {margin:.2f}% "
                f"(was {cand['margin']:.2f}% on prior poll, still >= {RECOVERY_MIN_ARB_PCT}% floor) — staking: "
                f"{match_label} | {mkey}"
            )
        else:
            print(f"[TN_live stake] candidate CONFIRMED at {margin:.2f}% (was {cand['margin']:.2f}%) — staking: {match_label} | {mkey}")
        return True

    if margin >= MIN_STAKE_ARB_PCT:
        candidates[mkey] = {"cycle": cycle_num, "margin": margin}
        print(f"[TN_live stake] candidate registered at {margin:.2f}% — waiting for next-poll confirmation: {match_label} | {mkey}")
    elif cand:
        del candidates[mkey]

    return False

async def execute_arb(
    client: httpx.AsyncClient,
    match_label: str,
    mkey: tuple,
    bk_entry: dict,
    bj_entry: dict,
    bk_outcome: str,
    bj_outcome: str,
    best: dict,
    source: dict,
    margin: float,
    bk_fixture_id: int,
    bj_event_id: str,
):
    t_start = time.monotonic()
    bk_odd  = best[bk_outcome]
    bj_odd  = best[bj_outcome]

    bk_stake, bj_stake = round_stakes(bk_odd, bj_odd, TOTAL_STAKE)

    if bk_stake < MIN_STAKE or bj_stake < MIN_STAKE:
        print(
            f"[TN_live stake] ⛔ skipping — stake below ₦{MIN_STAKE} minimum: "
            f"BetKing=₦{bk_stake} Bet9ja=₦{bj_stake}"
        )
        return

    print(
        f"[TN_live stake] {match_label} | {bk_entry.get('label')} | {margin:.2f}% | "
        f"stakes: BetKing=₦{bk_stake}@{bk_odd}({bk_outcome}) Bet9ja=₦{bj_stake}@{bj_odd}({bj_outcome})"
    )

    bk_payload = build_bk_payload(bk_entry, bk_outcome, bk_stake, allow_odd_changes=False)
    bj_payload = build_b9_payload(bj_entry, bj_outcome, bj_stake, accept_odds_changes=0)

    bk_resp_raw, bj_resp_raw = await asyncio.gather(
        _place_bk_bet(client, bk_payload),
        _place_b9_bet(client, bj_payload, bj_event_id),
        return_exceptions=True,
    )

    bk_coupon = None
    bk_status = None
    if isinstance(bk_resp_raw, Exception):
        print(f"[TN_live stake] BetKing POST exception: {bk_resp_raw}")
    elif isinstance(bk_resp_raw, dict):
        bk_status = bk_resp_raw.get("responseStatus")
        bk_coupon = bk_resp_raw.get("couponCode")
    bk_accepted = bool(bk_coupon)

    bj_pending_id = None
    if isinstance(bj_resp_raw, Exception):
        print(f"[TN_live stake] Bet9ja POST exception: {bj_resp_raw}")
    elif isinstance(bj_resp_raw, dict):
        bj_err = bj_resp_raw.get("error", {})
        if bj_resp_raw.get("status") == 1 and bj_err.get("code", -1) == 0:
            data_list = bj_resp_raw.get("data") or [{}]
            bj_pending_id = (data_list[0] if data_list else {}).get("PENDINGID")

    final_bj_status = -1
    poll_resp = {}
    if bj_pending_id:
        final_bj_status, poll_resp = await _poll_b9_pending(client, bj_pending_id)
    bj_accepted = final_bj_status == 1
    bj_balance = (poll_resp.get("data") or {}).get("balance", {}).get("val") if isinstance(poll_resp.get("data"), dict) else None

    both_ok  = bk_accepted and bj_accepted
    both_bad = (not bk_accepted) and (not bj_accepted)

    if both_bad:
        print(f"[TN_live stake] ❌ BOTH failed — aborting | BK coupon={bk_coupon} B9J status={final_bj_status}")
        return

    bk_source = "ORIGINAL"
    bj_source = "ORIGINAL"

    if not both_ok:
        failed_side  = "betking" if not bk_accepted else "bet9ja"
        locked_odd   = bj_odd if not bk_accepted else bk_odd
        locked_stake = bj_stake if not bk_accepted else bk_stake
        retry_side   = bk_outcome if not bk_accepted else bj_outcome
        retry_bk     = not bk_accepted

        original_retry_stake = bk_stake if retry_bk else bj_stake
        original_failed_odd  = bk_odd if retry_bk else bj_odd

        _resume_event.clear()
        print(f"[TN_live stake] ⚠️  {failed_side} failed — ENTER RECOVERY MODE (all workers paused)")

        recovered = False
        latest_fresh_odd    = None
        latest_fresh_market = None

        for refresh in range(RECOVERY_REFRESHES):
            await asyncio.sleep(1.0)
            try:
                if retry_bk:
                    fm_fresh     = await fetch_betking_event(client, bk_fixture_id, 1)
                    fresh_markets = parse_betking_event(fm_fresh.get("markets", []), fm_fresh)
                    fresh_market  = fresh_markets.get(mkey)
                    fresh_odd     = (fresh_market or {}).get("outcomes", {}).get(retry_side)
                else:
                    bj_fresh      = await fetch_bet9ja_event(client, bj_event_id)
                    fresh_markets = parse_bet9ja_event(bj_fresh)
                    fresh_market  = fresh_markets.get(mkey)
                    fresh_odd     = (fresh_market or {}).get("outcomes", {}).get(retry_side)
            except Exception as e:
                print(f"[TN_live stake] recovery fetch error (attempt {refresh+1}): {e}")
                continue

            if not fresh_odd or not fresh_market:
                print(
                    f"[TN_live stake] recovery attempt {refresh+1}/{RECOVERY_REFRESHES}: "
                    f"market no longer available (likely closed/transitioned) — skipping"
                )
                continue

            if not retry_bk and bk_status == 4 and fresh_odd == original_failed_odd:
                print(
                    f"[TN_live stake] recovery attempt {refresh+1}/{RECOVERY_REFRESHES}: "
                    f"BetKing odd still {fresh_odd} — unchanged since stale-odds rejection (status=4), waiting"
                )
                continue

            latest_fresh_odd    = fresh_odd
            latest_fresh_market = fresh_market

            retry_margin = _arb_margin_pct([max(fresh_odd, locked_odd), min(fresh_odd, locked_odd)])
            print(f"[TN_live stake] recovery attempt {refresh+1}/{RECOVERY_REFRESHES} | fresh_odd={fresh_odd} margin={retry_margin:.2f}%")

            if retry_margin >= RECOVERY_MIN_ARB_PCT:
                locked_payout = locked_stake * locked_odd
                retry_stake   = round(locked_payout / fresh_odd, 2)
                if retry_stake > original_retry_stake:
                    print(
                        f"[TN_live stake] recovery attempt {refresh+1}: computed retry_stake=₦{retry_stake} "
                        f"exceeds original ₦{original_retry_stake} — capping at original"
                    )
                    retry_stake = original_retry_stake

                if retry_stake < MIN_STAKE:
                    print(
                        f"[TN_live stake] recovery attempt {refresh+1}: retry_stake=₦{retry_stake} "
                        f"below ₦{MIN_STAKE} minimum — skipping attempt"
                    )
                    continue

                try:
                    if retry_bk:
                        r_payload = build_bk_payload(fresh_market, retry_side, retry_stake, allow_odd_changes=False)
                        r_resp = await _place_bk_bet(client, r_payload)
                        if r_resp.get("couponCode"):
                            bk_coupon = r_resp["couponCode"]
                            bk_status = r_resp.get("responseStatus")
                            bk_source = f"RECOVERY (attempt {refresh+1})"
                            print(f"[TN_live stake] ✅ BetKing {bk_source} accepted coupon={bk_coupon}")
                            recovered = True
                            bk_accepted = True
                            bk_stake = retry_stake
                            bk_odd   = fresh_odd
                            break
                        else:
                            bk_status = r_resp.get("responseStatus")
                            print(f"[TN_live stake] ❌ BetKing recovery attempt {refresh+1} rejected — status={bk_status}")
                    else:
                        r_payload = build_b9_payload(fresh_market, retry_side, retry_stake, accept_odds_changes=0)
                        r_resp = await _place_b9_bet(client, r_payload, bj_event_id)
                        if r_resp.get("status") == 1 and r_resp.get("error", {}).get("code", -1) == 0:
                            pid = ((r_resp.get("data") or [{}])[0]).get("PENDINGID")
                            if pid:
                                rs, _ = await _poll_b9_pending(client, pid)
                                if rs == 1:
                                    bj_source = f"RECOVERY (attempt {refresh+1})"
                                    print(f"[TN_live stake] ✅ Bet9ja {bj_source} accepted")
                                    recovered = True
                                    bj_accepted = True
                                    bj_stake = retry_stake
                                    bj_odd   = fresh_odd
                                    break
                                else:
                                    print(f"[TN_live stake] ❌ Bet9ja recovery attempt {refresh+1} rejected after polling — status={rs}")
                            else:
                                print(f"[TN_live stake] ❌ Bet9ja recovery attempt {refresh+1} rejected — no PENDINGID | raw={r_resp}")
                        else:
                            err = r_resp.get("error", {}) or {}
                            print(
                                f"[TN_live stake] ❌ Bet9ja recovery attempt {refresh+1} rejected — "
                                f"status={r_resp.get('status')} error_code={err.get('code')} msg={err.get('message')}"
                            )
                except Exception as e:
                    print(f"[TN_live stake] recovery place error: {e}")

        if not recovered:
            _locked_payout = locked_stake * locked_odd
            _hedge_ref_odd = latest_fresh_odd if latest_fresh_odd else (
                bk_odd if retry_bk else bj_odd
            )
            hedge_stake = round(_locked_payout / _hedge_ref_odd, 2)
            if hedge_stake > original_retry_stake:
                print(
                    f"[TN_live stake] computed hedge_stake=₦{hedge_stake} exceeds original "
                    f"₦{original_retry_stake} — capping at original (capital preservation)"
                )
                hedge_stake = original_retry_stake
            if hedge_stake < MIN_STAKE:
                print(
                    f"[TN_live stake] ⛔ hedge_stake=₦{hedge_stake} below ₦{MIN_STAKE} minimum — "
                    f"skipping hedge placement"
                )
            else:
                print(
                    f"[TN_live stake] ⚠️  recovery failed after {RECOVERY_REFRESHES} attempts — "
                    f"placing hedge with odds changes | locked_payout=₦{round(_locked_payout,2)} "
                    f"ref_odd={_hedge_ref_odd} hedge_stake=₦{hedge_stake} (capped at original ₦{original_retry_stake})"
                )
                if not latest_fresh_market:
                    print(
                        "[TN_live stake] ⚠️  no fresh market data ever arrived during recovery — "
                        "hedge is resubmitting the ORIGINAL (already-rejected) selection, "
                        "which may fail again for the same reason"
                    )
                _hedge_market = latest_fresh_market if latest_fresh_market else (bk_entry if retry_bk else bj_entry)
                try:
                    if retry_bk:
                        h_payload = build_bk_payload(_hedge_market, retry_side, hedge_stake, allow_odd_changes=True)
                        h_resp = await _place_bk_bet(client, h_payload)
                        bk_coupon   = h_resp.get("couponCode")
                        bk_status   = h_resp.get("responseStatus")
                        bk_accepted = bool(bk_coupon)
                        if bk_accepted:
                            bk_source = "HEDGE"
                            bk_stake  = hedge_stake
                            bk_odd    = _hedge_ref_odd
                            print(f"[TN_live stake] ✅ BetKing HEDGE accepted coupon={bk_coupon}")
                        else:
                            print(f"[TN_live stake] ❌ BetKing HEDGE rejected (status={bk_status})")
                    else:
                        h_payload = build_b9_payload(_hedge_market, retry_side, hedge_stake, accept_odds_changes=1)
                        h_resp = await _place_b9_bet(client, h_payload, bj_event_id)
                        pid = ((h_resp.get("data") or [{}])[0]).get("PENDINGID")
                        if pid:
                            hs, _ = await _poll_b9_pending(client, pid)
                            final_bj_status = hs
                            bj_accepted = hs == 1
                            if bj_accepted:
                                bj_source = "HEDGE"
                                bj_stake  = hedge_stake
                                bj_odd    = _hedge_ref_odd
                                print("[TN_live stake] ✅ Bet9ja HEDGE accepted")
                            else:
                                print(f"[TN_live stake] ❌ Bet9ja HEDGE rejected (status={hs})")
                        else:
                            final_bj_status = -1
                            print(f"[TN_live stake] ❌ Bet9ja HEDGE rejected — no PENDINGID | raw={h_resp}")
                except Exception as e:
                    print(f"[TN_live stake] hedge error: {e}")

        _resume_event.set()
        print("[TN_live stake] ✅ RECOVERY MODE EXIT — resuming TN_live workers")

    elapsed      = round(time.monotonic() - t_start, 2)
    total_staked = bk_stake + bj_stake
    profit_if_bk = round(bk_stake * bk_odd - total_staked, 2)
    profit_if_bj = round(bj_stake * bj_odd - total_staked, 2)
    net_profit   = min(profit_if_bk, profit_if_bj)

    bk_result = f"ACCEPTED [{bk_source}]" if bk_accepted else f"REJECTED(status={bk_status})"
    bj_result = f"ACCEPTED [{bj_source}]" if bj_accepted else f"REJECTED(status={final_bj_status})"
    bal_str   = f" | Bet9ja balance=₦{bj_balance}" if bj_balance is not None else ""

    print(
        f"[TN_live stake] {'✅' if bk_accepted and bj_accepted else '⚠️ '} ARB EXECUTED: "
        f"{match_label} | {bk_entry.get('label')} | {margin:.2f}%\n"
        f"         BetKing → outcome={bk_outcome} odd={bk_odd} stake=₦{bk_stake} | {bk_result}"
        + (f" coupon={bk_coupon}" if bk_coupon else "") + "\n"
        f"         Bet9ja  → outcome={bj_outcome} odd={bj_odd} stake=₦{bj_stake} | {bj_result}"
        + (f" (PENDINGID={bj_pending_id})" if bj_pending_id else "")
        + f"{bal_str}\n"
        f"         profit estimate ≈ ₦{net_profit} | elapsed={elapsed}s"
    )

    stake_result = {
        "stake_amount":    bk_stake + bj_stake,
        "b9_status":       "accepted" if bj_accepted else "rejected",
        "bk_status":       "accepted" if bk_accepted else "rejected",
        "bk_coupon":       bk_coupon,
        "profit_estimate": net_profit,
    }
    snapshot = {
        bk_outcome: {"odd": bk_odd, "book": "BetKing"},
        bj_outcome: {"odd": bj_odd, "book": "Bet9ja"},
        "margin_pct": round(margin, 2),
    }
    duration = int(time.monotonic() - t_start)
    await _store_arb(match_label, bk_entry.get("label") or str(mkey), bk_entry.get("market_type"),
                      snapshot, margin, duration, stake_result)

async def _store_arb(match_label: str, label: str, market_type: str, snapshot: dict,
                      margin: float, duration_seconds: int, stake_result: dict):
    if not stake_result or not stake_result.get("b9_status") or not stake_result.get("bk_status"):
        print(f"[TN_live] arb store skipped — missing stake_result/status: {match_label}")
        return

    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    date_str = f"{now.day}/{now.month}/{now.year}"
    try:
        sb = _get_supabase()
        row = {
            "name":                label,
            "match":               match_label,
            "market_type":         market_type,
            "market_options_odds": json.dumps(snapshot),
            "margin_pct":          margin,
            "duration_seconds":    duration_seconds,
            "date":                date_str,
            "stake_amount":        stake_result.get("stake_amount"),
            "b9_status":           stake_result.get("b9_status"),
            "bk_status":           stake_result.get("bk_status"),
            "bk_coupon":           stake_result.get("bk_coupon"),
            "profit_estimate":     stake_result.get("profit_estimate"),
        }
        sb.table("arbitrage").insert(row).execute()
        print(f"[TN_live] arb stored: {match_label} | {label} | {margin}% | {duration_seconds}s")
    except Exception as e:
        print(f"[TN_live] arb store failed: {e}")

# ---------------------------------------------------------------------------
# Match worker (single synchronized fetch per cycle — no cross-round cache)
# ---------------------------------------------------------------------------
_stop_event   = asyncio.Event()
_resume_event = asyncio.Event()
_resume_event.set()
_workers: dict = {}

async def shutdown():
    _stop_event.set()
    _resume_event.set()
    tasks = list(_workers.values())
    for t in tasks:
        t.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    _workers.clear()
    print("[TN_live] shutdown complete")

class StopTrading(Exception):
    pass

async def _match_worker(key, bk_fixture_id: int, bj_event_id: str, match_label: str, worker_num: int):
    tag = f"[worker {worker_num}]"
    print(f"{tag} monitoring: {match_label}")

    candidates: dict = {}
    cycle_num = 0

    async with httpx.AsyncClient(http2=True) as client:
        while not _stop_event.is_set():
            await _resume_event.wait()
            await asyncio.sleep(WORKER_POLL_SECONDS)
            if not _resume_event.is_set():
                continue
            if _stop_event.is_set():
                break

            cycle_num += 1
            _prune_stale_candidates(match_label, candidates, cycle_num)

            try:
                fm_raw, bj_raw = await asyncio.gather(
                    fetch_betking_event(client, bk_fixture_id, 1),
                    fetch_bet9ja_event(client, bj_event_id),
                )
            except Exception as e:
                print(f"{tag} {match_label} fetch failed: {e}")
                continue

            if _betking_match_ended(fm_raw) or _bet9ja_match_ended(bj_raw):
                print(f"{tag} {match_label} -- match ended, freeing worker")
                break

            bk_match_info = parse_betking_match_info(fm_raw)
            bj_match_info = parse_bet9ja_match_info(bj_raw)
            match_info = {**bj_match_info, **{k: v for k, v in bk_match_info.items() if v is not None}}

            bk_markets = parse_betking_event(fm_raw.get("markets", []), fm_raw)
            bj_markets = parse_bet9ja_event(bj_raw)

            # Only Set Winner markets exist now; iterate over common keys
            for mkey in set(bk_markets) & set(bj_markets):
                bk_entry = bk_markets[mkey]
                bj_entry = bj_markets[mkey]
                result = _log_arb(match_label, mkey, bk_entry, bj_entry, "joint-fetch", match_info)
                if result:
                    best, source, margin = result
                    if _process_candidate(match_label, mkey, margin, candidates, cycle_num):
                        await _check_and_stake(
                            client, match_label, mkey, bk_entry, bj_entry,
                            best, source, margin, bk_fixture_id, bj_event_id,
                            min_margin=RECOVERY_MIN_ARB_PCT,
                        )

    _workers.pop(key, None)
    print(f"{tag} worker finished for {match_label}")

# ---------------------------------------------------------------------------
# Main run loop (with robust error handling and log spam prevention)
# ---------------------------------------------------------------------------
_last_state = {
    "bk_len": -1,
    "bj_len": -1,
    "common_len": -1,
    "new_len": -1,
    "workers_len": -1,
    "cap_reached": False,
}

async def run_once():
    if _stop_event.is_set():
        print("[TN_live] stopped")
        raise StopTrading("TN_live stopped")

    async with httpx.AsyncClient(http2=True) as client:
        bk_raw, bj_raw = await asyncio.gather(
            fetch_betking_live(client),
            fetch_bet9ja_live(client),
            return_exceptions=True,
        )

    if isinstance(bk_raw, Exception):
        if not _last_state.get("bk_fail_logged", False):
            print(f"[TN_live][BetKing] fetch failed: {bk_raw}")
            _last_state["bk_fail_logged"] = True
        bk_matches = {}
    else:
        _last_state["bk_fail_logged"] = False
        bk_matches = parse_betking_live(bk_raw)

    if isinstance(bj_raw, Exception):
        if not _last_state.get("bj_fail_logged", False):
            print(f"[TN_live][Bet9ja] fetch failed: {bj_raw}")
            _last_state["bj_fail_logged"] = True
        bj_matches = {}
    else:
        _last_state["bj_fail_logged"] = False
        bj_matches = parse_bet9ja_live(bj_raw)

    common_keys = set(bk_matches) & set(bj_matches)

    for key in [k for k, t in _workers.items() if t.done()]:
        _workers.pop(key, None)

    new_matches = [k for k in common_keys if k not in _workers]

    current_state = {
        "bk_len": len(bk_matches),
        "bj_len": len(bj_matches),
        "common_len": len(common_keys),
        "new_len": len(new_matches),
        "workers_len": len(_workers),
        "cap_reached": len(_workers) >= MAX_WORKERS,
    }

    if common_keys:
        if current_state["cap_reached"] and not _last_state["cap_reached"]:
            print(f"[TN_live] worker cap reached ({MAX_WORKERS}), skipping remaining matches")
        elif not current_state["cap_reached"] and _last_state["cap_reached"]:
            print(f"[TN_live] worker cap no longer reached ({len(_workers)} workers)")

    _last_state.update(current_state)

    for key in new_matches:
        if len(_workers) >= MAX_WORKERS:
            break
        bkm, bjm = bk_matches[key], bj_matches[key]
        match_label = f"{bkm['home']} vs {bkm['away']}"
        worker_num = len(_workers) + 1
        _workers[key] = asyncio.create_task(
            _match_worker(key, bkm["event_id"], bjm["event_id"], match_label, worker_num)
        )
        print(f"[TN_live] worker {worker_num} started: {match_label} | BetKing: {bkm['status_label']} ({bkm['score']}) | Bet9ja: {bjm['status_label']} ({bjm['score']})")

if __name__ == "__main__":
    asyncio.run(run_once())