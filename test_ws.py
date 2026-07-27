"""
Standalone connection test for Bybit's v5 WebSocket API.

Mirrors the DB-loading pattern used in tracker.py (db.txt -> Supabase
api_keys table) but targets a row where exchange_name == "bybit".
Only exercises the connection itself:
  - public WS: connect + subscribe to one topic, confirm data arrives
  - private WS: connect + auth, confirm the server accepts the signature

Run with:  python bybit_ws_test.py
"""

import asyncio
import hmac
import hashlib
import json
import os
import time

import websockets

# --- toggle this exactly like DEMO_TRADING in tracker.py ---
TESTNET = True

PUBLIC_WS_URL = (
    "wss://stream-testnet.bybit.com/v5/public/linear"
    if TESTNET
    else "wss://stream.bybit.com/v5/public/linear"
)
PRIVATE_WS_URL = (
    "wss://stream-testnet.bybit.com/v5/private"
    if TESTNET
    else "wss://stream.bybit.com/v5/private"
)

# A harmless public topic just to prove data flows.
TEST_PUBLIC_TOPIC = "orderbook.1.BTCUSDT"

CONNECT_TIMEOUT_SEC = 10
RESPONSE_WAIT_SEC = 10


def _load_db_config() -> dict:
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.path.join(here, "db.txt"),
        os.path.join(os.path.dirname(here), "db.txt"),
    ]
    for path in candidates:
        if os.path.exists(path):
            config = {}
            with open(path, "r") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    if "=" in line:
                        k, _, v = line.partition("=")
                        config[k.strip()] = v.strip().strip('"')
            return config
    raise FileNotFoundError(f"db.txt not found in any of: {candidates}")


def _clean_secret(value):
    if value is None:
        return None
    if isinstance(value, str):
        value = value.strip()
        if value.upper() in ("", "NULL"):
            return None
    return value


def load_bybit_credentials() -> dict:
    """Same shape as tracker.py's load_bitmart_credentials(), but looks
    for the 'bybit' row in the api_keys Supabase table."""
    from supabase import create_client

    config = _load_db_config()
    sb = create_client(config["SUPABASE_URL"], config["SUPABASE_KEY"])

    rows = sb.table("api_keys").select("*").execute()
    match = next(
        (r for r in rows.data or [] if (r.get("exchange_name") or "").strip().lower() == "bybit"),
        None,
    )
    if not match:
        raise RuntimeError("No 'bybit' row found in the api_keys Supabase table")

    api_key = _clean_secret(match.get("api_key"))
    api_secret = _clean_secret(match.get("api_secret"))

    missing = [n for n, v in (("api_key", api_key), ("api_secret", api_secret)) if not v]
    if missing:
        raise RuntimeError(f"Bybit credentials incomplete in Supabase — missing: {', '.join(missing)}")

    return {"api_key": api_key, "api_secret": api_secret}


def build_ws_auth_signature(api_secret: str, expires_ms: int) -> str:
    payload = f"GET/realtime{expires_ms}"
    return hmac.new(api_secret.encode(), payload.encode(), hashlib.sha256).hexdigest()


async def test_public_ws():
    print(f"[public] connecting to {PUBLIC_WS_URL}")
    async with websockets.connect(PUBLIC_WS_URL, open_timeout=CONNECT_TIMEOUT_SEC) as ws:
        print("[public] connected — subscribing to", TEST_PUBLIC_TOPIC)
        await ws.send(json.dumps({"op": "subscribe", "args": [TEST_PUBLIC_TOPIC]}))

        saw_ack = False
        saw_data = False
        deadline = time.time() + RESPONSE_WAIT_SEC
        while time.time() < deadline and not (saw_ack and saw_data):
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=deadline - time.time())
            except asyncio.TimeoutError:
                break
            msg = json.loads(raw)
            if msg.get("op") == "subscribe":
                saw_ack = True
                print(f"[public] subscribe ack: success={msg.get('success')} ret_msg={msg.get('ret_msg')}")
            elif msg.get("topic") == TEST_PUBLIC_TOPIC:
                saw_data = True
                print(f"[public] received topic data (type={msg.get('type')}) — connection confirmed")

        if not saw_ack:
            print("[public] WARNING: no subscribe ack received within timeout")
        if not saw_data:
            print("[public] WARNING: no market data received within timeout")


async def test_private_ws(creds: dict):
    print(f"[private] connecting to {PRIVATE_WS_URL}")
    async with websockets.connect(PRIVATE_WS_URL, open_timeout=CONNECT_TIMEOUT_SEC) as ws:
        expires_ms = int((time.time() + 10) * 1000)
        sign = build_ws_auth_signature(creds["api_secret"], expires_ms)
        auth_msg = {"op": "auth", "args": [creds["api_key"], expires_ms, sign]}
        print("[private] connected — sending auth")
        await ws.send(json.dumps(auth_msg))

        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=RESPONSE_WAIT_SEC)
        except asyncio.TimeoutError:
            print("[private] WARNING: no auth response received within timeout")
            return

        msg = json.loads(raw)
        if msg.get("op") == "auth" and (msg.get("success") or msg.get("retCode") == 0):
            print(f"[private] \u2705 auth successful — {msg}")
        else:
            print(f"[private] \u274c auth failed — {msg}")
            print("[private] if this key requires IP whitelisting, confirm this machine's outbound IP is whitelisted on Bybit")


async def main():
    print(f"Bybit WS connection test (testnet={TESTNET})")
    await test_public_ws()

    creds = load_bybit_credentials()
    await test_private_ws(creds)


if __name__ == "__main__":
    asyncio.run(main())
