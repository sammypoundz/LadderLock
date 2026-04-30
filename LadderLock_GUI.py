"""
LadderLock GUI – Final corrected version (result.order, real‑time balance)
"""

import tkinter as tk
from tkinter import ttk, messagebox
import threading
import queue
import time
import random
import MetaTrader5 as mt5

# ------------------------------- Helper functions -------------------------------
def print_account_info():
    account_info = mt5.account_info()
    if account_info is None:
        return False, "❌ Could not retrieve account info."
    info = f"Account: {account_info.login}\nBalance: {account_info.balance:.2f} {account_info.currency}\nEquity: {account_info.equity:.2f} {account_info.currency}"
    return True, info

def calculate_volume_from_risk(symbol, entry_price, stop_loss_price, risk_usd, direction):
    symbol_info = mt5.symbol_info(symbol)
    if not symbol_info:
        return None, f"Symbol {symbol} not found"
    tick_value = symbol_info.trade_tick_value
    tick_size = symbol_info.trade_tick_size
    if tick_value == 0 or tick_size == 0:
        return None, "Tick value/size zero"
    if direction == "BUY":
        distance = entry_price - stop_loss_price
    else:
        distance = stop_loss_price - entry_price
    if distance <= 0:
        return None, "Stop loss on wrong side of entry"
    ticks = distance / tick_size
    risk_per_lot = ticks * tick_value
    volume = risk_usd / risk_per_lot
    volume_step = symbol_info.volume_step
    volume = round(volume / volume_step) * volume_step
    volume = max(volume, symbol_info.volume_min)
    volume = min(volume, symbol_info.volume_max)
    return volume, None

def calculate_profit_at_tp(entry_price, tp_price, volume, direction, symbol_info):
    tick_size = symbol_info.trade_tick_size
    tick_value = symbol_info.trade_tick_value
    if direction == "BUY":
        distance = tp_price - entry_price
    else:
        distance = entry_price - tp_price
    if distance <= 0:
        return 0
    ticks = distance / tick_size
    profit = ticks * tick_value * volume
    return profit

def price_for_profit(entry_price, volume, profit_usd, direction, symbol_info):
    tick_size = symbol_info.trade_tick_size
    tick_value = symbol_info.trade_tick_value
    digits = symbol_info.digits

    if tick_value == 0:
        return None

    points_needed = profit_usd / (volume * tick_value)
    price_change = points_needed * tick_size

    if direction == "BUY":
        price = entry_price + price_change
    else:
        price = entry_price - price_change

    price = round(price, digits)
    return price

def send_market_order(symbol, order_type, volume, sl_price, tp_price, magic, comment, deviation=20):
    tick = mt5.symbol_info_tick(symbol)
    if not tick:
        return None
    if order_type == "BUY":
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

def modify_position_sltp(position, symbol, new_sl):
    request = {
        "action": mt5.TRADE_ACTION_SLTP,
        "position": position.ticket,
        "symbol": symbol,
        "sl": new_sl,
        "tp": position.tp,
    }
    return mt5.order_send(request)

def close_position(position, symbol, magic):
    if position.type == mt5.POSITION_TYPE_BUY:
        order_type = mt5.ORDER_TYPE_SELL
    else:
        order_type = mt5.ORDER_TYPE_BUY
    tick = mt5.symbol_info_tick(symbol)
    if not tick:
        return None
    price = tick.bid if order_type == mt5.ORDER_TYPE_SELL else tick.ask
    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": position.volume,
        "type": order_type,
        "position": position.ticket,
        "price": price,
        "deviation": 20,
        "magic": magic,
        "comment": "LadderLock close",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }
    return mt5.order_send(request)

# ------------------------------- Bot Thread -------------------------------
class LadderLockBotThread:
    def __init__(self, symbol, direction, entry_price, sl_price, tp_price, risk_usd, magic, status_queue, app):
        self.symbol = symbol
        self.direction = direction
        self.entry_price = entry_price
        self.sl_price = sl_price
        self.tp_price = tp_price
        self.risk_usd = risk_usd
        self.magic = magic
        self.status_queue = status_queue
        self.app = app  # to refresh balance
        self.stop_flag = False
        self.step_profits = []
        self.locked_step = 0

    def log(self, msg):
        self.status_queue.put(('log', msg))

    def update_status(self, current_price, total_profit, current_stop, locked_step):
        self.status_queue.put(('status', {
            'price': current_price,
            'profit': total_profit,
            'stop': current_stop,
            'locked_step': locked_step,
            'step_profits': self.step_profits
        }))

    def run(self):
        if not mt5.terminal_info():
            self.log("❌ MT5 not connected. Please restart the app.")
            return

        ok, info = print_account_info()
        if not ok:
            self.log(info)
            return
        self.log("✅ MT5 connected")
        self.log(info)

        symbol_info = mt5.symbol_info(self.symbol)
        if not symbol_info:
            self.log(f"❌ Symbol {self.symbol} not found")
            return
        if not symbol_info.visible:
            mt5.symbol_select(self.symbol, True)

        if self.entry_price is None:
            tick = mt5.symbol_info_tick(self.symbol)
            if not tick:
                self.log("❌ Cannot get current price")
                return
            self.entry_price = tick.ask if self.direction == "BUY" else tick.bid
        self.log(f"📈 Entry: {self.entry_price:.5f}")

        if self.direction == "BUY":
            if self.tp_price <= self.entry_price or self.sl_price >= self.entry_price:
                self.log("❌ For BUY: TP > entry > SL")
                return
        else:
            if self.tp_price >= self.entry_price or self.sl_price <= self.entry_price:
                self.log("❌ For SELL: TP < entry < SL")
                return

        volume, err = calculate_volume_from_risk(self.symbol, self.entry_price, self.sl_price, self.risk_usd, self.direction)
        if volume is None:
            self.log(f"❌ {err}")
            return
        self.log(f"⚖️ Lot size: {volume:.5f} (risk ${self.risk_usd:.2f})")

        profit_at_tp = calculate_profit_at_tp(self.entry_price, self.tp_price, volume, self.direction, symbol_info)
        self.step_profits = [profit_at_tp / 3 * i for i in (1, 2, 3)]
        self.log(f"🎯 TP at {self.tp_price:.5f} → profit ${profit_at_tp:.2f}")
        self.log(f"📊 Ladder: ${self.step_profits[0]:.2f}, ${self.step_profits[1]:.2f}, close at ${self.step_profits[2]:.2f}")

        result = send_market_order(self.symbol, self.direction, volume, self.sl_price, self.tp_price, self.magic, "LadderLock", 20)
        if not result or result.retcode != mt5.TRADE_RETCODE_DONE:
            self.log(f"❌ Order failed: {result.comment if result else 'unknown'}")
            return

        # ✅ FIX: use .order (not .position)
        ticket = result.order
        self.log(f"✅ Position opened | Ticket: {ticket} | SL: {self.sl_price:.5f} | TP: {self.tp_price:.5f}")

        time.sleep(1)
        positions = mt5.positions_get(symbol=self.symbol, ticket=ticket)
        if not positions:
            self.log("❌ Position not found")
            return
        position = positions[0]

        stops_level = symbol_info.trade_stops_level * symbol_info.point
        digits = symbol_info.digits
        self.log(f"🔧 Broker: min stop distance = {stops_level:.5f} ({symbol_info.trade_stops_level} points)")

        self.log("🚀 Monitoring profit ladder...")
        while not self.stop_flag:
            time.sleep(0.5)

            # Refresh balance in GUI
            self.app.refresh_mt5_info_once()

            positions = mt5.positions_get(symbol=self.symbol, ticket=ticket)
            if not positions:
                self.log("📭 Position closed")
                break
            position = positions[0]

            tick = mt5.symbol_info_tick(self.symbol)
            if not tick:
                continue
            current_price = tick.bid if self.direction == "SELL" else tick.ask
            current_profit = position.profit

            for i in range(self.locked_step, 3):
                if current_profit >= self.step_profits[i] - 0.01:
                    if i == 2:
                        self.log(f"🏆 Final profit ${self.step_profits[i]:.2f} reached! Closing trade.")
                        close_position(position, self.symbol, self.magic)
                        self.log(f"💰 Final profit: ${current_profit:.2f}")
                        return
                    else:
                        stop_price = price_for_profit(self.entry_price, volume, self.step_profits[i], self.direction, symbol_info)
                        if stop_price is None:
                            self.log(f"⚠️ Cannot compute stop price for ${self.step_profits[i]:.2f}")
                            continue
                        stop_price = round(stop_price, digits)

                        # Min distance check
                        if self.direction == "BUY":
                            if (current_price - stop_price) < stops_level - 1e-8:
                                self.log(f"⚠️ New SL {stop_price:.5f} too close (min {stops_level:.5f})")
                                continue
                        else:
                            if (stop_price - current_price) < stops_level - 1e-8:
                                self.log(f"⚠️ New SL {stop_price:.5f} too close (min {stops_level:.5f})")
                                continue

                        if self.direction == "BUY" and stop_price >= current_price:
                            self.log(f"⚠️ Invalid SL for BUY: {stop_price:.5f} >= {current_price:.5f}")
                            continue
                        if self.direction == "SELL" and stop_price <= current_price:
                            self.log(f"⚠️ Invalid SL for SELL: {stop_price:.5f} <= {current_price:.5f}")
                            continue

                        mod = modify_position_sltp(position, self.symbol, stop_price)
                        if mod and mod.retcode == mt5.TRADE_RETCODE_DONE:
                            self.log(f"🔒 Locked ${self.step_profits[i]:.2f} → SL moved to {stop_price:.5f}")
                            self.locked_step = i + 1
                            self.update_status(current_price, current_profit, stop_price, self.locked_step)
                        else:
                            if mod:
                                self.log(f"❌ SL move failed: {mod.retcode} | {mod.comment}")
                            else:
                                self.log(f"❌ SL move failed: No response | MT5 error: {mt5.last_error()}")
                        break

            if position.sl is not None:
                if self.direction == "BUY" and current_price <= position.sl:
                    self.log(f"⚠️ Stop hit at {current_price:.5f}, profit: ${current_profit:.2f}")
                    close_position(position, self.symbol, self.magic)
                    break
                elif self.direction == "SELL" and current_price >= position.sl:
                    self.log(f"⚠️ Stop hit at {current_price:.5f}, profit: ${current_profit:.2f}")
                    close_position(position, self.symbol, self.magic)
                    break

            self.update_status(current_price, current_profit, position.sl or self.sl_price, self.locked_step)

        self.log("🔚 Bot finished.")

# ------------------------------- GUI -------------------------------
class LadderLockApp:
    def __init__(self, root):
        self.root = root
        self.root.title("LadderLock – TP/SL inputs, risk‑based lot size")
        self.root.geometry("1000x750")
        self.status_queue = queue.Queue()
        self.bot = None
        self.bot_thread = None
        self.create_widgets()
        self.update_from_queue()
        self.auto_connect()

    def create_widgets(self):
        main = ttk.Frame(self.root, padding="10")
        main.pack(fill=tk.BOTH, expand=True)

        conn_frame = ttk.LabelFrame(main, text="CONNECTION", padding=5)
        conn_frame.grid(row=0, column=0, sticky="nsew", padx=5, pady=5)
        self.conn_status = ttk.Label(conn_frame, text="⏳ Connecting...", foreground="orange")
        self.conn_status.pack(anchor=tk.W)
        self.account_label = ttk.Label(conn_frame, text="Account: --")
        self.account_label.pack(anchor=tk.W)
        self.balance_label = ttk.Label(conn_frame, text="Balance: --")
        self.balance_label.pack(anchor=tk.W)

        trade_frame = ttk.LabelFrame(main, text="TRADE PARAMETERS", padding=5)
        trade_frame.grid(row=0, column=1, sticky="nsew", padx=5, pady=5)
        ttk.Label(trade_frame, text="Symbol:").grid(row=0, column=0, sticky=tk.W)
        self.symbol_var = tk.StringVar(value="XAUUSDm")
        ttk.Entry(trade_frame, textvariable=self.symbol_var, width=12).grid(row=0, column=1, sticky=tk.W)

        ttk.Label(trade_frame, text="Direction:").grid(row=1, column=0, sticky=tk.W)
        self.direction_var = tk.StringVar(value="BUY")
        ttk.Radiobutton(trade_frame, text="BUY", variable=self.direction_var, value="BUY").grid(row=1, column=1, sticky=tk.W)
        ttk.Radiobutton(trade_frame, text="SELL", variable=self.direction_var, value="SELL").grid(row=1, column=2, sticky=tk.W)

        ttk.Label(trade_frame, text="Entry price (optional):").grid(row=2, column=0, sticky=tk.W)
        self.entry_var = tk.StringVar()
        ttk.Entry(trade_frame, textvariable=self.entry_var, width=12).grid(row=2, column=1, sticky=tk.W)
        ttk.Label(trade_frame, text="(empty = market)").grid(row=2, column=2, sticky=tk.W)

        ttk.Label(trade_frame, text="Stop Loss (price):").grid(row=3, column=0, sticky=tk.W)
        self.sl_var = tk.StringVar()
        ttk.Entry(trade_frame, textvariable=self.sl_var, width=12).grid(row=3, column=1, sticky=tk.W)

        ttk.Label(trade_frame, text="Take Profit (price):").grid(row=4, column=0, sticky=tk.W)
        self.tp_var = tk.StringVar()
        ttk.Entry(trade_frame, textvariable=self.tp_var, width=12).grid(row=4, column=1, sticky=tk.W)

        ttk.Label(trade_frame, text="Risk amount ($):").grid(row=5, column=0, sticky=tk.W)
        self.risk_var = tk.StringVar(value="30")
        ttk.Entry(trade_frame, textvariable=self.risk_var, width=12).grid(row=5, column=1, sticky=tk.W)

        btn_frame = ttk.Frame(trade_frame)
        btn_frame.grid(row=6, column=0, columnspan=3, pady=10)
        self.start_btn = ttk.Button(btn_frame, text="START BOT", command=self.start_bot)
        self.start_btn.pack(side=tk.LEFT, padx=5)
        self.stop_btn = ttk.Button(btn_frame, text="STOP BOT", command=self.stop_bot, state=tk.DISABLED)
        self.stop_btn.pack(side=tk.LEFT, padx=5)

        ladder_frame = ttk.LabelFrame(main, text="PROFIT LADDER", padding=5)
        ladder_frame.grid(row=1, column=0, columnspan=2, sticky="ew", padx=5, pady=5)
        self.ladder_canvas = tk.Canvas(ladder_frame, height=80, bg='white')
        self.ladder_canvas.pack(fill=tk.X, expand=True)

        live_frame = ttk.LabelFrame(main, text="LIVE STATUS", padding=5)
        live_frame.grid(row=2, column=0, columnspan=2, sticky="ew", padx=5, pady=5)
        self.price_label = ttk.Label(live_frame, text="Price: --")
        self.price_label.pack(anchor=tk.W)
        self.stop_label = ttk.Label(live_frame, text="Stop loss: --")
        self.stop_label.pack(anchor=tk.W)
        self.profit_label = ttk.Label(live_frame, text="Profit: 0.00", foreground="green")
        self.profit_label.pack(anchor=tk.W)

        log_frame = ttk.LabelFrame(main, text="LOG", padding=5)
        log_frame.grid(row=3, column=0, columnspan=2, sticky="nsew", padx=5, pady=5)
        self.log_text = tk.Text(log_frame, height=12, wrap=tk.WORD)
        scroll = ttk.Scrollbar(log_frame, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scroll.set)
        self.log_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)

        main.columnconfigure(0, weight=1)
        main.columnconfigure(1, weight=2)
        main.rowconfigure(3, weight=1)

    def auto_connect(self):
        def connect():
            if not mt5.initialize():
                self.root.after(0, lambda: self.conn_status.config(text="❌ MT5 init failed", foreground="red"))
                return
            ok, info = print_account_info()
            if ok:
                self.root.after(0, lambda: self.update_conn(True, info))
                self.refresh_mt5_info()
            else:
                self.root.after(0, lambda: self.update_conn(False, info))
        threading.Thread(target=connect, daemon=True).start()

    def update_conn(self, ok, info):
        if ok:
            self.conn_status.config(text="✅ Connected", foreground="green")
            for line in info.split('\n'):
                if "Account:" in line:
                    self.account_label.config(text=line)
                elif "Balance:" in line:
                    self.balance_label.config(text=line)
        else:
            self.conn_status.config(text="❌ Not connected", foreground="red")
            self.log_text.insert(tk.END, f"[Connect] {info}\n")

    def refresh_mt5_info(self):
        """Periodic refresh (every 2 seconds)"""
        self.refresh_mt5_info_once()
        self.root.after(2000, self.refresh_mt5_info)

    def refresh_mt5_info_once(self):
        """Called from bot thread to update balance in real time."""
        try:
            if mt5.terminal_info():
                acc = mt5.account_info()
                if acc:
                    self.account_label.config(text=f"Account: {acc.login}")
                    self.balance_label.config(text=f"Balance: {acc.balance:.2f} {acc.currency}")
                    self.conn_status.config(text="✅ Connected", foreground="green")
                else:
                    self.conn_status.config(text="⚠️ Not logged in", foreground="orange")
            else:
                self.conn_status.config(text="❌ MT5 not running", foreground="red")
        except:
            pass

    def update_ladder(self, locked_step, step_profits):
        self.ladder_canvas.delete("all")
        w = self.ladder_canvas.winfo_width()
        if w < 10:
            w = 400
        step_w = w // 3
        for i in range(3):
            x0 = i * step_w
            x1 = (i+1) * step_w - 2
            color = "lightgreen" if locked_step > i else "lightgray"
            self.ladder_canvas.create_rectangle(x0, 10, x1, 70, fill=color, outline="black")
            self.ladder_canvas.create_text((x0+x1)//2, 40, text=f"${step_profits[i]:.0f}", font=('Arial', 10, 'bold'))
            if locked_step > i:
                self.ladder_canvas.create_text((x0+x1)//2, 65, text="LOCKED", fill="darkgreen", font=('Arial', 8))

    def update_from_queue(self):
        try:
            while True:
                typ, data = self.status_queue.get_nowait()
                if typ == 'log':
                    self.log_text.insert(tk.END, f"[{time.strftime('%H:%M:%S')}] {data}\n")
                    self.log_text.see(tk.END)
                elif typ == 'status':
                    self.price_label.config(text=f"Price: {data['price']:.5f}")
                    self.stop_label.config(text=f"Stop loss: {data['stop']:.5f}")
                    profit = data['profit']
                    self.profit_label.config(text=f"Profit: {profit:.2f}", foreground="green" if profit >= 0 else "red")
                    self.update_ladder(data['locked_step'], data['step_profits'])
        except queue.Empty:
            pass
        self.root.after(200, self.update_from_queue)

    def start_bot(self):
        try:
            symbol = self.symbol_var.get().strip()
            direction = self.direction_var.get()
            sl = float(self.sl_var.get())
            tp = float(self.tp_var.get())
            risk = float(self.risk_var.get())
            entry = float(self.entry_var.get()) if self.entry_var.get().strip() else None
        except ValueError as e:
            messagebox.showerror("Input error", f"Invalid number: {e}")
            return

        if risk <= 0:
            messagebox.showerror("Risk", "Risk must be > 0")
            return

        if not mt5.terminal_info():
            if not mt5.initialize():
                messagebox.showerror("MT5", "Cannot connect to MT5")
                return
        if not mt5.account_info():
            messagebox.showerror("MT5", "No account logged in")
            return

        self.start_btn.config(state=tk.DISABLED)
        self.stop_btn.config(state=tk.NORMAL)
        self.log_text.delete(1.0, tk.END)

        magic = random.randint(100000, 999999)
        self.bot = LadderLockBotThread(symbol, direction, entry, sl, tp, risk, magic, self.status_queue, self)
        self.bot_thread = threading.Thread(target=self.bot.run, daemon=True)
        self.bot_thread.start()

    def stop_bot(self):
        if self.bot:
            self.bot.stop_flag = True
        self.start_btn.config(state=tk.NORMAL)
        self.stop_btn.config(state=tk.DISABLED)

if __name__ == "__main__":
    root = tk.Tk()
    app = LadderLockApp(root)
    root.mainloop()