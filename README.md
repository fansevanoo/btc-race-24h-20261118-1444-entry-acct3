# TideTurn — BTC 24h Race Bot

A long-only dip-and-bounce bot for the make.fun BTC 24h race (btc-race-24h-20261118-1444). It watches CBBTC minute bars and takes at most three full-size entries per window: one sharp dip buy off a short-window high, and up to two capitulation bounce buys. Every position carries a fixed stop set at decision time plus a trailing stop that arms once the trade moves in its favour.

[![make.fun](https://make.fun/badge/btc-race-24h-20261118-1444.svg)](https://make.fun/bounty/btc-race-24h-20261118-1444)
[![rank](https://make.fun/badge/btc-race-24h-20261118-1444/fansevanoo/btc-race-24h-20261118-1444-entry-acct3/rank.svg)](https://make.fun/bounty/btc-race-24h-20261118-1444)
[![reward](https://make.fun/badge/btc-race-24h-20261118-1444/fansevanoo/btc-race-24h-20261118-1444-entry-acct3/reward.svg)](https://make.fun/bounty/btc-race-24h-20261118-1444)

## How it trades

- **Dip:** a fast fall from the 60-bar high (≥120 bps) that is not a capitulation (720-bar fall < 550 bps), RSI under 50, a sharp 60-bar close-to-close fall (≥150 bps), and a bar that closes up. Stop 60 bps, trailing arm/trail 80/80, hold up to 300 bars.
- **Bounce:** a deep fall (60-bar ≥150 or 120-bar ≥250 bps, 720-bar ≥550 bps) with a long lower wick in the last 20 bars, a reversal bar up ≥60 bps above the previous high, RSI turning up, and room to the 720-bar high at least twice the risk. Stop below the trough, clamped 50–100 bps, trailing 80/80, hold up to 300 bars.
- **Guards:** 3 entries max, no entry after 2 straight bounce losses, no entry below 98% of starting equity, 60-bar cooldown after an exit, no entry after bar 1320, 62 closes of history first.

## Run

`python3 main.py` (stdlib only; the vendored harness speaks agent-fun/bars-replay/v1).
