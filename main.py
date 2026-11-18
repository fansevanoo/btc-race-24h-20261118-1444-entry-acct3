"""TideTurn — a long-only dip-and-bounce bot for the 24h BTC race.

The bot watches CBBTC minute bars and takes at most three full-size
entries per window: up to one sharp dip buy and up to two capitulation
bounce buys. A dip is a fast fall from a short-window high that is not
a capitulation; a bounce is a deep fall from a long-window high that
prints a long lower wick and then reverses with RSI turning up. Every
position carries a fixed stop set at decision time plus a trailing
stop that arms once the trade has moved in our favour. No trade is
taken before 62 closes of history, after bar 1320, inside a cooldown,
or once equity has fallen 2% below the start.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))
import makefun_bars

ASSET = "CBBTC"
UNIT = 10_000  # basis points per unit

# --- bounce (capitulation reversal) ---
BOUNCE_FALL_NEAR = 150      # 60-bar close-to-close fall, bps
BOUNCE_FALL_FAR = 250       # 120-bar close-to-close fall, bps
BOUNCE_FALL_DEEP = 550      # fall from the 720-bar high, bps
WICK_MIN_SPAN = 20          # wick bar range, bps
WICK_TAIL = 60              # close in the top 40% of the bar
WICK_WINDOW = 20            # bars back to look for a wick
BOUNCE_RISE = 60            # reversal bar up, bps
BOUNCE_STOP_UNDER = 20      # stop below the trough, bps
BOUNCE_STOP_FLOOR = 100     # stop at most 100 bps under entry
BOUNCE_STOP_CEIL = 50       # stop at least 50 bps under entry
BOUNCE_ARM = 80             # trailing arms at +80 bps
BOUNCE_TRAIL = 80           # ... then trails the peak by 80 bps
BOUNCE_HOLD = 300           # max bars held
BOUNCE_CAP = 2              # max bounce trades

# --- dip (short-window sharp drop) ---
DIP_DROP_NEAR = 120         # fall from the 60-bar high, bps
DIP_DROP_DEEP = 550         # 720-bar fall must stay under this
DIP_RSI_TOP = 50            # RSI must sit under 50
DIP_SHARP = 150             # 60-bar close-to-close fall, bps
DIP_STOP = 60               # hard stop under the entry, bps
DIP_ARM = 80
DIP_TRAIL = 80
DIP_HOLD = 300
DIP_CAP = 1

# --- session guards ---
MAX_ENTRIES = 3
MAX_BOUNCE_MISSES = 2
EQUITY_GIVEUP_BPS = 200
REST_AFTER_EXIT = 60
LAST_ENTRY_BAR = 1320
SLIP_LIMIT = 200
MIN_HISTORY = 62
RSI_SPAN = 14

# --- per-run state ---
closes = []                 # closes, newest last
highs = []                  # highs, newest last
lows = []                   # lows, newest last
rsi_track = []              # RSI(14) per bar, None until enough closes
_gain_avg = None            # Wilder RSI running averages
_loss_avg = None
_seen_t = None              # last batch time seen (duplicate guard)
_start_cash = None          # start capital from hello
_dip_count = 0
_bounce_count = 0
_bounce_misses = 0          # consecutive bounce losses
_rest_until = -1            # bar seq before which no new entry
_waiting = None             # order decided this batch, awaiting its fill
_trade = None               # open position, or None


def _bps_down(reference, value):
    """How far value sits below reference, in basis points."""
    if reference <= 0:
        return 0
    return (reference - value) * UNIT // reference


def _close_fall(bars_back):
    """Close-to-close fall over bars_back bars, measured at the previous bar.

    Returns 0 when the history is too short, so an early bar can never
    fire a setup on data that does not exist yet.
    """
    need = bars_back + 2
    if len(closes) < need:
        return 0
    return _bps_down(closes[-need], closes[-2])


def _high_fall(span):
    """Fall from the highest high of the last span bars to the previous close.

    The window ends just before the previous bar. Returns 0 when the
    history is too short for the span.
    """
    if len(highs) < span + 1:
        return 0
    top = max(highs[-span - 1:-1])
    if top <= 0:
        return 0
    return (top - closes[-2]) * UNIT // top


def _wick_seen():
    """A long lower-wick bar printed within the last WICK_WINDOW bars."""
    n = len(closes)
    first = max(0, n - WICK_WINDOW)
    for j in range(first, n):
        hi = highs[j]
        lo = lows[j]
        c = closes[j]
        if hi <= lo or c <= 0:
            continue
        span = (hi - lo) * UNIT // c
        if span < WICK_MIN_SPAN:
            continue
        # close in the top 40% of the range: lower wick >= 60% of it.
        if (c - lo) * 100 // (hi - lo) >= WICK_TAIL:
            return True
    return False


def _rsi_step():
    """Append the RSI(14) for the newest close (Wilder smoothing)."""
    global _gain_avg, _loss_avg
    n = len(closes)
    if n < 2:
        rsi_track.append(None)
        return
    up = closes[-1] - closes[-2]
    if up < 0:
        up = 0
    down = closes[-2] - closes[-1]
    if down < 0:
        down = 0
    if n == RSI_SPAN + 1:
        # First value: plain mean of the first RSI_SPAN changes.
        gains = 0
        losses = 0
        for i in range(1, RSI_SPAN + 1):
            d = closes[i] - closes[i - 1]
            if d > 0:
                gains += d
            else:
                losses += -d
        _gain_avg = gains / RSI_SPAN
        _loss_avg = losses / RSI_SPAN
    elif n > RSI_SPAN + 1:
        _gain_avg = (_gain_avg * (RSI_SPAN - 1) + up) / RSI_SPAN
        _loss_avg = (_loss_avg * (RSI_SPAN - 1) + down) / RSI_SPAN
    else:
        rsi_track.append(None)
        return
    if _loss_avg == 0:
        rsi_track.append(100.0)
    else:
        ratio = _gain_avg / _loss_avg
        rsi_track.append(100.0 - 100.0 / (1.0 + ratio))

def _bounce_ready():
    """Check every bounce condition on the latest bars.

    Returns (ok, stop): the prospective stop when the setup fires,
    else (False, 0). All fall measures anchor on the previous bar
    (index -2), because the reversal bar itself lifts the close and
    would shrink the drop.
    """
    n = len(closes)
    if n < 2 or rsi_track[-1] is None or rsi_track[-2] is None:
        return (False, 0)
    prev = closes[-2]
    now = closes[-1]
    # 1. deep fall measured at the previous bar: 60 bars or 120 bars.
    if _close_fall(60) < BOUNCE_FALL_NEAR and _close_fall(120) < BOUNCE_FALL_FAR:
        return (False, 0)
    # 2. capitulation: the fall from the 720-bar high must reach 550 bps.
    if _high_fall(720) < BOUNCE_FALL_DEEP:
        return (False, 0)
    # 3. a long lower wick printed in the last 20 bars.
    if not _wick_seen():
        return (False, 0)
    # 4. reversal: this bar closes up >= 60 bps and above the prev high.
    if (now - prev) * UNIT // prev < BOUNCE_RISE:
        return (False, 0)
    if now <= highs[-2]:
        return (False, 0)
    # 5. RSI(14) turning up (no level filter).
    if rsi_track[-1] <= rsi_track[-2]:
        return (False, 0)
    # 6. reward/risk: room back to the 720-bar high at least twice the risk.
    crest = max(highs[-721:-1])
    trough = min(lows[-721:-1])
    stop0 = trough * (UNIT - BOUNCE_STOP_UNDER) // UNIT
    low_stop = now * (UNIT - BOUNCE_STOP_FLOOR) // UNIT
    high_stop = now * (UNIT - BOUNCE_STOP_CEIL) // UNIT
    stop = min(max(stop0, low_stop), high_stop)
    if crest - now < 2 * (now - stop):
        return (False, 0)
    return (True, stop)


def _dip_ready():
    """Check every dip condition on the latest bars.

    A range dip that is also a FAST fall: the close dropped hard from
    the 60-bar high, is not a capitulation (720-bar fall under 550 bps),
    RSI(14) sits under 50, this bar closes up, and the close also fell
    at least 150 bps over the last 60 bars (the sharp-drop filter,
    close-to-close).
    """
    n = len(closes)
    if n < 2 or rsi_track[-1] is None:
        return False
    prev = closes[-2]
    now = closes[-1]
    # 1. fall from the highest high of the last 60 bars (prev bar).
    if _high_fall(60) < DIP_DROP_NEAR:
        return False
    # 2. not a capitulation: the 720-bar fall stays under 550 bps.
    if _high_fall(720) >= DIP_DROP_DEEP:
        return False
    # 3. RSI(14) under 50.
    if rsi_track[-1] >= DIP_RSI_TOP:
        return False
    # 4. this bar closes up (any amount).
    if now <= prev:
        return False
    # 5. sharp drop: close-to-close fall over the last 60 bars >= 150 bps.
    if _close_fall(60) < DIP_SHARP:
        return False
    return True


def _trade_won(exit_px):
    """Win/loss call for the exit price vs entry (cost-aware)."""
    if _trade is None:
        return False
    return exit_px > _trade["entry_px"]


def on_hello(state):
    """Capture the start capital from hello (never a hardcoded constant)."""
    global _start_cash
    _start_cash = int(state.hello["capital"]["quote_units"])


def on_batch(state, batch):
    """Decide the orders for this batch. Returns a list of orders (or [])."""
    global _seen_t, _dip_count, _bounce_count, _bounce_misses
    global _rest_until, _waiting, _trade, _start_cash

    # Find this batch's CBBTC bar; skip the batch when it carries none.
    bar = None
    for b in batch.get("bars", []):
        if b.get("symbol") == ASSET:
            bar = b
            break
    if bar is None:
        return []
    # Duplicate-bar guard: skip a batch that carries no new bar.
    t = batch.get("t")
    if t is not None and t == _seen_t:
        return []
    _seen_t = t

    now = int(bar["c"])
    closes.append(now)
    highs.append(int(bar["h"]))
    lows.append(int(bar["l"]))
    _rsi_step()

    if _start_cash is None:
        _start_cash = int(state.hello["capital"]["quote_units"])

    holding = state.positions.get(ASSET)
    seq = state.seq

    # A pending buy either filled (position now held) or was rejected.
    if _waiting is not None:
        if holding is not None:
            # Filled at this bar's open: entry_px is the fill bar's open
            # (the trailing-arm reference). The stop was already fixed
            # at decision time from the decision close.
            _trade = {
                "kind": _waiting["kind"],
                "entry_px": int(bar["o"]),
                "stop_px": _waiting["stop_px"],
                "peak_px": now,
                "bars_held": 0,
                "armed": False,
                "stop_bps": _waiting["stop_bps"],
                "arm_bps": _waiting["arm_bps"],
                "trail_bps": _waiting["trail_bps"],
                "max_hold": _waiting["max_hold"],
            }
            if _waiting["kind"] == "dip":
                _dip_count += 1
            else:
                _bounce_count += 1
        _waiting = None

    # --- Manage an open position: stop, trailing, max hold. ---
    if _trade is not None and holding is not None:
        _trade["bars_held"] += 1
        if now > _trade["peak_px"]:
            _trade["peak_px"] = now
        if not _trade["armed"] and now >= _trade["entry_px"] * (UNIT + _trade["arm_bps"]) // UNIT:
            _trade["armed"] = True
        exit_px = None
        if now <= _trade["stop_px"]:
            exit_px = _trade["stop_px"]
        elif _trade["armed"] and now <= _trade["peak_px"] * (UNIT - _trade["trail_bps"]) // UNIT:
            exit_px = _trade["peak_px"] * (UNIT - _trade["trail_bps"]) // UNIT
        elif _trade["bars_held"] >= _trade["max_hold"]:
            exit_px = now
        if exit_px is not None:
            won = _trade_won(exit_px)
            kind = _trade["kind"]
            _trade = None
            _rest_until = seq + REST_AFTER_EXIT
            if kind == "bounce":
                if won:
                    _bounce_misses = 0
                else:
                    _bounce_misses += 1
            return [{"side": "sell", "symbol": ASSET,
                     "qty": holding.qty, "max_slippage_bps": SLIP_LIMIT}]
        return []

    if _trade is not None and holding is None:
        # Position vanished without our sell (should not happen): clear.
        _trade = None

    # --- No position: look for an entry. ---
    if len(closes) < MIN_HISTORY:
        return []
    if seq > LAST_ENTRY_BAR:
        return []
    if _dip_count + _bounce_count >= MAX_ENTRIES:
        return []
    if seq < _rest_until:
        return []
    if _start_cash is not None and state.equity < _start_cash * (UNIT - EQUITY_GIVEUP_BPS) // UNIT:
        return []

    # Bounce first; dip second. They are mutually exclusive by the
    # 720-bar fall (bounce needs >= 550, dip needs < 550).
    if _bounce_count < BOUNCE_CAP and _bounce_misses < MAX_BOUNCE_MISSES:
        ok, stop_px = _bounce_ready()
        if ok:
            _waiting = {"kind": "bounce", "stop_px": stop_px,
                        "stop_bps": 0, "arm_bps": BOUNCE_ARM,
                        "trail_bps": BOUNCE_TRAIL, "max_hold": BOUNCE_HOLD}
            return [{"side": "buy", "symbol": ASSET,
                     "notional_quote_units": state.cash,
                     "max_slippage_bps": SLIP_LIMIT}]
    if _dip_count < DIP_CAP and _dip_ready():
        stop_px = now * (UNIT - DIP_STOP) // UNIT
        _waiting = {"kind": "dip", "stop_px": stop_px,
                    "stop_bps": DIP_STOP, "arm_bps": DIP_ARM,
                    "trail_bps": DIP_TRAIL, "max_hold": DIP_HOLD}
        return [{"side": "buy", "symbol": ASSET,
                 "notional_quote_units": state.cash,
                 "max_slippage_bps": SLIP_LIMIT}]
    return []


if __name__ == "__main__":
    makefun_bars.run(on_batch, on_hello=on_hello)
