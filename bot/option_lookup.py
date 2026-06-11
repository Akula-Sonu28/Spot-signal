"""NIFTY option strike selection and bid/LTP lookup via Upstox."""

from __future__ import annotations

import gzip
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime
from functools import lru_cache
from typing import Literal
from zoneinfo import ZoneInfo

from bot.futures_vwap import _encode_key, _http_json

NSE_JSON_URL = "https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz"
OptionSide = Literal["CE", "PE"]
StrikeMode = Literal["ATM", "ITM1", "ATM_OR_ITM1"]

# Manual option execution: buy at ask; SL = 50% premium loss (Pine v3.7 guidance).
PREMIUM_SL_LOSS_FRAC = 0.5


@dataclass(frozen=True)
class OptionQuote:
    strike: int
    option_type: str
    expiry: str
    trading_symbol: str
    instrument_key: str
    ltp: float | None = None
    bid: float | None = None
    ask: float | None = None
    spread: float | None = None
    oi: float | None = None
    quote_ok: bool = False
    note: str = ""

    def entry_ask(self) -> float | None:
        """Premium paid when buying — always use ask, not bid."""
        if self.ask is not None and self.ask > 0:
            return float(self.ask)
        return None

    def to_extra(self) -> dict:
        extra = {
            "option_strike": self.strike,
            "option_type": self.option_type,
            "option_expiry": self.expiry,
            "option_symbol": self.trading_symbol,
            "option_ltp": self.ltp,
            "option_bid": self.bid,
            "option_ask": self.ask,
            "option_spread": self.spread,
            "option_oi": self.oi,
            "option_note": self.note,
        }
        levels = compute_premium_levels(self.entry_ask())
        if levels:
            extra["option_premium_sl"], extra["option_premium_target"] = levels
        return extra


def round_nifty_strike(spot: float) -> int:
    return int(round(spot / 50.0) * 50)


def pick_strike(spot: float, option_type: OptionSide, mode: StrikeMode) -> int:
    atm = round_nifty_strike(spot)
    if mode == "ATM":
        return atm
    if mode == "ITM1":
        return atm - 50 if option_type == "CE" else atm + 50
    # ATM_OR_ITM1 — recommend ATM primary (same as Pine default display)
    return atm


@lru_cache(maxsize=1)
def _load_nifty_options_index() -> list[dict]:
    req = urllib.request.Request(NSE_JSON_URL, headers={"User-Agent": "nifty-spot-signal-engine/0.1"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        raw = gzip.decompress(resp.read())
    data = json.loads(raw)
    out = []
    for inst in data:
        if inst.get("instrument_type") not in ("CE", "PE"):
            continue
        sym = inst.get("trading_symbol") or ""
        if not sym.startswith("NIFTY ") or "BANK" in sym or "FIN" in sym or "MID" in sym:
            continue
        exp = inst.get("expiry")
        if isinstance(exp, int):
            exp_date = datetime.fromtimestamp(exp / 1000, tz=ZoneInfo("Asia/Kolkata")).date()
        elif isinstance(exp, str):
            exp_date = date.fromisoformat(exp[:10])
        else:
            continue
        strike_val = inst.get("strike_price") or inst.get("strike") or 0
        out.append(
            {
                "strike": int(float(strike_val)),
                "type": inst["instrument_type"],
                "expiry": exp_date.isoformat(),
                "expiry_date": exp_date,
                "instrument_key": inst["instrument_key"],
                "trading_symbol": sym,
            }
        )
    return out


def nearest_weekly_expiry(as_of: date | None = None) -> str:
    as_of = as_of or date.today()
    expiries = sorted({row["expiry_date"] for row in _load_nifty_options_index() if row["expiry_date"] >= as_of})
    if not expiries:
        raise RuntimeError("No NIFTY option expiries found in instrument master")
    return expiries[0].isoformat()


def _find_instrument(strike: int, option_type: OptionSide, expiry: str) -> dict | None:
    for row in _load_nifty_options_index():
        if row["strike"] == strike and row["type"] == option_type and row["expiry"] == expiry:
            return row
    return None


def _fetch_full_quote(instrument_key: str, token: str) -> dict | None:
    key = _encode_key(instrument_key)
    url = f"https://api.upstox.com/v2/market-quote/quotes?instrument_key={key}"
    try:
        payload = _http_json(url, token=token)
        if payload.get("status") != "success":
            return None
        data = payload.get("data") or {}
        if not data:
            return None
        return next(iter(data.values()))
    except urllib.error.HTTPError:
        return None
    except Exception:
        return None


def _parse_bid_ask(quote: dict) -> tuple[float | None, float | None]:
    depth = quote.get("depth") or {}
    bids = depth.get("buy") or []
    asks = depth.get("sell") or []
    bid = float(bids[0]["price"]) if bids and bids[0].get("price") else None
    ask = float(asks[0]["price"]) if asks and asks[0].get("price") else None
    if bid is None and quote.get("bid_price") is not None:
        bid = float(quote["bid_price"])
    if ask is None and quote.get("ask_price") is not None:
        ask = float(quote["ask_price"])
    return bid, ask


def lookup_option(
    spot: float,
    option_type: OptionSide,
    token: str,
    *,
    strike_mode: StrikeMode = "ATM_OR_ITM1",
    expiry: str | None = None,
) -> OptionQuote:
    """Resolve strike + fetch bid/LTP. Falls back to strike-only if quotes blocked."""
    expiry = expiry or nearest_weekly_expiry()
    strike = pick_strike(spot, option_type, strike_mode)
    inst = _find_instrument(strike, option_type, expiry)

    if inst is None:
        return OptionQuote(
            strike=strike,
            option_type=option_type,
            expiry=expiry,
            trading_symbol=f"NIFTY {strike} {option_type}",
            instrument_key="",
            note="Contract not found in master — verify expiry/strike manually",
        )

    quote = _fetch_full_quote(inst["instrument_key"], token)
    if quote is None:
        return OptionQuote(
            strike=strike,
            option_type=option_type,
            expiry=expiry,
            trading_symbol=inst["trading_symbol"],
            instrument_key=inst["instrument_key"],
            note="Live bid/LTP unavailable (market closed or quote API restricted)",
        )

    bid, ask = _parse_bid_ask(quote)
    ltp = quote.get("last_price")
    ltp_f = float(ltp) if ltp is not None else None
    spread = round(ask - bid, 2) if bid is not None and ask is not None else None
    oi = quote.get("oi")

    note = ""
    if spread is not None and spread > 2.0:
        note = f"Wide spread ({spread:.2f}) — check liquidity"

    return OptionQuote(
        strike=strike,
        option_type=option_type,
        expiry=expiry,
        trading_symbol=inst["trading_symbol"],
        instrument_key=inst["instrument_key"],
        ltp=ltp_f,
        bid=bid,
        ask=ask,
        spread=spread,
        oi=float(oi) if oi is not None else None,
        quote_ok=True,
        note=note,
    )


def lookup_for_signal(
    event_type: str,
    spot: float,
    token: str,
    strike_mode: StrikeMode = "ATM_OR_ITM1",
    stored: OptionQuote | None = None,
) -> OptionQuote | None:
    """Return option quote for entry/exit Telegram formatting."""
    if event_type == "BUY_CE":
        return lookup_option(spot, "CE", token, strike_mode=strike_mode)
    if event_type == "BUY_PE":
        return lookup_option(spot, "PE", token, strike_mode=strike_mode)
    if stored is not None:
        return stored
    if event_type.endswith("_CE"):
        return lookup_option(spot, "CE", token, strike_mode=strike_mode)
    if event_type.endswith("_PE"):
        return lookup_option(spot, "PE", token, strike_mode=strike_mode)
    return None


def compute_premium_levels(
    entry_ask: float | None,
    *,
    rr_ratio: float = 1.8,
    sl_loss_frac: float = PREMIUM_SL_LOSS_FRAC,
) -> tuple[float, float] | None:
    """Premium SL/target from buy-at-ask entry (50% SL, rr_ratio on premium risk)."""
    if entry_ask is None or entry_ask <= 0:
        return None
    sl = round(entry_ask * (1.0 - sl_loss_frac), 2)
    risk = entry_ask - sl
    target = round(entry_ask + risk * rr_ratio, 2)
    return sl, target


def format_premium_risk_lines(
    q: OptionQuote | None,
    *,
    entry_ask: float | None = None,
    rr_ratio: float = 1.8,
) -> list[str]:
    """Option SL/target for manual execution — based on ask (what you pay to buy)."""
    ask = entry_ask
    if ask is None and q is not None:
        ask = q.entry_ask()
    levels = compute_premium_levels(ask, rr_ratio=rr_ratio)
    if levels is None:
        return [
            "Premium entry: use ASK from broker before buying",
            "Premium SL/target: set after you have the ask",
        ]
    sl, target = levels
    return [
        f"Entry (ask): ₹{ask:.2f}",
        f"Premium SL (50% loss): exit at ₹{sl:.2f} or below",
        f"Premium target (~{rr_ratio}R on premium): ₹{target:.2f}",
    ]


def format_option_lines(q: OptionQuote | None, strike_mode: StrikeMode = "ATM_OR_ITM1") -> list[str]:
    if q is None:
        return ["Option: —"]

    lines = [
        f"Strike: {q.strike} {q.option_type}",
        f"Expiry: {q.expiry}",
        f"Symbol: {q.trading_symbol}",
    ]
    if strike_mode == "ATM_OR_ITM1":
        itm = q.strike - 50 if q.option_type == "CE" else q.strike + 50
        lines.append(f"Alt 1-ITM: {itm} {q.option_type}")

    if q.ask is not None:
        lines.append(f"Ask (buy here): ₹{q.ask:.2f}")
        if q.bid is not None:
            lines.append(f"Bid: ₹{q.bid:.2f} | LTP: ₹{(q.ltp or 0):.2f}")
        elif q.ltp is not None:
            lines.append(f"LTP: ₹{q.ltp:.2f}")
        if q.spread is not None:
            lines.append(f"Spread: {q.spread:.2f}")
    elif q.ltp is not None:
        lines.append(f"LTP: ₹{q.ltp:.2f} — ask unavailable; get ask before entry")
    else:
        lines.append("Ask/LTP: unavailable — check broker app before entry")

    if q.oi is not None:
        lines.append(f"OI: {int(q.oi):,}")
    if q.note:
        lines.append(q.note)

    return lines
