"""
LadderLock GUI – Exact replication of the command‑line logic
- Auto‑connects to MT5 when the app starts
- Places three orders with same SL, laddered TPs (TP1, TP2, TP3)
- On TP hit, raises all stops to that TP level
- On final TP3, closes all positions
- On pullback to stop, closes all positions
- Default symbol: XAUUSDm
"""

import tkinter as tk
from tkinter import ttk, messagebox
import threading
import queue
import time
import random
import MetaTrader5 as mt5

# -------------------------------
# Helper: parse price (remove commas)
# -------------------------------
def parse_price(price_str):
    return float(price_str.replace(',', '').strip())

# -------------------------------
# LadderLock Bot Logic (exact copy from command‑line, adapted for GUI)
# -------------------------------
class LadderLockBot:
    def __init__(self, symbol, direction, tp_price, sl_price, risk_usd, entry_price, magic, status_queue):
        self.symbol = symbol
        self.direction = direction.upper()
        self.tp_price = tp_price
        self.sl_price = sl_price
        self.risk_usd = risk_usd
        self.entry_price = entry_price
        self.magic = magic
        self.status_queue = status_queue
        self.stop_flag = False
        self.positions = []
        self.highest_tp_hit = 0
        self.tp_levels = []
        self.volume = None

    def log(self, msg):
        self.status_queue.put(('log', msg))

    def update_status(self, current_price, total_profit, stop_level=None, highest_tp=None):
        self.status_queue.put(('status', {
            'price': current_price,
            'profit': total_profit,
            'stop': stop_level if stop_level is not None else self.sl_price,
            'tp_hit': self.highest_tp_hit if highest_tp is None else highest_tp
        }))

    def calculate_volume(self):
        symbol_info = mt5.symbol_info(self.symbol)
        if not symbol_info:
            self.log(f"❌ Symbol {self.symbol} not found")
            return None
        tick_value = symbol_info.trade_tick_value
        tick_size = symbol_info.trade_tick_size
        if not tick_value or not tick_size:
            self.log("❌ Tick value or tick size is zero")
            return None
        if self.direction == "BUY":
            distance = self.entry_price - self.sl_price
        else:
            distance = self.sl_price - self.entry_price
        if distance <= 0:
            self.log("❌ Stop loss must be on correct side of entry")
            return None
        ticks = distance / tick_size
        risk_per_lot = ticks * tick_value
        if risk_per_lot <= 0:
            return None
        volume = self.risk_usd / risk_per_lot
        volume_step = symbol_info.volume_step
        volume = round(volume / volume_step) * volume_step
        volume = max(symbol_info.volume_min, min(symbol_info.volume_max, volume))
        if volume < symbol_info.volume_min:
            self.log(f"⚠️ Calculated volume {volume:.5f} below min {symbol_info.volume_min}. Using min.")
            volume = symbol_info.volume_min
        elif volume > symbol_info.volume_max:
            self.log(f"⚠️ Calculated volume {volume:.5f} above max {symbol_info.volume_max}. Using max.")
            volume = symbol_info.volume_max
        return volume

    def calculate_ladder(self):
        num_orders = 3
        if self.direction == "BUY":
            step = (self.tp_price - self.entry_price) / num_orders
            return [self.entry_price + (i+1)*step for i in range(num_orders)]
        else:
            step = (self.entry_price - self.tp_price) / num_orders
            return [self.entry_price - (i+1)*step for i in range(num_orders)]

    def send_market_order(self, tp):
        tick = mt5.symbol_info_tick(self.symbol)
        if not tick:
            return None
        if self.direction == "BUY":
            price = tick.ask
            order_type = mt5.ORDER_TYPE_BUY
        else:
            price = tick.bid
            order_type = mt5.ORDER_TYPE_SELL
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": self.symbol,
            "volume": self.volume,
            "type": order_type,
            "price": price,
            "sl": self.sl_price,
            "tp": tp,
            "deviation": 20,
            "magic": self.magic,
            "comment": "LadderLock",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        return mt5.order_send(request)

    def modify_stops(self, new_sl):
        for pos in self.positions:
            request = {
                "action": mt5.TRADE_ACTION_SLTP,
                "position": pos.ticket,
                "symbol": self.symbol,
                "sl": new_sl,
                "tp": None,
            }
            mt5.order_send(request)

    def close_all(self):
        positions = mt5.positions_get(symbol=self.symbol, magic=self.magic)
        if not positions:
            return
        self.log(f"🔒 Closing {len(positions)} position(s)...")
        for pos in positions:
            if pos.type == mt5.POSITION_TYPE_BUY:
                order_type = mt5.ORDER_TYPE_SELL
            else:
                order_type = mt5.ORDER_TYPE_BUY
            tick = mt5.symbol_info_tick(self.symbol)
            if not tick:
                continue
            price = tick.bid if order_type == mt5.ORDER_TYPE_SELL else tick.ask
            close_request = {
                "action": mt5.TRADE_ACTION_DEAL,
                "symbol": self.symbol,
                "volume": pos.volume,
                "type": order_type,
                "position": pos.ticket,
                "price": price,
                "deviation": 20,
                "magic": self.magic,
                "comment": "LadderLock close",
                "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": mt5.ORDER_FILLING_IOC,
            }
            mt5.order_send(close_request)
        self.log("✅ All positions closed.")

    def run(self):
        # Connect to MT5 if not already connected (should be already from auto_connect)
        if not mt5.terminal_info():
            if not mt5.initialize():
                self.log("❌ MT5 initialization failed. Is MetaTrader 5 running?")
                return
        self.log("✅ Connected to MT5")

        # Symbol check
        symbol_info = mt5.symbol_info(self.symbol)
        if not symbol_info:
            self.log(f"❌ Symbol {self.symbol} not found.")
            return
        if not symbol_info.visible:
            if not mt5.symbol_select(self.symbol, True):
                self.log(f"❌ Cannot select {self.symbol}.")
                return
        self.log(f"✅ Symbol {self.symbol} selected")

        # Entry price
        if self.entry_price is None:
            tick = mt5.symbol_info_tick(self.symbol)
            if not tick:
                self.log("❌ Cannot get current price.")
                return
            self.entry_price = tick.ask if self.direction == "BUY" else tick.bid
        self.log(f"📈 Entry price: {self.entry_price:.5f}")

        # Volume
        self.volume = self.calculate_volume()
        if self.volume is None or self.volume <= 0:
            self.log("❌ Failed to calculate lot size.")
            return
        self.log(f"⚖️ Auto-calculated volume per order: {self.volume:.5f} lots (risks ~{self.risk_usd} per position)")

        # Ladder TPs
        self.tp_levels = self.calculate_ladder()
        self.log(f"🎯 Ladder TPs: {[round(x,5) for x in self.tp_levels]}")
        self.log(f"🛡️ All orders share the same initial stop loss: {self.sl_price:.5f}")

        # Place three orders
        tickets = []
        for i, tp in enumerate(self.tp_levels):
            res = self.send_market_order(tp)
            if res and res.retcode == mt5.TRADE_RETCODE_DONE:
                tickets.append(res.order)
                self.log(f"✅ Order {i+1} placed | Ticket {res.order} | SL {self.sl_price:.5f} | TP {tp:.5f}")
            else:
                err = res.comment if res else 'no result'
                self.log(f"❌ Order {i+1} failed: {err}")
                mt5.shutdown()
                return

        # Get positions
        self.positions = mt5.positions_get(symbol=self.symbol, magic=self.magic)
        if not self.positions:
            self.log("❌ No positions found after placing orders")
            return
        self.log("🚀 LadderLock is now running")

        # Monitoring loop
        while not self.stop_flag:
            time.sleep(1)

            self.positions = mt5.positions_get(symbol=self.symbol, magic=self.magic)
            if not self.positions:
                self.log("📭 All positions closed. Exiting.")
                break

            tick = mt5.symbol_info_tick(self.symbol)
            if not tick:
                continue
            current_price = tick.bid if self.direction == "SELL" else tick.ask
            total_profit = sum(p.profit for p in self.positions)

            # Check TP hits
            for i in range(self.highest_tp_hit, 3):
                target = self.tp_levels[i]
                if self.direction == "BUY":
                    if current_price >= target:
                        self.highest_tp_hit = i + 1
                        self.log(f"🔒 TP{i+1} hit at {current_price:.5f}! Raising stops to {target:.5f}")
                        self.modify_stops(target)
                        self.update_status(current_price, total_profit, stop_level=target, highest_tp=self.highest_tp_hit)
                        break
                else:
                    if current_price <= target:
                        self.highest_tp_hit = i + 1
                        self.log(f"🔒 TP{i+1} hit at {current_price:.5f}! Raising stops to {target:.5f}")
                        self.modify_stops(target)
                        self.update_status(current_price, total_profit, stop_level=target, highest_tp=self.highest_tp_hit)
                        break

            # Check stop loss hit
            stop_hit = False
            for pos in self.positions:
                if pos.sl is None:
                    continue
                if self.direction == "BUY":
                    if current_price <= pos.sl:
                        self.log(f"⚠️ Stop loss {pos.sl:.5f} hit at {current_price:.5f}. Closing all positions.")
                        stop_hit = True
                        break
                else:
                    if current_price >= pos.sl:
                        self.log(f"⚠️ Stop loss {pos.sl:.5f} hit at {current_price:.5f}. Closing all positions.")
                        stop_hit = True
                        break
            if stop_hit:
                self.close_all()
                final_positions = mt5.positions_get(symbol=self.symbol, magic=self.magic)
                profit_locked = sum(p.profit for p in final_positions) if final_positions else total_profit
                self.log(f"💰 Total profit locked: {profit_locked:.2f}")
                break

            # Final TP3 reached
            if self.highest_tp_hit >= 3:
                self.log("🏆 Final TP3 reached! Closing all positions immediately.")
                self.close_all()
                break

            # Update GUI status
            current_stop = self.positions[0].sl if self.positions and self.positions[0].sl else self.sl_price
            self.update_status(current_price, total_profit, stop_level=current_stop)

        self.log("🔚 LadderLock finished.")
        mt5.shutdown()

# -------------------------------
# GUI Application (with auto-connect on launch)
# -------------------------------
class LadderLockApp:
    def __init__(self, root):
        self.root = root
        self.root.title("LadderLock Bot")
        self.root.geometry("950x700")
        self.root.resizable(True, True)

        self.bot_thread = None
        self.bot = None
        self.status_queue = queue.Queue()

        self.create_widgets()
        self.update_from_queue()
        # Auto‑connect to MT5 when the app starts
        self.auto_connect()

    def create_widgets(self):
        main_frame = ttk.Frame(self.root, padding="10")
        main_frame.pack(fill=tk.BOTH, expand=True)

        # Left: Connection info
        left_frame = ttk.LabelFrame(main_frame, text="CONNECTION", padding="5")
        left_frame.grid(row=0, column=0, sticky="nsew", padx=5, pady=5)

        self.conn_status = ttk.Label(left_frame, text="⏳ Connecting...", foreground="orange")
        self.conn_status.pack(anchor=tk.W, pady=2)
        self.account_label = ttk.Label(left_frame, text="Account: --")
        self.account_label.pack(anchor=tk.W, pady=2)
        self.balance_label = ttk.Label(left_frame, text="Balance: --")
        self.balance_label.pack(anchor=tk.W, pady=2)
        self.equity_label = ttk.Label(left_frame, text="Equity: --")
        self.equity_label.pack(anchor=tk.W, pady=2)

        # Right: Trade parameters
        right_frame = ttk.LabelFrame(main_frame, text="TRADE PARAMETERS", padding="5")
        right_frame.grid(row=0, column=1, sticky="nsew", padx=5, pady=5)

        ttk.Label(right_frame, text="Symbol:").grid(row=0, column=0, sticky=tk.W, pady=2)
        self.symbol_var = tk.StringVar(value="XAUUSDm")
        self.symbol_entry = ttk.Entry(right_frame, textvariable=self.symbol_var, width=15)
        self.symbol_entry.grid(row=0, column=1, sticky=tk.W, pady=2)

        ttk.Label(right_frame, text="Direction:").grid(row=1, column=0, sticky=tk.W, pady=2)
        self.direction_var = tk.StringVar(value="BUY")
        ttk.Radiobutton(right_frame, text="BUY", variable=self.direction_var, value="BUY").grid(row=1, column=1, sticky=tk.W)
        ttk.Radiobutton(right_frame, text="SELL", variable=self.direction_var, value="SELL").grid(row=1, column=2, sticky=tk.W)

        ttk.Label(right_frame, text="Final TP (price):").grid(row=2, column=0, sticky=tk.W, pady=2)
        self.tp_var = tk.StringVar()
        self.tp_entry = ttk.Entry(right_frame, textvariable=self.tp_var, width=15)
        self.tp_entry.grid(row=2, column=1, sticky=tk.W, pady=2)

        ttk.Label(right_frame, text="Final SL (price):").grid(row=3, column=0, sticky=tk.W, pady=2)
        self.sl_var = tk.StringVar()
        self.sl_entry = ttk.Entry(right_frame, textvariable=self.sl_var, width=15)
        self.sl_entry.grid(row=3, column=1, sticky=tk.W, pady=2)

        ttk.Label(right_frame, text="Risk per position ($):").grid(row=4, column=0, sticky=tk.W, pady=2)
        self.risk_var = tk.StringVar(value="10.0")
        self.risk_entry = ttk.Entry(right_frame, textvariable=self.risk_var, width=15)
        self.risk_entry.grid(row=4, column=1, sticky=tk.W, pady=2)

        ttk.Label(right_frame, text="Entry price (optional):").grid(row=5, column=0, sticky=tk.W, pady=2)
        self.entry_var = tk.StringVar()
        self.entry_entry = ttk.Entry(right_frame, textvariable=self.entry_var, width=15)
        self.entry_entry.grid(row=5, column=1, sticky=tk.W, pady=2)
        ttk.Label(right_frame, text="(leave empty = market)").grid(row=5, column=2, sticky=tk.W)

        btn_frame = ttk.Frame(right_frame)
        btn_frame.grid(row=6, column=0, columnspan=3, pady=10)
        self.start_btn = ttk.Button(btn_frame, text="START BOT", command=self.start_bot)
        self.start_btn.pack(side=tk.LEFT, padx=5)
        self.stop_btn = ttk.Button(btn_frame, text="STOP BOT", command=self.stop_bot, state=tk.DISABLED)
        self.stop_btn.pack(side=tk.LEFT, padx=5)

        # Live status
        live_frame = ttk.LabelFrame(main_frame, text="LIVE STATUS", padding="5")
        live_frame.grid(row=1, column=0, columnspan=2, sticky="nsew", padx=5, pady=5)

        self.price_label = ttk.Label(live_frame, text="Current Price: --", font=('Arial', 10, 'bold'))
        self.price_label.pack(anchor=tk.W, pady=2)
        self.tp_hit_label = ttk.Label(live_frame, text="Highest TP hit: 0/3")
        self.tp_hit_label.pack(anchor=tk.W, pady=2)
        self.stop_label = ttk.Label(live_frame, text="Stop loss now: --")
        self.stop_label.pack(anchor=tk.W, pady=2)
        self.profit_label = ttk.Label(live_frame, text="Total Profit: 0.00", foreground="green")
        self.profit_label.pack(anchor=tk.W, pady=2)

        # Log panel
        log_frame = ttk.LabelFrame(main_frame, text="LOG / EVENTS", padding="5")
        log_frame.grid(row=2, column=0, columnspan=2, sticky="nsew", padx=5, pady=5)

        self.log_text = tk.Text(log_frame, height=12, wrap=tk.WORD, bg="white", fg="black")
        scroll = ttk.Scrollbar(log_frame, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scroll.set)
        self.log_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)

        main_frame.columnconfigure(0, weight=1)
        main_frame.columnconfigure(1, weight=2)
        main_frame.rowconfigure(0, weight=0)
        main_frame.rowconfigure(1, weight=1)
        main_frame.rowconfigure(2, weight=2)

    def auto_connect(self):
        """Background thread: initialise MT5 and update connection panel."""
        def connect_task():
            if not mt5.initialize():
                self.root.after(0, lambda: self.update_connection_display(False, None))
                self.root.after(0, lambda: self.log_text.insert(tk.END, "[Auto‑connect] ❌ MT5 initialization failed. Is MetaTrader 5 running?\n"))
                return
            acc = mt5.account_info()
            if acc:
                self.root.after(0, lambda: self.update_connection_display(True, acc))
                self.root.after(0, lambda: self.log_text.insert(tk.END, f"[Auto‑connect] ✅ Connected to account {acc.login} (balance {acc.balance:.2f} {acc.currency})\n"))
                # Start periodic refresh
                self.root.after(2000, self.refresh_mt5_info)
            else:
                self.root.after(0, lambda: self.update_connection_display(False, None))
                self.root.after(0, lambda: self.log_text.insert(tk.END, "[Auto‑connect] ⚠️ MT5 is running but no account is logged in.\n"))
        threading.Thread(target=connect_task, daemon=True).start()

    def update_connection_display(self, connected, account_info):
        if connected and account_info:
            self.conn_status.config(text="✅ Connected", foreground="green")
            self.account_label.config(text=f"Account: {account_info.login}")
            self.balance_label.config(text=f"Balance: {account_info.balance:.2f} {account_info.currency}")
            self.equity_label.config(text=f"Equity: {account_info.equity:.2f} {account_info.currency}")
        else:
            self.conn_status.config(text="❌ Not connected", foreground="red")
            self.account_label.config(text="Account: --")
            self.balance_label.config(text="Balance: --")
            self.equity_label.config(text="Equity: --")

    def refresh_mt5_info(self):
        """Periodic update of connection info (balance/equity)."""
        try:
            if mt5.terminal_info():
                acc = mt5.account_info()
                if acc:
                    self.conn_status.config(text="✅ Connected", foreground="green")
                    self.account_label.config(text=f"Account: {acc.login}")
                    self.balance_label.config(text=f"Balance: {acc.balance:.2f} {acc.currency}")
                    self.equity_label.config(text=f"Equity: {acc.equity:.2f} {acc.currency}")
                else:
                    self.conn_status.config(text="⚠️ Not logged in", foreground="orange")
            else:
                self.conn_status.config(text="❌ MT5 not running", foreground="red")
        except:
            pass
        self.root.after(2000, self.refresh_mt5_info)

    def update_from_queue(self):
        try:
            while True:
                msg_type, data = self.status_queue.get_nowait()
                if msg_type == 'log':
                    self.log_text.insert(tk.END, f"[{time.strftime('%H:%M:%S')}] {data}\n")
                    self.log_text.see(tk.END)
                elif msg_type == 'status':
                    self.price_label.config(text=f"Current Price: {data['price']:.5f}")
                    self.tp_hit_label.config(text=f"Highest TP hit: {data['tp_hit']}/3")
                    self.stop_label.config(text=f"Stop loss now: {data['stop']:.5f}")
                    profit = data['profit']
                    color = "green" if profit >= 0 else "red"
                    self.profit_label.config(text=f"Total Profit: {profit:.2f}", foreground=color)
        except queue.Empty:
            pass
        self.root.after(200, self.update_from_queue)

    def start_bot(self):
        symbol = self.symbol_var.get().strip()
        direction = self.direction_var.get()
        try:
            tp = parse_price(self.tp_var.get())
            sl = parse_price(self.sl_var.get())
            risk = float(self.risk_var.get())
        except ValueError as e:
            messagebox.showerror("Invalid Input", f"Please check numbers (commas allowed): {e}")
            return
        entry = None
        if self.entry_var.get().strip():
            try:
                entry = parse_price(self.entry_var.get())
            except ValueError:
                messagebox.showerror("Invalid Input", "Entry price must be a number (commas allowed)")
                return

        if risk <= 0:
            messagebox.showerror("Invalid Risk", "Risk must be positive")
            return

        # Ensure MT5 is still connected (auto_connect already initialised it)
        if not mt5.terminal_info():
            if not mt5.initialize():
                messagebox.showerror("MT5 Error", "MT5 is not running or cannot connect.")
                return
        acc = mt5.account_info()
        if not acc:
            messagebox.showerror("MT5 Error", "No account logged in. Please log into MT5 first.")
            return

        self.start_btn.config(state=tk.DISABLED)
        self.stop_btn.config(state=tk.NORMAL)
        self.log_text.delete(1.0, tk.END)

        magic = random.randint(100000, 999999)
        self.bot = LadderLockBot(symbol, direction, tp, sl, risk, entry, magic, self.status_queue)
        self.bot_thread = threading.Thread(target=self.bot.run, daemon=True)
        self.bot_thread.start()

    def stop_bot(self):
        if self.bot:
            self.bot.log("🛑 Stop command received. Closing all positions...")
            self.bot.close_all()
            self.bot.stop_flag = True
        self.start_btn.config(state=tk.NORMAL)
        self.stop_btn.config(state=tk.DISABLED)

if __name__ == "__main__":
    root = tk.Tk()
    app = LadderLockApp(root)
    root.mainloop()