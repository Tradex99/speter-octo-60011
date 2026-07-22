import time
import logging

import analyzer as base

logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    handlers=[logging.FileHandler("trader_execution.log"), logging.StreamHandler()],
    force=True,
)
log = logging.getLogger()
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

MIN_ROI_PCT = 0.1
CHECK_INTERVAL_SEC = 60


def load_all_trade_coins() -> list:
    sb = base._get_supabase_cached()
    try:
        rows = sb.table("trade_coins").select("*").execute()
        return rows.data or []
    except Exception as e:
        log.warning(f"trade_coins fetch failed: {str(e)[:200]}")
        return []


def delete_trade_coin(row_id) -> bool:
    sb = base._get_supabase_cached()
    try:
        sb.table("trade_coins").delete().eq("id", row_id).execute()
        return True
    except Exception as e:
        log.warning(f"trade_coins delete failed for id {row_id}: {str(e)[:200]}")
        return False


def parse_buy_sell_exchanges(exchange_field: str):
    if not exchange_field or "/" not in exchange_field:
        return None, None
    buy_ex, sell_ex = exchange_field.split("/", 1)
    return buy_ex.strip(), sell_ex.strip()


def parse_usdt_transfer_fee(value: str):
    if not value or "/" not in value:
        return None, None
    network, fee = value.rsplit("/", 1)
    try:
        return network.strip(), float(fee)
    except ValueError:
        return network.strip(), None


def get_capital_holder() -> str:
    state = base.load_bot_state()
    return state.get("holds_usdt")


WALLET_TYPES = {
    'Bybit':   ['unified', 'spot', 'swap', 'funding'],
    'Bitget':  ['spot', 'swap'],
    'MEXC':    ['spot', 'swap'],
    'BingX':   ['spot', 'swap', 'funding'],
    'KuCoin':  ['main', 'trade', 'future'],
    'CoinEx':  ['spot', 'swap', 'margin'],
    'BitMart': ['spot', 'swap', 'account'],
    'OKX':     ['spot', 'swap', 'funding'],
    'LBank':   ['spot'],
}


def _extract_usdt(balance):
    if not isinstance(balance, dict):
        return None
    usdt = balance.get('USDT')
    if isinstance(usdt, dict) and any(k in usdt for k in ('free', 'used', 'total')):
        return usdt.get('free'), usdt.get('used'), usdt.get('total')
    free_map  = balance.get('free')  or {}
    used_map  = balance.get('used')  or {}
    total_map = balance.get('total') or {}
    if 'USDT' in total_map or 'USDT' in free_map or 'USDT' in used_map:
        return free_map.get('USDT'), used_map.get('USDT'), total_map.get('USDT')
    for code in set(list(total_map) + list(free_map) + list(used_map)):
        if isinstance(code, str) and code.upper() == 'USDT':
            return free_map.get(code), used_map.get(code), total_map.get(code)
    return None


def get_available_usdt_balance(exchange_name: str):
    ex = base.ensure_exchange(exchange_name)
    if ex is None:
        log.warning(f"{exchange_name}: could not initialize exchange for balance check")
        return None, None

    candidates = []

    try:
        bal = ex.fetch_balance()
        usdt = _extract_usdt(bal)
        if usdt and usdt[0]:
            candidates.append(('overview', usdt[0]))
    except Exception as e:
        log.warning(f"{exchange_name} overview fetch_balance failed: {str(e)[:200]}")

    for wallet_type in WALLET_TYPES.get(exchange_name, []):
        try:
            bal = ex.fetch_balance(params={'type': wallet_type})
            usdt = _extract_usdt(bal)
            if usdt and usdt[0]:
                candidates.append((wallet_type, usdt[0]))
        except Exception as e:
            log.warning(f"{exchange_name} {wallet_type} fetch_balance failed: {str(e)[:200]}")

    if not candidates:
        return None, None

    label, free = max(candidates, key=lambda c: c[1])
    return free, label


def check_live_depth(buy_ex: str, sell_ex: str, symbol: str, capital_usd: float):
    buy_venue  = base.ensure_exchange(buy_ex)
    sell_venue = base.ensure_exchange(sell_ex)
    if buy_venue is None or sell_venue is None:
        return {'ok': False, 'reason': 'could not initialize buy/sell exchange for depth check'}

    buy_ob  = base._fetch_order_book_safe(buy_ex,  buy_venue,  symbol)
    sell_ob = base._fetch_order_book_safe(sell_ex, sell_venue, symbol)
    if buy_ob is None or sell_ob is None:
        return {'ok': False, 'reason': 'could not fetch fresh order book from one or both exchanges'}

    asks = base._sort_book_side(buy_ob.get('asks', []) or [], 'asks')
    bids = base._sort_book_side(sell_ob.get('bids', []) or [], 'bids')

    buy_price,  _, buy_ok  = base.walk_book(asks, capital_usd)
    sell_price, _, sell_ok = base.walk_book(bids, capital_usd)

    if buy_price is None or sell_price is None or buy_price <= 0:
        return {'ok': False, 'reason': 'order book too thin to price the full balance'}

    return {
        'ok': True,
        'buy_price': buy_price,
        'sell_price': sell_price,
        'liquidity_ok': bool(buy_ok and sell_ok),
    }


def recalc_profit(trade_row: dict, buy_ex: str, sell_ex: str, symbol: str,
                   capital_usd: float, buy_price: float, sell_price: float):
    min_withdrawal = base._to_float(trade_row.get('min_withdrawal'))
    gas_deducted   = base._to_float(trade_row.get('gas_deducted'))

    buy_fee_rate  = base.get_trading_fee_rate(buy_ex,  symbol)
    sell_fee_rate = base.get_trading_fee_rate(sell_ex, symbol)

    profit = base.calc_arb_profit(
        capital_usd, buy_price, sell_price,
        fee_tokens=gas_deducted,
        min_withdrawal_tokens=min_withdrawal,
        buy_taker_rate=buy_fee_rate,
        sell_taker_rate=sell_fee_rate,
    )
    if not profit:
        return None

    network, fee_usd = parse_usdt_transfer_fee(trade_row.get('usdt_transfer_fee'))
    profit['usdt_transfer_network'] = network
    profit['usdt_transfer_fee_usd'] = fee_usd or 0.0
    profit['usdt_transfer_from']    = trade_row.get('usdt_holder')
    profit['usdt_transfer_to']      = buy_ex
    if fee_usd:
        profit['net_pnl'] -= fee_usd
        profit['roi_pct']  = (profit['net_pnl'] / profit['capital']) * 100 if profit['capital'] else 0.0

    return profit


def validate_trade(capital_usd, min_withdrawal_met, liquidity_ok, profit):
    if not capital_usd or capital_usd <= 0:
        return False, "available USDT balance is zero"
    if not min_withdrawal_met:
        return False, "available balance does not satisfy the minimum withdrawal requirement"
    if not liquidity_ok:
        return False, "order book liquidity is insufficient for the full balance"
    if profit is None or profit['net_pnl'] <= 0:
        return False, "recalculated net profit is not positive"
    if profit['roi_pct'] < MIN_ROI_PCT:
        return False, f"ROI {profit['roi_pct']:+.2f}% is below the {MIN_ROI_PCT}% minimum threshold"
    return True, None


def evaluate_trade(row: dict) -> dict:
    pair = row.get('pair')
    buy_ex, sell_ex = parse_buy_sell_exchanges(row.get('exchange'))
    result = {
        'pair': pair, 'buy_ex': buy_ex, 'sell_ex': sell_ex,
        'holder_ex': None, 'capital': None, 'profit': None,
        'valid': False, 'reason': None,
    }

    if not pair or not buy_ex or not sell_ex:
        result['reason'] = f"could not parse trade row: {row}"
        return result

    holder_ex = get_capital_holder()
    result['holder_ex'] = holder_ex
    if not holder_ex:
        result['reason'] = "bot_state.holds_usdt is not set"
        return result

    capital, _ = get_available_usdt_balance(holder_ex)
    result['capital'] = capital
    if capital is None:
        result['reason'] = "could not retrieve available USDT balance"
        return result

    depth = check_live_depth(buy_ex, sell_ex, pair, capital)
    if not depth['ok']:
        result['reason'] = depth['reason']
        return result

    profit = recalc_profit(row, buy_ex, sell_ex, pair, capital, depth['buy_price'], depth['sell_price'])
    if profit:
        profit['_buy_price']  = depth['buy_price']
        profit['_sell_price'] = depth['sell_price']
    result['profit'] = profit

    valid, reason = validate_trade(
        capital_usd=capital,
        min_withdrawal_met=profit['min_withdrawal_met'] if profit else False,
        liquidity_ok=depth['liquidity_ok'],
        profit=profit,
    )
    result['valid'] = valid
    result['reason'] = reason
    return result


def print_trade_report(result: dict):
    pair, buy_ex, sell_ex = result['pair'], result['buy_ex'], result['sell_ex']
    holder_ex, capital, profit = result['holder_ex'], result['capital'], result['profit']

    log.info(f"Pair: {pair}")
    log.info(f"Buy exchange: {buy_ex}")
    log.info(f"Sell exchange: {sell_ex}")
    log.info(f"Capital holder: {holder_ex}")
    log.info(f"Available USDT: {capital:.2f}" if capital is not None else "Available USDT: N/A")
    log.info("")
    log.info("Fetching fresh order books...")
    log.info("")

    if profit:
        base_symbol = pair.split('/')[0] if pair else ""
        log.info(f"Buy execution price: {base.fmt_price(profit['_buy_price'])}")
        log.info(f"Sell execution price: {base.fmt_price(profit['_sell_price'])}")
        log.info(f"Trading fee: -{profit['buy_fee_usd']:.4f} / -{profit['sell_fee_usd']:.4f} USDT")
        log.info(f"Transfer fee: -{profit['usdt_transfer_fee_usd']:.4f} USDT")
        log.info(f"Gas deduction: -{profit['gas_tokens']:.6f} {base_symbol}")
        log.info(f"Minimum withdrawal: {profit['min_withdrawal_tokens']} {base_symbol}")
        log.info(f"Tokens purchased: {profit['tokens_bought']:.6f}")
        log.info(f"Tokens received: {profit['tokens_remaining']:.6f}")
        log.info(f"Expected sell value: {profit['total_received']:.4f} USDT")
        log.info(f"Net profit: {profit['net_pnl']:+.4f} USDT")
        log.info(f"ROI: {profit['roi_pct']:+.2f}%")

    log.info(f"Trade status: {'Valid' if result['valid'] else 'Invalid'}")
    if not result['valid'] and result['reason']:
        log.info(f"Reason: {result['reason']}")


def execute_market_buy(buy_ex, symbol, capital_usd):
    raise NotImplementedError


def initiate_withdrawal(buy_ex, sell_ex, symbol, network, coin_dest_address):
    raise NotImplementedError


def monitor_transfer(sell_ex, symbol, expected_arrival):
    raise NotImplementedError


def execute_market_sell(sell_ex, symbol, amount):
    raise NotImplementedError


def update_trade_coins_and_bot_state(*args, **kwargs):
    raise NotImplementedError


def run_once():
    rows = load_all_trade_coins()
    total = len(rows)

    log.info("-" * 40)
    log.info("Trader started")
    log.info("-" * 40)
    log.info(f"Loaded {total} trade opportunities.")
    log.info("")

    if total == 0:
        log.info("Finished.")
        return

    valid_count = 0
    invalid_count = 0
    deleted_count = 0

    for i, row in enumerate(rows, start=1):
        log.info(f"Checking trade {i}/{total}...")
        result = evaluate_trade(row)
        print_trade_report(result)

        if result['valid']:
            valid_count += 1
            log.info("Keeping opportunity for execution...")
        else:
            invalid_count += 1
            log.info("Deleting invalid opportunity...")
            if delete_trade_coin(row.get('id')):
                deleted_count += 1
        log.info("")

    remaining = total - deleted_count

    log.info("Finished.")
    log.info("")
    log.info(f"Processed: {total}")
    log.info(f"Valid: {valid_count}")
    log.info(f"Invalid: {invalid_count}")
    log.info(f"Deleted: {deleted_count}")
    log.info(f"Remaining in trade_coins: {remaining}")


def main():
    base.set_exchange_mode('trader')
    while True:
        try:
            run_once()
        except Exception as e:
            log.error(f"trader.py: unexpected error in run cycle: {str(e)[:300]}")
        log.info("")
        log.info(f"Next check in {CHECK_INTERVAL_SEC}s...")
        log.info("")
        time.sleep(CHECK_INTERVAL_SEC)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.info("trader.py stopped.")
