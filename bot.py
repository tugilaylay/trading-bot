"""
Kripto Trading Bot - Sunucu Versiyonu (GitHub Actions ile çalışır)
RSI + MACD + Bollinger + Hacim stratejisi
Binance Testnet'e GERÇEK emir gönderir (testnet parası, risk yok)
Telegram bildirimi gönderir

Her çalıştığında:
1. Pozisyon durumunu state.json'dan okur (repo içinde saklanır)
2. Göstergeleri hesaplar
3. Karar verir: AL / SAT / BEKLE
4. Gerçek testnet emri gönderir
5. state.json'u güncelleyip commit eder (workflow bunu yapar)
6. Telegram'a bildirim yollar
"""

import os
import json
import time
import hmac
import hashlib
import urllib.request
import urllib.parse
from datetime import datetime, timezone

# ───────── Ayarlar (GitHub Secrets'tan okunur) ─────────
API_KEY = os.environ.get("BINANCE_TESTNET_API_KEY", "")
SECRET_KEY = os.environ.get("BINANCE_TESTNET_SECRET_KEY", "")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# ───────── Bot parametreleri ─────────
COINS = ["BTCUSDT", "ETHUSDT"]
INTERVAL = "5m"
TRADE_AMOUNT_USDT = 20      # her işlemde kullanılacak USDT miktarı
STOP_LOSS_PCT = 0.02        # %2
TAKE_PROFIT_PCT = 0.04      # %4
MIN_CONFIRM = 2             # 4 göstergeden en az kaçı onaylamalı
MIN_HOLD_MINUTES = 5        # gösterge bazlı çıkış için minimum bekleme süresi

BASE_URL = "https://testnet.binance.vision"
STATE_FILE = "state.json"


# ═══════════════════════════════════════════
# Telegram
# ═══════════════════════════════════════════
def send_telegram(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("⚠️ Telegram ayarlanmamış, mesaj gönderilmiyor:", message)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    data = urllib.parse.urlencode({
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML"
    }).encode()
    try:
        req = urllib.request.Request(url, data=data, method="POST")
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        print("Telegram gönderme hatası:", e)


# ═══════════════════════════════════════════
# Binance Testnet API (imzalı istekler)
# ═══════════════════════════════════════════
def binance_request(path, params=None, signed=False, method="GET"):
    params = params or {}
    if signed:
        params["timestamp"] = int(time.time() * 1000)
        params["recvWindow"] = 5000
        query = urllib.parse.urlencode(params)
        signature = hmac.new(SECRET_KEY.encode(), query.encode(), hashlib.sha256).hexdigest()
        query += f"&signature={signature}"
    else:
        query = urllib.parse.urlencode(params)

    url = f"{BASE_URL}{path}?{query}" if query else f"{BASE_URL}{path}"
    headers = {"X-MBX-APIKEY": API_KEY} if signed else {}

    req = urllib.request.Request(url, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        print(f"⚠️ Binance API hatası ({path}):", body)
        return None
    except Exception as e:
        print(f"⚠️ Binance bağlantı hatası ({path}):", e)
        return None


def get_klines(symbol, interval, limit=100):
    return binance_request("/api/v3/klines", {"symbol": symbol, "interval": interval, "limit": limit})


def get_price(symbol):
    data = binance_request("/api/v3/ticker/price", {"symbol": symbol})
    return float(data["price"]) if data else None


def place_market_order(symbol, side, quote_qty=None, qty=None):
    """side: 'BUY' veya 'SELL'. quote_qty: USDT cinsinden (BUY için), qty: coin cinsinden (SELL için)."""
    params = {"symbol": symbol, "side": side, "type": "MARKET"}
    if quote_qty:
        params["quoteOrderQty"] = round(quote_qty, 2)
    if qty:
        params["quantity"] = qty
    return binance_request("/api/v3/order", params, signed=True, method="POST")


# ═══════════════════════════════════════════
# Göstergeler
# ═══════════════════════════════════════════
def calc_rsi(closes, period=14):
    if len(closes) < period + 1:
        return 50
    gains, losses = 0, 0
    for i in range(len(closes) - period, len(closes)):
        diff = closes[i] - closes[i - 1]
        if diff > 0:
            gains += diff
        else:
            losses -= diff
    avg_gain, avg_loss = gains / period, losses / period
    if avg_loss == 0:
        return 100
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def calc_ema(closes, period):
    k = 2 / (period + 1)
    ema = closes[0]
    for price in closes[1:]:
        ema = price * k + ema * (1 - k)
    return ema


def calc_macd(closes):
    ema12 = calc_ema(closes, 12)
    ema26 = calc_ema(closes, 26)
    macd = ema12 - ema26
    signal = macd * 0.85
    return macd, macd - signal


def calc_bollinger(closes, period=20):
    slice_ = closes[-period:]
    ma = sum(slice_) / period
    variance = sum((x - ma) ** 2 for x in slice_) / period
    std = variance ** 0.5
    return ma + 2 * std, ma - 2 * std


def calc_volume_ratio(klines):
    vols = [float(k[5]) for k in klines]
    avg_vol = sum(vols[-20:]) / 20
    last_vol = vols[-1]
    return last_vol / avg_vol if avg_vol else 1


def evaluate_signals(klines, price):
    closes = [float(k[4]) for k in klines]
    rsi = calc_rsi(closes)
    macd, histogram = calc_macd(closes)
    upper, lower = calc_bollinger(closes)
    vol_ratio = calc_volume_ratio(klines)

    buy_count, sell_count = 0, 0
    if rsi < 35: buy_count += 1
    elif rsi > 65: sell_count += 1
    if histogram > 0 and macd > 0: buy_count += 1
    elif histogram < 0 and macd < 0: sell_count += 1
    if price <= lower: buy_count += 1
    elif price >= upper: sell_count += 1
    if vol_ratio > 1.5: buy_count += 1
    elif vol_ratio < 0.7: sell_count += 1

    return {
        "rsi": rsi, "macd": macd, "vol_ratio": vol_ratio,
        "buy_count": buy_count, "sell_count": sell_count
    }


# ═══════════════════════════════════════════
# State (pozisyon hafızası, repo içinde saklanır)
# ═══════════════════════════════════════════
def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    return {"positions": {}, "trades": [], "total_pnl": 0}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


# ═══════════════════════════════════════════
# Ana mantık - her coin için
# ═══════════════════════════════════════════
def process_coin(symbol, state):
    klines = get_klines(symbol, INTERVAL)
    price = get_price(symbol)
    if not klines or not price:
        print(f"⚠️ {symbol}: veri alınamadı")
        return

    sig = evaluate_signals(klines, price)
    position = state["positions"].get(symbol)
    last_candle_close = klines[-1][6]

    print(f"{symbol}: fiyat={price:.2f} RSI={sig['rsi']:.1f} AL={sig['buy_count']} SAT={sig['sell_count']}")

    if not position and sig["buy_count"] >= MIN_CONFIRM:
        # GERÇEK testnet AL emri
        order = place_market_order(symbol, "BUY", quote_qty=TRADE_AMOUNT_USDT)
        if order and order.get("status") == "FILLED":
            executed_qty = float(order["executedQty"])
            avg_price = sum(float(f["price"]) * float(f["qty"]) for f in order["fills"]) / executed_qty
            state["positions"][symbol] = {
                "entry_price": avg_price,
                "qty": executed_qty,
                "amount_usdt": TRADE_AMOUNT_USDT,
                "opened_at": datetime.now(timezone.utc).isoformat(),
                "entry_candle_close": last_candle_close,
                "confirms": sig["buy_count"]
            }
            msg = f"🟢 <b>{symbol} AL EMRİ</b>\nFiyat: {avg_price:.2f}\nMiktar: {executed_qty}\nOnay: {sig['buy_count']}/4"
            send_telegram(msg)
            print(msg)
        else:
            print(f"⚠️ {symbol} AL emri başarısız:", order)

    elif position:
        change = (price - position["entry_price"]) / position["entry_price"]
        opened_at = datetime.fromisoformat(position["opened_at"])
        elapsed_min = (datetime.now(timezone.utc) - opened_at).total_seconds() / 60
        new_candle = last_candle_close > position["entry_candle_close"]

        hit_sl = change <= -STOP_LOSS_PCT
        hit_tp = change >= TAKE_PROFIT_PCT
        hit_signal = new_candle and elapsed_min >= MIN_HOLD_MINUTES and sig["sell_count"] >= MIN_CONFIRM

        if hit_sl or hit_tp or hit_signal:
            order = place_market_order(symbol, "SELL", qty=position["qty"])
            if order and order.get("status") == "FILLED":
                executed_qty = float(order["executedQty"])
                avg_price = sum(float(f["price"]) * float(f["qty"]) for f in order["fills"]) / executed_qty
                pnl = (avg_price - position["entry_price"]) * position["qty"]
                reason = "Stop-Loss" if hit_sl else "Take-Profit" if hit_tp else f"{sig['sell_count']}/4 onay"

                state["trades"].append({
                    "symbol": symbol, "entry_price": position["entry_price"],
                    "exit_price": avg_price, "qty": position["qty"],
                    "pnl": pnl, "reason": reason,
                    "closed_at": datetime.now(timezone.utc).isoformat()
                })
                state["total_pnl"] += pnl
                del state["positions"][symbol]

                emoji = "✅" if pnl >= 0 else "🔴"
                msg = f"{emoji} <b>{symbol} POZİSYON KAPANDI</b>\nSebep: {reason}\nGiriş: {position['entry_price']:.2f} → Çıkış: {avg_price:.2f}\nK/Z: {pnl:+.2f} USDT\nToplam K/Z: {state['total_pnl']:+.2f} USDT"
                send_telegram(msg)
                print(msg)
            else:
                print(f"⚠️ {symbol} SAT emri başarısız:", order)


def main():
    if not API_KEY or not SECRET_KEY:
        print("❌ API_KEY / SECRET_KEY eksik! GitHub Secrets kontrol et.")
        return

    state = load_state()
    for symbol in COINS:
        try:
            process_coin(symbol, state)
        except Exception as e:
            print(f"❌ {symbol} işlenirken hata:", e)
            send_telegram(f"❌ Bot hatası ({symbol}): {e}")
    save_state(state)
    print("✅ Tur tamamlandı.")


if __name__ == "__main__":
    main()
