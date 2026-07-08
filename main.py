"""
GOLD (XAUUSDT) - 5m/15m Multi-Timeframe SMC Signal -> Telegram
Termux/telefon uchun YENGIL versiya - pandas/numpy shart emas, faqat ccxt + requests.
BOT_TOKEN va CHAT_ID muhit o'zgaruvchisidan olinadi.
"""

import os
import time
import threading
import traceback
import ccxt
import requests
from flask import Flask

SYMBOL = "XAUUSDT"
LIMIT = 200
RR1, RR2 = 1.5, 3.0
SWING_LEFT, SWING_RIGHT = 2, 2
CHECK_INTERVAL_SEC = 30

BOT_TOKEN = os.environ["8331849501:AAEAjvqQ1oAZiwu4a-EZs6t5AzGqyQyqle8"]
CHAT_ID = os.environ["@fhoveus_bot"]

_last_sent = None

# Har bir candle: [ts, open, high, low, close, volume]
O, H, L, C = 1, 2, 3, 4


def fetch_ohlcv(timeframe):
    exchange = ccxt.bybit({"enableRateLimit": True})
    return exchange.fetch_ohlcv(SYMBOL, timeframe=timeframe, limit=LIMIT)


def find_swings(candles, left=SWING_LEFT, right=SWING_RIGHT):
    highs, lows = [], []
    for i in range(left, len(candles) - right):
        window = candles[i - left:i + right + 1]
        if candles[i][H] == max(c[H] for c in window):
            highs.append((i, candles[i][H]))
        if candles[i][L] == min(c[L] for c in window):
            lows.append((i, candles[i][L]))
    return highs, lows


def market_bias(highs, lows):
    if len(highs) < 2 or len(lows) < 2:
        return None
    hh = highs[-1][1] > highs[-2][1]
    hl = lows[-1][1] > lows[-2][1]
    lh = highs[-1][1] < highs[-2][1]
    ll = lows[-1][1] < lows[-2][1]
    if hh and hl:
        return "bullish"
    if lh and ll:
        return "bearish"
    return None


def detect_fvg(candles):
    fvgs = []
    for i in range(2, len(candles)):
        c1, c3 = candles[i - 2], candles[i]
        if c3[L] > c1[H]:
            fvgs.append({"type": "bullish", "top": c3[L], "bottom": c1[H]})
        elif c3[H] < c1[L]:
            fvgs.append({"type": "bearish", "top": c1[L], "bottom": c3[H]})
    return fvgs


def detect_order_blocks(candles):
    bodies = [abs(c[C] - c[O]) for c in candles]
    obs = []
    for i in range(10, len(candles) - 1):
        avg_body = sum(bodies[i - 10:i]) / 10
        if avg_body == 0:
            continue
        impulsive = bodies[i + 1] > avg_body * 1.5
        c_i, c_next = candles[i], candles[i + 1]
        bull_ob = c_i[C] < c_i[O] and c_next[C] > c_next[O]
        bear_ob = c_i[C] > c_i[O] and c_next[C] < c_next[O]
        if impulsive and bull_ob:
            obs.append({"type": "bullish", "top": c_i[O], "bottom": c_i[L]})
        if impulsive and bear_ob:
            obs.append({"type": "bearish", "top": c_i[H], "bottom": c_i[O]})
    return obs


def build_trade(candles, bias):
    zones = [z for z in detect_fvg(candles) + detect_order_blocks(candles) if z["type"] == bias]
    if not zones:
        return None
    zone = zones[-1]
    if bias == "bullish":
        entry, sl = zone["top"], zone["bottom"] * 0.999
        risk = entry - sl
        tp1, tp2 = entry + risk * RR1, entry + risk * RR2
        side = "LONG"
    else:
        entry, sl = zone["bottom"], zone["top"] * 1.001
        risk = sl - entry
        tp1, tp2 = entry - risk * RR1, entry - risk * RR2
        side = "SHORT"
    return {
        "signal": side, "entry": round(entry, 2), "sl": round(sl, 2),
        "tp1": round(tp1, 2), "tp2": round(tp2, 2),
    }


def send_telegram(text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    requests.post(url, data={"chat_id": CHAT_ID, "text": text}, timeout=15)


def run():
    global _last_sent
    candles5 = fetch_ohlcv("5m")
    candles15 = fetch_ohlcv("15m")
    bias5 = market_bias(*find_swings(candles5))
    bias15 = market_bias(*find_swings(candles15))

    if bias5 is None or bias15 is None or bias5 != bias15:
        print(f"NONE - 5m={bias5}, 15m={bias15} mos kelmadi")
        return

    trade = build_trade(candles15, bias15)
    if trade is None:
        print("NONE - zona topilmadi")
        return

    price = candles15[-1][C]
    msg = (
        f"GOLD XAUUSDT - {trade['signal']}\n"
        f"5m/15m bias: {bias15}\n"
        f"Narx: {round(price, 2)}\n"
        f"Entry: {trade['entry']}\n"
        f"SL: {trade['sl']}\n"
        f"TP1: {trade['tp1']}\n"
        f"TP2: {trade['tp2']}"
    )
    print(msg)
    key = (trade["signal"], trade["entry"], trade["sl"])
    if key != _last_sent:
        send_telegram(msg)
        _last_sent = key
    else:
        print("(o'zgarish yo'q, Telegram'ga yuborilmadi)")


app = Flask(__name__)


@app.route("/")
def home():
    return "Bot ishlayapti"


def loop():
    print(f"Doimiy rejim ishga tushdi - har {CHECK_INTERVAL_SEC}s tekshiradi")
    while True:
        try:
            run()
        except Exception as e:
            print(f"Xatolik: {e}")
            traceback.print_exc()
        time.sleep(CHECK_INTERVAL_SEC)


if __name__ == "__main__":
    threading.Thread(target=loop, daemon=True).start()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))

