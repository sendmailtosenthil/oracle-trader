"""Momentum strategy core: scoring, ranking, allocation, rebalance plans.

Pure orchestration over the data layer (``momentum.services.data``) and the DB
models. Ports quant-momentum's ``weighted-momentum.js`` (factor-blended,
volatility-adjusted momentum score), ``allocate.js`` (bottom-up integer
allocation with residual cascade + min-1 share + capital top-up) and the
laggard-replacement rule from ``rebalance.js``.

The oracle module is a *live* tracker rather than a backtester, so "rebalance
date" is the latest trading day in the cache and execution prices come from the
latest bar. Holdings and cash are derived from the append-only
``MomentumTrade`` ledger so the books always reconcile.
"""
import json

from common.database import (
    MomentumConfig, MomentumHolding, MomentumTrade, MomentumRanking,
)
from momentum.services import data as mdata


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
def get_config(db):
    cfg = db.query(MomentumConfig).first()
    if cfg is None:
        cfg = MomentumConfig()
        db.add(cfg)
        db.commit()
    return cfg


def zerodha_charges(price, shares, side):
    """Zerodha equity-delivery charges for one order (port of ``zerodhaCharges.ts``).

    Brokerage is zero for delivery. Returns total ₹ charges (STT, NSE txn, SEBI,
    GST, stamp duty on buy, and a flat DP charge on sell).
    """
    turnover = price * shares
    stt = turnover * 0.001                       # 0.1% buy & sell
    exchange_txn = turnover * 0.0000322          # NSE 0.00322%
    sebi = turnover * 0.000001                   # ₹10 / crore
    gst = (sebi + exchange_txn) * 0.18           # 18% on (txn + sebi); brokerage = 0
    stamp = turnover * 0.00015 if side == "buy" else 0.0   # 0.015% on buy
    dp = 15.93 if side == "sell" else 0.0        # ₹13.5 + 18% GST per scrip on sell
    return stt + exchange_txn + sebi + gst + stamp + dp


def factors(cfg):
    """Parsed lookback factors: ``[{months, weight}, ...]``."""
    try:
        return json.loads(cfg.factors_json)
    except (ValueError, TypeError):
        return [{"months": 3, "weight": 0.40}, {"months": 6, "weight": 0.32},
                {"months": 9, "weight": 0.28}]


# ---------------------------------------------------------------------------
# Scoring / ranking (port of weighted-momentum.js)
# ---------------------------------------------------------------------------
def score_universe(price_book, calendar, candidates, as_of, cfg):
    """Score & rank ``candidates`` as of ``as_of``. Returns (ranked, excluded).

    ``ranked`` is a list of dicts sorted best-first with a 1-based ``rank``;
    ``excluded`` is a list of ``{symbol, reason}``.
    """
    facs = factors(cfg)
    w_sum = sum(f["weight"] for f in facs) or 1.0
    min_cov = cfg.min_history_coverage or 0.8
    vol_enabled = bool(cfg.vol_enabled)
    vol_months = cfg.vol_months or 3

    max_months = max([f["months"] for f in facs] + ([vol_months] if vol_enabled else [0]))
    window_start = calendar.months_back(as_of, max_months)
    expected_bars = sum(1 for d in calendar.dates if window_start <= d <= as_of) if window_start else 0
    vol_start = calendar.months_back(as_of, vol_months) if vol_enabled else None

    ranked, excluded = [], []
    for sym in candidates:
        if not price_book.has(sym):
            excluded.append({"symbol": sym, "reason": "no-price-data"})
            continue
        if expected_bars > 0:
            cov = price_book.coverage(sym, window_start, as_of)
            if cov / expected_bars < min_cov:
                excluded.append({"symbol": sym, "reason": "insufficient-history"})
                continue

        factor_rets, bad = [], False
        for f in facs:
            anchor = calendar.months_back(as_of, f["months"])
            now = price_book.close_as_of(sym, as_of)
            then = price_book.close_as_of(sym, anchor) if anchor else None
            if now is None or then in (None, 0):
                bad = True
                break
            factor_rets.append({"months": f["months"], "weight": f["weight"], "ret": now / then - 1})
        if bad:
            excluded.append({"symbol": sym, "reason": "missing-lookback-anchor"})
            continue

        blended = sum((fr["weight"] / w_sum) * fr["ret"] for fr in factor_rets)
        value, vol = blended, None
        if vol_enabled:
            vol = price_book.volatility(sym, vol_start, as_of)
            if vol is None or vol <= 0:
                excluded.append({"symbol": sym, "reason": "missing-volatility"})
                continue
            value = blended / vol

        row = {"symbol": sym, "value": value, "blended": blended, "vol": vol}
        for fr in factor_rets:
            row[f"r{fr['months']}m"] = fr["ret"]
        ranked.append(row)

    # Primary rank: risk-adjusted score (value). Also assign a raw-momentum rank
    # (by blended return only) so the UI can show both orderings side by side.
    ranked.sort(key=lambda r: r["value"], reverse=True)
    for i, row in enumerate(ranked):
        row["rank"] = i + 1
    for i, row in enumerate(sorted(ranked, key=lambda r: r["blended"], reverse=True)):
        row["raw_rank"] = i + 1
    return ranked, excluded


def rank_universe(price_book, calendar, cfg, as_of=None):
    """Score the point-in-time Nifty 500 universe — the single ranking entry point.

    Builds candidates from the membership snapshot effective on ``as_of`` (latest
    cached trading day by default) and scores them. Returns
    ``{as_of, ranked, excluded, snapshot_date, n_universe}``. Pure (no DB / no
    Streamlit) so both the cached view path and ``compute_ranking`` reuse it.
    """
    as_of = as_of or calendar.last()
    if as_of is None:
        return {"as_of": None, "ranked": [], "excluded": [], "snapshot_date": None,
                "n_universe": 0}
    member = mdata.Universe.load().as_of(as_of)
    candidates = [mdata.to_yahoo(s) for s in member["symbols"]] or price_book.symbols()
    ranked, excluded = score_universe(price_book, calendar, candidates, as_of, cfg)
    return {"as_of": as_of, "ranked": ranked, "excluded": excluded,
            "snapshot_date": member["snapshot_date"], "n_universe": len(candidates)}


def compute_ranking(db, as_of=None, persist=True):
    """Rank from the cached prices and (optionally) persist the snapshot to the DB."""
    cfg = get_config(db)
    series = mdata.load_series()
    if not series:
        return {"as_of": None, "ranked": [], "excluded": [], "snapshot_date": None,
                "n_universe": 0, "error": "No cached price data found."}
    result = rank_universe(mdata.PriceBook(series), mdata.Calendar.from_series(series), cfg, as_of)
    if persist and result["ranked"]:
        _persist_ranking(db, result["as_of"], result["ranked"])
    return result


def _persist_ranking(db, as_of, ranked):
    db.query(MomentumRanking).filter(MomentumRanking.as_of == as_of).delete()
    for row in ranked:
        db.add(MomentumRanking(
            as_of=as_of, symbol=row["symbol"], rank=row["rank"],
            score=row["value"], blended=row["blended"],
            r3m=row.get("r3m"), r6m=row.get("r6m"), r9m=row.get("r9m"), vol=row.get("vol"),
        ))
    db.commit()


# ---------------------------------------------------------------------------
# Allocation — equal-capital, gap-based redistribution
# ---------------------------------------------------------------------------
def allocate(picks, capital, side="buy", round_up=False):
    """Allocate ``capital`` across ``picks`` (each has ``price``) into whole units.

    Algorithm (replaces the old residual cascade that could starve a stock when a
    neighbour was expensive):

    1. Each pick targets an equal part ``per_part = capital / n``. Base units =
       ``floor(per_part / price)`` (min 1). A stock priced above ``per_part`` gets
       exactly 1 unit.
    2. Whatever capital is left (from whole-unit rounding, net of charges) is
       redistributed one unit at a time to the stock furthest BELOW ``per_part``
       (largest gap ≈ highest priced). A stock that reaches/exceeds ``per_part``
       stops receiving more — so redistribution can push a stock past the part by
       at most one unit, never further.
    3. ``round_up`` (replace): if usable leftover covers more than half a unit of
       some stock (``leftover > price/2``), buy ONE more unit — choosing the stock
       that needs the LEAST extra from pocket (smallest ``price − leftover`` shortfall,
       i.e. the cheapest qualifying stock) — and top up the shortfall as injection.
    4. Leftover that can't buy another eligible unit becomes cash.

    Charges are accounted, so the leftover is post-charge. Returns
    ``{allocations, total_cost, charges, cash_left, injection}`` — ``injection`` is
    the extra capital the min-1 / round-up rules force.
    """
    n = len(picks)
    if n == 0:
        return {"allocations": [], "total_cost": 0.0, "charges": 0.0,
                "cash_left": capital, "injection": 0.0}
    per_part = capital / n

    alloc = []
    for p in picks:
        price = p["price"]
        units = int(per_part // price) if price and price > 0 else 0
        if units < 1:
            units = 1
        alloc.append({**p, "price": price, "shares": units, "cost": units * price})

    def total_cost():
        return sum(a["cost"] for a in alloc)

    def total_charges():
        return sum(zerodha_charges(a["price"], a["shares"], side) for a in alloc)

    # Redistribute leftover to the largest-gap stocks, one unit at a time.
    leftover = capital - total_cost() - total_charges()
    while leftover > 0:
        cands = [a for a in alloc if a["cost"] < per_part and a["price"] <= leftover]
        if not cands:
            break
        target = max(cands, key=lambda a: per_part - a["cost"])
        target["shares"] += 1
        target["cost"] += target["price"]
        leftover = capital - total_cost() - total_charges()

    # Round-up: leftover can't fully fund a unit, but if it covers >half a unit of
    # some stock, buy that one unit (inject the small shortfall) — round to nearest.
    # Pick the stock needing the LEAST extra from pocket = the cheapest qualifying
    # one (smallest price ⇒ smallest price − leftover shortfall).
    if round_up and leftover > 0:
        affordable = [a for a in alloc if a["cost"] < per_part and a["price"] < 2 * leftover]
        if affordable:
            target = min(affordable, key=lambda a: a["price"])
            target["shares"] += 1
            target["cost"] += target["price"]

    tc, chg = total_cost(), total_charges()
    injection = max(0.0, tc + chg - capital)
    cash_left = capital + injection - tc - chg
    return {"allocations": alloc, "total_cost": tc, "charges": chg,
            "cash_left": cash_left, "injection": injection}


# ---------------------------------------------------------------------------
# Rebalance plan (port of deployTopN + monthlyReplace)
# ---------------------------------------------------------------------------
def build_plan(db, ranking, as_of, price_book=None):
    """Build a buy/sell rebalance plan from the latest ranking + current holdings.

    Returns a dict with ``type`` ('deploy' first time, else 'replace' or 'hold'),
    ``sells``, ``buys``, ``proceeds``, ``investable``, ``injection``, ``cash_left``.
    No DB writes — purely advisory until ``execute_plan`` is called.
    """
    cfg = get_config(db)
    if price_book is None:
        price_book = mdata.PriceBook(mdata.load_series())
    ranked = ranking["ranked"]
    rank_map = {r["symbol"]: r["rank"] for r in ranked}

    holdings = db.query(MomentumHolding).filter(MomentumHolding.shares > 0).all()

    def buy_price(sym):
        bp = price_book.exec_price(sym, as_of, "buy")
        return bp["price"] if bp else None

    def sell_price(sym):
        sp = price_book.exec_price(sym, as_of, "sell")
        return sp["price"] if sp else None

    # --- Initial deployment: no holdings yet -> deploy top-N ---
    if not holdings:
        picks = []
        for row in ranked:
            p = buy_price(row["symbol"])
            if not p:
                continue
            picks.append({**row, "price": p})
            if len(picks) >= cfg.num_stocks:
                break
        res = allocate(picks, cfg.investment, side="buy")
        buys = [{"symbol": a["symbol"], "rank": a.get("rank"), "shares": a["shares"],
                 "price": a["price"], "cost": a["cost"],
                 "charges": zerodha_charges(a["price"], a["shares"], "buy")}
                for a in res["allocations"] if a["shares"] > 0 and a["price"]]
        return {"type": "deploy", "as_of": as_of, "sells": [], "buys": buys,
                "proceeds": 0.0, "investable": cfg.investment,
                "per_part": cfg.investment / max(1, len(picks)),
                "injection": res["injection"], "cash_left": res["cash_left"]}

    # --- Replacement: sell laggards, redeploy proceeds into best unheld ---
    threshold = cfg.replace_rank_threshold
    sells = []
    for h in holdings:
        rank = rank_map.get(h.symbol)
        reason = None
        if rank is None:
            reason = "left-index-or-unranked"
        elif rank > threshold:
            reason = f"rank>{threshold}"
        sp = sell_price(h.symbol)
        if sp is None:
            reason = reason or "no-price"
        if reason:
            chg = zerodha_charges(sp, h.shares, "sell") if sp else 0.0
            sells.append({"symbol": h.symbol, "rank": rank, "reason": reason,
                          "shares": h.shares, "price": sp, "avg_cost": h.avg_cost,
                          "charges": chg,
                          "pnl": ((sp - h.avg_cost) * h.shares - chg) if sp else None})

    if not sells:
        return {"type": "hold", "as_of": as_of, "sells": [], "buys": [],
                "proceeds": 0.0, "investable": 0.0, "injection": 0.0, "cash_left": cfg.cash,
                "note": "All holdings still ranked within threshold."}

    # Proceeds are net of sell-side charges.
    proceeds = sum(s["price"] * s["shares"] - s["charges"] for s in sells if s["price"])
    still_held = {h.symbol for h in holdings} - {s["symbol"] for s in sells}
    needed = len(sells)
    picks = []
    for row in ranked:
        if row["symbol"] in still_held:
            continue
        p = buy_price(row["symbol"])
        if not p:
            continue
        picks.append({**row, "price": p})
        if len(picks) >= needed:
            break

    investable = cfg.cash + proceeds if cfg.reinvest_idle_cash else proceeds
    res = allocate(picks, investable, side="buy", round_up=True)
    buys = [{"symbol": a["symbol"], "rank": a.get("rank"), "shares": a["shares"],
             "price": a["price"], "cost": a["cost"],
             "charges": zerodha_charges(a["price"], a["shares"], "buy")}
            for a in res["allocations"] if a["shares"] > 0 and a["price"]]
    return {"type": "replace", "as_of": as_of, "sells": sells, "buys": buys,
            "proceeds": proceeds, "investable": investable,
            "per_part": investable / max(1, len(picks)),
            "injection": res["injection"], "cash_left": res["cash_left"]}


def execute_plan(db, plan):
    """Apply a plan: append SELL/BUY trades, recompute holdings + cash + injections."""
    cfg = get_config(db)
    as_of = plan["as_of"]

    for s in plan.get("sells", []):
        if not s.get("price"):
            continue
        db.add(MomentumTrade(
            date=as_of, symbol=s["symbol"], side="SELL", shares=s["shares"],
            price=s["price"], value=s["price"] * s["shares"], charges=s.get("charges", 0.0),
            rank=s.get("rank"), avg_cost=s.get("avg_cost"), pnl=s.get("pnl"),
            reason=s.get("reason"),
        ))

    if plan.get("injection", 0) > 0:
        cfg.capital_injected = (cfg.capital_injected or 0.0) + plan["injection"]

    for b in plan.get("buys", []):
        db.add(MomentumTrade(
            date=as_of, symbol=b["symbol"], side="BUY", shares=b["shares"],
            price=b["price"], value=b["cost"], charges=b.get("charges", 0.0),
            rank=b.get("rank"), reason=plan["type"],
        ))

    db.commit()
    recalc_holdings(db)
    recalc_cash(db, cfg)
    db.commit()


# ---------------------------------------------------------------------------
# Books: rebuild holdings & cash from the trade ledger (source of truth)
# ---------------------------------------------------------------------------
def recalc_holdings(db):
    trades = db.query(MomentumTrade).order_by(MomentumTrade.date.asc(), MomentumTrade.id.asc()).all()
    state = {}
    for t in trades:
        s = state.setdefault(t.symbol, {"shares": 0, "invested": 0.0, "entry": None})
        if t.side == "BUY":
            s["invested"] += t.value
            s["shares"] += t.shares
            if s["entry"] is None:
                s["entry"] = t.date
        else:  # SELL
            if s["shares"] > 0:
                avg = s["invested"] / s["shares"]
                s["invested"] -= avg * t.shares
            s["shares"] -= t.shares
            if s["shares"] <= 0:
                s["shares"], s["invested"], s["entry"] = 0, 0.0, None

    db.query(MomentumHolding).delete()
    for sym, s in state.items():
        if s["shares"] > 0:
            db.add(MomentumHolding(symbol=sym, shares=s["shares"],
                                   avg_cost=s["invested"] / s["shares"], entry_date=s["entry"]))
    db.commit()


def recalc_cash(db, cfg=None):
    """Cash identity: investment + injected + Σ(sells) − Σ(buys) − Σ(charges)."""
    cfg = cfg or get_config(db)
    trades = db.query(MomentumTrade).all()
    buys = sum(t.value for t in trades if t.side == "BUY")
    sells = sum(t.value for t in trades if t.side == "SELL")
    charges = sum(t.charges or 0.0 for t in trades)
    cfg.cash = (cfg.investment or 0.0) + (cfg.capital_injected or 0.0) + sells - buys - charges
    db.commit()
    return cfg.cash


# ---------------------------------------------------------------------------
# Portfolio valuation
# ---------------------------------------------------------------------------
def portfolio_value(db, rank_map=None, raw_rank_map=None, price_book=None):
    """Value current holdings at the latest close. Returns a summary dict.

    ``rank_map`` (symbol -> vol-adjusted rank) and ``raw_rank_map`` (symbol ->
    raw-momentum rank) annotate each holding with its live ranks for the dashboard.
    """
    cfg = get_config(db)
    if price_book is None:
        price_book = mdata.PriceBook(mdata.load_series())
    rank_map = rank_map or {}
    raw_rank_map = raw_rank_map or {}

    # Entry rank (rank when first bought) and total buy-side charges per symbol,
    # derived from the trade ledger — no denormalised columns needed.
    buys = (db.query(MomentumTrade)
            .filter(MomentumTrade.side == "BUY")
            .order_by(MomentumTrade.date.asc(), MomentumTrade.id.asc()).all())
    entry_rank, charges_by = {}, {}
    for t in buys:
        entry_rank.setdefault(t.symbol, t.rank)
        charges_by[t.symbol] = charges_by.get(t.symbol, 0.0) + (t.charges or 0.0)

    holdings = db.query(MomentumHolding).filter(MomentumHolding.shares > 0).all()
    rows, holdings_value = [], 0.0
    for h in holdings:
        last = price_book.latest_close(h.symbol)
        # Without a live price (e.g. cache not yet refreshed) value at cost basis.
        mkt = (last if last else h.avg_cost) * h.shares
        cost = h.avg_cost * h.shares
        holdings_value += mkt
        rows.append({
            "symbol": h.symbol, "shares": h.shares, "avg_cost": h.avg_cost,
            "last": last, "value": mkt, "cost": cost, "charges": charges_by.get(h.symbol, 0.0),
            "pnl": mkt - cost, "pnl_pct": ((mkt / cost - 1) * 100) if cost else 0.0,
            "entry_rank": entry_rank.get(h.symbol), "rank": rank_map.get(h.symbol),
            "raw_rank": raw_rank_map.get(h.symbol), "entry_date": h.entry_date,
        })
    # Order by entry rank (the order they were bought in), unranked last.
    rows.sort(key=lambda r: (r["entry_rank"] is None, r["entry_rank"] or 0))

    equity = cfg.cash + holdings_value
    invested = (cfg.investment or 0.0) + (cfg.capital_injected or 0.0)
    realized = sum(t.pnl or 0.0 for t in db.query(MomentumTrade).filter(MomentumTrade.side == "SELL").all())
    return {
        "cash": cfg.cash, "holdings_value": holdings_value, "equity": equity,
        "invested": invested, "realized_pnl": realized,
        "unrealized_pnl": sum(r["pnl"] for r in rows),
        "total_return_pct": ((equity / invested - 1) * 100) if invested else 0.0,
        "n_holdings": len(rows), "holdings": rows,
    }
