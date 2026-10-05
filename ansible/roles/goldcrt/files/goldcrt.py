"""Gold CRT sweep alert: a Telegram message when a 1H XAUUSD candle sweeps the
previous candle's high or low into a key level and closes back inside it.

The rules are a line-by-line port of the TradingView indicator
crt_sweep_alert.pine (Desktop\\gold-crt-alert). Keep the two in step: the
indicator draws the levels for review, this service sends the alerts, because
TradingView's free plan allows no indicator alerts.

Standard library only, like the llmobs collector. Every hour it pulls the last
few thousand H1 candles from Twelve Data, replays the rules over all of them
(so a restart never loses state) and reports a signal on the candle that just
closed. Over the 10,400 candles to 2 Oct 2026 it gave 301 buys / 293 sells;
the indicator on TradingView's OANDA:XAUUSD chart gave 305 / 283.

    python goldcrt.py                 run the service
    python goldcrt.py backtest 5000   replay N candles and print the signals
"""

import json
import os
import statistics
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

UTC = timezone.utc


# ── Rules (mirror of the indicator's inputs and defaults) ──────────────────
@dataclass
class Settings:
    need_c2_color: bool = True      # candle 2 closes in trade direction
    need_c1_color: bool = False     # candle 1 is the opposite colour
    tol_atr: float = 0.0            # near-miss tolerance, × ATR
    use_fvg: bool = True
    use_ifvg: bool = True
    use_ob: bool = True
    use_bb: bool = True
    use_swing: bool = True
    use_pd: bool = True
    use_pw: bool = True
    first_touch: bool = True        # fresh zones only
    swing_len: int = 5
    fvg_min_atr: float = 0.1
    max_age: int = 500

    def kind_on(self, kind):
        return {"FVG": self.use_fvg, "IFVG": self.use_ifvg, "OB": self.use_ob, "BB": self.use_bb}.get(kind, False)


@dataclass
class Bar:
    t: int          # candle open, unix seconds UTC
    o: float
    h: float
    l: float
    c: float
    day: tuple = ()
    week: tuple = ()


@dataclass
class Zone:
    top: float
    bottom: float
    dir: int        # 1 support (buys), -1 resistance (sells)
    kind: str
    born: int
    touched: int = None


@dataclass
class Level:
    price: float
    dir: int        # 1 a low (buys), -1 a high (sells)
    kind: str
    born: int


@dataclass
class Signal:
    index: int
    t: int
    side: str
    tags: list = field(default_factory=list)
    info: list = field(default_factory=list)
    c1_high: float = 0.0
    c1_low: float = 0.0
    wick: float = 0.0
    close: float = 0.0


def _ny_offset(dt):
    """UTC offset of New York in hours (-4 in summer time, -5 otherwise)."""
    y = dt.year
    mar8 = datetime(y, 3, 8, 7, tzinfo=UTC)     # 2nd Sunday of March, 02:00 EST
    start = mar8 + timedelta(days=(6 - mar8.weekday()) % 7)
    nov1 = datetime(y, 11, 1, 6, tzinfo=UTC)    # 1st Sunday of November, 02:00 EDT
    end = nov1 + timedelta(days=(6 - nov1.weekday()) % 7)
    return -4 if start <= dt < end else -5


def _session_time(t):
    """Gold trades Sunday 18:00 to Friday 17:00 New York, with a break from
    17:00 to 18:00 each day, and its trading day and week roll over at 17:00,
    like TradingView's and OANDA's daily candles. Shifted by +7 h, the rollover
    falls at midnight: the calendar date and ISO week name the session, the
    daily break is hour 0 and the weekend is Saturday and Sunday."""
    dt = datetime.fromtimestamp(t, UTC)
    return dt + timedelta(hours=_ny_offset(dt) + 7)


def in_session(t):
    s = _session_time(t)
    return s.weekday() < 5 and s.hour > 0


def tag_sessions(bars):
    """Tag each candle with its trading day and week, and drop the ones the
    OANDA chart doesn't have. Twelve Data keeps publishing filler candles that
    move a few cents while the market is shut: weekends, the daily break, and
    whole holidays (Good Friday, Christmas, New Year)."""
    out = []
    for b in bars:
        if not in_session(b.t) or b.h == b.l:
            continue
        s = _session_time(b.t)
        b.day = (s.year, s.month, s.day)
        b.week = tuple(s.isocalendar()[:2])
        out.append(b)
    ranges = {}
    for b in out:
        ranges.setdefault(b.day, []).append(b.h - b.l)
    # An open session's typical hour moves $10-20; a holiday's moves cents.
    shut = {d for d, r in ranges.items() if statistics.median(r) < 1.0}
    return [b for b in out if b.day not in shut]


def run(bars, cfg=None):
    """Replay the indicator bar by bar. Same order of steps as the Pine
    script: check candle 2 against the levels as they stood at candle 1's
    close, then update the levels with candle 2."""
    cfg = cfg or Settings()
    L = cfg.swing_len

    # Previous day / week high-low, from the H1 candles of that session.
    day_hl, week_hl, day_order, week_order = {}, {}, [], []
    for b in bars:
        if b.day not in day_hl:
            day_hl[b.day] = [b.h, b.l]
            day_order.append(b.day)
        else:
            day_hl[b.day][0] = max(day_hl[b.day][0], b.h)
            day_hl[b.day][1] = min(day_hl[b.day][1], b.l)
        if b.week not in week_hl:
            week_hl[b.week] = [b.h, b.l]
            week_order.append(b.week)
        else:
            week_hl[b.week][0] = max(week_hl[b.week][0], b.h)
            week_hl[b.week][1] = min(week_hl[b.week][1], b.l)
    prev_day = {d: day_order[k - 1] for k, d in enumerate(day_order) if k > 0}
    prev_week = {w: week_order[k - 1] for k, w in enumerate(week_order) if k > 0}

    zones, levels, signals = [], [], []
    last_ph = last_pl = None
    last_ph_bar = last_pl_bar = None
    ph_broken = pl_broken = True
    d_hi = d_lo = w_hi = w_lo = None
    atr, tr_first = None, []

    for i, b in enumerate(bars):
        p = bars[i - 1] if i > 0 else None
        if p is not None and b.day != p.day:
            d_hi = d_lo = None
        if p is not None and b.week != p.week:
            w_hi = w_lo = None

        # ta.atr(14): RMA of true range, seeded with the simple average.
        tr = b.h - b.l if p is None else max(b.h - b.l, abs(b.h - p.c), abs(b.l - p.c))
        if atr is None:
            tr_first.append(tr)
            if len(tr_first) == 14:
                atr = sum(tr_first) / 14
        else:
            atr = (atr * 13 + tr) / 14
        tol = atr * cfg.tol_atr if atr is not None else None

        # ta.pivothigh / ta.pivotlow(L, L): confirmed L bars after the pivot.
        ph = pl = None
        if i >= 2 * L:
            c = i - L
            hc, lc = bars[c].h, bars[c].l
            others = [bars[j] for j in range(c - L, i + 1) if j != c]
            if all(hc > o.h for o in others):
                ph = hc
            if all(lc < o.l for o in others):
                pl = lc

        pd = prev_day.get(b.day)
        pw = prev_week.get(b.week)
        pdh, pdl = (day_hl[pd] if pd else (None, None))
        pwh, pwl = (week_hl[pw] if pw else (None, None))

        # ── 1. CRT check on candle 2 ──
        if p is not None and tol is not None:
            crt_buy = (b.l < p.l and p.l < b.c < p.h
                       and (not cfg.need_c2_color or b.c > b.o)
                       and (not cfg.need_c1_color or p.c < p.o))
            crt_sell = (b.h > p.h and p.l < b.c < p.h
                        and (not cfg.need_c2_color or b.c < b.o)
                        and (not cfg.need_c1_color or p.c > p.o))
            for d, on in ((1, crt_buy), (-1, crt_sell)):
                if not on:
                    continue
                tags, info = [], []
                for z in zones:
                    fresh = not cfg.first_touch or z.touched is None or z.touched == i - 1
                    if z.dir == d and cfg.kind_on(z.kind) and fresh:
                        tapped = (b.l <= z.top + tol and z.bottom <= p.l) if d == 1 \
                            else (b.h >= z.bottom - tol and z.top >= p.h)
                        if tapped:
                            if z.kind not in tags:
                                tags.append(z.kind)
                            info.append(f"{'Bullish' if d == 1 else 'Bearish'} {z.kind} {z.bottom:.2f}-{z.top:.2f}")
                if cfg.use_swing:
                    for lv in levels:
                        if lv.dir == d:
                            swept = (b.l < lv.price + tol and b.c > lv.price) if d == 1 \
                                else (b.h > lv.price - tol and b.c < lv.price)
                            if swept:
                                if lv.kind not in tags:
                                    tags.append(lv.kind)
                                info.append(f"{lv.kind} {lv.price:.2f}")
                if cfg.use_pd and pdl is not None:
                    if d == 1 and b.l < pdl + tol and b.c > pdl and (d_lo is None or d_lo > pdl):
                        tags.append("PDL")
                        info.append(f"Previous day low {pdl:.2f}")
                    if d == -1 and b.h > pdh - tol and b.c < pdh and (d_hi is None or d_hi < pdh):
                        tags.append("PDH")
                        info.append(f"Previous day high {pdh:.2f}")
                if cfg.use_pw and pwl is not None:
                    if d == 1 and b.l < pwl + tol and b.c > pwl and (w_lo is None or w_lo > pwl):
                        tags.append("PWL")
                        info.append(f"Previous week low {pwl:.2f}")
                    if d == -1 and b.h > pwh - tol and b.c < pwh and (w_hi is None or w_hi < pwh):
                        tags.append("PWH")
                        info.append(f"Previous week high {pwh:.2f}")
                if tags:
                    signals.append(Signal(i, b.t, "BUY" if d == 1 else "SELL", tags, info,
                                          p.h, p.l, b.l if d == 1 else b.h, b.c))

        # ── 2. Update the key levels with candle 2 ──
        # A close through the far side kills a zone; FVG flips to IFVG, OB to BB.
        for k in range(len(zones) - 1, -1, -1):
            z = zones[k]
            broken = b.c < z.bottom if z.dir == 1 else b.c > z.top
            stale = i - z.born > cfg.max_age
            if broken or stale:
                del zones[k]
                if broken and not stale and z.kind in ("FVG", "OB"):
                    zones.append(Zone(z.top, z.bottom, -z.dir, "IFVG" if z.kind == "FVG" else "BB", i))
            elif z.touched is None and (b.l <= z.top if z.dir == 1 else b.h >= z.bottom):
                z.touched = i

        # Liquidity is gone once price trades through it.
        levels = [lv for lv in levels
                  if not ((b.l < lv.price if lv.dir == 1 else b.h > lv.price) or i - lv.born > cfg.max_age)]

        d_hi = b.h if d_hi is None else max(d_hi, b.h)
        d_lo = b.l if d_lo is None else min(d_lo, b.l)
        w_hi = b.h if w_hi is None else max(w_hi, b.h)
        w_lo = b.l if w_lo is None else min(w_lo, b.l)

        # New fair value gaps ending on this candle.
        if i >= 2 and atr is not None:
            q = bars[i - 2]
            fvg_min = cfg.fvg_min_atr * atr
            if b.l > q.h and b.l - q.h >= fvg_min:
                zones.append(Zone(b.l, q.h, 1, "FVG", i - 2))
            if b.h < q.l and q.l - b.h >= fvg_min:
                zones.append(Zone(q.l, b.h, -1, "FVG", i - 2))

        # New swing points become liquidity levels.
        if pl is not None:
            kind = "HL" if last_pl is not None and pl > last_pl else "SL"
            last_pl, last_pl_bar, pl_broken = pl, i - L, False
            levels.append(Level(pl, 1, kind, last_pl_bar))
        if ph is not None:
            kind = "LH" if last_ph is not None and ph < last_ph else "SH"
            last_ph, last_ph_bar, ph_broken = ph, i - L, False
            levels.append(Level(ph, -1, kind, last_ph_bar))

        # Break of structure → order block (extreme candle of the breaking leg).
        if not ph_broken and b.c > last_ph:
            ph_broken = True
            span = min(i - last_ph_bar, 300)
            idx = 1
            for k in range(2, span + 1):
                if bars[i - k].l < bars[i - idx].l:
                    idx = k
            ob = bars[i - idx]
            zones.append(Zone(ob.h, ob.l, 1, "OB", i - idx))
        if not pl_broken and b.c < last_pl:
            pl_broken = True
            span = min(i - last_pl_bar, 300)
            idx = 1
            for k in range(2, span + 1):
                if bars[i - k].h > bars[i - idx].h:
                    idx = k
            ob = bars[i - idx]
            zones.append(Zone(ob.h, ob.l, -1, "OB", i - idx))

        while len(zones) > 150:
            zones.pop(0)
        while len(levels) > 60:
            levels.pop(0)

    return signals


# ── Data: Twelve Data XAU/USD spot candles ─────────────────────────────────
# OANDA would match TradingView's OANDA:XAUUSD chart exactly, but its EU demo
# accounts are MT5-only, with no API. Twelve Data's free plan allows 800
# requests a day; the service uses about 30.
def fetch_candles(count):
    """The last `count` completed H1 candles in trading hours, oldest first."""
    need = int(count * 1.4) + 100    # about 30% of Twelve Data's hours are filler
    out, end = [], None
    while len(out) < need:
        q = {"symbol": os.environ.get("GOLD_SYMBOL", "XAU/USD"), "interval": "1h", "timezone": "UTC",
             "outputsize": min(5000, need - len(out) + 2), "apikey": os.environ["TWELVEDATA_KEY"]}
        if end is not None:
            q["end_date"] = end
        with urllib.request.urlopen(f"https://api.twelvedata.com/time_series?{urllib.parse.urlencode(q)}",
                                    timeout=30) as r:
            data = json.load(r)
        if data.get("status") != "ok":
            raise RuntimeError(f"twelvedata: {data.get('message', data)}")
        now = time.time()
        page = []
        for v in reversed(data["values"]):     # newest first in the reply
            t = int(datetime.strptime(v["datetime"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC).timestamp())
            # The newest value is the candle still forming; keep closed ones only.
            if t + 3600 <= now and (not out or t < out[0].t):
                page.append(Bar(t, float(v["open"]), float(v["high"]), float(v["low"]), float(v["close"])))
        if not page:
            break
        out = page + out
        end = datetime.fromtimestamp(out[0].t, UTC).strftime("%Y-%m-%d %H:%M:%S")
    return tag_sessions(out)[-count:]


# ── Telegram ───────────────────────────────────────────────────────────────
def local_time(ts):
    try:
        from zoneinfo import ZoneInfo
        return datetime.fromtimestamp(ts, ZoneInfo(os.environ.get("TZ_DISPLAY", "Europe/Berlin")))
    except Exception:
        return datetime.fromtimestamp(ts, UTC)


def message(s):
    start = local_time(s.t)
    end = start + timedelta(hours=1)
    head = "🟢 BUY CRT" if s.side == "BUY" else "🔴 SELL CRT"
    wick = "Wick low" if s.side == "BUY" else "Wick high"
    return (f"{head} · XAUUSD 1H\n"
            f"Candle {start:%a %d %b %H:%M}–{end:%H:%M}\n"
            f"Swept: {', '.join(s.info)}\n"
            f"CRT high {s.c1_high:.2f} | CRT low {s.c1_low:.2f}\n"
            f"{wick} {s.wick:.2f} | Close {s.close:.2f}")


def send(text):
    token = os.environ["TELEGRAM_TOKEN"]
    for chat in os.environ["TELEGRAM_CHAT_IDS"].split(","):
        body = urllib.parse.urlencode({"chat_id": chat.strip(), "text": text}).encode()
        for attempt in range(3):
            try:
                urllib.request.urlopen(f"https://api.telegram.org/bot{token}/sendMessage", body, timeout=20).read()
                break
            except Exception as e:
                log(f"telegram to {chat.strip()} failed (try {attempt + 1}): {e}")
                time.sleep(5 * (attempt + 1))


def heartbeat(msg):
    url = os.environ.get("KUMA_PUSH_URL")
    if url:
        try:
            urllib.request.urlopen(f"{url}?status=up&msg={urllib.parse.quote(msg)}", timeout=10).read()
        except Exception as e:
            log(f"kuma push failed: {e}")


def log(msg):
    print(f"{datetime.now(UTC):%Y-%m-%d %H:%M:%S}Z {msg}", flush=True)


# ── Service loop ───────────────────────────────────────────────────────────
STATE = os.environ.get("STATE_FILE", "/data/state.json")


def load_state():
    try:
        with open(STATE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(state):
    tmp = STATE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.replace(tmp, STATE)


def check(state, count):
    bars = fetch_candles(count)
    latest = bars[-1].t
    if "last_bar" not in state:
        state["last_bar"] = latest      # first start: never replay old signals
        save_state(state)
        return f"started at candle {latest}"
    if latest <= state["last_bar"]:
        return "no new candle (market closed?)"
    # Signals on candles closed since the last check, at most two hours back,
    # so an outage never floods the chat with stale setups.
    fresh = [s for s in run(bars) if s.t > state["last_bar"] and s.t >= latest - 2 * 3600]
    for s in fresh:
        log(f"signal {s.side} {s.t} {s.tags}")
        send(message(s))
    state["last_bar"] = latest
    save_state(state)
    return f"checked candle {latest}, {len(fresh)} signal(s)"


def main():
    count = int(os.environ.get("HISTORY_BARS", "3000"))
    state = load_state()
    log(f"goldcrt up, replaying {count} candles per check")
    while True:
        now = time.time()
        expected = int(now // 3600) * 3600 - 3600   # open time of the candle that just closed
        for attempt in range(4):
            try:
                result = check(state, count)
                log(result)
                heartbeat(result)
                # No candle to wait for while the market is shut.
                if state.get("last_bar", 0) >= expected or not in_session(expected) or attempt == 3:
                    break
            except Exception as e:
                log(f"check failed: {e}")
            time.sleep(30)                        # candle not published yet, or a network blip
        nxt = (int(time.time()) // 3600 + 1) * 3600 + 20
        time.sleep(max(5, nxt - time.time()))


def backtest(count):
    bars = fetch_candles(count)
    sigs = run(bars)
    buys = sum(s.side == "BUY" for s in sigs)
    print(f"{len(bars)} candles {datetime.fromtimestamp(bars[0].t, UTC):%Y-%m-%d} .. "
          f"{datetime.fromtimestamp(bars[-1].t, UTC):%Y-%m-%d %H:%M}Z | buys {buys} | sells {len(sigs) - buys} | "
          f"per 23 bars {len(sigs) * 23 / len(bars):.2f}")
    for s in sigs[-8:]:
        print("---\n" + message(s))


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "backtest":
        backtest(int(sys.argv[2]) if len(sys.argv) > 2 else 5000)
    else:
        main()
