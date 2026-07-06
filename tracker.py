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
    "module.BB_live",
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
            sb.table("cookies")
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
    """Re-check each account's cookie age every hour; re-login only if stale.
    Startup check is handled synchronously in main() before modules start."""
    while True:
        await asyncio.sleep(COOKIE_CHECK_INTERVAL)
        for account in COOKIE_ACCOUNTS:
            if _cookie_is_stale(account["name"]):
                _run_login_script(account["script"])


async def run_module_loop(module_name: str):
    mod = importlib.import_module(module_name)
    interval = getattr(mod, "INTERVAL_SECONDS", 60)
    print(f"[controller] starting {module_name} (every {interval}s)")

    stop_exc = getattr(mod, "StopTrading", None)

    while True:
        start = time.monotonic()
        try:
            await mod.run_once()
        except Exception as e:
            if stop_exc is not None and isinstance(e, stop_exc):
                print(f"[controller] {module_name} requested a full stop: {e}")
                if hasattr(mod, "shutdown"):
                    await mod.shutdown()
                raise  # propagate so main() tears the whole session down
            print(f"[controller] {module_name} crashed this cycle: {e}")

        elapsed = time.monotonic() - start
        sleep_for = max(0, interval - elapsed)
        await asyncio.sleep(sleep_for)


async def main():
    # Step 1: Cookie refresh (runs on startup, blocks until done)
    for account in COOKIE_ACCOUNTS:
        if _cookie_is_stale(account["name"]):
            _run_login_script(account["script"])

    # Step 2: Balance check via TT_betlist — exit entirely if insufficient funds
    print("[controller] checking account balances before starting...")
    try:
        from module.TT_betlist import check_and_wait, NeedRelogin
        try:
            can_proceed = await check_and_wait()
        except NeedRelogin as e:
            print(f"[controller] ⚠️ {e} — running b9_login.py to refresh cookie")
            _run_login_script("b9_login.py")
            try:
                can_proceed = await check_and_wait(allow_relogin=False)
            except NeedRelogin:
                # shouldn't happen with allow_relogin=False, but be safe
                can_proceed = False

        if not can_proceed:
            print("[controller] ❌ insufficient balance on both platforms — exiting")
            return
        print("[controller] ✅ balance check passed — starting modules")
    except Exception as e:
        print(f"[controller] balance check failed: {e} — proceeding anyway")

    # Step 3: Start cookie refresh loop + all modules
    print("[controller] STARTING -> RUNNING")
    tasks = [asyncio.create_task(cookie_refresh_loop())]
    tasks += [asyncio.create_task(run_module_loop(name)) for name in MODULES]

    try:
        await asyncio.gather(*tasks)
    except Exception as e:
  
        print(f"[controller] RUNNING -> STOPPING ({e})")
    finally:
        for t in tasks:
            if not t.done():
                t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        print("[controller] STOPPING -> STOPPED — trading session ended")


if __name__ == "__main__":
    asyncio.run(main())
