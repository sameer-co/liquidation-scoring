"""
╔══════════════════════════════════════════════════════════════════╗
║         SOL TRADING DECISION SYSTEM — Binance Public API        ║
║  Scoring + Live Liquidation WebSocket + Buy/Sell Signal Engine  ║
╚══════════════════════════════════════════════════════════════════╝

Requirements:
    pip install requests websocket-client numpy colorama

Run:
    python sol_trading_system.py
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

SYMBOL            = "SOLUSDT"
SYMBOL_LOWER      = SYMBOL.lower()

# Scoring Thresholds (tune these based on backtesting)
FUNDING_BEAR_THRESHOLD     = -0.0005   # Below = heavy shorts
FUNDING_BULL_THRESHOLD     =  0.0005   # Above = heavy longs
LS_RATIO_SHORT_HEAVY       =  0.90     # Below = more shorts
LS_RATIO_LONG_HEAVY        =  1.10     # Above = more longs
ORDER_BOOK_THIN_THRESHOLD  =  5000     # SOL per side = thin
VOLUME_LOW_RATIO           =  0.50     # Below avg = low liquidity
VOLUME_SPIKE_RATIO         =  2.00     # Above avg = spike
LIQUIDATION_WINDOW_SEC     =  60       # Rolling window for liq tracking
LIQUIDATION_SPIKE_MULT     =  2.0      # X times avg = spike alert
MIN_BUY_SCORE              =  6        # Minimum score to trigger BUY signal
MIN_SELL_SCORE             = -6        # Minimum score to trigger SELL signal

# ═══════════════════════════════════════════════════════════
#                  GLOBAL STATE
# ═══════════════════════════════════════════════════════════

liquidation_log    = deque(maxlen=500)   # stores recent liquidation events
liq_buy_amounts    = deque(maxlen=200)   # short liquidations  (price went up)
liq_sell_amounts   = deque(maxlen=200)   # long  liquidations  (price went down)
liq_lock           = threading.Lock()
last_analysis      = {}
ws_connected       = False
current_price      = 0.0

# ═══════════════════════════════════════════════════════════
#                  HELPER UTILITIES
# ═══════════════════════════════════════════════════════════

def ts():
    return datetime.now().strftime("%H:%M:%S")

def print_header(title):
    print(f"\n{Fore.CYAN}{'═'*60}")
    print(f"  {title}")
    print(f"{'═'*60}{Style.RESET_ALL}")

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
#                  BINANCE API CALLS
# ═══════════════════════════════════════════════════════════

def get_current_price():
    data = safe_get(f"https://api.binance.com/api/v3/ticker/price", {"symbol": SYMBOL})
    return float(data["price"]) if data else 0.0

def get_funding_rate():
    data = safe_get(f"https://fapi.binance.com/fapi/v1/fundingRate", {"symbol": SYMBOL, "limit": 5})
    if data:
        return float(data[-1]["fundingRate"])
    return 0.0

def get_long_short_ratio():
    data = safe_get(
        "https://fapi.binance.com/futures/data/globalLongShortAccountRatio",
        {"symbol": SYMBOL, "period": "5m", "limit": 3}
    )
    if data:
        return float(data[-1]["longShortRatio"])
    return 1.0

def get_open_interest():
    data = safe_get(f"https://fapi.binance.com/fapi/v1/openInterest", {"symbol": SYMBOL})
    if data:
        return float(data["openInterest"])
    return 0.0

def get_open_interest_history():
    """Returns list of OI values over last 10 x 5min periods"""
    data = safe_get(
        "https://fapi.binance.com/futures/data/openInterestHist",
        {"symbol": SYMBOL, "period": "5m", "limit": 10}
    )
    if data:
        return [float(d["sumOpenInterest"]) for d in data]
    return []

def get_order_book_liquidity():
    data = safe_get(f"https://api.binance.com/api/v3/depth", {"symbol": SYMBOL, "limit": 20})
    if data:
        bid_liq = sum(float(b[1]) for b in data["bids"])
        ask_liq = sum(float(a[1]) for a in data["asks"])
        return bid_liq, ask_liq
    return 0.0, 0.0

def get_volume_ratio():
    """Current candle volume vs 50-candle average"""
    data = safe_get(
        f"https://api.binance.com/api/v3/klines",
        {"symbol": SYMBOL, "interval": "5m", "limit": 51}
    )
    if data and len(data) > 5:
        volumes     = [float(k[5]) for k in data]
        current_vol = volumes[-1]
        avg_vol     = np.mean(volumes[:-1])
        return current_vol / avg_vol if avg_vol > 0 else 1.0, current_vol, avg_vol
    return 1.0, 0.0, 0.0

def get_rsi(period=14):
    data = safe_get(
        f"https://api.binance.com/api/v3/klines",
        {"symbol": SYMBOL, "interval": "15m", "limit": period + 2}
    )
    if data and len(data) > period:
        closes = [float(k[4]) for k in data]
        deltas = np.diff(closes)
        gains  = np.where(deltas > 0, deltas, 0)
        losses = np.where(deltas < 0, -deltas, 0)
        avg_gain = np.mean(gains[-period:])
        avg_loss = np.mean(losses[-period:])
        if avg_loss == 0:
            return 100.0
        rs  = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))
        return round(rsi, 2)
    return 50.0

def get_price_change_pct(minutes=60):
    limit = max(minutes // 5, 2)
    data = safe_get(
        f"https://api.binance.com/api/v3/klines",
        {"symbol": SYMBOL, "interval": "5m", "limit": limit + 1}
    )
    if data and len(data) > 1:
        open_price  = float(data[0][1])
        close_price = float(data[-1][4])
        return ((close_price - open_price) / open_price) * 100
    return 0.0

# ═══════════════════════════════════════════════════════════
#              WEBSOCKET — LIVE LIQUIDATION MONITOR
# ═══════════════════════════════════════════════════════════

def on_liquidation_message(ws, message):
    global ws_connected
    try:
        data  = json.loads(message)
        order = data.get("o", {})

        side     = order.get("S", "")       # BUY = short liq, SELL = long liq
        qty      = float(order.get("q", 0))
        price    = float(order.get("p", 0))
        usd_val  = qty * price
        now      = time.time()

        event = {
            "time"    : now,
            "side"    : side,
            "qty"     : qty,
            "price"   : price,
            "usd_val" : usd_val
        }

        with liq_lock:
            liquidation_log.append(event)
            if side == "BUY":       # short being liquidated → bullish
                liq_buy_amounts.append((now, usd_val))
            else:                   # long being liquidated → bearish
                liq_sell_amounts.append((now, usd_val))

        # ── Print live liquidation alert ──
        direction = f"{Fore.GREEN}SHORT LIQ ▲" if side == "BUY" else f"{Fore.RED}LONG  LIQ ▼"
        print(
            f"  [{ts()}] {direction}{Style.RESET_ALL}  "
            f"${usd_val:>10,.0f}  |  "
            f"{qty:.1f} SOL @ ${price:.2f}  "
            f"{'⚡ LARGE' if usd_val > 100_000 else ''}"
        )

        # ── Check if this liquidation is above average (spike) ──
        check_liquidation_spike(side, usd_val)

    except Exception as e:
        pass

def check_liquidation_spike(side, usd_val):
    """Alert if current liquidation is above rolling average by LIQUIDATION_SPIKE_MULT"""
    cutoff = time.time() - LIQUIDATION_WINDOW_SEC
    with liq_lock:
        if side == "BUY":
            recent = [v for t, v in liq_buy_amounts if t > cutoff]
        else:
            recent = [v for t, v in liq_sell_amounts if t > cutoff]

    if len(recent) > 3:
        avg = np.mean(recent[:-1])
        if avg > 0 and usd_val > avg * LIQUIDATION_SPIKE_MULT:
            label = "SHORT SQUEEZE BUILDING" if side == "BUY" else "LONG DUMP BUILDING"
            color = Fore.GREEN if side == "BUY" else Fore.RED
            print(
                f"\n  {color}🚨 LIQUIDATION SPIKE: {label}{Style.RESET_ALL}\n"
                f"     This liq: ${usd_val:,.0f} vs avg: ${avg:,.0f} "
                f"({usd_val/avg:.1f}x above average)\n"
            )

def on_ws_open(ws):
    global ws_connected
    ws_connected = True
    print(f"\n{Fore.GREEN}  ✅ WebSocket connected — Listening for {SYMBOL} liquidations...{Style.RESET_ALL}\n")

def on_ws_error(ws, error):
    global ws_connected
    ws_connected = False
    print(f"{Fore.RED}  WebSocket error: {error}{Style.RESET_ALL}")

def on_ws_close(ws, close_status_code, close_msg):
    global ws_connected
    ws_connected = False
    print(f"{Fore.YELLOW}  WebSocket closed. Reconnecting...{Style.RESET_ALL}")

def start_websocket():
    """Runs WebSocket in background thread with auto-reconnect"""
    def run():
        while True:
            try:
                ws_url = f"wss://fstream.binance.com/ws/{SYMBOL_LOWER}@forceOrder"
                ws = websocket.WebSocketApp(
                    ws_url,
                    on_message = on_liquidation_message,
                    on_open    = on_ws_open,
                    on_error   = on_ws_error,
                    on_close   = on_ws_close
                )
                ws.run_forever(ping_interval=30, ping_timeout=10)
            except Exception as e:
                print(f"{Fore.RED}  WS exception: {e}{Style.RESET_ALL}")
            time.sleep(5)  # wait before reconnect

    t = threading.Thread(target=run, daemon=True)
    t.start()

# ═══════════════════════════════════════════════════════════
#              LIQUIDATION SUMMARY (ROLLING WINDOW)
# ═══════════════════════════════════════════════════════════

def get_liquidation_summary():
    """Returns buy/sell liquidation totals and spike status for last 60s"""
    cutoff = time.time() - LIQUIDATION_WINDOW_SEC
    with liq_lock:
        buy_recent  = [(t, v) for t, v in liq_buy_amounts  if t > cutoff]
        sell_recent = [(t, v) for t, v in liq_sell_amounts if t > cutoff]

    buy_total  = sum(v for _, v in buy_recent)
    sell_total = sum(v for _, v in sell_recent)
    buy_count  = len(buy_recent)
    sell_count = len(sell_recent)

    # Historical average per 60s window (use older data)
    older_cutoff = time.time() - LIQUIDATION_WINDOW_SEC * 10
    with liq_lock:
        all_buy  = [v for t, v in liq_buy_amounts  if t > older_cutoff]
        all_sell = [v for t, v in liq_sell_amounts if t > older_cutoff]

    avg_buy_window  = (sum(all_buy)  / 10) if all_buy  else 1
    avg_sell_window = (sum(all_sell) / 10) if all_sell else 1

    buy_spike  = buy_total  > avg_buy_window  * LIQUIDATION_SPIKE_MULT
    sell_spike = sell_total > avg_sell_window * LIQUIDATION_SPIKE_MULT

    return {
        "buy_total"  : buy_total,
        "sell_total" : sell_total,
        "buy_count"  : buy_count,
        "sell_count" : sell_count,
        "buy_spike"  : buy_spike,
        "sell_spike" : sell_spike,
        "avg_buy"    : avg_buy_window,
        "avg_sell"   : avg_sell_window,
    }

# ═══════════════════════════════════════════════════════════
#                  SCORING ENGINE
# ═══════════════════════════════════════════════════════════

def run_analysis():
    global current_price

    print_header(f"SOL MARKET ANALYSIS — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    # ── Fetch all data ──
    print(f"  {Fore.CYAN}Fetching market data...{Style.RESET_ALL}")
    current_price = get_current_price()
    funding       = get_funding_rate()
    ls_ratio      = get_long_short_ratio()
    oi_history    = get_open_interest_history()
    bid_liq, ask_liq = get_order_book_liquidity()
    vol_ratio, cur_vol, avg_vol = get_volume_ratio()
    rsi           = get_rsi()
    price_chg_1h  = get_price_change_pct(60)
    liq_summary   = get_liquidation_summary()

    # OI change %
    oi_change_pct = 0.0
    if len(oi_history) >= 2:
        oi_change_pct = ((oi_history[-1] - oi_history[0]) / oi_history[0]) * 100

    avg_book_liq  = (bid_liq + ask_liq) / 2

    # ══════════════════════════════════
    #   SCORING  (+ve = bullish, -ve = bearish)
    # ══════════════════════════════════
    score   = 0
    signals = []

    # ── 1. Funding Rate ──
    if funding < FUNDING_BEAR_THRESHOLD:
        score += 2
        signals.append(("+2", Fore.GREEN, f"Funding NEGATIVE ({funding:.5f}) — shorts dominant, squeeze risk HIGH"))
    elif funding > FUNDING_BULL_THRESHOLD:
        score -= 2
        signals.append(("-2", Fore.RED, f"Funding POSITIVE ({funding:.5f}) — longs dominant, dump risk HIGH"))
    else:
        signals.append((" 0", Fore.WHITE, f"Funding NEUTRAL ({funding:.5f})"))

    # ── 2. Long/Short Ratio ──
    if ls_ratio < LS_RATIO_SHORT_HEAVY:
        score += 2
        signals.append(("+2", Fore.GREEN, f"L/S Ratio {ls_ratio:.2f} — more shorts than longs, squeeze likely"))
    elif ls_ratio > LS_RATIO_LONG_HEAVY:
        score -= 2
        signals.append(("-2", Fore.RED, f"L/S Ratio {ls_ratio:.2f} — more longs than shorts, long squeeze risk"))
    else:
        signals.append((" 0", Fore.WHITE, f"L/S Ratio {ls_ratio:.2f} — balanced"))

    # ── 3. Open Interest Change ──
    if oi_change_pct < -3.0:
        score += 1
        signals.append(("+1", Fore.GREEN, f"OI dropping ({oi_change_pct:.2f}%) — liquidations happening, short squeeze possible"))
    elif oi_change_pct > 3.0:
        score -= 1
        signals.append(("-1", Fore.RED, f"OI rising ({oi_change_pct:.2f}%) — new shorts being added, pressure building"))
    else:
        signals.append((" 0", Fore.WHITE, f"OI change {oi_change_pct:.2f}% — stable"))

    # ── 4. Order Book Liquidity ──
    if avg_book_liq < ORDER_BOOK_THIN_THRESHOLD:
        score += 1
        signals.append(("+1", Fore.YELLOW, f"Order book THIN ({avg_book_liq:.0f} SOL/side) — small orders move price easily"))
    else:
        signals.append((" 0", Fore.WHITE, f"Order book normal ({avg_book_liq:.0f} SOL/side)"))

    # ── 5. Volume ──
    if vol_ratio < VOLUME_LOW_RATIO:
        score += 1
        signals.append(("+1", Fore.YELLOW, f"Volume LOW ({vol_ratio:.2f}x avg) — thin liquidity window active"))
    elif vol_ratio > VOLUME_SPIKE_RATIO:
        score += 1
        signals.append(("+1", Fore.CYAN, f"Volume SPIKE ({vol_ratio:.2f}x avg) — unusual activity, possible cascade starting"))

    # ── 6. RSI ──
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

    # ── 7. Price Change 1H ──
    if price_chg_1h < -3.0:
        score += 1
        signals.append(("+1", Fore.GREEN, f"Price down {price_chg_1h:.2f}% in 1H — oversold bounce setup"))
    elif price_chg_1h > 3.0:
        score -= 1
        signals.append(("-1", Fore.RED, f"Price up {price_chg_1h:.2f}% in 1H — extended, reversal watch"))
    else:
        signals.append((" 0", Fore.WHITE, f"Price change 1H: {price_chg_1h:.2f}%"))

    # ── 8. Live Liquidation Data from WebSocket ──
    if liq_summary["buy_spike"]:
        score += 2
        signals.append(("+2", Fore.GREEN,
            f"SHORT LIQ SPIKE — ${liq_summary['buy_total']:,.0f} in last 60s "
            f"(avg: ${liq_summary['avg_buy']:,.0f}) — squeeze accelerating"))
    elif liq_summary["buy_total"] > 0:
        score += 1
        signals.append(("+1", Fore.GREEN,
            f"Short liquidations active — ${liq_summary['buy_total']:,.0f} in last 60s ({liq_summary['buy_count']} events)"))

    if liq_summary["sell_spike"]:
        score -= 2
        signals.append(("-2", Fore.RED,
            f"LONG LIQ SPIKE — ${liq_summary['sell_total']:,.0f} in last 60s "
            f"(avg: ${liq_summary['avg_sell']:,.0f}) — dump accelerating"))
    elif liq_summary["sell_total"] > 0:
        score -= 1
        signals.append(("-1", Fore.RED,
            f"Long liquidations active — ${liq_summary['sell_total']:,.0f} in last 60s ({liq_summary['sell_count']} events)"))

    # ══════════════════════════════════
    #   PRINT SIGNAL TABLE
    # ══════════════════════════════════
    print(f"\n  {'Score':<6} {'Signal'}")
    print(f"  {'─'*55}")
    for s, color, msg in signals:
        print(f"  {color}[{s}]  {msg}{Style.RESET_ALL}")

    # ══════════════════════════════════
    #   MARKET DATA SUMMARY
    # ══════════════════════════════════
    print(f"\n  {Fore.CYAN}{'─'*55}")
    print(f"  Market Snapshot @ ${current_price:.2f}{Style.RESET_ALL}")
    print(f"  {'Funding Rate':<22}: {funding:.5f}")
    print(f"  {'L/S Ratio':<22}: {ls_ratio:.3f}")
    print(f"  {'OI Change (10 periods)':<22}: {oi_change_pct:.2f}%")
    print(f"  {'Bid Liquidity':<22}: {bid_liq:,.0f} SOL")
    print(f"  {'Ask Liquidity':<22}: {ask_liq:,.0f} SOL")
    print(f"  {'Volume Ratio':<22}: {vol_ratio:.2f}x avg")
    print(f"  {'RSI (15m)':<22}: {rsi}")
    print(f"  {'Price Change 1H':<22}: {price_chg_1h:.2f}%")
    print(f"  {'Short Liqs (60s)':<22}: ${liq_summary['buy_total']:,.0f} ({liq_summary['buy_count']} events)")
    print(f"  {'Long Liqs (60s)':<22}: ${liq_summary['sell_total']:,.0f} ({liq_summary['sell_count']} events)")

    # ══════════════════════════════════
    #   FINAL DECISION
    # ══════════════════════════════════
    max_score = 16
    print(f"\n  {Fore.CYAN}{'═'*55}{Style.RESET_ALL}")
    print(f"  {Style.BRIGHT}TOTAL SCORE: {score} / ±{max_score}{Style.RESET_ALL}")
    print(f"  {Fore.CYAN}{'═'*55}{Style.RESET_ALL}")

    if score >= MIN_BUY_SCORE:
        print(f"\n  {Fore.GREEN}{Style.BRIGHT}🟢 DECISION: BUY SIGNAL{Style.RESET_ALL}")
        print(f"  {Fore.GREEN}Conditions strongly favour a long entry.{Style.RESET_ALL}")
        print(f"  {Fore.GREEN}→ High short liquidation pressure + oversold conditions{Style.RESET_ALL}")
        print(f"  {Fore.YELLOW}  ⚠  Always use a stop loss. This is NOT financial advice.{Style.RESET_ALL}")

    elif score <= MIN_SELL_SCORE:
        print(f"\n  {Fore.RED}{Style.BRIGHT}🔴 DECISION: SELL / SHORT SIGNAL{Style.RESET_ALL}")
        print(f"  {Fore.RED}Conditions strongly favour a short entry or exiting longs.{Style.RESET_ALL}")
        print(f"  {Fore.RED}→ High long liquidation pressure + overbought conditions{Style.RESET_ALL}")
        print(f"  {Fore.YELLOW}  ⚠  Always use a stop loss. This is NOT financial advice.{Style.RESET_ALL}")

    elif score >= 3:
        print(f"\n  {Fore.GREEN}🟡 DECISION: WEAK BUY — Conditions building bullishly.{Style.RESET_ALL}")
        print(f"  {Fore.YELLOW}  Reduce size, wait for confirmation.{Style.RESET_ALL}")

    elif score <= -3:
        print(f"\n  {Fore.RED}🟡 DECISION: WEAK SELL — Conditions building bearishly.{Style.RESET_ALL}")
        print(f"  {Fore.YELLOW}  Caution. Watch for further deterioration.{Style.RESET_ALL}")

    else:
        print(f"\n  {Fore.WHITE}⚪ DECISION: NEUTRAL — No clear edge. Stay flat.{Style.RESET_ALL}")

    print(f"\n  {Fore.CYAN}Next analysis in 60 seconds... (Ctrl+C to stop){Style.RESET_ALL}\n")

    return score

# ═══════════════════════════════════════════════════════════
#                     MAIN LOOP
# ═══════════════════════════════════════════════════════════

def main():
    print(f"""
{Fore.CYAN}{Style.BRIGHT}
╔══════════════════════════════════════════════════════════════════╗
║         SOL TRADING DECISION SYSTEM — Binance Public API        ║
║  Scoring + Live Liquidation WebSocket + Buy/Sell Signal Engine  ║
╚══════════════════════════════════════════════════════════════════╝
{Style.RESET_ALL}
  Symbol  : {SYMBOL}
  Buy Signal at score  ≥ {MIN_BUY_SCORE}
  Sell Signal at score ≤ {MIN_SELL_SCORE}

  Score Components:
    Funding Rate     : ±2 pts
    Long/Short Ratio : ±2 pts
    Open Interest    : ±1 pt
    Order Book Depth : ±1 pt
    Volume           : ±1 pt
    RSI (15m)        : ±2 pts
    Price Change 1H  : ±1 pt
    Live Liquidations: ±2 pts  ← from WebSocket
    ─────────────────────────
    Max possible     : ±12 pts
""")

    # Start WebSocket in background
    print(f"  {Fore.CYAN}Starting WebSocket liquidation monitor...{Style.RESET_ALL}")
    start_websocket()
    time.sleep(2)  # give WS time to connect

    # Main analysis loop
    try:
        while True:
            run_analysis()
            time.sleep(60)
    except KeyboardInterrupt:
        print(f"\n{Fore.YELLOW}  Stopped by user.{Style.RESET_ALL}\n")

if __name__ == "__main__":
    main()
