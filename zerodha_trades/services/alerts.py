"""Alert delivery for triggered groups.

Deliberately thin: the poller decides *that* a group tripped, this decides *how*
the user hears about it. Email is the only channel wired up for now — add others
by extending :func:`send_group_alert`.
"""
import logging

from common.notifications import send_email

log = logging.getLogger("ztrade.alerts")


def send_group_alert(group, pnl, trigger_type, message, recipient=None):
    """Notify that ``group`` hit its target or stoploss. Never raises.

    A delivery failure must not stop the poller or lose the trigger — the group
    is already marked triggered in the database by the time we get here.
    """
    subject = (f"[Oracle] {group.name}: "
               f"{'TARGET' if trigger_type == 'TARGET' else 'STOPLOSS'} hit "
               f"({_money(pnl)})")
    try:
        send_email(_html(group, pnl, message), subject,
                   **({'receiver_email': recipient} if recipient else {}))
        return True
    except Exception:  # noqa: BLE001 - alerting is best-effort
        log.exception("failed to send alert for group %s", group.name)
        return False


def _money(value):
    return f"{'+' if value >= 0 else '-'}₹{abs(value):,.2f}"


def _html(group, pnl, message):
    colour = "#0a7d33" if pnl >= 0 else "#c0392b"
    return f"""\
<html><body style="font-family:-apple-system,Segoe UI,Roboto,sans-serif">
  <h2 style="margin-bottom:4px">{group.name}</h2>
  <p style="margin-top:0;color:#555">{message}</p>
  <table cellpadding="6" style="border-collapse:collapse;font-size:14px">
    <tr><td style="color:#555">Group P&amp;L</td>
        <td style="color:{colour};font-weight:600">{_money(pnl)}</td></tr>
    <tr><td style="color:#555">Stoploss</td>
        <td>{_money(group.stoploss) if group.stoploss is not None else '—'}</td></tr>
    <tr><td style="color:#555">Target</td>
        <td>{_money(group.target) if group.target is not None else '—'}</td></tr>
  </table>
  <p style="color:#888;font-size:12px">
    Project Oracle — Zerodha Trades. This is an alert only; nothing was traded.
  </p>
</body></html>"""
