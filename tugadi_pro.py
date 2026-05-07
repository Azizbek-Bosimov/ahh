import asyncio
import ccxt
import pandas as pd
import numpy as np
import ta
import sys
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
from telegram.constants import ParseMode

# ==================== KONFIGURATSIYA ====================
TELEGRAM_TOKEN = "8385582858:AAFF-M8Y5O-lTCo5hBVZc0zLk9MLUf1dBEE"
CHANNEL_ID     = "@fhoveuss"
SYMBOL         = "XAUUSDT"
WAIT_TIME      = 20

exchange = ccxt.bybit({'options': {'defaultType': 'linear'}, 'enableRateLimit': True})
trade_log = {
    "active": False,
    "dir": None,
    "entry": 0,
    "tp1": 0,
    "tp2": 0,
    "sl": 0,
    "tp1_hit": False,
    "tp2_hit": False,
    "score": 0
}

# ==================== 📊 MURAKKAB TAHLIL FUNKSIYALARI ====================

def find_swing_levels(df, lookback=30):
    """Swing High / Swing Low - Likvidlik nuqtalari"""
    highs = df['high'].tail(lookback)
    lows = df['low'].tail(lookback)
    
    swing_high = highs.nlargest(3).mean()   # Top 3 yukori nuqtalar o'rtacha
    swing_low  = lows.nsmallest(3).mean()   # Top 3 quyi nuqtalar o'rtacha
    
    last_high = highs.max()
    last_low  = lows.min()
    return swing_high, swing_low, last_high, last_low


def find_fvg(df):
    """Fair Value Gap (FVG) - Narxdagi bo'shliq"""
    best_fvg = None
    best_price = 0
    best_size = 0

    for i in range(len(df) - 3, max(len(df) - 15, 2), -1):
        c1 = df.iloc[i - 1]
        c3 = df.iloc[i + 1]
        gap_size = 0

        if c3['low'] > c1['high']:   # Bullish FVG
            gap_size = c3['low'] - c1['high']
            if gap_size > best_size:
                best_size = gap_size
                best_fvg = "BULLISH_FVG"
                best_price = (c1['high'] + c3['low']) / 2

        elif c3['high'] < c1['low']:  # Bearish FVG
            gap_size = c1['low'] - c3['high']
            if gap_size > best_size:
                best_size = gap_size
                best_fvg = "BEARISH_FVG"
                best_price = (c1['low'] + c3['high']) / 2

    return best_fvg, best_price, best_size


def find_order_block(df):
    """Order Block - Institutional Buy/Sell zonasi"""
    # Oxirgi kuchli harakatdan oldingi sham = Order Block
    for i in range(len(df) - 2, max(len(df) - 20, 2), -1):
        curr = df.iloc[i]
        prev = df.iloc[i - 1]
        body_size = abs(curr['close'] - curr['open'])
        avg_body = abs(df['close'] - df['open']).rolling(10).mean().iloc[i]
        
        # Kuchli sham (avg dan 1.5x katta)
        if body_size > avg_body * 1.5:
            if curr['close'] > curr['open']:  # Bullish OB
                ob_low  = prev['low']
                ob_high = prev['high']
                return "BULLISH_OB", ob_low, ob_high
            else:  # Bearish OB
                ob_low  = prev['low']
                ob_high = prev['high']
                return "BEARISH_OB", ob_low, ob_high
    return None, 0, 0


def find_breaker_block(df):
    """Breaker Block - Buzilib qaytgan Order Block"""
    swing_high, swing_low, _, _ = find_swing_levels(df, 20)
    price = df.iloc[-1]['close']
    
    if price < swing_low:
        return "BEARISH_BREAKER", swing_low
    if price > swing_high:
        return "BULLISH_BREAKER", swing_high
    return None, 0


def detect_choch_bos(df):
    """Change of Character (CHoCH) va Break of Structure (BOS)"""
    highs = df['high'].values
    lows  = df['low'].values
    close = df['close'].values
    n = len(close)

    # Oxirgi 20 shamda strukturani tekshirish
    recent_highs = highs[-20:]
    recent_lows  = lows[-20:]

    prev_high = recent_highs[:-5].max()
    prev_low  = recent_lows[:-5].min()
    curr_price = close[-1]

    bos_bull  = curr_price > prev_high   # Bullish BOS
    bos_bear  = curr_price < prev_low    # Bearish BOS
    choch     = (close[-5] < prev_low and curr_price > prev_low) or \
                (close[-5] > prev_high and curr_price < prev_high)

    return bos_bull, bos_bear, choch


def find_liquidity_zones(df):
    """Equal Highs/Lows - Likvidlik joylari"""
    highs = df['high'].tail(50).values
    lows  = df['low'].tail(50).values
    price = df.iloc[-1]['close']
    atr   = ta.volatility.AverageTrueRange(df['high'], df['low'], df['close']).average_true_range().iloc[-1]
    
    tol = atr * 0.3  # Tolerans

    # Equal Highs (SSL - Sell Side Liquidity)
    eq_highs = []
    for i in range(len(highs) - 1):
        for j in range(i + 1, len(highs)):
            if abs(highs[i] - highs[j]) < tol:
                eq_highs.append((highs[i] + highs[j]) / 2)
    
    # Equal Lows (BSL - Buy Side Liquidity)
    eq_lows = []
    for i in range(len(lows) - 1):
        for j in range(i + 1, len(lows)):
            if abs(lows[i] - lows[j]) < tol:
                eq_lows.append((lows[i] + lows[j]) / 2)

    ssl = max(eq_highs) if eq_highs else df['high'].tail(50).max()  # Sell Side Liquidity
    bsl = min(eq_lows)  if eq_lows  else df['low'].tail(50).min()   # Buy Side Liquidity
    return ssl, bsl


def get_multi_tf_bias():
    """Ko'p vaqtli oyna (MTF) tahlili - 5m, 15m, 1h"""
    biases = {}
    for tf in ['5m', '15m', '1h']:
        try:
            ohlcv = exchange.fetch_ohlcv(SYMBOL, timeframe=tf, limit=250)
            df = pd.DataFrame(ohlcv, columns=['ts', 'open', 'high', 'low', 'close', 'volume'])
            ema200 = ta.trend.EMAIndicator(df['close'], window=200).ema_indicator().iloc[-1]
            ema50  = ta.trend.EMAIndicator(df['close'], window=50).ema_indicator().iloc[-1]
            price  = df.iloc[-1]['close']
            if price > ema200 and price > ema50:
                biases[tf] = "BULLISH"
            elif price < ema200 and price < ema50:
                biases[tf] = "BEARISH"
            else:
                biases[tf] = "NEUTRAL"
        except:
            biases[tf] = "NEUTRAL"
    return biases


def get_market_bias(df):
    """1m bias - EMA200 asosida"""
    ema200 = ta.trend.EMAIndicator(df['close'], window=200).ema_indicator().iloc[-1]
    price  = df.iloc[-1]['close']
    return "BULLISH" if price > ema200 else "BEARISH"


def compute_advanced_indicators(df):
    """RSI, MACD, Bollinger, Stochastic, Williams %R, CCI"""
    close = df['close']
    high  = df['high']
    low   = df['low']

    rsi = ta.momentum.RSIIndicator(close, window=14).rsi().iloc[-1]

    macd_obj  = ta.trend.MACD(close)
    macd_line = macd_obj.macd().iloc[-1]
    macd_sig  = macd_obj.macd_signal().iloc[-1]
    macd_hist = macd_obj.macd_diff().iloc[-1]

    bb    = ta.volatility.BollingerBands(close, window=20, window_dev=2)
    bb_up = bb.bollinger_hband().iloc[-1]
    bb_lo = bb.bollinger_lband().iloc[-1]
    bb_mi = bb.bollinger_mavg().iloc[-1]
    price = close.iloc[-1]
    bb_pos = (price - bb_lo) / (bb_up - bb_lo) * 100 if (bb_up - bb_lo) > 0 else 50

    stoch = ta.momentum.StochasticOscillator(high, low, close, window=14, smooth_window=3)
    stoch_k = stoch.stoch().iloc[-1]
    stoch_d = stoch.stoch_signal().iloc[-1]

    wr = ta.momentum.WilliamsRIndicator(high, low, close, lbp=14).williams_r().iloc[-1]
    cci = ta.trend.CCIIndicator(high, low, close, window=20).cci().iloc[-1]

    adx_obj = ta.trend.ADXIndicator(high, low, close, window=14)
    adx     = adx_obj.adx().iloc[-1]
    dmi_pos = adx_obj.adx_pos().iloc[-1]
    dmi_neg = adx_obj.adx_neg().iloc[-1]

    return {
        "rsi": rsi, "macd": macd_line, "macd_signal": macd_sig, "macd_hist": macd_hist,
        "bb_pos": bb_pos, "bb_up": bb_up, "bb_lo": bb_lo, "bb_mi": bb_mi,
        "stoch_k": stoch_k, "stoch_d": stoch_d,
        "wr": wr, "cci": cci, "adx": adx, "dmi_pos": dmi_pos, "dmi_neg": dmi_neg
    }


def compute_entry_zone(df, direction, atr, swing_high, swing_low):
    """
    Mukammal Kirish Zonasi (Entry Zone)
    OB, FVG, va Liquidity asosida aniq zona
    """
    price = df.iloc[-1]['close']
    _, fvg_price, _ = find_fvg(df)
    _, ob_low, ob_high = find_order_block(df)
    ssl, bsl = find_liquidity_zones(df)

    if direction == "BUY":
        candidates = []
        if ob_low > 0: candidates.append(ob_low)
        if fvg_price > 0 and fvg_price < price: candidates.append(fvg_price)
        candidates.append(swing_low + atr * 0.3)
        candidates.append(bsl + atr * 0.2)

        entry_ideal = min(candidates, key=lambda x: abs(x - price))
        entry_low   = entry_ideal - atr * 0.3
        entry_high  = entry_ideal + atr * 0.3
        return round(entry_ideal, 2), round(entry_low, 2), round(entry_high, 2)

    else:  # SELL
        candidates = []
        if ob_high > 0: candidates.append(ob_high)
        if fvg_price > 0 and fvg_price > price: candidates.append(fvg_price)
        candidates.append(swing_high - atr * 0.3)
        candidates.append(ssl - atr * 0.2)

        entry_ideal = min(candidates, key=lambda x: abs(x - price))
        entry_low   = entry_ideal - atr * 0.3
        entry_high  = entry_ideal + atr * 0.3
        return round(entry_ideal, 2), round(entry_low, 2), round(entry_high, 2)


def compute_smart_sl(df, direction, atr, swing_high, swing_low):
    """
    Aqlli SL - Likvidlik ortiga yashiringan, ATR bilan kengaytirilgan
    """
    ssl, bsl = find_liquidity_zones(df)
    price = df.iloc[-1]['close']
    buffer = atr * 0.7  # Kichikroq buffer - SL urmaslik uchun

    if direction == "BUY":
        # SL = Buy Side Liquidity ostidan, swing_low pastidan
        sl_candidates = [
            bsl - buffer,
            swing_low - buffer,
        ]
        sl = max(sl_candidates)  # Eng yaqin (xavfsiz) SL
        # Minimum narxdan 0.3% pastda bo'lsin
        min_sl = price * 0.997
        sl = min(sl, min_sl)

    else:  # SELL
        sl_candidates = [
            ssl + buffer,
            swing_high + buffer,
        ]
        sl = min(sl_candidates)  # Eng yaqin (xavfsiz) SL
        # Minimum narxdan 0.3% yuqorida bo'lsin
        min_sl = price * 1.003
        sl = max(sl, min_sl)

    return round(sl, 2)


def compute_dual_tp(price, sl, direction):
    """
    2 ta TP: TP1 = 1:1.5 RR, TP2 = 1:3 RR
    """
    risk = abs(price - sl)
    if direction == "BUY":
        tp1 = round(price + risk * 1.5, 2)
        tp2 = round(price + risk * 3.0, 2)
    else:
        tp1 = round(price - risk * 1.5, 2)
        tp2 = round(price - risk * 3.0, 2)
    return tp1, tp2


def compute_signal_score(bias, mtf_biases, inds, fvg_type, ob_type, bos_bull, bos_bear, choch, direction, volume_spike):
    """
    Signal sifatini 0-10 ball bilan baholash
    """
    score = 0

    # MTF alignment (max 2 ball)
    aligned = sum(1 for v in mtf_biases.values() if v == ("BULLISH" if direction == "BUY" else "BEARISH"))
    score += aligned * 0.67  # 3 TF = 2 ball

    # FVG (1 ball)
    if (direction == "BUY" and fvg_type == "BULLISH_FVG") or \
       (direction == "SELL" and fvg_type == "BEARISH_FVG"):
        score += 1

    # Order Block (1 ball)
    if (direction == "BUY" and ob_type == "BULLISH_OB") or \
       (direction == "SELL" and ob_type == "BEARISH_OB"):
        score += 1

    # BOS/CHoCH (1 ball)
    if (direction == "BUY" and bos_bull) or (direction == "SELL" and bos_bear):
        score += 1
    if choch:
        score += 0.5

    # RSI (1 ball)
    rsi = inds['rsi']
    if (direction == "BUY" and 30 < rsi < 55) or (direction == "SELL" and 45 < rsi < 70):
        score += 1

    # MACD (1 ball)
    if (direction == "BUY" and inds['macd_hist'] > 0) or \
       (direction == "SELL" and inds['macd_hist'] < 0):
        score += 1

    # ADX trend kuchi (1 ball)
    if inds['adx'] > 20:
        score += 0.5
    if inds['adx'] > 30:
        score += 0.5

    # Volume (1 ball)
    if volume_spike:
        score += 1

    return round(min(score, 10), 1)


def get_session():
    """Savdo sessiyasini aniqlash (UTC)"""
    import datetime
    h = datetime.datetime.utcnow().hour
    if 0 <= h < 8:   return "🌏 Osiyo sessiyasi"
    elif 8 <= h < 13: return "🇬🇧 London sessiyasi"
    elif 13 <= h < 22:return "🇺🇸 New York sessiyasi"
    else:             return "🌙 Oraliq sessiya"


# ==================== 🧠 ASOSIY ANALIZ ====================
async def elite_analyser():
    global trade_log
    try:
        # 1m va qo'shimcha TF ma'lumotlarini olish
        ohlcv = exchange.fetch_ohlcv(SYMBOL, timeframe='1m', limit=250)
        df = pd.DataFrame(ohlcv, columns=['ts', 'open', 'high', 'low', 'close', 'volume'])
        price = df.iloc[-1]['close']

        # ---- MONITORING: Mavjud savdo natijasi ----
        if trade_log["active"]:
            # TP1 tekshirish
            if not trade_log["tp1_hit"]:
                if (trade_log["dir"] == "BUY" and price >= trade_log["tp1"]) or \
                   (trade_log["dir"] == "SELL" and price <= trade_log["tp1"]):
                    trade_log["tp1_hit"] = True
                    return (
                        f"🎯 *TP1 URILDI!* {SYMBOL}\n"
                        f"━━━━━━━━━━━━━━\n"
                        f"✅ 1-Maqsad: `{trade_log['tp1']}` — Foyda qilinldi!\n"
                        f"📌 Pozitsiyaning yarmi yopildi\n"
                        f"🔄 SL → Kirish narxiga ko'chirish tavsiya etiladi\n"
                        f"⏳ TP2 → `{trade_log['tp2']}` ni kutmoqda..."
                    )

            # TP2 tekshirish
            if trade_log["tp1_hit"]:
                if (trade_log["dir"] == "BUY" and price >= trade_log["tp2"]) or \
                   (trade_log["dir"] == "SELL" and price <= trade_log["tp2"]):
                    trade_log.update({"active": False, "tp1_hit": False, "tp2_hit": False})
                    return (
                        f"💎 *TP2 URILDI! TO'LIQ FOYDA!* {SYMBOL}\n"
                        f"━━━━━━━━━━━━━━\n"
                        f"🏆 2-Maqsad: `{trade_log['tp2']}` — Maksimal foyda!\n"
                        f"💰 *RR = 1:3.0 — To'liq bajarildi!*"
                    )

            # SL tekshirish
            loss = (trade_log["dir"] == "BUY" and price <= trade_log["sl"]) or \
                   (trade_log["dir"] == "SELL" and price >= trade_log["sl"])
            if loss:
                trade_log.update({"active": False, "tp1_hit": False, "tp2_hit": False})
                return (
                    f"🛑 *STOP LOSS URILDI.* {SYMBOL}\n"
                    f"━━━━━━━━━━━━━━\n"
                    f"❌ Pozitsiya yopildi: `{trade_log['sl']}`\n"
                    f"📊 Keyingi signalni kutmoqda..."
                )
            return None  # Savdo davom etmoqda, yangi signal yo'q

        # ---- YANGI SIGNAL TAHLILI ----
        inds        = compute_advanced_indicators(df)
        bias        = get_market_bias(df)
        mtf_biases  = get_multi_tf_bias()
        fvg_type, fvg_price, fvg_size = find_fvg(df)
        ob_type, ob_low, ob_high      = find_order_block(df)
        bk_type, bk_price             = find_breaker_block(df)
        bos_bull, bos_bear, choch     = detect_choch_bos(df)
        swing_high, swing_low, last_high, last_low = find_swing_levels(df, 30)
        ssl, bsl                      = find_liquidity_zones(df)
        session                       = get_session()

        atr = ta.volatility.AverageTrueRange(df['high'], df['low'], df['close']).average_true_range().iloc[-1]
        vol_spike = df['volume'].iloc[-1] > df['volume'].rolling(10).mean().iloc[-1] * 1.5

        # MTF mos kelishi
        mtf_bull = sum(1 for v in mtf_biases.values() if v == "BULLISH")
        mtf_bear = sum(1 for v in mtf_biases.values() if v == "BEARISH")

        confs      = []
        final_dir  = None

        # ============ BUY SHAROITLARI ============
        if bias == "BULLISH" and mtf_bull >= 2:
            if fvg_type == "BULLISH_FVG":
                confs.append("⚡ Fair Value Gap (FVG) — Bullish Imbalance")
            if ob_type == "BULLISH_OB" and price >= ob_low and price <= ob_high + atr:
                confs.append("🏛 Order Block (OB) — Institutional Buy Zona")
            if bos_bull:
                confs.append("📈 Break of Structure (BOS) — Yangi Yuqori Tepa")
            if choch:
                confs.append("🔄 Change of Character (CHoCH) — Trend o'zgarishi")
            if bk_type == "BULLISH_BREAKER":
                confs.append("💥 Breaker Block — Kuchli Qayta Kirish")
            if 30 < inds['rsi'] < 55:
                confs.append(f"📊 RSI Optimal ({inds['rsi']:.1f}) — Haddan tashqari emas")
            if inds['macd_hist'] > 0 and inds['macd'] > inds['macd_signal']:
                confs.append("📉 MACD Bullish Crossover — Momentum yuqori")
            if inds['stoch_k'] < 40 and inds['stoch_k'] > inds['stoch_d']:
                confs.append(f"🎯 Stochastic ({inds['stoch_k']:.1f}) — Oversold dan qaytish")
            if inds['adx'] > 20 and inds['dmi_pos'] > inds['dmi_neg']:
                confs.append(f"💪 ADX({inds['adx']:.1f}) DMI+ ustun — Kuchli trend")
            if vol_spike:
                confs.append("🐋 Yirik Hajm — Institutional Flow (Katta Kapital)")
            if price < bsl + atr:
                confs.append("💰 Buy Side Liquidity (BSL) yaqinida — Likvidlik ovlash")

            if len(confs) >= 4:
                final_dir = "BUY"

        # ============ SELL SHAROITLARI ============
        if not final_dir and bias == "BEARISH" and mtf_bear >= 2:
            if fvg_type == "BEARISH_FVG":
                confs.append("⚡ Fair Value Gap (FVG) — Bearish Imbalance")
            if ob_type == "BEARISH_OB" and price <= ob_high and price >= ob_low - atr:
                confs.append("🏛 Order Block (OB) — Institutional Sell Zona")
            if bos_bear:
                confs.append("📉 Break of Structure (BOS) — Yangi Pastki Dip")
            if choch:
                confs.append("🔄 Change of Character (CHoCH) — Trend o'zgarishi")
            if bk_type == "BEARISH_BREAKER":
                confs.append("💥 Breaker Block — Kuchli Qayta Kirish")
            if 45 < inds['rsi'] < 70:
                confs.append(f"📊 RSI Optimal ({inds['rsi']:.1f}) — Haddan tashqari emas")
            if inds['macd_hist'] < 0 and inds['macd'] < inds['macd_signal']:
                confs.append("📉 MACD Bearish Crossover — Momentum pastga")
            if inds['stoch_k'] > 60 and inds['stoch_k'] < inds['stoch_d']:
                confs.append(f"🎯 Stochastic ({inds['stoch_k']:.1f}) — Overbought dan qaytish")
            if inds['adx'] > 20 and inds['dmi_neg'] > inds['dmi_pos']:
                confs.append(f"💪 ADX({inds['adx']:.1f}) DMI- ustun — Kuchli trend")
            if vol_spike:
                confs.append("🐋 Yirik Hajm — Institutional Flow (Katta Kapital)")
            if price > ssl - atr:
                confs.append("💰 Sell Side Liquidity (SSL) yaqinida — Likvidlik ovlash")

            if len(confs) >= 4:
                final_dir = "SELL"

        if not final_dir:
            return None

        # ---- KIRISH, SL, TP HISOBLASH ----
        entry, entry_low, entry_high = compute_entry_zone(df, final_dir, atr, swing_high, swing_low)
        sl  = compute_smart_sl(df, final_dir, atr, swing_high, swing_low)
        tp1, tp2 = compute_dual_tp(price, sl, final_dir)

        # Signal sifat balli
        score = compute_signal_score(
            bias, mtf_biases, inds, fvg_type, ob_type,
            bos_bull, bos_bear, choch, final_dir, vol_spike
        )

        # Yulduz reytingi
        stars = "⭐" * int(score / 2) if score > 0 else "—"

        # Risk/Reward
        risk   = abs(price - sl)
        rr_tp1 = round(abs(tp1 - price) / risk, 2) if risk > 0 else 0
        rr_tp2 = round(abs(tp2 - price) / risk, 2) if risk > 0 else 0

        trade_log.update({
            "active": True, "dir": final_dir, "entry": price,
            "tp1": tp1, "tp2": tp2, "sl": sl,
            "tp1_hit": False, "tp2_hit": False,
            "score": score
        })

        # MTF holati
        mtf_str = " | ".join([f"{tf}: {'🟢' if v == 'BULLISH' else '🔴' if v == 'BEARISH' else '⚪'}"
                               for tf, v in mtf_biases.items()])

        emoji = "🟢" if final_dir == "BUY" else "🔴"

        msg = (
            f"🎩 *ELITE SIGNAL: {SYMBOL}* {emoji}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📌 *Yo'nalish:* `{final_dir}`\n"
            f"⏰ *Sessiya:* {session}\n"
            f"🏆 *Signal Sifati:* {stars} `{score}/10`\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"💰 *KIRISH ZONASI*\n"
            f"   🔑 Ideal Kirish: `{entry}`\n"
            f"   📐 Zona: `{entry_low}` — `{entry_high}`\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🎯 *MAQSADLAR (TP)*\n"
            f"   TP1 → `{tp1}` *(RR 1:{rr_tp1})*\n"
            f"   TP2 → `{tp2}` *(RR 1:{rr_tp2})*\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🛡 *HIMOYA (SL)*\n"
            f"   Stop Loss: `{sl}`\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📊 *KO'P VAQTLI OYNA (MTF)*\n"
            f"   {mtf_str}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🔬 *TASDIQLOVCHI OMILLAR ({len(confs)}/11):*\n"
            + "\n".join([f"  ✅ {c}" for c in confs]) +
            f"\n━━━━━━━━━━━━━━━━━━━━\n"
            f"📈 *INDIKATORLAR:*\n"
            f"   RSI: `{inds['rsi']:.1f}` | ADX: `{inds['adx']:.1f}` | ATR: `{atr:.2f}`\n"
            f"   MACD: `{inds['macd_hist']:.3f}` | Stoch: `{inds['stoch_k']:.1f}`\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"⚠️ *Tavsiya:* TP1 da 50% yopib, SL→BE ga o'tkazing"
        )
        return msg

    except Exception as e:
        return f"⚠️ Xato: {e}"


# ==================== 📩 BOT BOSHQARUVI ====================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🎩 *Elite Treyder Boti Faollashdi!*\n\n"
        "📊 SMC/ICT usulida tahlil qilmoqda:\n"
        "• Fair Value Gap (FVG)\n"
        "• Order Block (OB)\n"
        "• Breaker Block\n"
        "• BOS / CHoCH\n"
        "• Multi-Timeframe (MTF)\n"
        "• Likvidlik Zonalari\n"
        "• 2 ta TP + Smart SL\n\n"
        "⏳ Signal kutilmoqda...",
        parse_mode=ParseMode.MARKDOWN
    )
    res = await elite_analyser()
    if res:
        await update.message.reply_text(res, parse_mode=ParseMode.MARKDOWN)
    else:
        await update.message.reply_text(
            "⏳ Hozircha sifatli signal yo'q.\n"
            "Bot avtomatik monitoring qilmoqda..."
        )


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Joriy savdo holati"""
    if trade_log["active"]:
        tp1_status = "✅ Urildi" if trade_log["tp1_hit"] else "⏳ Kutilmoqda"
        msg = (
            f"📊 *FAOL SAVDO*\n"
            f"Yo'nalish: `{trade_log['dir']}`\n"
            f"Kirish: `{trade_log['entry']}`\n"
            f"TP1: `{trade_log['tp1']}` — {tp1_status}\n"
            f"TP2: `{trade_log['tp2']}` — ⏳\n"
            f"SL: `{trade_log['sl']}`\n"
            f"Ball: `{trade_log['score']}/10`"
        )
    else:
        msg = "😴 Hozirda faol savdo yo'q. Yangi signal kutilmoqda."
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)


async def loop(app):
    while True:
        try:
            res = await elite_analyser()
            if res:
                await app.bot.send_message(
                    chat_id=CHANNEL_ID, text=res, parse_mode=ParseMode.MARKDOWN
                )
        except Exception as e:
            print(f"Loop xato: {e}")
        for i in range(WAIT_TIME, 0, -1):
            sys.stdout.write(f"\r🔍 Elite Monitoring: {i}s  ")
            sys.stdout.flush()
            await asyncio.sleep(1)


async def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("status", status))
    await app.initialize()
    await app.start()
    await asyncio.gather(app.updater.start_polling(), loop(app))


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n✋ Bot to'xtatildi.")
    except Exception as e:
        print(f"Xato: {e}")
