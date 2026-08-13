"""
historical_engine.py — 3-candle pattern strategy (5-minute candles).

Compares the latest three completed 5-minute candles against historical patterns
(~1 year) and emits a LONG/SHORT signal if at least 70% of similar historical
patterns were followed by a meaningful, sustained trend lasting >3 candles.
"""

from __future__ import annotations
import asyncio
import csv
import hashlib
import json
import logging
import math
import os
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from market_data import MarketDataStore, TradeStore, Signal, DEFAULT_SYMBOL_WHITELIST
from .base import StrategyContext, StrategyEngine

log = logging.getLogger("okx_futures.historical")
BASE = "https://www.okx.com"
HISTORY_PATH = "/api/v5/market/history-trades"


# ---------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------

@dataclass
class Candle:
    ts: float
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class HistoricalEngineConfig:
    # Data download
    history_days: int = 365
    history_cache_dir: str = ".historical_cache"
    history_refresh_hours: float = 24.0
    request_timeout_sec: float = 20.0
    request_delay_sec: float = 0.12
    max_history_requests_per_refresh: int = 0
    require_requested_history: bool = False

    # Candles
    candle_interval_sec: int = 300           # 5 minutes (was 60)
    live_window_ms: int = 900_000
    min_live_warmup_sec: float = 45.0
    min_live_trade_count: int = 20

    # Pattern matching
    pattern_length: int = 3
    min_historical_matches: int = 10
    min_directional_agreement: float = 0.70
    min_forward_trend_candles: int = 4
    min_forward_move_pct: float = 0.003
    min_forward_directional_ratio: float = 0.60   # new
    min_pattern_similarity: float = 0.85
    min_candle_range_atr_multiplier: float = 0.5
    min_candle_body_atr_multiplier: float = 0.3
    atr_period: int = 14

    # Signal gate
    cooldown_sec: float = 60.0
    max_observation_minutes: float = 6.0
    symbol_whitelist: Optional[frozenset] = field(
        default_factory=lambda: DEFAULT_SYMBOL_WHITELIST
    )
    log_top_matches: int = 3


@dataclass
class Candidate:
    symbol: str
    started_at: float = field(default_factory=time.time)
    status: str = "OBSERVING"
    last_checked_at: float = 0.0
    data_ready: bool = False
    direction: str = ""

    @property
    def elapsed_sec(self):
        return time.time() - self.started_at


# ---------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------

def f(v, default=0.0):
    try:
        x = float(v)
        return x if math.isfinite(x) else default
    except (TypeError, ValueError):
        return default


def timestamp(v):
    x = f(v, float("nan"))
    if math.isfinite(x):
        return x / 1000 if x > 10_000_000_000 else x
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def side_of(item):
    s = str(item.get("side", "")).lower()
    if s in ("buy", "sell"):
        return s
    m = item.get("m")
    if isinstance(m, bool):
        return "sell" if m else "buy"
    if str(m).lower() in ("true", "1"):
        return "sell"
    if str(m).lower() in ("false", "0"):
        return "buy"
    try:
        w = int(item.get("way"))
        if 1 <= w <= 4:
            return "buy"
        if 5 <= w <= 8:
            return "sell"
    except Exception:
        pass
    return None


def okx_get(path, params, timeout):
    url = BASE + path + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "historical-engine/1.0", "Accept": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read().decode())
    if data.get("code") != "0":
        raise RuntimeError(f"OKX error {data.get('code')}: {data.get('msg')}")
    return data.get("data") or []


async def download_trades(symbol, cfg):
    """Download trades for the symbol for the last cfg.history_days days."""
    end = time.time()
    start = end - cfg.history_days * 86400
    cursor = str(int(end * 1000))
    rows = []
    requests = 0
    while True:
        if cfg.max_history_requests_per_refresh and requests >= cfg.max_history_requests_per_refresh:
            break
        try:
            batch = await asyncio.to_thread(
                okx_get,
                HISTORY_PATH,
                {"instId": symbol, "limit": "100", "before": cursor},
                cfg.request_timeout_sec
            )
        except Exception as e:
            log.error("[historical] %s download failed: %s", symbol, e)
            break
        requests += 1
        if not batch:
            break
        parsed = []
        for z in batch:
            ts = timestamp(z.get("ts"))
            px = f(z.get("px"))
            qty = f(z.get("sz"))
            side = side_of(z)
            if ts is not None and px > 0 and qty > 0 and side:
                parsed.append({
                    "ts": ts,
                    "price": px,
                    "qty": qty,
                    "side": side,
                    "tradeId": str(z.get("tradeId") or "")
                })
        if not parsed:
            break
        rows.extend(t for t in parsed if start <= t["ts"] <= end)
        oldest = min(t["ts"] for t in parsed)
        if oldest <= start:
            break
        nxt = str(int(oldest * 1000) - 1)
        if nxt == cursor:
            break
        cursor = nxt
        await asyncio.sleep(max(0, cfg.request_delay_sec))
    # deduplicate
    seen = set()
    out = []
    for t in sorted(rows, key=lambda x: (x["ts"], x["tradeId"])):
        k = (t["ts"], t["price"], t["qty"], t["side"], t["tradeId"])
        if k not in seen:
            seen.add(k)
            out.append(t)
    return out


def build_candles_from_trades(trades, interval_sec=300):
    """Aggregate trades into candles of given interval (default 5 minutes)."""
    if not trades:
        return []
    candles = []
    trades_sorted = sorted(trades, key=lambda x: x["ts"])
    start_ts = trades_sorted[0]["ts"]
    end_ts = trades_sorted[-1]["ts"]
    current_bucket_start = math.floor(start_ts / interval_sec) * interval_sec
    bucket_end = current_bucket_start + interval_sec
    bucket_open = None
    bucket_high = -float("inf")
    bucket_low = float("inf")
    bucket_close = None
    bucket_volume = 0.0
    for t in trades_sorted:
        ts = t["ts"]
        if ts >= bucket_end:
            # finalize bucket
            if bucket_open is not None and bucket_close is not None:
                candles.append(Candle(
                    ts=current_bucket_start,
                    open=bucket_open,
                    high=bucket_high,
                    low=bucket_low,
                    close=bucket_close,
                    volume=bucket_volume
                ))
            # advance to next bucket(s)
            while ts >= bucket_end:
                current_bucket_start += interval_sec
                bucket_end += interval_sec
            bucket_open = t["price"]
            bucket_high = t["price"]
            bucket_low = t["price"]
            bucket_close = t["price"]
            bucket_volume = t["qty"]
        else:
            bucket_high = max(bucket_high, t["price"])
            bucket_low = min(bucket_low, t["price"])
            bucket_close = t["price"]
            bucket_volume += t["qty"]
            if bucket_open is None:
                bucket_open = t["price"]
    # final bucket
    if bucket_open is not None and bucket_close is not None:
        candles.append(Candle(
            ts=current_bucket_start,
            open=bucket_open,
            high=bucket_high,
            low=bucket_low,
            close=bucket_close,
            volume=bucket_volume
        ))
    return candles


def compute_atr(candles, period=14):
    """Compute ATR (high-low range) over the last `period` candles."""
    if len(candles) < period:
        return None
    ranges = [c.high - c.low for c in candles[-period:]]
    return sum(ranges) / len(ranges)


def candle_significant(candle, atr, cfg):
    """Check if a candle meets the minimum size requirements."""
    if atr is None or atr <= 0:
        return False
    candle_range = candle.high - candle.low
    candle_body = abs(candle.close - candle.open)
    return (candle_range >= cfg.min_candle_range_atr_multiplier * atr and
            candle_body >= cfg.min_candle_body_atr_multiplier * atr)


def pattern_features(candles):
    """
    Extract feature vector for a 3-candle pattern.
    Features: normalized OHLC of each candle relative to first candle's open.
    Returns list of 12 floats (3 candles * 4 OHLC).
    """
    if len(candles) != 3:
        raise ValueError("Pattern must have exactly 3 candles")
    base = candles[0].open
    if base == 0:
        base = 1e-6
    vec = []
    for c in candles:
        vec.extend([c.open / base, c.high / base, c.low / base, c.close / base])
    return vec


def pattern_similarity(vec1, vec2):
    """
    Compute similarity (0..1) between two feature vectors using inverse
    of Euclidean distance.
    """
    if len(vec1) != len(vec2):
        return 0.0
    diff_sq = sum((a - b) ** 2 for a, b in zip(vec1, vec2))
    dist = math.sqrt(diff_sq)
    sim = max(0.0, 1.0 - dist / 2.0)
    return min(1.0, sim)


def classify_forward_trend(candles, start_idx, cfg):
    """
    Analyze candles after the pattern (from start_idx+3 onward) to determine
    if a meaningful, sustained trend exists.
    Returns 'bullish', 'bearish', or 'neutral'.

    Conditions:
      - At least cfg.min_forward_trend_candles candles exist after the pattern.
      - Net percentage change over that period exceeds cfg.min_forward_move_pct
        in either direction.
      - At least cfg.min_forward_directional_ratio of the forward candles are
        in the direction of the net move (i.e., candle close > open for bullish,
        close < open for bearish).
    """
    total_candles = len(candles)
    pattern_end = start_idx + cfg.pattern_length
    if pattern_end + cfg.min_forward_trend_candles > total_candles:
        return "neutral"

    # Use exactly min_forward_trend_candles candles after the pattern
    forward_candles = candles[pattern_end:pattern_end + cfg.min_forward_trend_candles]
    start_price = candles[pattern_end - 1].close
    end_price = forward_candles[-1].close
    net_move_pct = (end_price - start_price) / start_price if start_price else 0.0

    # Determine direction based on net move
    if abs(net_move_pct) < cfg.min_forward_move_pct:
        return "neutral"   # too small

    # Count directional candles
    direction = 1 if net_move_pct > 0 else -1  # +1 bullish, -1 bearish
    dir_count = 0
    for c in forward_candles:
        move = c.close - c.open
        if (move > 0 and direction > 0) or (move < 0 and direction < 0):
            dir_count += 1
    ratio = dir_count / len(forward_candles)

    if ratio < cfg.min_forward_directional_ratio:
        return "neutral"   # insufficient persistence

    return "bullish" if direction > 0 else "bearish"


# ---------------------------------------------------------------------
# Dataset: stores historical candles and precomputed pattern features
# ---------------------------------------------------------------------

class Dataset:
    def __init__(self, candles: List[Candle], cfg: HistoricalEngineConfig):
        self.candles = candles
        self.cfg = cfg
        self.atr = compute_atr(candles, cfg.atr_period)
        self.patterns = []   # list of (start_idx, feature_vector)
        self._build_patterns()

    def _build_patterns(self):
        """Precompute feature vectors for all valid 3-candle patterns."""
        n = len(self.candles)
        if n < self.cfg.pattern_length + self.cfg.min_forward_trend_candles:
            return
        max_start = n - self.cfg.pattern_length - self.cfg.min_forward_trend_candles
        for i in range(max_start + 1):
            pattern_candles = self.candles[i:i+self.cfg.pattern_length]
            if self.atr is not None:
                if not all(candle_significant(c, self.atr, self.cfg) for c in pattern_candles):
                    continue
            vec = pattern_features(pattern_candles)
            self.patterns.append((i, vec))

    def find_matches(self, current_pattern_candles: List[Candle]) -> List[Tuple[int, float]]:
        """Return list of (start_idx, similarity) for matches above threshold."""
        if self.atr is None:
            return []
        if not all(candle_significant(c, self.atr, self.cfg) for c in current_pattern_candles):
            return []
        cur_vec = pattern_features(current_pattern_candles)
        matches = []
        for start_idx, hist_vec in self.patterns:
            sim = pattern_similarity(cur_vec, hist_vec)
            if sim >= self.cfg.min_pattern_similarity:
                matches.append((start_idx, sim))
        matches.sort(key=lambda x: x[1], reverse=True)
        return matches

    def classify_forward(self, start_idx: int) -> str:
        """Classify the forward trend after the pattern at start_idx."""
        return classify_forward_trend(self.candles, start_idx, self.cfg)


# ---------------------------------------------------------------------
# Main engine
# ---------------------------------------------------------------------

class HistoricalEngine(StrategyEngine):
    name = "historical_engine"

    def __init__(self, trade_store, market_data, candle_fetcher=None, config=None):
        self._trade_store = trade_store
        self._market_data = market_data
        self._candle_fetcher = candle_fetcher
        self.config = config or HistoricalEngineConfig()
        self._candidates = {}
        self._datasets = {}
        self._ready = {}
        self._tasks = {}
        self._last_signal = {}
        self._lock = asyncio.Lock()

    def _cache_path(self, symbol):
        h = hashlib.sha1(symbol.encode()).hexdigest()[:10]
        return os.path.join(self.config.history_cache_dir, f"{symbol.replace('-', '_')}_{h}.csv")

    async def sync_watchlist(self, symbols):
        symbols = set(symbols)
        if self.config.symbol_whitelist:
            symbols &= set(self.config.symbol_whitelist)
        async with self._lock:
            for s in symbols:
                self._candidates.setdefault(s, Candidate(s))
            for s in list(self._candidates):
                if s not in symbols:
                    self._candidates.pop(s, None)
        for s in symbols:
            if s not in self._tasks or self._tasks[s].done():
                self._tasks[s] = asyncio.create_task(self._prepare(s))

    async def snapshot(self):
        async with self._lock:
            return list(self._candidates.values())

    async def _prepare(self, symbol):
        """Download trades, build 5-minute candles, and create dataset."""
        cfg = self.config
        os.makedirs(cfg.history_cache_dir, exist_ok=True)
        cache_path = self._cache_path(symbol)

        # Try loading from cache
        if os.path.exists(cache_path) and time.time() - os.path.getmtime(cache_path) <= cfg.history_refresh_hours * 3600:
            try:
                candles = self._load_candles(cache_path)
                if candles:
                    ds = Dataset(candles, cfg)
                    self._datasets[symbol] = ds
                    self._ready[symbol] = True
                    log.info("[historical] %s loaded %d candles from cache", symbol, len(candles))
                    return
            except Exception as e:
                log.warning("[historical] %s cache invalid: %s", symbol, e)

        # Download fresh
        trades = await download_trades(symbol, cfg)
        if not trades:
            self._ready[symbol] = False
            return
        covered = (trades[-1]["ts"] - trades[0]["ts"]) / 86400
        if cfg.require_requested_history and covered < cfg.history_days * 0.9:
            log.error("[historical] %s only %.1f days available; refusing to trade", symbol, covered)
            self._ready[symbol] = False
            return

        # Build 5-minute candles (interval from config)
        candles = build_candles_from_trades(trades, interval_sec=cfg.candle_interval_sec)
        if len(candles) < cfg.pattern_length + cfg.min_forward_trend_candles:
            self._ready[symbol] = False
            return
        self._save_candles(cache_path, candles)
        ds = Dataset(candles, cfg)
        self._datasets[symbol] = ds
        self._ready[symbol] = True
        log.info("[historical] %s READY candles=%d coverage=%.1f days", symbol, len(candles), covered)

    def _save_candles(self, path, candles):
        tmp = path + ".tmp"
        with open(tmp, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=["ts", "open", "high", "low", "close", "volume"])
            writer.writeheader()
            for c in candles:
                writer.writerow({
                    "ts": c.ts,
                    "open": c.open,
                    "high": c.high,
                    "low": c.low,
                    "close": c.close,
                    "volume": c.volume
                })
        os.replace(tmp, path)

    @staticmethod
    def _load_candles(path):
        candles = []
        with open(path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                candles.append(Candle(
                    ts=f(row.get("ts")),
                    open=f(row.get("open")),
                    high=f(row.get("high")),
                    low=f(row.get("low")),
                    close=f(row.get("close")),
                    volume=f(row.get("volume"))
                ))
        return candles

    async def _get_current_pattern(self, symbol):
        """Fetch the latest 3 completed 5-minute candles using candle_fetcher."""
        if self._candle_fetcher is None:
            # fallback: build from trade store (less accurate)
            trades = await self._trade_store.get_window(symbol, self.config.live_window_ms)
            if len(trades) < 20:
                return None
            live_candles = build_candles_from_trades(trades, interval_sec=self.config.candle_interval_sec)
            if len(live_candles) < 3:
                return None
            return live_candles[-3:]
        try:
            # Fetch 5 candles to ensure we get 3 completed ones (5-minute)
            raw = await self._candle_fetcher(symbol, "5m", 5)
        except Exception as e:
            log.warning("[historical] %s cannot fetch candles: %s", symbol, e)
            return None
        if not raw:
            return None
        # Take last 3 completed (confirm=1)
        completed = [c for c in raw if str(c.get("confirm", "1")) == "1"]
        if len(completed) < 3:
            return None
        completed_sorted = sorted(completed, key=lambda x: x.get("ts", 0))
        last_three = completed_sorted[-3:]
        candles = []
        for c in last_three:
            candles.append(Candle(
                ts=f(c.get("ts", 0)),
                open=f(c.get("open")),
                high=f(c.get("high")),
                low=f(c.get("low")),
                close=f(c.get("close")),
                volume=f(c.get("volume", 0))
            ))
        return candles

    async def evaluate(self, symbol):
        cfg = self.config
        c = self._candidates.get(symbol)
        if c is None:
            return None
        now = time.time()
        c.last_checked_at = now
        if c.elapsed_sec >= cfg.max_observation_minutes * 60:
            c.status = "EXPIRED"
            async with self._lock:
                self._candidates.pop(symbol, None)
            return None

        if not self._ready.get(symbol, False):
            return None

        ds = self._datasets.get(symbol)
        if ds is None:
            return None

        if now - self._last_signal.get(symbol, 0) < cfg.cooldown_sec:
            return None

        market = await self._market_data.get(symbol)
        if not market:
            return None
        price = f(market.get("last_price"))
        if price <= 0:
            return None

        trades = await self._trade_store.get_window(symbol, cfg.live_window_ms)
        if len(trades) < cfg.min_live_trade_count:
            return None
        c.data_ready = c.elapsed_sec >= cfg.min_live_warmup_sec and len(trades) >= cfg.min_live_trade_count
        if not c.data_ready:
            return None

        # Get current 3 completed 5-minute candles
        current_pattern = await self._get_current_pattern(symbol)
        if current_pattern is None or len(current_pattern) != cfg.pattern_length:
            return None

        # Find historical matches
        matches = ds.find_matches(current_pattern)
        if len(matches) < cfg.min_historical_matches:
            log.debug("[historical] %s: only %d matches, need %d", symbol, len(matches), cfg.min_historical_matches)
            return None

        # Classify forward trends for each match
        bullish = 0
        bearish = 0
        neutral = 0
        details = []
        top_n = min(cfg.log_top_matches, len(matches))
        for idx, (start_idx, sim) in enumerate(matches):
            outcome = ds.classify_forward(start_idx)
            if outcome == "bullish":
                bullish += 1
            elif outcome == "bearish":
                bearish += 1
            else:
                neutral += 1
            if idx < top_n:
                # For debug, we can also compute directional ratio etc.
                details.append((sim, outcome))
        total = bullish + bearish + neutral
        bullish_ratio = bullish / total if total else 0.0
        bearish_ratio = bearish / total if total else 0.0

        log.info(
            "[historical] %s pattern matches=%d (bullish=%d, bearish=%d, neutral=%d) "
            "bull=%.1f%% bear=%.1f%%",
            symbol, total, bullish, bearish, neutral,
            bullish_ratio*100, bearish_ratio*100
        )
        if details:
            log.debug("[historical] Top matches: %s", details)

        direction = ""
        if bullish_ratio >= cfg.min_directional_agreement:
            direction = "long"
        elif bearish_ratio >= cfg.min_directional_agreement:
            direction = "short"
        else:
            return None

        c.direction = direction
        log.info("[historical] ACCEPTED %s %s with %.1f%% agreement", symbol, direction.upper(), max(bullish_ratio, bearish_ratio)*100)
        self._last_signal[symbol] = now
        async with self._lock:
            self._candidates.pop(symbol, None)

        return Signal(
            symbol=symbol,
            direction=direction,
            confidence=max(bullish_ratio, bearish_ratio),
            entry_price=price,
            take_profit=price,
            stop_loss=price,
            timestamp=now,
            reasons=[
                "engine=historical_3candle_pattern",
                f"matches={total}",
                f"bullish={bullish}",
                f"bearish={bearish}",
                f"neutral={neutral}",
                f"agreement={max(bullish_ratio, bearish_ratio):.2f}"
            ]
        )


def build(ctx: StrategyContext) -> HistoricalEngine:
    cfg = ctx.build_config(HistoricalEngineConfig)
    return HistoricalEngine(ctx.trade_store, ctx.market_data, ctx.candle_fetcher, cfg)