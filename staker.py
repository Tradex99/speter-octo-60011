import httpx
from collections import deque
import asyncio
import json
import os
from urllib.parse import urlencode


#Config
STAKE_AMOUNT = 10

PLACEBET_URL = (
    "https://m.betking.com/en-ng/sports/action/placebet"
    "?_data=routes%2F%28%24locale%29.sports.action.placebet"
)

PLACEBET_HEADERS = {
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
}


def load_db_config(filepath="db.txt"):
    config = {}
    base = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(base, filepath), "r") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, _, value = line.partition("=")
                config[key.strip()] = value.strip().strip('"')
    return config


def get_cookie():
    from supabase import create_client
    config   = load_db_config("db.txt")
    supabase = create_client(config["SUPABASE_URL"], config["SUPABASE_KEY"])
    row      = supabase.table("betking").select("cookie").eq("id", 1).single().execute()
    return row.data["cookie"]


def _build_coupon(p):
    odd   = p["oddValue"]
    stake = STAKE_AMOUNT
    win   = round(stake * odd, 2)

    grouping_block = {
        "grouping":           1,
        "combinations":       1,
        "minWin":             win,
        "minWinNet":          win,
        "netStakeMinWin":     win,
        "maxWin":             win,
        "maxWinNet":          win,
        "netStakeMaxWin":     win,
        "minBonus":           0,
        "maxBonus":           0,
        "minPercentageBonus": 0,
        "maxPercentageBonus": 0,
        "stake":              stake,
        "netStake":           stake,
        "selected":           True,
    }

    return {
        "betCoupon": {
            "isClientSideCoupon":  True,
            "couponTypeId":        1,
            "minWin":              win,
            "minWinNet":           win,
            "netStakeMinWin":      win,
            "maxWin":              win,
            "maxWinNet":           win,
            "netStakeMaxWin":      win,
            "minBonus":            0,
            "maxBonus":            0,
            "minPercentageBonus":  0,
            "maxPercentageBonus":  0,
            "minOdd":              odd,
            "maxOdd":              odd,
            "totalOdds":           odd,
            "stake":               stake,
            "useGroupsStake":      False,
            "stakeGross":          stake,
            "stakeTaxed":          0,
            "taxPercentage":       0,
            "tax":                 0,
            "minWithholdingTax":   0,
            "maxWithholdingTax":   0,
            "turnoverTax":         0,
            "totalCombinations":   1,
            "odds": [{
                "IDSelectionType":    p["selectionTypeId"],
                "IDSport":            1,
                "allowFixed":         True,
                "compatibilityLevel": 0,
                "eventCategory":      "L",
                "eventId":            p["categoryId"],
                "eventName":          p["categoryName"],
                "fixed":              False,
                "gamePlay":           1,
                "incompatibleEvents": [],
                "isExpired":          False,
                "isLocked":           False,
                "isBetBuilder":       False,
                "marketId":           p["marketId"],
                "marketName":         p["marketName"],
                "marketTag":          0,
                "marketTypeId":       p["marketTypeId"],
                "matchId":            p["matchId"],
                "matchName":          p["matchName"],
                "oddValue":           odd,
                "parentEventId":      p["parentEventId"],
                "selectionId":        p["selectionId"],
                "selectionName":      p["selectionName"],
                "smartCode":          0,
                "specialValue":       p["specialValue"],
                "sportName":          "Football",
                "tournamentId":       p["tournamentId"],
                "tournamentName":     p["tournamentName"],
                "selectionKMId":      p["selectionKMId"],
                "matchKMId":          p["matchKMId"],
                "marketKMId":         p["marketKMId"],
                "isTransitioned":     False,
            }],
            "groupings":               [grouping_block],
            "possibleMissingGroupings": [],
            "currencyId":              -1,
            "isLive":                  True,
            "isVirtual":               False,
            "currentEvalMotivation":   0,
            "betCouponGlobalVariable": {
                "currencyId":                -1,
                "defaultStakeGross":         100,
                "isVirtualsInstallation":    False,
                "maxBetStake":               75000000,
                "maxCombinationBetWin":      75000000,
                "maxCombinationsByGrouping": 10000,
                "maxCouponCombinations":     10000,
                "maxGroupingsBetStake":      41641682,
                "maxMultipleBetWin":         75000000,
                "maxNoOfEvents":             40,
                "maxNoOfSelections":         40,
                "maxSingleBetWin":           75000000,
                "minBetStake":               10,
                "minBonusOdd":               1.35,
                "minFlexiCutOdds":           1.05,
                "minFlexiCutSelections":     5,
                "minGroupingsBetStake":      5,
                "stakeInnerMod0Combination": 0.01,
                "stakeMod0Multiple":         0,
                "stakeMod0Single":           0,
                "stakeThresholdMultiple":    75000,
                "stakeThresholdSingle":      7500,
                "flexiCutGlobalVariable": {
                    "parameters": {
                        "formulaId":            1,
                        "minOddThreshold":      1.05,
                        "minWinningSelections": 2,
                    }
                },
            },
            "language":     "en",
            "hasLive":      True,
            "couponType":   1,
            "allGroupings": [grouping_block],
        },
        "allowOddChanges":        True,
        "allowStakeReduction":    False,
        "requestTransactionId":   p["requestTransactionId"],
        "transferStakeFromAgent": False,
        "trackingData": {
            "category":          "football",
            "product":           "sportsbook-live",
            "is_reuse_bet":      False,
            "reused_selections": 0,
            "origin_bet_status": None,
            "bet_type":          "Singles",
        },
    }


def _build_body(p):
    coupon = _build_coupon(p)
    adjust = {"adjustId": "", "adjustIdfa": "", "gpsAdId": ""}
    return urlencode({
        "data":      json.dumps(coupon, separators=(",", ":")),
        "adjustIds": json.dumps(adjust, separators=(",", ":")),
    })


class Staker:
    def __init__(self):
        self._queue     = asyncio.Queue()
        self._bet_cache = deque(maxlen=20)  # event_ids of successfully placed bets

    async def queue(self, payload):
        await self._queue.put(payload)

    async def run(self):
        while True:
            payload = await self._queue.get()
            try:
                await self._place_bet(payload)
            except Exception as e:
                print(f"[staker] error      : {e}")
            finally:
                self._queue.task_done()

    async def _place_bet(self, p):
        match   = p["matchName"]
        line    = p["targetLine"]
        odd     = p["oddValue"]
        score   = p["score"]
        minutes = p["matchTime"]

        event_id = p["eventId"]
        if event_id in self._bet_cache:
            print(f"[staker] cached     : {match} already bet, skipping")
            return

        print(f"[staker] placing    : {match}")
        print(f"[staker] bet        : {score} {minutes}' | {line} @ {odd}")

        from module.betlist import check_can_bet
        allowed = await check_can_bet()
        if not allowed:
            print(f"[staker] skipped    : betlist check failed")
            return

        try:
            cookie = get_cookie()
        except Exception as e:
            print(f"[staker] cookie err : {e}")
            return

        headers = {**PLACEBET_HEADERS, "Cookie": cookie}
        body    = _build_body(p)

        async with httpx.AsyncClient(http2=True, timeout=20.0) as client:
            resp = await client.put(PLACEBET_URL, headers=headers, content=body.encode())

        if resp.status_code in (401, 301):
            print(f"[staker] session    : expired ({resp.status_code}) - attempting re-login")
            import sys, os
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            from login import login
            await login()
            return

        if resp.status_code != 200:
            print(f"[staker] failed     : HTTP {resp.status_code}")
            return

        if not resp.text or not resp.text.strip():
            print(f"[staker] session    : expired (empty body) - attempting re-login")
            import sys, os
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            from login import login
            await login()
            return

        try:
            data        = resp.json()
            coupon_code = data.get("couponCode")
            status      = data.get("responseStatus")
        except Exception:
            print(f"[staker] failed     : could not parse response")
            return

        if coupon_code:
            self._bet_cache.append(event_id)
            print(f"[staker] placed     : {match} | {line} @ {odd}")
            print(f"[staker] coupon     : {coupon_code}")
            print(f"[staker] cache      : {len(self._bet_cache)}/20 slots used")
        else:
            errors = data.get("errorsList", {})
            print(f"[staker] rejected   : {match} | status={status} | errors={errors}")
