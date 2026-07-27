"""Telegram notification transport.

Generic sender, mirroring :mod:`common.notifications`: it knows how to deliver a
message and nothing about what the message is for. Feature modules build their
own text and call :func:`send_message`.

Credentials come from the environment — never from source, since this repo is
public:

    TELEGRAM_BOT_TOKEN   from @BotFather, e.g. 8847942948:AAF...
    TELEGRAM_CHAT_ID     your chat/user id, e.g. 1594969534

Missing credentials are a no-op with a log line, exactly like the email path, so
a host without Telegram configured still runs.
"""
import logging
import os

import requests

log = logging.getLogger("telegram")

_API = "https://api.telegram.org/bot{token}/sendMessage"
_TIMEOUT = 10


def is_configured():
    return bool(os.environ.get("TELEGRAM_BOT_TOKEN")
                and os.environ.get("TELEGRAM_CHAT_ID"))


def send_message(text, chat_id=None, parse_mode="HTML", disable_preview=True):
    """Send ``text`` to Telegram. Returns True on success; never raises.

    Delivery is best-effort: a notification failing must not take down the
    poller that produced it.
    """
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = chat_id or os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        log.info("Telegram not configured — set TELEGRAM_BOT_TOKEN and "
                 "TELEGRAM_CHAT_ID to enable.")
        return False

    try:
        resp = requests.post(
            _API.format(token=token),
            json={
                "chat_id": str(chat_id),
                "text": text,
                "parse_mode": parse_mode,
                "disable_web_page_preview": disable_preview,
            },
            timeout=_TIMEOUT,
        )
        payload = resp.json() if resp.content else {}
        if resp.status_code == 200 and payload.get("ok"):
            return True
        # Telegram puts the useful part in `description`, not the status line.
        log.error("Telegram send failed (HTTP %s): %s", resp.status_code,
                  payload.get("description") or resp.text[:200])
        return False
    except Exception as exc:  # noqa: BLE001 - alerting is best-effort
        log.error("Telegram send failed: %s", exc)
        return False
