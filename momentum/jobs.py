"""Scheduled job for the momentum module: a daily ADVISORY email (no trading).

Runs after market close on weekdays. It refreshes Nifty 500 daily prices via the
stored enctoken, ranks on the latest close, values the current book, and emails:
current holdings + P&L + ranks, any holdings that breached the trailing exit
(what to SELL), and the suggested replacements (what to BUY, with price). It does
NOT execute anything — the user reviews the email and logs the trades in the app.
"""
import datetime

import pytz

from common.database import BrokerConfig, MomentumHolding, get_db
from common.notifications import send_email
from common.zerodha_client import ZerodhaClient
from momentum.services import data as mdata
from momentum.services import strategy

IST = pytz.timezone("Asia/Kolkata")
HISTORY_DAYS = 455  # ~15 months — covers the 9-month factor + 180d Clenow windows


def run_daily_momentum_advisory():
    """Refresh prices, rank, and email the daily momentum advisory (Mon–Fri)."""
    now = datetime.datetime.now(IST)
    if now.weekday() >= 5:
        print("Momentum advisory skipped: weekend.")
        return

    db = next(get_db())
    bc = db.query(BrokerConfig).filter(BrokerConfig.broker_name == "ZERODHA").first()
    if not bc or not bc.enctoken:
        print("Momentum advisory skipped: no Zerodha enctoken configured.")
        db.close()
        return
    if not ZerodhaClient(bc.enctoken, user_id=bc.user_id).validate():
        send_email("<h2>📊 Momentum advisory skipped</h2><p>The Zerodha enctoken is "
                   "inactive/expired. Update it in <b>Broker Setup</b> to resume.</p>",
                   "📊 Momentum advisory skipped — enctoken inactive")
        db.close()
        return

    # Refresh the current Nifty 500 ∪ holdings daily prices.
    mdata.ensure_current_constituents()
    current = {mdata.to_yahoo(s) for s in mdata.Universe.load().latest()}
    held = {h.symbol for h in db.query(MomentumHolding).filter(MomentumHolding.shares > 0).all()}
    syms = sorted(current | held)
    frm = (now.date() - datetime.timedelta(days=HISTORY_DAYS)).isoformat()
    print(f"[{now:%H:%M:%S}] Momentum advisory: refreshing {len(syms)} symbols from {frm}...")
    res = mdata.refresh_prices(enctoken=bc.enctoken, user_id=bc.user_id, symbols=syms,
                               history_from=frm, progress_cb=lambda m: None)
    if res["fatal"]:
        send_email(f"<h2>⚠️ Momentum advisory FAILED</h2><p>{res['fatal']}</p>",
                   "⚠️ Momentum advisory FAILED")
        db.close()
        return
    mdata.prune_cache(syms)

    # Rank (updates best_rank daily), value the book, build the advisory plan.
    ranking = strategy.compute_ranking(db)
    rank_map = {r["symbol"]: r["rank"] for r in ranking["ranked"]}
    raw_map = {r["symbol"]: r.get("raw_rank") for r in ranking["ranked"]}
    pv = strategy.portfolio_value(db, rank_map, raw_map)
    plan = strategy.build_plan(db, ranking, ranking["as_of"])
    cfg = strategy.get_config(db)

    n_exit = len(plan.get("sells", []))
    if plan["type"] == "deploy":
        tag = f"deploy {plan['n_target']} stocks"
    elif n_exit:
        tag = f"{n_exit} exit(s) — action needed"
    else:
        tag = "no changes"
    subject = f"📊 Momentum {ranking['as_of']} — {tag}"
    send_email(_advisory_html(cfg, ranking, pv, plan), subject)
    print(f"Momentum advisory emailed: {subject}")
    db.close()


def _m(x):
    return f"₹{x:,.0f}" if x is not None else "—"


def _advisory_html(cfg, ranking, pv, plan):
    as_of = ranking.get("as_of") or "—"
    trailing = getattr(cfg, "exit_mode", "trailing") != "fixed"
    gap = int(getattr(cfg, "trail_gap", 25))
    rule = (f"trailing — sell when rank &gt; best-since-entry + {gap}" if trailing
            else f"fixed — sell when rank &gt; {cfg.replace_rank_threshold}")
    ret = pv.get("total_return_pct", 0.0)
    rc = "#0a8f4f" if ret >= 0 else "#d23b3b"

    cards = "".join(
        f'<div style="display:inline-block;min-width:130px;margin:4px 10px 4px 0">'
        f'<div style="font-size:11px;color:#778;text-transform:uppercase">{k}</div>'
        f'<div style="font-size:16px;font-weight:650;{s}">{v}</div></div>'
        for k, v, s in [
            ("Equity", _m(pv.get("equity")), ""),
            ("Invested", _m(pv.get("invested")), ""),
            ("Cash", _m(pv.get("cash")), ""),
            ("Total return", f"{ret:+.2f}%", f"color:{rc}"),
            ("Realised", _m(pv.get("realized_pnl")), ""),
            ("Unrealised", _m(pv.get("unrealized_pnl")), ""),
            ("Holdings", str(pv.get("n_holdings", 0)), ""),
        ])

    # --- EXITS (what to sell) ---
    sells = plan.get("sells", [])
    if sells:
        rows = "".join(
            f"<tr><td>{s['symbol']}</td><td style='text-align:right'>{s.get('rank') or '—'}</td>"
            f"<td style='text-align:right'>{s['shares']}</td>"
            f"<td style='text-align:right'>₹{(s['price'] or 0):,.2f}</td>"
            f"<td style='text-align:right;color:{'#0a8f4f' if (s.get('pnl') or 0) >= 0 else '#d23b3b'}'>"
            f"{_m(s.get('pnl'))}</td><td>{s['reason']}</td></tr>" for s in sells)
        exits_html = (
            "<h3 style='color:#d23b3b'>⚠️ Exit these (breached the exit rule)</h3>"
            "<table border='1' cellpadding='6' cellspacing='0' style='border-collapse:collapse;font-size:13px'>"
            "<tr style='background:#fde8e8'><th>Symbol</th><th>Today rank</th><th>Shares</th>"
            "<th>~Price (close)</th><th>Est. P&amp;L</th><th>Reason</th></tr>" + rows + "</table>")
    else:
        exits_html = "<h3 style='color:#0a8f4f'>✅ No exits — all holdings within the exit rule.</h3>"

    # --- ADDS (what to buy) ---
    pool = plan.get("pool", [])[:max(plan.get("n_target", 0), 0)]
    if pool:
        rows = "".join(
            f"<tr><td>{p['symbol']}</td><td style='text-align:right'>{p['rank']}</td>"
            f"<td style='text-align:right'>{p.get('score', 0):.3f}</td>"
            f"<td style='text-align:right'>₹{p['price']:,.2f}</td></tr>" for p in pool)
        label = ("Deploy — buy these" if plan["type"] == "deploy"
                 else "Suggested replacements — buy these (after logging the exits)")
        adds_html = (
            f"<h3 style='color:#0a8f4f'>➕ {label}</h3>"
            "<table border='1' cellpadding='6' cellspacing='0' style='border-collapse:collapse;font-size:13px'>"
            "<tr style='background:#e8f5ec'><th>Symbol</th><th>Rank</th><th>Score</th>"
            "<th>Buy price (close)</th></tr>" + rows + "</table>"
            "<p style='font-size:12px;color:#667'>Quantities are set when you log the buys in the app "
            "(equal-weight allocation across the freed capital).</p>")
    else:
        adds_html = ""

    # --- Current holdings ---
    hrows = ""
    for h in pv.get("holdings", []):
        pnl = h.get("pnl")
        pc = "#0a8f4f" if (pnl or 0) >= 0 else "#d23b3b"
        hrows += (
            f"<tr><td>{h['symbol']}</td><td style='text-align:right'>{h['shares']}</td>"
            f"<td style='text-align:right'>₹{h['avg_cost']:,.2f}</td>"
            f"<td style='text-align:right'>{('₹%.2f' % h['last']) if h.get('last') else '—'}</td>"
            f"<td style='text-align:right'>{_m(h.get('value'))}</td>"
            f"<td style='text-align:right;color:{pc}'>{_m(pnl)}"
            f"{(' (%+.1f%%)' % h['pnl_pct']) if h.get('last') else ''}</td>"
            f"<td style='text-align:right'>{h.get('entry_rank') or '—'}</td>"
            f"<td style='text-align:right'>{h.get('best_rank') or '—'}</td>"
            f"<td style='text-align:right'>{h.get('rank') or '—'}</td></tr>")
    holdings_html = (
        "<h3>Current holdings</h3>"
        "<table border='1' cellpadding='6' cellspacing='0' style='border-collapse:collapse;font-size:13px'>"
        "<tr style='background:#f0f3f7'><th>Symbol</th><th>Shares</th><th>Avg cost</th><th>LTP</th>"
        "<th>Value</th><th>P&amp;L</th><th>Entry rank</th><th>Best rank</th><th>Today rank</th></tr>"
        + (hrows or "<tr><td colspan=9>No holdings.</td></tr>") + "</table>")

    # --- Top rankings ---
    top = ranking.get("ranked", [])[:20]
    trows = "".join(
        f"<tr><td style='text-align:right'>{r['rank']}</td><td>{r['symbol']}</td>"
        f"<td style='text-align:right'>{r.get('value', 0):.3f}</td>"
        f"<td style='text-align:right'>{(r.get('blended') or 0) * 100:.1f}%</td></tr>" for r in top)
    rank_html = (
        "<h3>Top 20 momentum ranking</h3>"
        "<table border='1' cellpadding='6' cellspacing='0' style='border-collapse:collapse;font-size:13px'>"
        "<tr style='background:#f0f3f7'><th>Rank</th><th>Symbol</th><th>Score</th><th>Blended ret</th></tr>"
        + trows + "</table>")

    return (
        f"<div style='font-family:-apple-system,system-ui,sans-serif;color:#1a1f26'>"
        f"<h2>📊 Momentum advisory — {as_of}</h2>"
        f"<p style='color:#667;font-size:13px'>Based on the latest close &amp; rankings. "
        f"Exit rule: <b>{rule}</b>. Model: {getattr(cfg,'scoring_model','?')}, "
        f"{cfg.num_stocks} holdings. This is advisory — review and log trades yourself.</p>"
        f"<div style='margin:12px 0'>{cards}</div>"
        f"{exits_html}{adds_html}{holdings_html}{rank_html}</div>")
