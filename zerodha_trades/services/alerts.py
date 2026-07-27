"""Alert delivery for triggered groups.

Deliberately thin: the poller decides *that* a group tripped, this decides *how*
the user hears about it. Each group picks its own channels — email, Telegram,
both, or neither — and every channel is best-effort: a failure is logged and the
others still go out, because the group is already marked triggered by the time
we get here and losing the notification must not lose the fact.
"""
import logging

from common.notifications import send_email
from common.telegram import send_message

log = logging.getLogger("ztrade.alerts")

EMAIL = 'email'
TELEGRAM = 'telegram'
CHANNELS = (EMAIL, TELEGRAM)
LABELS = {EMAIL: 'Email', TELEGRAM: 'Telegram'}


def channels_of(group):
    """The channels a group should notify on. Defined by the group service."""
    from zerodha_trades.services.groups import channels_of as _of
    return _of(group)


def send_group_alert(group, pnl, trigger_type, message, recipient=None):
    """Notify that ``group`` hit its target or stoploss. Never raises.

    Returns the channels that actually delivered.
    """
    wanted = channels_of(group)
    if not wanted:
        log.info("no channels enabled for group %s — not notifying", group.name)
        return []

    delivered = []
    if EMAIL in wanted and _send_email(group, pnl, trigger_type, message, recipient):
        delivered.append(EMAIL)
    if TELEGRAM in wanted and _send_telegram(group, pnl, trigger_type, message):
        delivered.append(TELEGRAM)
    if not delivered:
        log.error("all channels failed for group %s", group.name)
    return delivered


def _subject(group, pnl, trigger_type):
    return (f"[Oracle] {group.name}: "
            f"{'TARGET' if trigger_type == 'TARGET' else 'STOPLOSS'} hit "
            f"({_money(pnl)})")


def _send_email(group, pnl, trigger_type, message, recipient):
    # send_email quietly no-ops without credentials, so check first rather than
    # report a delivery that never happened.
    import os
    if not (os.environ.get('GMAIL_USER') and os.environ.get('GMAIL_PASS')):
        log.info("email not configured — skipping for group %s", group.name)
        return False
    try:
        send_email(_html(group, pnl, message), _subject(group, pnl, trigger_type),
                   **({'receiver_email': recipient} if recipient else {}))
        return True
    except Exception:  # noqa: BLE001 - best-effort
        log.exception("email alert failed for group %s", group.name)
        return False


def _send_telegram(group, pnl, trigger_type, message):
    icon = "🎯" if trigger_type == 'TARGET' else "🛑"
    text = (
        f"{icon} <b>{_esc(group.name)}</b> <code>{_esc(group.user_id or '')}</code>\n"
        f"{_esc(message)}\n\n"
        f"P&amp;L: <b>{_money(pnl)}</b>\n"
        f"Stoploss {_money(group.stoploss)} · Target {_money(group.target)}\n\n"
        f"<i>Alert only — nothing was traded.</i>"
    )
    return send_message(text)


def _esc(text):
    """Escape for Telegram's HTML parse mode."""
    return (str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def _money(value):
    if value is None:
        return "—"
    return f"{'+' if value >= 0 else '-'}₹{abs(value):,.2f}"


def _html(group, pnl, message):
    colour = "#0a7d33" if pnl >= 0 else "#c0392b"
    return f"""\
<html><body style="font-family:-apple-system,Segoe UI,Roboto,sans-serif">
  <h2 style="margin-bottom:4px">{group.name} <small style="color:#888">{group.user_id or ''}</small></h2>
  <p style="margin-top:0;color:#555">{message}</p>
  <table cellpadding="6" style="border-collapse:collapse;font-size:14px">
    <tr><td style="color:#555">Group P&amp;L</td>
        <td style="color:{colour};font-weight:600">{_money(pnl)}</td></tr>
    <tr><td style="color:#555">Stoploss</td><td>{_money(group.stoploss)}</td></tr>
    <tr><td style="color:#555">Target</td><td>{_money(group.target)}</td></tr>
  </table>
  <p style="color:#888;font-size:12px">
    Project Oracle — Zerodha Trades. This is an alert only; nothing was traded.
  </p>
</body></html>"""
