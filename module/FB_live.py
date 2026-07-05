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
MAX_ODDS_AGE_DIFF       = 5.0  # seconds; skip if the two books' snapshots are older apart than this

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
        print(f"[FB_live] no cookie row found for '{account_name}'")
        return ""
    except Exception as e:
        print(f"[FB_live] cookie lookup failed for '{account_name}': {e}")
        return ""

# ---------------------------------------------------------------------------
# BetKing API (sportId = 1 for football)
# ---------------------------------------------------------------------------
SPORT_ID_BK = 1

BETKING_LIVE_URL = (
    f"https://m.betking.com/en-ng/sports/live/api/overview/{SPORT_ID_BK}"
    "?_data=routes%2F%28%24locale%29.sports.live.api.overview.%24sportId"
)
BETKING_LIVE_HEADERS = {
    "User-Agent":      "Mozilla/5.0 (X11; Linux x86_64; rv:140.0) Gecko/20100101 Firefox/140.0",
    "Accept":          "*/*",
    "Accept-Language": "en-US,en;q=0.5",
    "Referer":         "https://m.betking.com/en-ng/sports/live/football/1",
}

async def fetch_betking_live(client: httpx.AsyncClient) -> dict:
    headers = dict(BETKING_LIVE_HEADERS)
    cookie = get_cookie(BETKING_ACCOUNT_NAME)
    if cookie:
        headers["Cookie"] = cookie
    resp = await client.get(BETKING_LIVE_URL, headers=headers, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    return resp.json()

async def fetch_betking_event(client: httpx.AsyncClient, fixture_id: int, area_id: int) -> dict:
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
        "period_scores": ev.get("setScores") or ev.get("periodScores"),
        "status_id": ev.get("matchStatusId"),
        "status_label": ev.get("matchStatusLabel"),
    }

def parse_betking_event(first_half_markets: list, full_match_markets: list) -> dict:
    """
    Parse both first‑half (areaId=11) and full‑match (areaId=1) markets.
    Returns a dict keyed by (market_type, line) where market_type includes a
    prefix "1h_" for first‑half markets and "full_" for full‑match.
    Only includes markets where all required selections are ACTIVE (status=="VALID")
    and have odds > 0.
    """
    # Combine markets: first half first, then full match (avoid duplicates)
    seen_ids = set()
    fh_flat = _flatten_betking_markets(first_half_markets)
    for m in fh_flat:
        seen_ids.add(m.get("marketId"))
    fm_extra = [m for m in _flatten_betking_markets(full_match_markets) if m.get("marketId") not in seen_ids]
    markets = fh_flat + fm_extra

    out = {}

    for m in markets:
        market_type_id = m.get("marketTypeId")
        name = m.get("name", "")
        selections = m.get("selections") or []
        if len(selections) < 2:
            continue

        odd_by_name = {}
        for s in selections:
            sel_name = s.get("name")
            status = s.get("status")
            odd_val = s.get("odd", {}).get("value")
            # Only include if status is VALID and odd > 0
            if sel_name is not None and status == "VALID" and odd_val is not None and odd_val > 0:
                odd_by_name[sel_name] = float(odd_val)

        # ----- 1st Half markets (areaId=11) -----
        # 1H 1X2 (typeId 142)
        if market_type_id == 142 and "1st half - 1x2" in name:
            if all(k in odd_by_name for k in ("1", "X", "2")):
                key = ("1h_threeway",)
                out[key] = {
                    "market_type": "1h_threeway",
                    "label": "1st Half 1X2",
                    "outcomes": {"1": odd_by_name["1"], "X": odd_by_name["X"], "2": odd_by_name["2"]},
                    "line": None,
                }
        # 1H Double Chance (typeId 9588)
        elif market_type_id == 9588 and "1st half - double chance" in name:
            if all(k in odd_by_name for k in ("1X", "12", "X2")):
                key = ("1h_double_chance",)
                out[key] = {
                    "market_type": "1h_double_chance",
                    "label": "1st Half Double Chance",
                    "outcomes": {"1X": odd_by_name["1X"], "12": odd_by_name["12"], "X2": odd_by_name["X2"]},
                    "line": None,
                }
        # 1H Draw No Bet (typeId 9328)
        elif market_type_id == 9328 and "1st half - draw no bet" in name:
            if "1" in odd_by_name and "2" in odd_by_name:
                key = ("1h_dnb",)
                out[key] = {
                    "market_type": "1h_dnb",
                    "label": "1st Half Draw No Bet",
                    "outcomes": {"1": odd_by_name["1"], "2": odd_by_name["2"]},
                    "line": None,
                }
        # 1H Over/Under (typeId 161)
        elif market_type_id == 161 and "1st half - total {line}" in name:
            line = m.get("specifiers", {}).get("line")
            if line is None:
                line = m.get("specialValue")
            if line is None:
                continue
            try:
                line_val = float(line)
            except (TypeError, ValueError):
                continue
            if "Over" in odd_by_name and "Under" in odd_by_name:
                key = ("1h_total", round(line_val, 1))
                out[key] = {
                    "market_type": "1h_total",
                    "label": f"1st Half Total {line_val}",
                    "outcomes": {"Over": odd_by_name["Over"], "Under": odd_by_name["Under"]},
                    "line": line_val,
                }
        # 1H Both Teams to Score (typeId 9887)
        elif market_type_id == 9887 and "1st half - both teams to score" in name:
            if "Yes" in odd_by_name and "No" in odd_by_name:
                key = ("1h_btts",)
                out[key] = {
                    "market_type": "1h_btts",
                    "label": "1st Half Both Teams to Score",
                    "outcomes": {"Yes": odd_by_name["Yes"], "No": odd_by_name["No"]},
                    "line": None,
                }

        # ----- Full Match markets (areaId=1) -----
        # 1X2 (typeId 110)
        elif market_type_id == 110 and name.lower() == "1x2":
            if all(k in odd_by_name for k in ("1", "X", "2")):
                key = ("full_threeway",)
                out[key] = {
                    "market_type": "full_threeway",
                    "label": "1X2",
                    "outcomes": {"1": odd_by_name["1"], "X": odd_by_name["X"], "2": odd_by_name["2"]},
                    "line": None,
                }
        # Double Chance (typeId 146)
        elif market_type_id == 146 and name.lower() == "double chance":
            if all(k in odd_by_name for k in ("1X", "12", "X2")):
                key = ("full_double_chance",)
                out[key] = {
                    "market_type": "full_double_chance",
                    "label": "Double Chance",
                    "outcomes": {"1X": odd_by_name["1X"], "12": odd_by_name["12"], "X2": odd_by_name["X2"]},
                    "line": None,
                }
        # Draw No Bet (typeId 147)
        elif market_type_id == 147 and name.lower() == "draw no bet":
            if "1" in odd_by_name and "2" in odd_by_name:
                key = ("full_dnb",)
                out[key] = {
                    "market_type": "full_dnb",
                    "label": "Draw No Bet",
                    "outcomes": {"1": odd_by_name["1"], "2": odd_by_name["2"]},
                    "line": None,
                }
        # Over/Under (typeId 160)
        elif market_type_id == 160 and "Total points {line}" in name:
            line = m.get("specifiers", {}).get("line")
            if line is None:
                line = m.get("specialValue")
            if line is None:
                continue
            try:
                line_val = float(line)
            except (TypeError, ValueError):
                continue
            if "Over" in odd_by_name and "Under" in odd_by_name:
                key = ("full_total", round(line_val, 1))
                out[key] = {
                    "market_type": "full_total",
                    "label": f"Total {line_val}",
                    "outcomes": {"Over": odd_by_name["Over"], "Under": odd_by_name["Under"]},
                    "line": line_val,
                }
        # Both Teams to Score (typeId 302)
        elif market_type_id == 302 and name.lower() == "both teams to score":
            if "Yes" in odd_by_name and "No" in odd_by_name:
                key = ("full_btts",)
                out[key] = {
                    "market_type": "full_btts",
                    "label": "Both Teams to Score",
                    "outcomes": {"Yes": odd_by_name["Yes"], "No": odd_by_name["No"]},
                    "line": None,
                }

    return out

def _betking_match_ended(full_match_raw: dict) -> bool:
    return full_match_raw.get("event") is None and not full_match_raw.get("markets")

# ---------------------------------------------------------------------------
# Bet9ja API (sportId = 3000001 for football)
# ---------------------------------------------------------------------------
BET9JA_SPORT_ID  = "3000001"
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
        "period_scores": r.get("SS"),
        "status_label": A.get("ES"),
    }

def parse_bet9ja_event(b9_raw: dict) -> dict:
    """
    Parse both first‑half and full‑match markets from Bet9ja using the MKT object.
    Returns a dict with same key structure as parse_betking_event.
    Only includes markets where all required selections have odds > 1 (active).
    """
    try:
        D = b9_raw["D"]
    except (KeyError, TypeError):
        return {}

    mkt_defs = D.get("MKT") or {}
    odds = D.get("O") or {}

    # Define the market keys we care about
    market_keys = {
        "full_threeway": "LIVES_1X2",
        "full_double_chance": "LIVES_DC",
        "full_dnb": "LIVES_DNB",
        "full_total": "LIVES_OU",
        "full_btts": "LIVES_GGNG",
        "1h_threeway": "LIVES_1X21T",
        "1h_double_chance": "LIVES_DCHT",
        "1h_dnb": "LIVES_DNB1TN",
        "1h_total": "LIVES_OU1T",
        "1h_btts": "LIVES_GGNG1T",
    }

    # Mapping from Bet9ja suffix to our outcome keys
    outcome_map = {
        "LIVES_1X2": {"_1": "1", "_X": "X", "_2": "2"},
        "LIVES_DC": {"_1X": "1X", "_12": "12", "_X2": "X2"},
        "LIVES_DNB": {"_1": "1", "_2": "2"},
        "LIVES_OU": {"_O": "Over", "_U": "Under"},
        "LIVES_GGNG": {"_Y": "Yes", "_N": "No"},
        "LIVES_1X21T": {"_1": "1", "_X": "X", "_2": "2"},
        "LIVES_DCHT": {"_1X": "1X", "_12": "12", "_X2": "X2"},
        "LIVES_DNB1TN": {"_Home": "1", "_Away": "2"},
        "LIVES_OU1T": {"_O": "Over", "_U": "Under"},
        "LIVES_GGNG1T": {"_Y": "Yes", "_N": "No"},
    }

    out = {}

    for our_key, mk in market_keys.items():
        if mk not in mkt_defs:
            continue
        prefix = mk
        # For total markets we need to parse the line from O keys
        if our_key in ("full_total", "1h_total"):
            # Find all O keys that start with prefix and contain '@'
            line_odds = {}
            for ok, val in odds.items():
                if not ok.startswith(prefix):
                    continue
                if "@" not in ok:
                    continue
                # Format: LIVES_OU@{line}_O or _U
                parts = ok.split("@", 1)
                if len(parts) != 2:
                    continue
                rest = parts[1]
                if "_" not in rest:
                    continue
                line_str, sel = rest.rsplit("_", 1)
                try:
                    line_val = float(line_str)
                except ValueError:
                    continue
                odd_val = val.get("v")
                # Active only if odd > 1
                if odd_val is None or odd_val <= 1:
                    continue
                if our_key == "full_total":
                    mkey = ("full_total", round(line_val, 1))
                else:
                    mkey = ("1h_total", round(line_val, 1))
                if mkey not in out:
                    out[mkey] = {
                        "market_type": "full_total" if our_key == "full_total" else "1h_total",
                        "label": f"{'Total' if our_key == 'full_total' else '1st Half Total'} {line_val}",
                        "outcomes": {},
                        "line": line_val,
                    }
                # Map suffix to outcome
                suffix_map = outcome_map.get(prefix, {})
                if sel in suffix_map:
                    outcome_name = suffix_map[sel]
                    out[mkey]["outcomes"][outcome_name] = float(odd_val)
            # Only keep if both Over and Under are present
            for mkey in list(out.keys()):
                if our_key in mkey[0] and len(out[mkey]["outcomes"]) != 2:
                    del out[mkey]
        else:
            # Non-spread markets: collect odds for each suffix
            mkey = (our_key,)
            outcomes = {}
            suffix_map = outcome_map.get(prefix, {})
            for suffix, outcome_name in suffix_map.items():
                key = prefix + suffix
                if key in odds:
                    odd_val = odds[key].get("v")
                    if odd_val is not None and odd_val > 1:
                        outcomes[outcome_name] = float(odd_val)
            # Only keep if we have all expected outcomes for that market type
            expected = len(suffix_map)
            if outcomes and len(outcomes) == expected:
                out[mkey] = {
                    "market_type": our_key,
                    "label": mkt_defs[mk].get("M_LABEL", mk),
                    "outcomes": outcomes,
                    "line": None,
                }

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
    # Guard against comparing a fresh snapshot from one book against a stale
    # one from the other (e.g. right after a goal, before both pollers have
    # re-fetched). A "true" arb should survive with both sides reasonably
    # current; a gap that only exists because one side is seconds behind
    # isn't actually executable.
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
        line_info = ""
        if mkey[0] in ("full_total", "1h_total") and len(mkey) > 1:
            line_info = f" line={mkey[1]}"
        odds_info = {sel: (best[sel], source[sel]) for sel in best}

        # Build match context
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
            f"[ARB] {match_label}{context} | {label}{line_info} | "
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
    print("[FB_live] shutdown complete")

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
            # areaId=11 -> first half, areaId=1 -> full match
            fh_raw, fm_raw = await asyncio.gather(
                fetch_betking_event(client, bk_fixture_id, 11),
                fetch_betking_event(client, bk_fixture_id, 1),
            )
        except Exception as e:
            print(f"{tag} [BetKing] {match_label} fetch failed: {e}")
            continue

        # Check if full match has ended
        if _betking_match_ended(fm_raw):
            print(f"{tag} {match_label} -- match ended (BetKing), freeing worker")
            match_over.set()
            break

        # Extract match info from full match response
        match_info = parse_betking_match_info(fm_raw)

        bk_markets = parse_betking_event(fh_raw.get("markets", []), fm_raw.get("markets", []))
        now = time.monotonic()
        live_bk_keys.clear()
        for mkey, m in bk_markets.items():
            live_bk_keys.add(mkey)
            cache.setdefault(mkey, {})["betking"] = {**m, "_ts": now}

        # Store match info in cache for later use
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

        # Extract match info from Bet9ja response
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
        print("[FB_live] stopped")
        raise StopTrading("FB_live stopped")

    async with httpx.AsyncClient(http2=True) as client:
        bk_raw, bj_raw = await asyncio.gather(
            fetch_betking_live(client),
            fetch_bet9ja_live(client),
            return_exceptions=True,
        )

    if isinstance(bk_raw, Exception):
        if not _last_state.get("bk_fail_logged", False):
            print(f"[FB_live][BetKing] fetch failed: {bk_raw}")
            _last_state["bk_fail_logged"] = True
        bk_matches = {}
    else:
        _last_state["bk_fail_logged"] = False
        bk_matches = parse_betking_live(bk_raw)

    if isinstance(bj_raw, Exception):
        if not _last_state.get("bj_fail_logged", False):
            print(f"[FB_live][Bet9ja] fetch failed: {bj_raw}")
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
            print(f"[FB_live] worker cap reached ({MAX_WORKERS}), skipping remaining matches")
        elif not current_state["cap_reached"] and _last_state["cap_reached"]:
            print(f"[FB_live] worker cap no longer reached ({len(_workers)} workers)")

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
        print(f"[FB_live] worker {worker_num} started: {match_label} | BetKing: {bkm['status_label']} ({bkm['score']}) | Bet9ja: {bjm['status_label']} ({bjm['score']})")

if __name__ == "__main__":
    asyncio.run(run_once())