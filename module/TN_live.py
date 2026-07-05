import asyncio
import json
import os
import re
import time

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

MIN_ARB_PCT            = 5.0   # only log if margin >= this
MAX_ODDS_AGE_DIFF      = 5.0   # seconds; skip if the two books' snapshots are older apart than this

EXCLUDED_MARKET_TYPES  = set()

BETKING_ACCOUNT_NAME   = "Chukwuebuka"
BET9JA_ACCOUNT_NAME    = "wixnation"

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
BETKING_LIVE_HEADERS = {
    "User-Agent":      "Mozilla/5.0 (X11; Linux x86_64; rv:140.0) Gecko/20100101 Firefox/140.0",
    "Accept":          "*/*",
    "Accept-Language": "en-US,en;q=0.5",
    "Referer":         "https://m.betking.com/en-ng/sports/live/tennis/5",
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

def parse_betking_event(markets: list) -> dict:
    """
    Parse tennis markets from BetKing event.
    Returns dict keyed by:
      - ("winner",) -> Match Winner
      - ("total_games", line) -> Total Games Over/Under
      - ("set_winner", set_num) -> Set Winner
      - ("game_winner", set_num, game_num) -> Winner of a specific game
    Only includes markets where all required selections are VALID and odds > 0.
    """
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
        for s in selections:
            sel_name = s.get("name")
            status = s.get("status")
            odd_val = s.get("odd", {}).get("value")
            if sel_name is not None and status == "VALID" and odd_val is not None and odd_val > 0:
                odd_by_name[sel_name] = float(odd_val)

        # ----- Match Winner (typeId 9388) -----
        if market_type_id == 9388 and "Winner" in name:
            if "1" in odd_by_name and "2" in odd_by_name:
                key = ("winner",)
                out[key] = {
                    "market_type": "winner",
                    "label": "Match Winner",
                    "outcomes": {"1": odd_by_name["1"], "2": odd_by_name["2"]},
                    "line": None,
                    "set_num": None,
                    "game_num": None,
                }

        # ----- Total Games (typeId 285) -----
        elif market_type_id == 285 and "Total games {line}" in name:
            line = specifiers.get("line")
            if line is None:
                line = m.get("specialValue")
            if line is None:
                continue
            try:
                line_val = float(line)
            except (TypeError, ValueError):
                continue
            if "Over" in odd_by_name and "Under" in odd_by_name:
                key = ("total_games", round(line_val, 1))
                out[key] = {
                    "market_type": "total_games",
                    "label": f"Total games {line_val}",
                    "outcomes": {"Over": odd_by_name["Over"], "Under": odd_by_name["Under"]},
                    "line": line_val,
                    "set_num": None,
                    "game_num": None,
                }

        # ----- Set Winner (typeId 9389) -----
        elif market_type_id == 9389 and "Set {line} - Winner" in name:
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
                }

        # ----- Specific Game Winner (typeId 10436) -----
        elif market_type_id == 10436 and "{setnr} set game {gamenr} - winner" in name:
            set_num = specifiers.get("setnr")
            game_num = specifiers.get("gamenr")
            if set_num is None or game_num is None:
                continue
            try:
                set_num = int(set_num)
                game_num = int(game_num)
            except (TypeError, ValueError):
                continue
            if "1" in odd_by_name and "2" in odd_by_name:
                key = ("game_winner", set_num, game_num)
                out[key] = {
                    "market_type": "game_winner",
                    "label": f"Set {set_num} Game {game_num} Winner",
                    "outcomes": {"1": odd_by_name["1"], "2": odd_by_name["2"]},
                    "line": None,
                    "set_num": set_num,
                    "game_num": game_num,
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
    Returns dict with same key structure as parse_betking_event.
    Only includes markets with odds > 1 (active).
    """
    try:
        D = b9_raw["D"]
    except (KeyError, TypeError):
        return {}

    odds = D.get("O") or {}
    out = {}

    # ----- Match Winner -----
    if "LIVET_12_1" in odds and "LIVET_12_2" in odds:
        v1 = odds["LIVET_12_1"].get("v")
        v2 = odds["LIVET_12_2"].get("v")
        if v1 is not None and v1 > 1 and v2 is not None and v2 > 1:
            key = ("winner",)
            out[key] = {
                "market_type": "winner",
                "label": "Match Winner",
                "outcomes": {"1": float(v1), "2": float(v2)},
                "line": None,
                "set_num": None,
                "game_num": None,
            }

    # ----- Total Games (LIVET_OUG@{line}_O / _U) -----
    for key, val in odds.items():
        if key.startswith("LIVET_OUG@") and "_" in key:
            # Extract line and side
            try:
                parts = key.split("@", 1)[1].rsplit("_", 1)
                if len(parts) != 2:
                    continue
                line_str, side = parts
                line = float(line_str)
            except (ValueError, IndexError):
                continue
            if side not in ("O", "U"):
                continue
            odd = val.get("v")
            if odd is None or odd <= 1:
                continue
            # We'll group by line
            mkey = ("total_games", round(line, 1))
            if mkey not in out:
                out[mkey] = {
                    "market_type": "total_games",
                    "label": f"Total games {line}",
                    "outcomes": {},
                    "line": line,
                    "set_num": None,
                    "game_num": None,
                }
            out[mkey]["outcomes"]["Over" if side == "O" else "Under"] = float(odd)
    # Keep only those with both Over and Under
    for mkey in list(out.keys()):
        if mkey[0] == "total_games" and len(out[mkey]["outcomes"]) != 2:
            del out[mkey]

    # ----- Set Winner (LIVET_12P{set}_1 / _2) -----
    for key, val in odds.items():
        if key.startswith("LIVET_12P") and "_" in key:
            # Extract set number and side
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
                }
            out[mkey]["outcomes"]["1" if side == "1" else "2"] = float(odd)
    # Keep only those with both 1 and 2
    for mkey in list(out.keys()):
        if mkey[0] == "set_winner" and len(out[mkey]["outcomes"]) != 2:
            del out[mkey]

    # ----- Specific Game Winner (LIVET_12G{game}S{set}_1 / _2) -----
    for key, val in odds.items():
        if key.startswith("LIVET_12G") and "_" in key:
            # Format: LIVET_12G{game}S{set}_1 or _2
            try:
                rest = key.replace("LIVET_12G", "").split("_", 1)
                if len(rest) != 2:
                    continue
                gs_str, side = rest
                # gs_str should be like "3S2"
                if "S" not in gs_str:
                    continue
                game_str, set_str = gs_str.split("S", 1)
                game_num = int(game_str)
                set_num = int(set_str)
            except (ValueError, IndexError):
                continue
            if side not in ("1", "2"):
                continue
            odd = val.get("v")
            if odd is None or odd <= 1:
                continue
            mkey = ("game_winner", set_num, game_num)
            if mkey not in out:
                out[mkey] = {
                    "market_type": "game_winner",
                    "label": f"Set {set_num} Game {game_num} Winner",
                    "outcomes": {},
                    "line": None,
                    "set_num": set_num,
                    "game_num": game_num,
                }
            out[mkey]["outcomes"]["1" if side == "1" else "2"] = float(odd)
    # Keep only those with both 1 and 2
    for mkey in list(out.keys()):
        if mkey[0] == "game_winner" and len(out[mkey]["outcomes"]) != 2:
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
    """
    Logs the best available odds for each selection, along with the book that provides them.
    Only considers odds > 0.
    Also includes match time and score if match_info is provided.
    """
    bk_ts = bk_entry.get("_ts")
    bj_ts = bj_entry.get("_ts")
    if bk_ts is not None and bj_ts is not None and abs(bk_ts - bj_ts) > MAX_ODDS_AGE_DIFF:
        return

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
        return

    margin = _arb_margin_pct(list(best.values()))
    if margin >= MIN_ARB_PCT:
        label = bk_entry.get("label") or bj_entry.get("label") or str(mkey)
        # Build extra details
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

# ---------------------------------------------------------------------------
# Match worker (decoupled pollers)
# ---------------------------------------------------------------------------
_stop_event = asyncio.Event()
_workers: dict = {}

async def shutdown():
    _stop_event.set()
    tasks = list(_workers.values())
    for t in tasks:
        t.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    _workers.clear()
    print("[TN_live] shutdown complete")

class StopTrading(Exception):
    pass

def _prune_stale_cache(cache: dict, book: str, live_keys: set):
    for mkey in [k for k, entry in cache.items() if book in entry and k not in live_keys]:
        del cache[mkey][book]
        if not cache[mkey]:
            del cache[mkey]

async def _betking_poller(
    client: httpx.AsyncClient,
    bk_fixture_id: int,
    match_label: str,
    tag: str,
    cache: dict,
    live_bk_keys: set,
    live_bj_keys: set,
    match_over: asyncio.Event,
):
    while not _stop_event.is_set() and not match_over.is_set():
        await asyncio.sleep(WORKER_POLL_SECONDS)
        if _stop_event.is_set() or match_over.is_set():
            break

        try:
            fm_raw = await fetch_betking_event(client, bk_fixture_id, 1)
        except Exception as e:
            print(f"{tag} [BetKing] {match_label} fetch failed: {e}")
            continue

        if _betking_match_ended(fm_raw):
            print(f"{tag} {match_label} -- match ended (BetKing), freeing worker")
            match_over.set()
            break

        match_info = parse_betking_match_info(fm_raw)
        bk_markets = parse_betking_event(fm_raw.get("markets", []))
        now = time.monotonic()
        live_bk_keys.clear()
        for mkey, m in bk_markets.items():
            live_bk_keys.add(mkey)
            cache.setdefault(mkey, {})["betking"] = {**m, "_ts": now}

        cache["_match_info"] = match_info

        _prune_stale_cache(cache, "betking", live_bk_keys)

        for mkey in live_bk_keys:
            entry = cache.get(mkey)
            if entry and "bet9ja" in entry:
                _log_arb(match_label, mkey, entry["betking"], entry["bet9ja"],
                         "BetKing", match_info)

async def _bet9ja_poller(
    client: httpx.AsyncClient,
    bj_event_id: str,
    match_label: str,
    tag: str,
    cache: dict,
    live_bk_keys: set,
    live_bj_keys: set,
    match_over: asyncio.Event,
):
    while not _stop_event.is_set() and not match_over.is_set():
        await asyncio.sleep(WORKER_POLL_SECONDS)
        if _stop_event.is_set() or match_over.is_set():
            break

        try:
            bj_raw = await fetch_bet9ja_event(client, bj_event_id)
        except Exception as e:
            print(f"{tag} [Bet9ja] {match_label} fetch failed: {e}")
            continue

        if _bet9ja_match_ended(bj_raw):
            print(f"{tag} {match_label} -- match ended (Bet9ja), freeing worker")
            match_over.set()
            break

        match_info = parse_bet9ja_match_info(bj_raw)
        bj_markets = parse_bet9ja_event(bj_raw)
        now = time.monotonic()
        live_bj_keys.clear()
        for mkey, m in bj_markets.items():
            live_bj_keys.add(mkey)
            cache.setdefault(mkey, {})["bet9ja"] = {**m, "_ts": now}

        cache["_match_info"] = match_info

        _prune_stale_cache(cache, "bet9ja", live_bj_keys)

        for mkey in live_bj_keys:
            entry = cache.get(mkey)
            if entry and "betking" in entry:
                _log_arb(match_label, mkey, entry["betking"], entry["bet9ja"],
                         "Bet9ja", match_info)

async def _match_worker(key, bk_fixture_id: int, bj_event_id: str, match_label: str, worker_num: int):
    tag = f"[worker {worker_num}]"
    print(f"{tag} monitoring: {match_label}")

    cache: dict = {}
    live_bk_keys: set = set()
    live_bj_keys: set = set()
    match_over = asyncio.Event()

    async with httpx.AsyncClient(http2=True) as client:
        await asyncio.gather(
            _betking_poller(client, bk_fixture_id, match_label, tag, cache, live_bk_keys, live_bj_keys, match_over),
            _bet9ja_poller(client, bj_event_id, match_label, tag, cache, live_bk_keys, live_bj_keys, match_over),
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

    # Log worker cap only when it newly becomes true
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
