"""
common.py -- shared helpers for every sport/market module.

Anything used by more than one module (TT_winner, TT_live, FB_pmatch, ...)
belongs here, not copy-pasted into each module. That's the #1 source of
the bugs we kept hitting earlier (two copies of the same parsing logic
drifting apart).
"""
import json
import os
import asyncio
from datetime import datetime, timezone

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))

_supabase_client = None  # cached so we don't re-read db.txt / reconnect every call


def norm(name: str) -> str:
    return name.strip().lower()


def get_supabase():
    """Returns a cached Supabase client. Credentials read from db.txt one
    level above this project folder (same convention as betlist.py)."""
    global _supabase_client
    if _supabase_client is None:
        from supabase import create_client
        base = os.path.dirname(PROJECT_DIR)
        config = {}
        with open(os.path.join(base, "db.txt"), "r") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    key, _, value = line.partition("=")
                    config[key.strip()] = value.strip().strip('"')
        _supabase_client = create_client(config["SUPABASE_URL"], config["SUPABASE_KEY"])
    return _supabase_client


def today_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def day_label() -> str:
    today = today_str()
    y, m, d = today.split("-")
    return f"{int(d)}/{int(m)}/{y}"


def implied_sum(odd_a: float, odd_b: float) -> float:
    return 1 / odd_a + 1 / odd_b


def margin_pct(odd_a: float, odd_b: float) -> float:
    """Positive = real arbitrage profit %. Negative = bookmaker edge."""
    return (1 - implied_sum(odd_a, odd_b)) * 100


class ArbDurationTracker:
    """Tracks how long each arbitrage opportunity lasts across repeated
    run_once() calls, using a small local JSON state file.

    - New arb this scan (not seen before)  -> start tracking locally only.
    - Still arb this scan                  -> do nothing, keep waiting.
    - No longer arb this scan (was tracked) -> compute duration, INSERT
      exactly one row into Supabase, then stop tracking it.
    - Never arb at all                     -> never touches Supabase.

    Every Supabase write is .insert(), never .update() -- so nothing is
    ever overwritten, and each arb episode gets its own permanent row.
    """

    def __init__(self, state_filename: str, table_name: str = "arbitrage", market_name: str = "Winner"):
        self.state_path = os.path.join(PROJECT_DIR, state_filename)
        self.table_name = table_name
        self.market_name = market_name

    def _load(self) -> dict:
        if not os.path.exists(self.state_path):
            return {}
        try:
            with open(self.state_path) as f:
                return json.load(f)
        except Exception:
            return {}

    def _save(self, active: dict):
        with open(self.state_path, 'w') as f:
            json.dump(active, f, indent=2)

    async def update(self, current_arb_keys: set):
        """Call once per scan with the set of match keys that currently
        show arbitrage (e.g. {"Team A vs Team B", ...})."""
        active = self._load()
        now = datetime.now(timezone.utc)

        for key in current_arb_keys:
            if key not in active:
                active[key] = now.isoformat()

        ended_keys = [k for k in active if k not in current_arb_keys]
        if ended_keys:
            sb = None
            try:
                sb = await asyncio.to_thread(get_supabase)
            except Exception as e:
                print(f"[arb-tracker] could not reach Supabase: {e}")

            for key in ended_keys:
                started = datetime.fromisoformat(active[key])
                duration_seconds = int((now - started).total_seconds())
                if sb is not None:
                    try:
                        await asyncio.to_thread(
                            lambda k=key, d=duration_seconds: sb.table(self.table_name).insert({
                                "name": self.market_name,
                                "match": k,
                                "duration_seconds": d,
                                "date": day_label(),
                            }).execute()
                        )
                        print(f"[arb-tracker] logged ended arb: {key} ({duration_seconds}s)")
                    except Exception as e:
                        print(f"[arb-tracker] failed to log '{key}': {e}")
                del active[key]

        self._save(active)
