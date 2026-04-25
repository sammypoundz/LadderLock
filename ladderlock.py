"""
LadderLock Bot for MT5 – Auto lot sizing based on risk per position
- All orders share the same initial stop loss (the final SL)
- Each order has its own ladder TP (TP1, TP2, TP3)
- On TP hit, all stops are raised to that level
- Press 'c' to kill bot and close all trades
"""

import time
import sys
import threading
import argparse
import random
import MetaTrader5 as mt5

# --- GLOBAL FLAG ---
stop_bot = False

# --- HELPER FUNCTIONS ---
def print_account_info():
    account_info = mt5.account_info()
    if account_info is None:
        print("❌ Could not retrieve account info. Is MT5 running and logged in?")
        return False
    print("\n" + "="*60)
    print("✅ CONNECTED TO MT5")
    print(f"   Account ID : {account_info.login}")
    print(f"   Balance    : {account_info.balance:.2f} {account_info.currency}")
    print(f"   Equity     : {account_info.equity:.2f} {account_info.currency}")
    print(f"   Server     : {account_info.server}")
    print("="*60 + "\n")
    return True

def calculate_volume_from_risk(symbol, entry_price, stop_loss_price, risk_usd, direction):
    """Calculate lot size to risk exactly 'risk_usd' dollars."""
    symbol_info = mt5.symbol_info(symbol)
    if not symbol_info:
        print(f"❌ Symbol {symbol} info not found.")
        return None
    
    tick_value = symbol_info.trade_tick_value
    tick_size = symbol_info.trade_tick_size
    
    if tick_value is None or tick_size is None or tick_value == 0 or tick_size == 0:
        print("❌ Tick value or tick size is zero. Cannot calculate volume.")
        return None
    
    if direction.upper() == "BUY":
        distance = entry_price - stop_loss_price
    else:
        distance = stop_loss_price - entry_price
    
    if distance <= 0:
        print("❌ Stop loss must be on the correct side of entry price.")
        return None
    
    ticks = distance / tick_size
    risk_per_lot = ticks * tick_value
    if risk_per_lot <= 0:
        return None
    
    volume = risk_usd / risk_per_lot
    
    volume_step = symbol_info.volume_step
    volume = round(volume / volume_step) * volume_step
    if volume < symbol_info.volume_min:
        print(f"⚠️ Calculated volume {volume:.5f} is below minimum {symbol_info.volume_min}. Using minimum.")
        volume = symbol_info.volume_min
    elif volume > symbol_info.volume_max:
        print(f"⚠️ Calculated volume {volume:.5f} exceeds maximum {symbol_info.volume_max}. Using maximum.")
        volume = symbol_info.volume_max
    else:
        volume = max(symbol_info.volume_min, min(symbol_info.volume_max, volume))
    return volume

def calculate_ladder(entry_price, total_tp, total_sl, num_orders, direction):
    """Return list of ladder TP levels (three steps). SL is not ladddered."""
    if direction.upper() == "BUY":
        tp_step = (total_tp - entry_price) / num_orders
        tp_levels = [entry_price + (i+1)*tp_step for i in range(num_orders)]
    else:
        tp_step = (entry_price - total_tp) / num_orders
        tp_levels = [entry_price - (i+1)*tp_step for i in range(num_orders)]
    return tp_levels

def send_market_order(symbol, order_type, volume, sl_price, tp_price, magic, comment, deviation=20):
    tick = mt5.symbol_info_tick(symbol)
    if not tick:
        return None
    if order_type.upper() == "BUY":
        price = tick.ask
        order_type_mt5 = mt5.ORDER_TYPE_BUY
    else:
        price = tick.bid
        order_type_mt5 = mt5.ORDER_TYPE_SELL
    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": volume,
        "type": order_type_mt5,
        "price": price,
        "sl": sl_price,
        "tp": tp_price,
        "deviation": deviation,
        "magic": magic,
        "comment": comment,
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }
    return mt5.order_send(request)

def modify_positions_sltp(symbol, positions, new_sl, new_tp):
    for pos in positions:
        request = {
            "action": mt5.TRADE_ACTION_SLTP,
            "position": pos.ticket,
            "symbol": symbol,
            "sl": new_sl,
            "tp": new_tp,
        }
        mt5.order_send(request)

def close_all_positions(symbol, positions, magic):
    for pos in positions:
        if pos.type == mt5.POSITION_TYPE_BUY:
            order_type = mt5.ORDER_TYPE_SELL
        else:
            order_type = mt5.ORDER_TYPE_BUY
        tick = mt5.symbol_info_tick(symbol)
        if not tick:
            continue
        price = tick.bid if order_type == mt5.ORDER_TYPE_SELL else tick.ask
        close_request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": pos.volume,
            "type": order_type,
            "position": pos.ticket,
            "price": price,
            "deviation": 20,
            "magic": magic,
            "comment": "LadderLock - close all",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        mt5.order_send(close_request)

def get_total_unrealized_profit(positions):
    return sum(pos.profit for pos in positions) if positions else 0.0

def print_status(positions, current_price, highest_tp_hit, symbol):
    if not positions:
        print("No open positions.")
        return
    total_profit = get_total_unrealized_profit(positions)
    currency = mt5.symbol_info(symbol).currency_profit
    print("\n" + "="*60)
    print(f"LadderLock @ {time.strftime('%H:%M:%S')}  |  Price: {current_price:.5f}")
    print(f"Ladder step (TP hit): {highest_tp_hit}/3")
    if highest_tp_hit > 0:
        print(f"   → Stop loss now at: {positions[0].sl:.5f}")
    for i, pos in enumerate(positions):
        print(f"   Position {i+1}: profit = {pos.profit:.2f} {currency}")
    print(f"TOTAL UNREALIZED PROFIT: {total_profit:.2f}")
    print("="*60)

def key_listener():
    global stop_bot
    try:
        while not stop_bot:
            if sys.stdin.read(1).lower() == 'c':
                print("\n🛑 'c' pressed – stopping bot and closing all trades...")
                stop_bot = True
                break
    except:
        pass

# --- MAIN ---
def main():
    global stop_bot

    parser = argparse.ArgumentParser(description='LadderLock Bot for MT5 – Auto lot sizing')
    parser.add_argument('--symbol', type=str, default='EURUSD', help='Trading symbol (default: EURUSD)')
    parser.add_argument('--direction', type=str, choices=['BUY', 'SELL'], required=True, help='BUY or SELL')
    parser.add_argument('--tp', type=float, required=True, help='Final take-profit price')
    parser.add_argument('--sl', type=float, required=True, help='Final stop-loss price')
    parser.add_argument('--risk', type=float, default=10.0, help='Risk per position in account currency (default: 10 USD)')
    parser.add_argument('--entry', type=float, default=None, help='Entry price (default: current market)')
    parser.add_argument('--magic', type=int, default=None, help='Magic number (default: random)')
    args = parser.parse_args()

    if args.magic is None:
        args.magic = random.randint(100000, 999999)
        print(f"🔢 Using random magic number: {args.magic}")

    print("\n" + "="*60)
    print("      LADDERLOCK BOT - Starting...")
    print("="*60)
    print(f"Symbol: {args.symbol}  Direction: {args.direction}")
    print(f"Final TP: {args.tp}  Final SL: {args.sl}")
    print(f"Risk per position: {args.risk} {mt5.account_info().currency if mt5.account_info() else 'USD'}")
    print("(Press 'c' at any time to close all trades and exit)")

    # --- Connect to MT5 ---
    if not mt5.initialize():
        print("❌ MT5 initialization failed. Is MetaTrader 5 running?")
        return
    if not print_account_info():
        mt5.shutdown()
        return

    # --- Symbol check ---
    symbol_info = mt5.symbol_info(args.symbol)
    if not symbol_info:
        print(f"❌ Symbol {args.symbol} not found.")
        mt5.shutdown()
        return
    if not symbol_info.visible:
        if not mt5.symbol_select(args.symbol, True):
            print(f"❌ Cannot select {args.symbol}.")
            mt5.shutdown()
            return

    # --- Entry price ---
    if args.entry is None:
        tick = mt5.symbol_info_tick(args.symbol)
        if not tick:
            print("❌ Cannot get current price.")
            mt5.shutdown()
            return
        entry_price = tick.ask if args.direction.upper() == "BUY" else tick.bid
    else:
        entry_price = args.entry

    # --- Calculate lot size based on risk ---
    volume = calculate_volume_from_risk(args.symbol, entry_price, args.sl, args.risk, args.direction)
    if volume is None or volume <= 0:
        print("❌ Failed to calculate lot size. Check symbol and stop loss distance.")
        mt5.shutdown()
        return
    print(f"\n⚖️ Auto-calculated volume per order: {volume:.5f} lots (risks ~{args.risk} per position)")

    # --- Ladder calculation (only TP levels) ---
    tp_levels = calculate_ladder(entry_price, args.tp, args.sl, 3, args.direction)
    print(f"\n📈 Entry price: {entry_price:.5f}")
    print(f"🎯 Ladder TPs: {[round(x,5) for x in tp_levels]}")
    print(f"🛡️ All orders share the same initial stop loss: {args.sl:.5f}")

    # --- Place three orders with same SL, different TPs ---
    order_tickets = []
    for i, tp in enumerate(tp_levels):
        comment = f"LadderLock_{i+1}"
        result = send_market_order(args.symbol, args.direction, volume,
                                   args.sl, tp,
                                   args.magic, comment)
        if result and result.retcode == mt5.TRADE_RETCODE_DONE:
            order_tickets.append(result.order)
            print(f"✅ Order {i+1} placed | Ticket: {result.order} | SL: {args.sl:.5f} | TP: {tp:.5f}")
        else:
            print(f"❌ Order {i+1} failed: {result.comment if result else 'No result'}")
            mt5.shutdown()
            return

    # --- Start key listener ---
    listener_thread = threading.Thread(target=key_listener, daemon=True)
    listener_thread.start()

    # --- Monitoring loop ---
    highest_tp_hit = 0
    print("\n🚀 LadderLock is running. Press 'c' to kill.\n")
    try:
        while not stop_bot:
            time.sleep(1)

            positions = mt5.positions_get(symbol=args.symbol)
            if not positions:
                print("📭 All positions closed. Exiting.")
                break

            positions = [p for p in positions if p.magic == args.magic]
            if not positions:
                print("📭 LadderLock positions no longer exist. Exiting.")
                break

            tick = mt5.symbol_info_tick(args.symbol)
            if not tick:
                continue
            current_price = tick.bid if args.direction.upper() == "SELL" else tick.ask

            print_status(positions, current_price, highest_tp_hit, args.symbol)

            # Check for new TP hit (only if not already at final TP)
            for i in range(highest_tp_hit, 3):
                target_tp = tp_levels[i]
                if args.direction.upper() == "BUY":
                    if current_price >= target_tp:
                        highest_tp_hit = i + 1
                        print(f"\n🔒 LadderLock: TP{i+1} hit at {current_price:.5f}! Raising all stops to {target_tp:.5f}")
                        # Raise all stops to this TP level (keep TP unchanged for now)
                        modify_positions_sltp(args.symbol, positions, target_tp, None)
                        break
                else:
                    if current_price <= target_tp:
                        highest_tp_hit = i + 1
                        print(f"\n🔒 LadderLock: TP{i+1} hit at {current_price:.5f}! Raising all stops to {target_tp:.5f}")
                        modify_positions_sltp(args.symbol, positions, target_tp, None)
                        break

            # Check if stop loss of any position is hit (pullback)
            stop_hit = False
            for pos in positions:
                if pos.sl is None:
                    continue
                if args.direction.upper() == "BUY":
                    if current_price <= pos.sl:
                        print(f"\n⚠️ Stop loss {pos.sl:.5f} hit at {current_price:.5f}. Closing all positions...")
                        stop_hit = True
                        break
                else:
                    if current_price >= pos.sl:
                        print(f"\n⚠️ Stop loss {pos.sl:.5f} hit at {current_price:.5f}. Closing all positions...")
                        stop_hit = True
                        break
            if stop_hit:
                close_all_positions(args.symbol, positions, args.magic)
                final_positions = mt5.positions_get(symbol=args.symbol, magic=args.magic)
                profit_locked = get_total_unrealized_profit(final_positions) if final_positions else 0
                print(f"💰 Total profit locked: {profit_locked:.2f}")
                break

            # If final TP3 reached, close everything immediately
            if highest_tp_hit >= 3:
                print("\n🏆 Final TP3 reached! Closing all positions immediately.")
                close_all_positions(args.symbol, positions, args.magic)
                break

    except KeyboardInterrupt:
        print("\n🛑 Manual interrupt. Closing positions...")
        positions = mt5.positions_get(symbol=args.symbol, magic=args.magic)
        if positions:
            close_all_positions(args.symbol, positions, args.magic)
        print("✅ Closed.")

    finally:
        mt5.shutdown()
        print("\n🔚 LadderLock finished.")

if __name__ == "__main__":
    main()