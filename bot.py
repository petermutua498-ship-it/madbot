import os
import time
import requests
import pandas as pd

from dotenv import load_dotenv
from binance.client import Client
from binance.exceptions import BinanceAPIException, BinanceRequestException
from requests.exceptions import RequestException

import threading
from http.server import HTTPServer, BaseHTTPRequestHandler




# =========================================================
# CONFIGURATION
# =========================================================

load_dotenv()

API_KEY = os.getenv("BINANCE_API_KEY")
API_SECRET = os.getenv("BINANCE_API_SECRET")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = str(os.getenv("TELEGRAM_CHAT_ID", ""))

# Keep True for Binance Spot Testnet
TESTNET = True

SYMBOL = "TRXUSDT"
INTERVAL = Client.KLINE_INTERVAL_1MINUTE

TRADE_AMOUNT_USDT = 10

TAKE_PROFIT = 0.03       # 3%
STOP_LOSS = 0.02         # 2%
TRAILING_STOP = 0.015    # 1.5%

RSI_PERIOD = 2
MA_PERIOD = 20

COOLDOWN_CANDLES = 2

# Telegram periodic update settings (in seconds)
RSI_BROADCAST_INTERVAL = 300  # 5 minutes

# Light HTTP server to satisfy Render's free Web Service checks
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/html")
        self.end_headers()
        self.wfile.write(b"Bot is active!")

def start_health_check_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    server.serve_forever()

# Start dummy web server in a daemon thread
threading.Thread(target=start_health_check_server, daemon=True).start()


# =========================================================
# CHECK CONFIGURATION
# =========================================================

if not API_KEY or not API_SECRET:
    raise ValueError("Binance API keys are missing from .env")

if not TELEGRAM_TOKEN or not CHAT_ID:
    raise ValueError("Telegram settings are missing from .env")


# =========================================================
# BINANCE CLIENT INITIALIZATION WITH RETRY
# =========================================================

def init_binance_client():
    while True:
        try:
            c = Client(API_KEY, API_SECRET, testnet=TESTNET)
            c.ping()
            print("Binance connection: OK")
            return c
        except (RequestException, BinanceAPIException, Exception) as e:
            print(f"Failed to connect to Binance API ({e}). Retrying in 10s...")
            time.sleep(10)

client = init_binance_client()


# =========================================================
# BOT STATE
# =========================================================

bot_running = True

last_update_id = None
last_candle_time = None
last_rsi_broadcast = 0

in_position = False

entry_price = 0.0
quantity = 0.0
highest_price = 0.0

total_profit = 0.0
total_trades = 0
wins = 0
losses = 0

cooldown_counter = 0


# =========================================================
# SAFE NETWORK REQUEST HELPER
# =========================================================

def safe_request(method, url, retries=3, backoff_factor=2, **kwargs):
    for attempt in range(retries):
        try:
            response = requests.request(method, url, **kwargs)
            return response
        except RequestException:
            if attempt == retries - 1:
                return None
            time.sleep(backoff_factor ** attempt)


# =========================================================
# TELEGRAM FUNCTIONS
# =========================================================

def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": message}

    response = safe_request("POST", url, data=payload, timeout=15, retries=3)
    return True if response and response.ok else False


def check_telegram():
    global last_update_id
    global bot_running

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
    params = {}

    if last_update_id is not None:
        params["offset"] = last_update_id + 1

    response = safe_request("GET", url, params=params, timeout=25, retries=2)

    if not response or not response.ok:
        return

    try:
        data = response.json()
        if not data.get("ok"):
            return

        updates = data.get("result", [])

        for update in updates:
            last_update_id = update["update_id"]
            message = update.get("message")

            if not message:
                continue

            chat = message.get("chat", {})
            user_chat_id = str(chat.get("id", ""))

            if user_chat_id != CHAT_ID:
                continue

            text = message.get("text", "").strip().lower()

            if text == "/start":
                bot_running = True
                send_telegram("🟢 BOT STARTED\n\nTrading is enabled.")

            elif text == "/stop":
                bot_running = False
                send_telegram("🛑 BOT STOPPED\n\nNew trades disabled.\nOpen positions are still monitored.")

            elif text == "/status":
                send_status()

            elif text == "/balance":
                send_balance()

            elif text == "/help":
                send_telegram(
                    "🤖 Binance Bot Commands\n\n"
                    "/start - Start trading\n"
                    "/stop - Stop new trades\n"
                    "/status - Bot status & live RSI\n"
                    "/balance - Binance balance\n"
                    "/help - Show commands"
                )

    except Exception as e:
        print("Telegram processing error:", e)


# =========================================================
# BALANCE & STATUS
# =========================================================

def get_balances():
    try:
        account = client.get_account()
        balances = {}

        for item in account.get("balances", []):
            asset_name = str(item["asset"]).upper()
            free = float(item["free"])
            locked = float(item["locked"])

            if free > 0 or locked > 0:
                balances[asset_name] = {"free": free, "locked": locked}

        return balances
    except Exception as e:
        print("Error fetching account balance:", e)
        return {}


def send_balance():
    try:
        balances = get_balances()

        usdt = balances.get("USDT", {"free": 0.0, "locked": 0.0})
        trx = balances.get("TRX", {"free": 0.0, "locked": 0.0})

        ticker = client.get_symbol_ticker(symbol=SYMBOL)
        price = float(ticker["price"])

        trx_value = trx["free"] * price
        total_usdt = usdt["free"] + trx_value

        message = (
            "💰 BINANCE BALANCE\n\n"
            f"USDT Free: {usdt['free']:.4f}\n"
            f"USDT Locked: {usdt['locked']:.4f}\n\n"
            f"TRX Free: {trx['free']:.4f}\n"
            f"TRX Locked: {trx['locked']:.4f}\n\n"
            f"TRX Price: {price:.6f} USDT\n"
            f"Estimated Total: {total_usdt:.4f} USDT"
        )

        send_telegram(message)

    except Exception as e:
        send_telegram(f"❌ Could not get balance.\n\n{e}")


def send_status():
    try:
        status = "🟢 RUNNING" if bot_running else "🔴 STOPPED"

        win_rate = (
            (wins / total_trades) * 100
            if total_trades > 0
            else 0
        )

        df = get_data()
        current_rsi = "N/A"
        current_price = "N/A"

        if df is not None and len(df) >= MA_PERIOD + 5:
            df = calculate_indicators(df)
            current_rsi = f"{df.iloc[-2]['rsi']:.2f}"
            current_price = f"{df.iloc[-2]['close']:.6f}"

        message = (
            "📊 BOT STATUS\n\n"
            f"Status: {status}\n"
            f"Symbol: {SYMBOL}\n"
            f"Mode: {'TESTNET' if TESTNET else 'LIVE'}\n"
            f"Live Price: {current_price}\n"
            f"Current RSI: {current_rsi}\n\n"
            f"In Position: {'YES' if in_position else 'NO'}\n"
        )

        if in_position:
            price = float(client.get_symbol_ticker(symbol=SYMBOL)["price"])
            unrealized = (price - entry_price) * quantity

            message += (
                f"Entry: {entry_price:.6f}\n"
                f"Current: {price:.6f}\n"
                f"Quantity: {quantity:.4f}\n"
                f"Unrealized P/L: {unrealized:.4f} USDT\n\n"
            )

        message += (
            f"Realized Profit: {total_profit:.4f} USDT\n"
            f"Trades: {total_trades}\n"
            f"Wins: {wins}\n"
            f"Losses: {losses}\n"
            f"Win Rate: {win_rate:.2f}%"
        )

        send_telegram(message)

    except Exception as e:
        send_telegram(f"❌ Status error:\n{e}")


# =========================================================
# DATA & SMOOTHED RSI INDICATOR
# =========================================================

def get_data():
    retries = 3
    for attempt in range(retries):
        try:
            klines = client.get_klines(
                symbol=SYMBOL,
                interval=INTERVAL,
                limit=100  # Pulls 100 candles for stable exponential RSI
            )

            df = pd.DataFrame(
                klines,
                columns=[
                    "time", "open", "high", "low", "close", "volume",
                    "close_time", "quote_volume", "trades",
                    "base_volume", "quote_volume2", "ignore"
                ]
            )

            df["close"] = df["close"].astype(float)
            return df

        except (BinanceAPIException, BinanceRequestException, RequestException):
            if attempt < retries - 1:
                time.sleep(2)
            else:
                return None
        except Exception as e:
            print("Unexpected data fetch error:", e)
            return None


def calculate_indicators(df):
    delta = df["close"].diff()

    gain = delta.where(delta > 0, 0.0)
    loss = -delta.where(delta < 0, 0.0)

    # Exponential Moving Average for Wilder's Smoothed RSI
    avg_gain = gain.ewm(alpha=1/RSI_PERIOD, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/RSI_PERIOD, adjust=False).mean()

    rs = avg_gain / avg_loss
    df["rsi"] = 100 - (100 / (1 + rs))

    df["ma"] = df["close"].rolling(MA_PERIOD).mean()

    return df


# =========================================================
# ORDER SIZING & PRECISION FIX
# =========================================================

def get_quantity():
    try:
        price = float(client.get_symbol_ticker(symbol=SYMBOL)["price"])
        raw_quantity = TRADE_AMOUNT_USDT / price

        symbol_info = client.get_symbol_info(SYMBOL)
        if not symbol_info:
            return 0

        lot_filter = None
        notional_filter = None

        for f in symbol_info["filters"]:
            if f["filterType"] == "LOT_SIZE":
                lot_filter = f
            elif f["filterType"] in ["NOTIONAL", "MIN_NOTIONAL"]:
                notional_filter = f

        if not lot_filter:
            return 0

        step_size = float(lot_filter["stepSize"])
        min_qty = float(lot_filter["minQty"])

        # Precision calculation
        step_str = str(lot_filter["stepSize"]).rstrip('0')
        precision = len(step_str.split('.')[1]) if '.' in step_str else 0

        # Calculate step-aligned quantity
        quantity = round(raw_quantity - (raw_quantity % step_size), precision)

        # Check minimum lot size quantity
        if quantity < min_qty:
            print(f"Quantity {quantity} below minimum {min_qty}")
            return 0

        # Check minimum notional value (Price * Quantity >= Min Notional)
        if notional_filter:
            min_notional = float(notional_filter.get("minNotional", 10.0))
            if (quantity * price) < min_notional:
                print(f"Order value ({quantity * price:.2f} USDT) below minimum notional ({min_notional} USDT)")
                send_telegram(f"⚠️ Trade cancelled: Order amount must be at least {min_notional} USDT.")
                return 0

        return quantity

    except Exception as e:
        print("Quantity error:", e)
        return 0

def get_average_fill_price(order):
    fills = order.get("fills", [])

    if fills:
        total_quantity = 0
        total_value = 0
        for fill in fills:
            qty = float(fill["qty"])
            price = float(fill["price"])
            total_quantity += qty
            total_value += qty * price

        if total_quantity > 0:
            return total_value / total_quantity

    executed_qty = float(order.get("executedQty", 0))
    quote_qty = float(order.get("cummulativeQuoteQty", 0))

    if executed_qty > 0:
        return quote_qty / executed_qty

    return 0


# =========================================================
# EXECUTION LOGIC
# =========================================================

def execute_buy():
    global in_position, entry_price, quantity, highest_price

    try:
        quantity_to_buy = get_quantity()

        if quantity_to_buy <= 0:
            send_telegram("❌ BUY cancelled.\nQuantity is below minimum or balance is insufficient.")
            return

        order = client.order_market_buy(
            symbol=SYMBOL,
            quantity=quantity_to_buy
        )

        actual_price = get_average_fill_price(order)
        executed_quantity = float(order.get("executedQty", quantity_to_buy))

        if actual_price <= 0:
            return

        entry_price = actual_price
        quantity = executed_quantity
        highest_price = actual_price
        in_position = True

        send_telegram(
            "🟢 BUY EXECUTED\n\n"
            f"Symbol: {SYMBOL}\n"
            f"Price: {actual_price:.6f}\n"
            f"Quantity: {executed_quantity:.4f}\n\n"
            f"TP: {actual_price * (1 + TAKE_PROFIT):.6f}\n"
            f"SL: {actual_price * (1 - STOP_LOSS):.6f}"
        )

    except Exception as e:
        send_telegram(f"❌ BUY failed:\n{e}")


def execute_sell(reason):
    global in_position, entry_price, quantity, highest_price
    global total_profit, total_trades, wins, losses, cooldown_counter

    try:
        if quantity <= 0:
            return

        order = client.order_market_sell(
            symbol=SYMBOL,
            quantity=quantity
        )

        sell_price = get_average_fill_price(order)
        executed_quantity = float(order.get("executedQty", quantity))

        if sell_price <= 0:
            return

        profit = (sell_price - entry_price) * executed_quantity
        total_profit += profit
        total_trades += 1

        if profit > 0:
            wins += 1
        else:
            losses += 1

        emoji = "🟢" if profit >= 0 else "🔴"

        send_telegram(
            f"{emoji} SELL EXECUTED\n\n"
            f"Reason: {reason}\n"
            f"Price: {sell_price:.6f}\n"
            f"Quantity: {executed_quantity:.4f}\n\n"
            f"Gross Profit: {profit:.4f} USDT\n"
            f"Total Profit: {total_profit:.4f} USDT"
        )

        in_position = False
        entry_price = 0
        quantity = 0
        highest_price = 0
        cooldown_counter = COOLDOWN_CANDLES

    except Exception as e:
        send_telegram(f"❌ SELL error:\n{e}")


# =========================================================
# POSITION & STRATEGY MONITORING
# =========================================================

def manage_position(price):
    global highest_price

    if not in_position:
        return

    if price > highest_price:
        highest_price = price

    take_profit_price = entry_price * (1 + TAKE_PROFIT)
    if price >= take_profit_price:
        execute_sell("TAKE PROFIT")
        return

    stop_loss_price = entry_price * (1 - STOP_LOSS)
    if price <= stop_loss_price:
        execute_sell("STOP LOSS")
        return

    trailing_price = highest_price * (1 - TRAILING_STOP)
    if price <= trailing_price:
        execute_sell("TRAILING STOP")


def check_periodic_rsi_broadcast(current_rsi, current_price):
    global last_rsi_broadcast
    now = time.time()

    if now - last_rsi_broadcast >= RSI_BROADCAST_INTERVAL:
        send_telegram(
            "📈 PERIODIC RSI UPDATE\n\n"
            f"Symbol: {SYMBOL}\n"
            f"Price: {current_price:.6f} USDT\n"
            f"RSI: {current_rsi:.2f}"
        )
        last_rsi_broadcast = now


def trade_logic():
    global last_candle_time, cooldown_counter

    df = get_data()
    if df is None or len(df) < MA_PERIOD + 5:
        return

    closed_candle = df.iloc[-2]
    candle_time = closed_candle["time"]

    try:
        live_price = float(client.get_symbol_ticker(symbol=SYMBOL)["price"])
        manage_position(live_price)
    except Exception as e:
        print("Position ticker error:", e)

    if last_candle_time == candle_time:
        return

    last_candle_time = candle_time
    df = calculate_indicators(df)

    previous = df.iloc[-3]
    current = df.iloc[-2]

    prev_rsi = float(previous["rsi"])
    current_rsi = float(current["rsi"])
    price = float(current["close"])
    ma = float(current["ma"])

    if pd.isna(prev_rsi) or pd.isna(current_rsi) or pd.isna(ma):
        return

    print(f"New Candle | RSI: {current_rsi:.2f} | Price: {price:.6f} | MA: {ma:.6f}")

    # Broadcast RSI every 5 minutes to Telegram
    check_periodic_rsi_broadcast(current_rsi, price)

    if cooldown_counter > 0:
        cooldown_counter -= 1
        return

    if in_position:
        return

    # Entry strategy: RSI crosses above 30 and price is above 20 MA
    if prev_rsi < 30 and current_rsi > 30 and price > ma:
        send_telegram(
            "📈 BUY SIGNAL\n\n"
            f"RSI: {current_rsi:.2f}\n"
            f"Price: {price:.6f}\n"
            f"MA: {ma:.6f}"
        )
        execute_buy()


# =========================================================
# MAIN LOOP
# =========================================================

def run_bot():
    global last_rsi_broadcast
    last_rsi_broadcast = time.time()

    print("================================")
    print(" Binance Telegram Trading Bot")
    print("================================")
    print(f"Symbol: {SYMBOL} | Mode: {'TESTNET' if TESTNET else 'LIVE'}")

    send_telegram(
        "🤖 Binance Trading Bot Online\n\n"
        f"Mode: {'TESTNET' if TESTNET else 'LIVE'}\n"
        f"Symbol: {SYMBOL}\n"
        f"RSI Broadcasts: Every 5 mins\n"
        "Commands:\n"
        "• /start — Enable trading\n"
        "• /stop — Pause new trades\n"
        "• /status — Bot status\n"
        "• /balance — Check wallet balance\n"

        "• /help — List commands"
    )

    while True:
        try:
            check_telegram()

            if in_position:
                try:
                    price = float(client.get_symbol_ticker(symbol=SYMBOL)["price"])
                    manage_position(price)
                except Exception as e:
                    print("Position monitoring error:", e)

            if bot_running:
                trade_logic()

            time.sleep(5)

        except KeyboardInterrupt:
            print("Bot stopped manually.")
            send_telegram("🛑 Bot stopped from computer.")
            break

        except Exception as e:
            print("MAIN LOOP ERROR:", e)
            time.sleep(5)


if __name__ == "__main__":
    run_bot()