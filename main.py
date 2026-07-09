"""
GOLD (XAUUSDT) 5m/15m SMC Bot - "aqlliroq" versiya
MUHIM: bu yo'qotishsiz strategiya emas - bunday narsa mavjud emas. SL vaqti-vaqti
bilan tegadi. Maqsad - eskirgan signallarni kamaytirish va yo'qotishni boshqarish.
"""

import os
import time
import json
import threading
import traceback
import ccxt
import requests
from flask import Flask

SYMBOL = "XAUUSDT"
LIMIT = 200
RR1, RR2 = 1.5, 3.0
SWING_LEFT, SWING_RIGHT = 2, 2
CHECK_INTERVAL_SEC = 90
ZONE_MAX_DISTANCE_PCT = 0.4
LOG_FILE = "trade_log.json"

BOT_TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = os.environ["CHAT_ID"]

O, H, L, C = 1, 2, 3, 4

current_trade = None
warned_flip = False
last_impulse_ts = None
IMPULSE_LOOKBACK = 20
IMPULSE_THRESHOLD = 2.5

post_trade = None
POST_TRADE_CHECKS = 20  # ~30 daqiqa (20 x 90s)
POST_TRADE_MIN_CONTINUATION = 20  # $ - shuncha yoki undan ko'p davom etsagina xabar beradi


def detect_impulse(candles, lookback=IMPULSE_LOOKBACK, threshold=IMPULSE_THRESHOLD):
    if len(candles) < lookback + 1:
        return None
    ranges = [c[H] - c[L] for c in candles]
    avg_range = sum(ranges[-lookback - 1:-1]) / lookback
    last = candles[-1]
    last_range = last[H] - last[L]
    if avg_range == 0:
        return None
    ratio = last_range / avg_range
    if ratio >= threshold:
        direction = "yuqoriga" if last[C] > last[O] else "pastga"
        return {"ts": last[0], "ratio": round(ratio, 1), "direction": direction,
                "range": round(last_range, 2), "price": round(last[C], 2)}
    return None


def load_log():
    if os.path.exists(LOG_FILE):
        with open(LOG_FILE) as f:
            return json.load(f)
    return []


def save_log(log):
    with open(LOG_FILE, "w") as f:
        json.dump(log, f)


def win_rate_text(log):
    if not log:
        return "hali statistika yo'q (birinchi savdolar)"
    wins = sum(1 for t in log if t["result"] in ("TP1", "TP2"))
    be = sum(1 for t in log if t["result"] == "BE")
    total = len(log)
    return f"{wins/total*100:.1f}% g'alaba, {be} ta breakeven, jami {total} ta savdo"


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


def build_trade(candles, bias, price):
    zones = [z for z in detect_fvg(candles) + detect_order_blocks(candles) if z["type"] == bias]
    if not zones:
        return None
    zones.sort(key=lambda z: abs(price - (z["top"] + z["bottom"]) / 2))
    zone = zones[0]
    mid = (zone["top"] + zone["bottom"]) / 2
    if abs(price - mid) / price * 100 > ZONE_MAX_DISTANCE_PCT:
        return None

    if bias == "bullish":
        entry, sl = zone["top"], zone["bottom"] * 0.999
        risk = entry - sl
        if risk <= 0:
            return None
        tp1, tp2 = entry + risk * RR1, entry + risk * RR2
        side = "LONG"
    else:
        entry, sl = zone["bottom"], zone["top"] * 1.001
        risk = sl - entry
        if risk <= 0:
            return None
        tp1, tp2 = entry - risk * RR1, entry - risk * RR2
        side = "SHORT"
    return {"signal": side, "bias": bias, "entry": round(entry, 2), "sl": round(sl, 2),
            "tp1": round(tp1, 2), "tp2": round(tp2, 2)}


def send_telegram(text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    requests.post(url, data={"chat_id": CHAT_ID, "text": text}, timeout=15)


def start_post_trade_tracking(side, close_price):
    global post_trade
    post_trade = {"side": side, "close_price": close_price, "extreme": close_price, "checks": 0}


def update_post_trade(price):
    global post_trade
    if post_trade is None:
        return
    t = post_trade
    if t["side"] == "LONG":
        t["extreme"] = max(t["extreme"], price)
    else:
        t["extreme"] = min(t["extreme"], price)
    t["checks"] += 1
    if t["checks"] >= POST_TRADE_CHECKS:
        moved = abs(t["extreme"] - t["close_price"])
        if moved >= POST_TRADE_MIN_CONTINUATION:
            send_telegram(
                f"Ma'lumot: oldingi {t['side']} bitim yopilgandan keyin narx yana "
                f"{round(moved, 2)}$ davom etdi (eng yaxshi nuqta: {round(t['extreme'], 2)})."
            )
        post_trade = None


def close_trade(result, price):
    global current_trade, warned_flip
    t = current_trade
    log = load_log()
    log.append({"signal": t["signal"], "entry": t["entry"], "result": result, "close_price": price})
    save_log(log)
    emoji = {"TP2": "\u2705", "SL": "\u274c", "BE": "\u2796"}[result]
    send_telegram(
        f"GOLD {t['signal']} bitim yopildi {emoji} - {result} (narx {price})\n"
        f"Yangilangan statistika: {win_rate_text(log)}"
    )
    start_post_trade_tracking(t["signal"], price)
    current_trade = None
    warned_flip = False


def monitor_open_trade(price, bias15):
    global current_trade, warned_flip
    t = current_trade
    side = t["signal"]

    sl_hit = price <= t["sl"] if side == "LONG" else price >= t["sl"]
    tp2_hit = price >= t["tp2"] if side == "LONG" else price <= t["tp2"]
    tp1_hit = price >= t["tp1"] if side == "LONG" else price <= t["tp1"]

    if sl_hit:
        result = "BE" if t.get("breakeven") else "SL"
        close_trade(result, price)
        return
    if tp2_hit:
        close_trade("TP2", price)
        return
    if tp1_hit and not t.get("tp1_notified"):
        t["tp1_notified"] = True
        t["breakeven"] = True
        t["sl"] = t["entry"]
        send_telegram(
            f"GOLD {side} - TP1 oldi \u2705 (narx {price})\n"
            f"SL breakeven'ga (entry) ko'chirildi - bu savdo endi minus bermaydi.\n"
            f"Qolgan qism TP2 kutmoqda."
        )

    if bias15 is not None and bias15 != t["bias"] and not warned_flip:
        send_telegram(
            f"DIQQAT: GOLD {side} ochiq, lekin bozor struktura {bias15}ga o'zgardi.\n"
            f"Narx: {price} - SL hali tegmadi, lekin bitimni qo'lda yopishni ko'rib chiqing."
        )
        warned_flip = True


def run():
    global current_trade, last_impulse_ts

    candles15 = fetch_ohlcv("15m")
    candles5 = fetch_ohlcv("5m")
    bias15 = market_bias(*find_swings(candles15))
    bias5 = market_bias(*find_swings(candles5))
    price = candles15[-1][C]

    impulse = detect_impulse(candles5)
    if impulse and impulse["ts"] != last_impulse_ts:
        last_impulse_ts = impulse["ts"]
        send_telegram(
            f"\u26a1 IMPULSIV HARAKAT (5m): narx {impulse['direction']} {impulse['range']}$ siljidi "
            f"- odatdagidan {impulse['ratio']}x katta.\n"
            f"Narx: {impulse['price']}. Ehtimol yangilik (Fed/NFP/geosiyosat) sababli - "
            f"volatillik yuqori, ehtiyot bo'ling."
        )

    if current_trade:
        monitor_open_trade(price, bias15)
        return

    update_post_trade(price)

    if bias5 is None or bias15 is None or bias5 != bias15:
        print(f"NONE - 5m={bias5}, 15m={bias15} mos kelmadi")
        return

    trade = build_trade(candles15, bias15, price)
    if trade is None:
        print("NONE - narxga yaqin zona yo'q")
        return

    log = load_log()
    msg = (
        f"GOLD XAUUSDT - {trade['signal']}\n"
        f"5m/15m bias: {bias15}\n"
        f"Narx: {round(price, 2)}\n"
        f"Entry: {trade['entry']}\n"
        f"SL: {trade['sl']}\n"
        f"TP1: {trade['tp1']}\n"
        f"TP2: {trade['tp2']}\n"
        f"Win rate: {win_rate_text(log)}"
    )
    print(msg)
    send_telegram(msg)
    current_trade = trade


app = Flask(__name__)


@app.route("/")
def home():
    return "Bot ishlayapti"


def loop():
    print(f"Aqlli bot ishga tushdi - har {CHECK_INTERVAL_SEC}s tekshiradi")
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
