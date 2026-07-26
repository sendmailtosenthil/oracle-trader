"""Background position poller for the Zerodha Trades module.

Every ``poll_seconds`` (default 10) during market hours it fetches the position
book once, writes the snapshot, re-marks every monitored group and fires alerts
for any that crossed their stoploss or target.

It runs as a daemon **thread inside the existing oracle-api process** rather
than as its own service: that process is already always-on and already has
SQLAlchemy and the DB layer resident, so the poller costs a thread stack and the
HTTP client instead of a second ~85 MB interpreter. ``python -m
zerodha_trades.poller`` still runs it standalone for debugging.

Memory discipline, since the VPS is tight:
  * one ``requests`` session reused across cycles, rebuilt only when the
    enctoken rotates — no per-poll connection churn;
  * a fresh DB session per cycle, always closed, so nothing accumulates in the
    identity map;
  * only monitored groups are loaded, and the position book is a few dozen small
    dicts. No pandas, no instruments dump.
"""
import datetime
import logging
import threading
import time

import pytz

from common.database import get_db
from common.market_calendar import is_trading_day
from common.zerodha_client import FatalAuthError, ZerodhaClient
from zerodha_trades.services import alerts
from zerodha_trades.services import groups as G
from zerodha_trades.services import positions as P

log = logging.getLogger("ztrade.poller")

IST = pytz.timezone("Asia/Kolkata")
MARKET_OPEN = datetime.time(9, 15)
MARKET_CLOSE = datetime.time(15, 45)

IDLE_SLEEP = 60.0      # outside the window / nothing to watch — check back in a minute
ERROR_SLEEP = 30.0     # after a failure, back off before retrying


def market_is_open(now=None):
    """True on an NSE trading day between 09:15 and 15:45 IST."""
    now = now or datetime.datetime.now(IST)
    return is_trading_day(now) and MARKET_OPEN <= now.time() <= MARKET_CLOSE


def should_poll(settings, now=None):
    """``(ok, reason)`` — whether this cycle should hit the broker.

    The window is the rule: NSE trading days only, 09:15-15:45 IST. Test mode is
    the single, explicit way past it, for trying the poller out off-hours.
    """
    if not settings.poller_enabled:
        return False, "poller switched off"
    if settings.test_mode:
        return True, "test mode"
    now = now or datetime.datetime.now(IST)
    if not is_trading_day(now):
        return False, "not a trading day"
    if not (MARKET_OPEN <= now.time() <= MARKET_CLOSE):
        return False, "outside 09:15-15:45 IST"
    return True, "market open"


class Poller:
    """Owns the poll loop and the reused Kite client."""

    def __init__(self):
        self._client = None
        self._client_token = None
        self._stop = threading.Event()

    # ----- client reuse -------------------------------------------------
    def _client_for(self, enctoken, user_id):
        """One client (and TCP session) for the life of an enctoken."""
        if self._client is None or self._client_token != enctoken:
            self._client = ZerodhaClient(enctoken, user_id=user_id, pace_seconds=0)
            self._client_token = enctoken
        return self._client

    # ----- one cycle ----------------------------------------------------
    def poll_once(self, db):
        """Fetch, snapshot, mark and alert. Returns a short status string."""
        settings = G.get_settings(db)
        watched = G.monitored(db)
        if not watched:
            return "idle: no deployed groups"

        enctoken, user_id = P.broker_credentials(db)
        if not enctoken:
            return "error: no enctoken configured"

        client = self._client_for(enctoken, user_id)
        book = [P.normalize(r) for r in client.get_positions()]
        P.save_snapshot(db, book)

        live_map = P.as_map(book)
        marks = [G.mark_group(db, g, live_map) for g in watched]
        fired = G.apply_marks(db, marks, on_trigger=self._notify(settings))

        settings.last_poll_at = datetime.datetime.utcnow()
        status = f"ok: {len(book)} positions, {len(marks)} groups"
        if fired:
            status += f", {len(fired)} triggered"
        settings.last_poll_status = status
        db.commit()
        return status

    @staticmethod
    def _note_skip(db, settings, reason):
        """Record why a cycle was skipped, so the dashboard can explain silence."""
        status = f"idle: {reason}"
        if settings.last_poll_status != status:
            settings.last_poll_status = status
            db.commit()

    def _notify(self, settings):
        recipient = settings.alert_email or None

        def handler(group, pnl, trigger_type, message):
            log.warning("TRIGGER %s %s at %.2f", group.name, trigger_type, pnl)
            alerts.send_group_alert(group, pnl, trigger_type, message, recipient)

        return handler

    # ----- loop ---------------------------------------------------------
    def run_forever(self):
        log.info("Zerodha Trades poller started")
        while not self._stop.is_set():
            delay = IDLE_SLEEP
            db = None
            try:
                db = next(get_db())
                settings = G.get_settings(db)
                ok, reason = should_poll(settings)
                if not ok:
                    log.debug("skipping: %s", reason)
                    self._note_skip(db, settings, reason)
                else:
                    status = self.poll_once(db)
                    log.debug("%s", status)
                    # Only pace at the configured interval when actually polling;
                    # an idle cycle waits a minute instead of spinning.
                    delay = (max(1, settings.poll_seconds or 10)
                             if status.startswith("ok") else IDLE_SLEEP)
            except FatalAuthError as exc:
                log.error("enctoken rejected (%s) — waiting for a refresh", exc)
                self._client = self._client_token = None
                delay = IDLE_SLEEP
            except Exception:  # noqa: BLE001 - the loop must outlive any one cycle
                log.exception("poll cycle failed")
                delay = ERROR_SLEEP
            finally:
                if db is not None:
                    db.close()
            self._stop.wait(delay)
        log.info("Zerodha Trades poller stopped")

    def stop(self):
        self._stop.set()


_thread = None
_poller = None


def start_background():
    """Start the poller as a daemon thread. Idempotent."""
    global _thread, _poller
    if _thread is not None and _thread.is_alive():
        return _poller
    _poller = Poller()
    _thread = threading.Thread(target=_poller.run_forever,
                               name="ztrade-poller", daemon=True)
    _thread.start()
    return _poller


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [ztrade-poller] %(levelname)s %(message)s",
    )
    from common.database import init_db
    init_db()
    Poller().run_forever()


if __name__ == "__main__":
    main()
