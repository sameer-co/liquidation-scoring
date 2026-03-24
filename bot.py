"""
╔══════════════════════════════════════════════════════════════════╗
║         SOL TRADING DECISION SYSTEM — Binance Futures API       ║
║  Scoring + Live Liquidation WebSocket + Full Telegram Alerts    ║
╚══════════════════════════════════════════════════════════════════╝

Fixes applied:
  #1  Deadlock — liq_lock no longer re-acquired inside callback
  #2  Spot API → Futures API for klines (volume, RSI, price change)
  #3  Liquidation avg calculation fixed (window-based, not event-count)
  #4  Score display max corrected (asymmetric scoring documented)
  #5  RSI uses Wilder's smoothing (matches TradingView)
  #6  deque maxlen increased to 1000
  #7  WS startup wait replaced with proper ws_connected polling

Telegram alerts added for:
  - Bot start / stop
  - Every BUY / SELL / WEAK signal after each 60s analysis
  - Live liquidation spike (short squeeze / long dump)
  - Large single liquidation (> $100k)

Requirements:
    pip install requests websocket-client numpy colorama
"""

import json
import time
import threading
import requests
import numpy as np
from datetime import datetime
from collections import deque

try:
    from colorama import init, Fore, Style
    init(autoreset=True)
except ImportError:
    class Fore:
        RED = GREEN = YELLOW = CYAN = WHITE = MAGENTA = BLUE = ""
    class Style:
        RESET_ALL = BRIGHT = ""

try:
    import websocket
except ImportError:
    print("websocket-client not installed. Run: pip install websocket-client")
    exit()

# ═══════════════════════════════════════════════════════════
#                     CONFIGURATION
# ═══════════════════════════════════════════════════════════

TELEGRAM_BOT_TOKEN = '8050135427:AAFNQYFpU8lMQ-reJlvLnPYFKc8pyPrHblE'
TELEGRAM_CHAT_ID   = '1950462171'

SYMBOL       = "SOLUSDT"
SYMBOL_LOWER = SYMBOL.lower()

FUTURES_BASE = "https://fapi.binance.com"
SPOT_BASE    = "https://api.binance.com"   # only for order book depth

# Scoring thresholds
FUNDING_BEAR_THRESHOLD    = -0.0005
FUNDING_BULL_THRESHOLD    =  0.0005
LS_RATIO_SHORT_HEAVY      =  0.90
LS_RATIO_LONG_HEAVY       =  1.10
ORDER_BOOK_THIN_THRESHOLD =  5000
VOLUME_LOW_RATIO          =  0.50
VOLUME_SPIKE_RATIO        =  2.00
LIQUIDATION_WINDOW_SEC    =  60
LIQUIDATION_SPIKE_MULT    =  2.0
LARGE_LIQ_USD             =  100_000   # single liq alert threshold
MIN_BUY_SCORE             =  6
MIN_SELL_SCORE            = -6

# ═══════════════════════════════════════════════════════════
#                  GLOBAL STATE
# ═══════════════════════════════════════════════════════════

liquidation_log  = deque(maxlen=1000)
liq_buy_amounts  = deque(maxlen=1000)   # (timestamp, usd_val) for short liqs
liq_sell_amounts = deque(maxlen=1000)   # (timestamp, usd_val) for long  liqs
liq_lock         = threading.RLock()   # FIX #1: RLock allows re-entry
ws_connected     = False
current_price    = 0.0

# ═══════════════════════════════════════════════════════════
#                  TELEGRAM
# ═══════════════════════════════════════════════════════════

def send_telegram(msg: str, silent: bool = False):
    """Send a Telegram message. silent=True suppresses notification sound."""
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id"              : TELEGRAM_CHAT_ID,
            "text"                 : msg,
            "parse_mode"           : "HTML",
            "disable_notification" : silent,
        }
        r = requests.post(url, json=payload, timeout=10)
        r.raise_for_status()
    except Exception as e:
        print(f"{Fore.RED}  [TG ERROR] {e}{Style.RESET_ALL}")

def tg_signal_alert(score: int, decision: str, signals: list, price: float,
                    funding: float, ls_ratio: float, rsi: float,
                    liq_buy: float, liq_sell: float):
    """Full analysis summary alert."""
    emoji = "🟢" if score >= MIN_BUY_SCORE else ("🔴" if score <= MIN_SELL_SCORE else "🟡")
    bull_sigs  = [m for s, _, m in signals if s.strip().startswith("+")]
    bear_sigs  = [m for s, _, m in signals if s.strip().startswith("-")]

    bull_str = "\n".join(f"  ✅ {m}" for m in bull_sigs) or "  —"
    bear_str = "\n".join(f"  ❌ {m}" for m in bear_sigs) or "  —"

    send_telegram(
        f"{emoji} <b>[{SYMBOL}] {decision}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🕐 Time    : {datetime.now().strftime('%H:%M:%S IST')}\n"
        f"💵 Price   : ${price:.4f}\n"
        f"📊 Score   : {score:+d}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>Bullish signals</b>\n{bull_str}\n"
        f"<b>Bearish signals</b>\n{bear_str}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📈 Funding  : {funding:.5f}\n"
        f"⚖️ L/S Ratio: {ls_ratio:.3f}\n"
        f"📉 RSI(15m) : {rsi:.1f}\n"
        f"🟢 Short liqs: ${liq_buy:,.0f}\n"
        f"🔴 Long  liqs: ${liq_sell:,.0f}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"⚠️ Not financial advice. Always use SL."
    )

def tg_liq_spike(side: str, total: float, avg: float, mult: float):
    """Alert for liquidation spike."""
    if side == "BUY":
        label = "SHORT SQUEEZE BUILDING"
        emoji = "🚀"
    else:
        label = "LONG DUMP BUILDING"
        emoji = "💥"
    send_telegram(
        f"{emoji} <b>LIQ SPIKE — {label}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💵 Price     : ${current_price:.4f}\n"
        f"💰 Last 60s  : ${total:,.0f}\n"
        f"📊 Avg 60s   : ${avg:,.0f}\n"
        f"📈 Magnitude : {mult:.1f}x above average\n"
        f"🕐 Time      : {datetime.now().strftime('%H:%M:%S IST')}"
    )

def tg_large_liq(side: str, usd_val: float, qty: float, price: float):
    """Alert for single large liquidation event."""
    direction = "SHORT liquidated (bullish)" if side == "BUY" else "LONG liquidated (bearish)"
    emoji     = "🟢" if side == "BUY" else "🔴"
    send_telegram(
        f"{emoji} <b>LARGE LIQUIDATION</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📌 {direction}\n"
        f"💰 Value : ${usd_val:,.0f}\n"
        f"📦 Size  : {qty:.1f} SOL @ ${price:.2f}\n"
        f"🕐 Time  : {datetime.now().strftime('%H:%M:%S IST')}",
        silent=False
    )

# ═══════════════════════════════════════════════════════════
#                  HELPER UTILITIES
# ═══════════════════════════════════════════════════════════

def ts():
    return datetime.now().strftime("%H:%M:%S")

def print_header(title):
    print(f"\n{Fore.CYAN}{'═'*60}\n  {title}\n{'═'*60}{Style.RESET_ALL}")

def safe_get(url, params=None, retries=3):
    for i in range(retries):
        try:
            r = requests.get(url, params=params, timeout=8)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            if i == retries - 1:
                print(f"{Fore.RED}  [API ERROR] {url} → {e}{Style.RESET_ALL}")
                return None
            time.sleep(1)

# ═══════════════════════════════════════════════════════════
#                  BINANCE FUTURES API
# ═══════════════════════════════════════════════════════════

def get_current_price():
    data = safe_get(f"{FUTURES_BASE}/fapi/v1/ticker/price", {"symbol": SYMBOL})
    return float(data["price"]) if data else 0.0

def get_funding_rate():
    data = safe_get(f"{FUTURES_BASE}/fapi/v1/fundingRate", {"symbol": SYMBOL, "limit": 5})
    return float(data[-1]["fundingRate"]) if data else 0.0

def get_long_short_ratio():
    data = safe_get(
        f"{FUTURES_BASE}/futures/data/globalLongShortAccountRatio",
        {"symbol": SYMBOL, "period": "5m", "limit": 3}
    )
    return float(data[-1]["longShortRatio"]) if data else 1.0

def get_open_interest_history():
    data = safe_get(
        f"{FUTURES_BASE}/futures/data/openInterestHist",
        {"symbol": SYMBOL, "period": "5m", "limit": 10}
    )
    return [float(d["sumOpenInterest"]) for d in data] if data else []

def get_order_book_liquidity():
    # Order book is fine from spot for depth purposes
    data = safe_get(f"{SPOT_BASE}/api/v3/depth", {"symbol": SYMBOL, "limit": 20})
    if data:
        return sum(float(b[1]) for b in data["bids"]), \
               sum(float(a[1]) for a in data["asks"])
    return 0.0, 0.0

def get_volume_ratio():
    # FIX #2: use futures klines not spot
    data = safe_get(
        f"{FUTURES_BASE}/fapi/v1/klines",
        {"symbol": SYMBOL, "interval": "5m", "limit": 51}
    )
    if data and len(data) > 5:
        volumes     = [float(k[5]) for k in data]
        current_vol = volumes[-1]
        avg_vol     = np.mean(volumes[:-1])
        return (current_vol / avg_vol if avg_vol > 0 else 1.0), current_vol, avg_vol
    return 1.0, 0.0, 0.0

def get_rsi(period=14):
    # FIX #2: use futures klines
    # FIX #5: Wilder's smoothing (matches TradingView)
    data = safe_get(
        f"{FUTURES_BASE}/fapi/v1/klines",
        {"symbol": SYMBOL, "interval": "15m", "limit": period * 3}
    )
    if data and len(data) > period + 1:
        closes = [float(k[4]) for k in data]
        deltas = np.diff(closes)
        gains  = np.where(deltas > 0, deltas, 0.0)
        losses = np.where(deltas < 0, -deltas, 0.0)

        # Wilder's smoothing: seed with simple average then RMA
        avg_gain = np.mean(gains[:period])
        avg_loss = np.mean(losses[:period])
        for g, l in zip(gains[period:], losses[period:]):
            avg_gain = (avg_gain * (period - 1) + g) / period
            avg_loss = (avg_loss * (period - 1) + l) / period

        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return round(100 - (100 / (1 + rs)), 2)
    return 50.0

def get_price_change_pct(minutes=60):
    # FIX #2: use futures klines
    limit = max(minutes // 5, 2)
    data = safe_get(
        f"{FUTURES_BASE}/fapi/v1/klines",
        {"symbol": SYMBOL, "interval": "5m", "limit": limit + 1}
    )
    if data and len(data) > 1:
        return ((float(data[-1][4]) - float(data[0][1])) / float(data[0][1])) * 100
    return 0.0

# ═══════════════════════════════════════════════════════════
#              WEBSOCKET — LIVE LIQUIDATION MONITOR
# ═══════════════════════════════════════════════════════════

def on_liquidation_message(ws, message):
    global current_price
    try:
        data  = json.loads(message)
        order = data.get("o", {})
        side    = order.get("S", "")
        qty     = float(order.get("q", 0))
        price   = float(order.get("p", 0))
        usd_val = qty * price
        now     = time.time()

        event = {"time": now, "side": side, "qty": qty, "price": price, "usd_val": usd_val}

        # FIX #1: collect snapshot data while holding lock, release before spike check
        with liq_lock:
            liquidation_log.append(event)
            if side == "BUY":
                liq_buy_amounts.append((now, usd_val))
                recent_snapshot = list(liq_buy_amounts)
            else:
                liq_sell_amounts.append((now, usd_val))
                recent_snapshot = list(liq_sell_amounts)

        # Console print
        direction = f"{Fore.GREEN}SHORT LIQ ▲" if side == "BUY" else f"{Fore.RED}LONG  LIQ ▼"
        large_tag = "  ⚡ LARGE" if usd_val > LARGE_LIQ_USD else ""
        print(
            f"  [{ts()}] {direction}{Style.RESET_ALL}  "
            f"${usd_val:>10,.0f}  |  {qty:.1f} SOL @ ${price:.2f}{large_tag}"
        )

        # Large single liq → immediate TG alert
        if usd_val > LARGE_LIQ_USD:
            threading.Thread(
                target=tg_large_liq, args=(side, usd_val, qty, price), daemon=True
            ).start()

        # Spike check — no lock needed, works on snapshot
        check_liquidation_spike(side, usd_val, recent_snapshot)

    except Exception:
        pass

def check_liquidation_spike(side: str, usd_val: float, recent_snapshot: list):
    """FIX #1: no lock acquired here — works on pre-copied snapshot."""
    cutoff  = time.time() - LIQUIDATION_WINDOW_SEC
    recent  = [v for t, v in recent_snapshot if t > cutoff]

    if len(recent) > 3:
        avg = np.mean(recent[:-1])
        if avg > 0 and usd_val > avg * LIQUIDATION_SPIKE_MULT:
            label = "SHORT SQUEEZE BUILDING" if side == "BUY" else "LONG DUMP BUILDING"
            color = Fore.GREEN if side == "BUY" else Fore.RED
            mult  = usd_val / avg
            print(
                f"\n  {color}🚨 LIQUIDATION SPIKE: {label}{Style.RESET_ALL}\n"
                f"     This liq: ${usd_val:,.0f} vs avg: ${avg:,.0f} ({mult:.1f}x)\n"
            )
            threading.Thread(
                target=tg_liq_spike,
                args=(side, sum(recent), avg, mult),
                daemon=True
            ).start()

def on_ws_open(ws):
    global ws_connected
    ws_connected = True
    print(f"\n{Fore.GREEN}  ✅ WebSocket connected — listening for {SYMBOL} liquidations...{Style.RESET_ALL}\n")

def on_ws_error(ws, error):
    global ws_connected
    ws_connected = False
    print(f"{Fore.RED}  WebSocket error: {error}{Style.RESET_ALL}")

def on_ws_close(ws, close_status_code, close_msg):
    global ws_connected
    ws_connected = False
    print(f"{Fore.YELLOW}  WebSocket closed. Reconnecting...{Style.RESET_ALL}")

def start_websocket():
    def run():
        while True:
            try:
                ws_url = f"wss://fstream.binance.com/ws/{SYMBOL_LOWER}@forceOrder"
                ws = websocket.WebSocketApp(
                    ws_url,
                    on_message = on_liquidation_message,
                    on_open    = on_ws_open,
                    on_error   = on_ws_error,
                    on_close   = on_ws_close,
                )
                ws.run_forever(ping_interval=30, ping_timeout=10)
            except Exception as e:
                print(f"{Fore.RED}  WS exception: {e}{Style.RESET_ALL}")
            time.sleep(5)

    threading.Thread(target=run, daemon=True).start()

def wait_for_websocket(timeout=15):
    """FIX #7: poll until connected instead of fixed sleep."""
    print(f"  {Fore.CYAN}Waiting for WebSocket...{Style.RESET_ALL}", end="", flush=True)
    deadline = time.time() + timeout
    while not ws_connected and time.time() < deadline:
        time.sleep(0.5)
        print(".", end="", flush=True)
    print()
    if ws_connected:
        print(f"  {Fore.GREEN}WebSocket ready.{Style.RESET_ALL}")
    else:
        print(f"  {Fore.YELLOW}WebSocket not connected yet — continuing anyway.{Style.RESET_ALL}")

# ═══════════════════════════════════════════════════════════
#              LIQUIDATION SUMMARY (ROLLING WINDOW)
# ═══════════════════════════════════════════════════════════

def get_liquidation_summary():
    """
    FIX #3: Correct window-based average.
    Current window  = last LIQUIDATION_WINDOW_SEC seconds.
    Historical avg  = average of the 9 prior windows of same duration.
    """
    now    = time.time()
    cutoff = now - LIQUIDATION_WINDOW_SEC

    with liq_lock:
        buy_snap  = list(liq_buy_amounts)
        sell_snap = list(liq_sell_amounts)

    # Current window totals
    buy_recent  = [(t, v) for t, v in buy_snap  if t > cutoff]
    sell_recent = [(t, v) for t, v in sell_snap if t > cutoff]
    buy_total   = sum(v for _, v in buy_recent)
    sell_total  = sum(v for _, v in sell_recent)

    # Historical average: split older data into LIQUIDATION_WINDOW_SEC buckets
    def window_avg(snap, n_windows=9):
        buckets = []
        for w in range(1, n_windows + 1):
            w_end   = cutoff - (w - 1) * LIQUIDATION_WINDOW_SEC
            w_start = w_end  - LIQUIDATION_WINDOW_SEC
            total   = sum(v for t, v in snap if w_start < t <= w_end)
            buckets.append(total)
        filled = [b for b in buckets if b > 0]
        return np.mean(filled) if filled else 1.0

    avg_buy  = window_avg(buy_snap)
    avg_sell = window_avg(sell_snap)

    return {
        "buy_total"  : buy_total,
        "sell_total" : sell_total,
        "buy_count"  : len(buy_recent),
        "sell_count" : len(sell_recent),
        "buy_spike"  : buy_total  > avg_buy  * LIQUIDATION_SPIKE_MULT,
        "sell_spike" : sell_total > avg_sell * LIQUIDATION_SPIKE_MULT,
        "avg_buy"    : avg_buy,
        "avg_sell"   : avg_sell,
    }

# ═══════════════════════════════════════════════════════════
#                  SCORING ENGINE
# ═══════════════════════════════════════════════════════════

def run_analysis():
    global current_price

    print_header(f"SOL MARKET ANALYSIS — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  {Fore.CYAN}Fetching market data...{Style.RESET_ALL}")

    current_price            = get_current_price()
    funding                  = get_funding_rate()
    ls_ratio                 = get_long_short_ratio()
    oi_history               = get_open_interest_history()
    bid_liq, ask_liq         = get_order_book_liquidity()
    vol_ratio, cur_vol, avg_vol = get_volume_ratio()
    rsi                      = get_rsi()
    price_chg_1h             = get_price_change_pct(60)
    liq_summary              = get_liquidation_summary()

    oi_change_pct = 0.0
    if len(oi_history) >= 2:
        oi_change_pct = ((oi_history[-1] - oi_history[0]) / oi_history[0]) * 100

    avg_book_liq = (bid_liq + ask_liq) / 2

    score   = 0
    signals = []   # list of (score_str, color, message)

    # ── 1. Funding Rate (±2) ──
    if funding < FUNDING_BEAR_THRESHOLD:
        score += 2
        signals.append(("+2", Fore.GREEN, f"Funding NEGATIVE ({funding:.5f}) — shorts dominant, squeeze risk HIGH"))
    elif funding > FUNDING_BULL_THRESHOLD:
        score -= 2
        signals.append(("-2", Fore.RED, f"Funding POSITIVE ({funding:.5f}) — longs dominant, dump risk HIGH"))
    else:
        signals.append((" 0", Fore.WHITE, f"Funding NEUTRAL ({funding:.5f})"))

    # ── 2. Long/Short Ratio (±2) ──
    if ls_ratio < LS_RATIO_SHORT_HEAVY:
        score += 2
        signals.append(("+2", Fore.GREEN, f"L/S Ratio {ls_ratio:.2f} — more shorts, squeeze likely"))
    elif ls_ratio > LS_RATIO_LONG_HEAVY:
        score -= 2
        signals.append(("-2", Fore.RED, f"L/S Ratio {ls_ratio:.2f} — more longs, long squeeze risk"))
    else:
        signals.append((" 0", Fore.WHITE, f"L/S Ratio {ls_ratio:.2f} — balanced"))

    # ── 3. Open Interest Change (±1) ──
    if oi_change_pct < -3.0:
        score += 1
        signals.append(("+1", Fore.GREEN, f"OI dropping ({oi_change_pct:.2f}%) — short squeeze possible"))
    elif oi_change_pct > 3.0:
        score -= 1
        signals.append(("-1", Fore.RED, f"OI rising ({oi_change_pct:.2f}%) — new shorts being added"))
    else:
        signals.append((" 0", Fore.WHITE, f"OI change {oi_change_pct:.2f}% — stable"))

    # ── 4. Order Book Liquidity (+1) ──
    if avg_book_liq < ORDER_BOOK_THIN_THRESHOLD:
        score += 1
        signals.append(("+1", Fore.YELLOW, f"Order book THIN ({avg_book_liq:.0f} SOL/side) — moves easily"))
    else:
        signals.append((" 0", Fore.WHITE, f"Order book normal ({avg_book_liq:.0f} SOL/side)"))

    # ── 5. Volume (±1) ──
    if vol_ratio < VOLUME_LOW_RATIO:
        score += 1
        signals.append(("+1", Fore.YELLOW, f"Volume LOW ({vol_ratio:.2f}x avg) — thin liquidity"))
    elif vol_ratio > VOLUME_SPIKE_RATIO:
        score += 1
        signals.append(("+1", Fore.CYAN, f"Volume SPIKE ({vol_ratio:.2f}x avg) — unusual activity"))
    else:
        signals.append((" 0", Fore.WHITE, f"Volume normal ({vol_ratio:.2f}x avg)"))

    # ── 6. RSI — Wilder's (±2/±1) ──
    if rsi < 30:
        score += 2
        signals.append(("+2", Fore.GREEN, f"RSI OVERSOLD ({rsi}) — strong bounce candidate"))
    elif rsi > 70:
        score -= 2
        signals.append(("-2", Fore.RED, f"RSI OVERBOUGHT ({rsi}) — reversal risk high"))
    elif rsi < 45:
        score += 1
        signals.append(("+1", Fore.GREEN, f"RSI weak ({rsi}) — mild bullish lean"))
    elif rsi > 55:
        score -= 1
        signals.append(("-1", Fore.RED, f"RSI strong ({rsi}) — mild bearish lean"))
    else:
        signals.append((" 0", Fore.WHITE, f"RSI neutral ({rsi})"))

    # ── 7. Price Change 1H (±1) ──
    if price_chg_1h < -3.0:
        score += 1
        signals.append(("+1", Fore.GREEN, f"Price down {price_chg_1h:.2f}% in 1H — oversold bounce setup"))
    elif price_chg_1h > 3.0:
        score -= 1
        signals.append(("-1", Fore.RED, f"Price up {price_chg_1h:.2f}% in 1H — extended, watch reversal"))
    else:
        signals.append((" 0", Fore.WHITE, f"Price change 1H: {price_chg_1h:.2f}%"))

    # ── 8. Live Liquidations from WebSocket (±2) ──
    if liq_summary["buy_spike"]:
        score += 2
        signals.append(("+2", Fore.GREEN,
            f"SHORT LIQ SPIKE — ${liq_summary['buy_total']:,.0f} in 60s "
            f"(avg ${liq_summary['avg_buy']:,.0f}) — squeeze accelerating"))
    elif liq_summary["buy_total"] > 0:
        score += 1
        signals.append(("+1", Fore.GREEN,
            f"Short liqs active — ${liq_summary['buy_total']:,.0f} in 60s ({liq_summary['buy_count']} events)"))

    if liq_summary["sell_spike"]:
        score -= 2
        signals.append(("-2", Fore.RED,
            f"LONG LIQ SPIKE — ${liq_summary['sell_total']:,.0f} in 60s "
            f"(avg ${liq_summary['avg_sell']:,.0f}) — dump accelerating"))
    elif liq_summary["sell_total"] > 0:
        score -= 1
        signals.append(("-1", Fore.RED,
            f"Long liqs active — ${liq_summary['sell_total']:,.0f} in 60s ({liq_summary['sell_count']} events)"))

    # ══════════════════════════════════
    #   PRINT SIGNAL TABLE
    # ══════════════════════════════════
    print(f"\n  {'Score':<6} Signal")
    print(f"  {'─'*55}")
    for s, color, msg in signals:
        print(f"  {color}[{s}]  {msg}{Style.RESET_ALL}")

    # ══════════════════════════════════
    #   MARKET SNAPSHOT
    # ══════════════════════════════════
    ws_status = f"{Fore.GREEN}Connected{Style.RESET_ALL}" if ws_connected else f"{Fore.RED}Disconnected{Style.RESET_ALL}"
    print(f"\n  {Fore.CYAN}{'─'*55}")
    print(f"  Market Snapshot @ ${current_price:.4f}  |  WS: {ws_status}{Style.RESET_ALL}")
    print(f"  {'Funding Rate':<24}: {funding:.5f}")
    print(f"  {'L/S Ratio':<24}: {ls_ratio:.3f}")
    print(f"  {'OI Change (10 periods)':<24}: {oi_change_pct:.2f}%")
    print(f"  {'Bid Liquidity':<24}: {bid_liq:,.0f} SOL")
    print(f"  {'Ask Liquidity':<24}: {ask_liq:,.0f} SOL")
    print(f"  {'Volume Ratio':<24}: {vol_ratio:.2f}x avg")
    print(f"  {'RSI (15m, Wilders)':<24}: {rsi}")
    print(f"  {'Price Change 1H':<24}: {price_chg_1h:.2f}%")
    print(f"  {'Short Liqs (60s)':<24}: ${liq_summary['buy_total']:,.0f} ({liq_summary['buy_count']} events)")
    print(f"  {'Long  Liqs (60s)':<24}: ${liq_summary['sell_total']:,.0f} ({liq_summary['sell_count']} events)")

    # ══════════════════════════════════
    #   FIX #4: correct max score display
    #   Max bullish: +2+2+1+1+1+2+1+2 = +12
    #   Max bearish: -2-2-1-1-2-1-2   = -11 (volume has no -ve)
    # ══════════════════════════════════
    print(f"\n  {Fore.CYAN}{'═'*55}{Style.RESET_ALL}")
    print(f"  {Style.BRIGHT}TOTAL SCORE: {score:+d}  (max +12 / min -11){Style.RESET_ALL}")
    print(f"  {Fore.CYAN}{'═'*55}{Style.RESET_ALL}")

    # ══════════════════════════════════
    #   DECISION + TELEGRAM ALERT
    # ══════════════════════════════════
    if score >= MIN_BUY_SCORE:
        decision = "BUY SIGNAL"
        print(f"\n  {Fore.GREEN}{Style.BRIGHT}🟢 DECISION: BUY SIGNAL{Style.RESET_ALL}")
        print(f"  {Fore.GREEN}→ High short liq pressure + oversold conditions{Style.RESET_ALL}")
        print(f"  {Fore.YELLOW}  ⚠  Always use a stop loss. Not financial advice.{Style.RESET_ALL}")
        threading.Thread(target=tg_signal_alert, args=(
            score, decision, signals, current_price,
            funding, ls_ratio, rsi,
            liq_summary["buy_total"], liq_summary["sell_total"]
        ), daemon=True).start()

    elif score <= MIN_SELL_SCORE:
        decision = "SELL / SHORT SIGNAL"
        print(f"\n  {Fore.RED}{Style.BRIGHT}🔴 DECISION: SELL / SHORT SIGNAL{Style.RESET_ALL}")
        print(f"  {Fore.RED}→ High long liq pressure + overbought conditions{Style.RESET_ALL}")
        print(f"  {Fore.YELLOW}  ⚠  Always use a stop loss. Not financial advice.{Style.RESET_ALL}")
        threading.Thread(target=tg_signal_alert, args=(
            score, decision, signals, current_price,
            funding, ls_ratio, rsi,
            liq_summary["buy_total"], liq_summary["sell_total"]
        ), daemon=True).start()

    elif score >= 3:
        decision = "WEAK BUY"
        print(f"\n  {Fore.GREEN}🟡 DECISION: WEAK BUY — Conditions building bullishly.{Style.RESET_ALL}")
        print(f"  {Fore.YELLOW}  Reduce size, wait for confirmation.{Style.RESET_ALL}")
        threading.Thread(target=tg_signal_alert, args=(
            score, decision, signals, current_price,
            funding, ls_ratio, rsi,
            liq_summary["buy_total"], liq_summary["sell_total"]
        ), daemon=True).start()

    elif score <= -3:
        decision = "WEAK SELL"
        print(f"\n  {Fore.RED}🟡 DECISION: WEAK SELL — Conditions building bearishly.{Style.RESET_ALL}")
        print(f"  {Fore.YELLOW}  Caution. Watch for further deterioration.{Style.RESET_ALL}")
        threading.Thread(target=tg_signal_alert, args=(
            score, decision, signals, current_price,
            funding, ls_ratio, rsi,
            liq_summary["buy_total"], liq_summary["sell_total"]
        ), daemon=True).start()

    else:
        print(f"\n  {Fore.WHITE}⚪ DECISION: NEUTRAL — No clear edge. Stay flat.{Style.RESET_ALL}")

    print(f"\n  {Fore.CYAN}Next analysis in 60 seconds... (Ctrl+C to stop){Style.RESET_ALL}\n")
    return score

# ═══════════════════════════════════════════════════════════
#                     MAIN LOOP
# ═══════════════════════════════════════════════════════════

def main():
    start_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    print(f"""
{Fore.CYAN}{Style.BRIGHT}
╔══════════════════════════════════════════════════════════════════╗
║         SOL TRADING DECISION SYSTEM — Binance Futures API       ║
║  Scoring + Live Liquidation WebSocket + Full Telegram Alerts    ║
╚══════════════════════════════════════════════════════════════════╝
{Style.RESET_ALL}
  Symbol      : {SYMBOL}
  Buy Signal  : score ≥ {MIN_BUY_SCORE}
  Sell Signal : score ≤ {MIN_SELL_SCORE}

  Score components:
    Funding Rate      : ±2
    Long/Short Ratio  : ±2
    Open Interest     : ±1
    Order Book Depth  : +1
    Volume            : +1  (no bearish vol score)
    RSI Wilder (15m)  : ±2
    Price Change 1H   : ±1
    Live Liquidations : ±2
    ─────────────────────
    Max bullish       : +12
    Max bearish       : -11

  Telegram alerts:
    - BUY / SELL / WEAK signals (every analysis)
    - Liquidation spikes (live, from WebSocket)
    - Large single liquidations > $100k
""")

    send_telegram(
        f"🤖 <b>SOL Trading System Started</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🕐 Time    : {start_time}\n"
        f"📊 Symbol  : {SYMBOL} Futures\n"
        f"🟢 Buy at  : score ≥ {MIN_BUY_SCORE}\n"
        f"🔴 Sell at : score ≤ {MIN_SELL_SCORE}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"👀 Watching liquidations + scoring every 60s..."
    )

    start_websocket()
    wait_for_websocket(timeout=15)   # FIX #7

    try:
        while True:
            run_analysis()
            time.sleep(60)
    except (KeyboardInterrupt, SystemExit):
        stop_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"\n{Fore.YELLOW}  Stopped by user.{Style.RESET_ALL}\n")
        send_telegram(
            f"🛑 <b>SOL Trading System Stopped</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🚀 Started : {start_time}\n"
            f"🛑 Stopped : {stop_time}"
        )


if __name__ == "__main__":
    main()
