"""
tracker.py -- general controller. Runs every registered module on its
own interval, forever, inside one asyncio event loop.

On startup and every 5 hours, refreshes cookies by running bk_login.py
and b9_login.py if the wixnation cookie in the DB is older than 5 hours.
"""
import asyncio
import importlib
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

MODULES = [
    "module.TT_live",   # TT live match cross-reference -- every 5s
    # "module.TT_winner",
    # "module.FB_pmatch",
]

COOKIE_ACCOUNTS = [
    {"name": "wixnation",   "script": "b9_login.py"},
    {"name": "Chukwuebuka", "script": "bk_login.py"},
]

COOKIE_MAX_AGE_HOURS = 5
COOKIE_CHECK_INTERVAL = 60 * 60  # re-check every hour


def _get_supabase():
    from supabase import create_client
    base = os.path.dirname(os.path.abspath(__file__))
    config = {}
    with open(os.path.join(base, "db.txt"), "r") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                k, _, v = line.partition("=")
                config[k.strip()] = v.strip().strip('"')
    return create_client(config["SUPABASE_URL"], config["SUPABASE_KEY"])


def _cookie_is_stale(account_name: str) -> bool:
    """Return True if the named account's cookie is older than COOKIE_MAX_AGE_HOURS."""
    try:
        sb = _get_supabase()
        row = (
            sb.table("betking")
            .select("created_at")
            .eq("name", account_name)
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )
        if not row.data:
            print(f"[controller] {account_name}: no cookie found — will run login")
            return True

        created_at_str = row.data[0]["created_at"]
        created_at = datetime.fromisoformat(created_at_str.replace(" ", "T"))
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)

        age = datetime.now(timezone.utc) - created_at
        hours_old = age.total_seconds() / 3600

        if hours_old >= COOKIE_MAX_AGE_HOURS:
            print(f"[controller] {account_name}: cookie age {hours_old:.1f}h — stale, refreshing")
            return True
        else:
            print(f"[controller] {account_name}: cookie age {hours_old:.1f}h — fresh")
            return False

    except Exception as e:
        print(f"[controller] {account_name}: age check failed ({e}) — will run login")
        return True


def _run_login_script(script: str):
    """Run a login script with live stdout/stderr output."""
    print(f"[controller] running {script}...")
    try:
        process = subprocess.Popen(
            [sys.executable, "-u", script],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        for line in process.stdout:
            print(f"  [{script}] {line}", end="")
        process.wait()
        if process.returncode == 0:
            print(f"[controller] {script} completed successfully")
        else:
            print(f"[controller] {script} exited with code {process.returncode}")
    except subprocess.TimeoutExpired:
        print(f"[controller] {script} timed out after 120s")
    except FileNotFoundError:
        print(f"[controller] {script} not found — skipping")
    except Exception as e:
        print(f"[controller] {script} error: {e}")


async def cookie_refresh_loop():
    """Check each account's cookie age on startup and every hour; re-login only if stale."""
    while True:
        for account in COOKIE_ACCOUNTS:
            if _cookie_is_stale(account["name"]):
                _run_login_script(account["script"])
        await asyncio.sleep(COOKIE_CHECK_INTERVAL)


async def run_module_loop(module_name: str):
    mod = importlib.import_module(module_name)
    interval = getattr(mod, "INTERVAL_SECONDS", 60)
    print(f"[controller] starting {module_name} (every {interval}s)")

    while True:
        start = time.monotonic()
        try:
            await mod.run_once()
        except Exception as e:
            print(f"[controller] {module_name} crashed this cycle: {e}")

        elapsed = time.monotonic() - start
        sleep_for = max(0, interval - elapsed)
        await asyncio.sleep(sleep_for)


async def main():
    tasks = [asyncio.create_task(cookie_refresh_loop())]
    tasks += [asyncio.create_task(run_module_loop(name)) for name in MODULES]
    await asyncio.gather(*tasks)


if __name__ == "__main__":
    asyncio.run(main())
