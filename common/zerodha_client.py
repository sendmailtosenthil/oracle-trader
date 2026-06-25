"""Shared Zerodha / Kite client driven by an ``enctoken`` session.

This is the single place that knows how to talk to Kite using an enctoken
(the session token copied from a logged-in kite.zerodha.com browser session).
All other modules — token validation, the downloader, the bot — go through
``ZerodhaClient`` so the auth headers, instrument loading, retry/backoff and
rate-limiting logic live in exactly one place.

No Streamlit / no DB dependencies here on purpose: pure, reusable logic.
"""
import codecs
import csv
import time

import requests

# --- Well-known index instrument tokens (stable on Kite) ---
NIFTY_INDEX_TOKEN = 256265      # "NIFTY 50"
BANKNIFTY_INDEX_TOKEN = 260105  # "NIFTY BANK"
INDIA_VIX_TOKEN = 264969        # "INDIA VIX"

_KITE_HOST = "https://kite.zerodha.com"
_INSTRUMENTS_URL = "https://api.kite.trade/instruments"

# Instruments dump CSV column order (api.kite.trade/instruments), 0-indexed:
# 0 instrument_token, 1 exchange_token, 2 tradingsymbol, 3 name, 4 last_price,
# 5 expiry, 6 strike, 7 tick_size, 8 lot_size, 9 instrument_type, 10 segment, 11 exchange


class FatalAuthError(Exception):
    """Raised when the enctoken is invalid/expired — retrying will not help."""


def _is_fatal_auth(message):
    m = (message or "").lower()
    return "invalid token" in m or "access denied" in m or "token" in m and "expire" in m


def fetch_with_retry(fn, retries=3, backoff=2.0):
    """Run ``fn`` with retry/backoff. Fatal auth errors raise immediately.

    Mirrors quant-downloader's ``fetchWithRetry``: auth failures are fatal,
    HTTP 429 waits 10s, everything else uses exponential backoff (2s, 4s, 8s).
    """
    last_exc = None
    for attempt in range(retries):
        try:
            return fn()
        except FatalAuthError:
            raise
        except Exception as exc:  # noqa: BLE001 - we classify below
            msg = str(exc).lower()
            if _is_fatal_auth(msg):
                raise FatalAuthError(str(exc)) from exc
            last_exc = exc
            if attempt < retries - 1:
                is_rate_limit = "429" in msg or "too many requests" in msg
                wait = 10.0 if is_rate_limit else backoff * (2 ** attempt)
                time.sleep(wait)
            else:
                raise
    if last_exc:
        raise last_exc


class ZerodhaClient:
    """Thin Kite HTTP client authenticated via an ``enctoken``."""

    def __init__(self, enctoken, user_id="PC8006", pace_seconds=0.2):
        self.enctoken = enctoken
        self.user_id = user_id
        self.pace_seconds = pace_seconds
        self._session = requests.Session()
        # Loaded lazily by load_instruments()
        self.instruments = []          # list[dict]
        self._by_name = {}             # name -> list[dict] (e.g. "NIFTY" -> [...])

    # ----- low level ---------------------------------------------------
    def _headers(self):
        return {
            "Authorization": f"enctoken {self.enctoken}",
            "X-Kite-Version": "3",
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
            "Cookie": f"enctoken={requests.utils.quote(self.enctoken)}",
            "Referer": "https://kite.zerodha.com/orders",
            "Origin": "https://kite.zerodha.com",
        }

    def _get(self, path, params=None, timeout=30):
        params = dict(params or {})
        params["user_id"] = self.user_id
        resp = self._session.get(
            f"{_KITE_HOST}{path}", headers=self._headers(), params=params, timeout=timeout
        )
        # Kite returns JSON {status, data|message} for OMS endpoints.
        try:
            payload = resp.json()
        except ValueError as exc:
            raise RuntimeError(f"Bad/non-JSON response (HTTP {resp.status_code})") from exc
        if payload.get("status") == "success":
            return payload
        message = payload.get("message", f"API error (HTTP {resp.status_code})")
        if resp.status_code == 429:
            raise RuntimeError(f"429 too many requests: {message}")
        raise RuntimeError(message)

    # ----- auth --------------------------------------------------------
    def validate(self):
        """Return True if the enctoken can fetch the user profile."""
        if not self.enctoken:
            return False
        try:
            resp = self._session.get(
                f"{_KITE_HOST}/oms/user/profile/full",
                headers=self._headers(),
                params={"user_id": self.user_id},
                timeout=5,
            )
            return resp.status_code == 200
        except Exception:
            return False

    # ----- instruments -------------------------------------------------
    def nse_eq_token_map(self, timeout=60):
        """Stream the instruments dump and return only ``{tradingsymbol: token}``
        for NSE equity (segment NSE, type EQ).

        Low-memory: streams line-by-line and keeps just the small map (~2k
        entries) — never materialises the full ~100k-row master. Used by the
        momentum price refresh on memory-constrained hosts.
        """
        resp = self._session.get(_INSTRUMENTS_URL, timeout=timeout, stream=True)
        resp.raise_for_status()
        reader = csv.reader(codecs.iterdecode(resp.iter_lines(), "utf-8"))
        try:
            next(reader)  # header
        except StopIteration:
            return {}
        # Columns: instrument_token,exchange_token,tradingsymbol,name,last_price,
        # expiry,strike,tick_size,lot_size,instrument_type,segment,exchange
        out = {}
        for row in reader:
            if len(row) < 12:
                continue
            if row[10] == "NSE" and row[9] == "EQ":
                try:
                    out[row[2]] = int(row[0])
                except ValueError:
                    continue
        return out

    def load_instruments(self, keep_names=None, timeout=60):
        """Stream the instruments master and index it, keeping only what's needed.

        ``keep_names`` (e.g. ``{"NIFTY", "BANKNIFTY"}``) retains only those
        underlyings — turning the ~100k-row master into a few thousand rows,
        which keeps memory low on constrained hosts. Default ``None`` keeps
        everything. Parsed as a stream so the full CSV is never held in memory,
        and each kept row is a compact dict (only the fields downstream needs).
        """
        keep = set(keep_names) if keep_names else None
        resp = self._session.get(_INSTRUMENTS_URL, timeout=timeout, stream=True)
        resp.raise_for_status()
        reader = csv.reader(codecs.iterdecode(resp.iter_lines(), "utf-8"))
        try:
            next(reader)  # skip header
        except StopIteration:
            self.instruments = []
            self._by_name = {}
            return []

        instruments = []
        by_name = {}
        for row in reader:
            if len(row) < 12:
                continue
            name = row[3]
            if keep is not None and name not in keep:
                continue
            try:
                token = int(row[0])
            except ValueError:
                continue
            item = {
                "instrument_token": token,
                "tradingsymbol": row[2],
                "name": name,
                "expiry": row[5] or "",
                "strike": _safe_float(row[6]),
                "instrument_type": row[9],
                "segment": row[10],
            }
            instruments.append(item)
            if name:
                by_name.setdefault(name, []).append(item)
        self.instruments = instruments
        self._by_name = by_name
        return instruments

    def filter_instruments(self, name, instrument_type=None, segment=None, min_expiry=None):
        """Return instruments for an underlying name, optionally filtered.

        ``min_expiry`` (``datetime.date``) keeps only contracts expiring on or
        after that date — used to skip already-expired options/futures.
        """
        out = []
        for item in self._by_name.get(name, []):
            if instrument_type and item["instrument_type"] != instrument_type:
                continue
            if segment and item["segment"] != segment:
                continue
            if min_expiry:
                exp = _parse_expiry(item["expiry"])
                if exp is None or exp < min_expiry:
                    continue
            out.append(item)
        return out

    def vix_token(self):
        """Resolve the India VIX token from instruments, fall back to constant."""
        for item in self.instruments:
            if item["tradingsymbol"] == "INDIA VIX" and "INDICES" in item["segment"]:
                return item["instrument_token"]
        return INDIA_VIX_TOKEN

    # ----- historical --------------------------------------------------
    def get_historical(self, token, interval, frm, to=None, oi=False):
        """Fetch historical candles. ``frm``/``to`` are ``datetime`` objects.

        Returns a list of dicts with keys timestamp, open, high, low, close,
        volume and (when ``oi``) oi.
        """
        def fmt(dt):
            return dt.strftime("%Y-%m-%d %H:%M:%S")

        params = {"from": fmt(frm)}
        if to is not None:
            params["to"] = fmt(to)
        if oi:
            params["oi"] = "1"

        payload = self._get(f"/oms/instruments/historical/{token}/{interval}", params=params)
        candles = (payload.get("data") or {}).get("candles") or []
        out = []
        for c in candles:
            rec = {
                "timestamp": c[0],
                "open": c[1],
                "high": c[2],
                "low": c[3],
                "close": c[4],
                "volume": c[5],
            }
            if oi and len(c) > 6:
                rec["oi"] = c[6]
            out.append(rec)
        if self.pace_seconds:
            time.sleep(self.pace_seconds)
        return out


def _safe_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _parse_expiry(value):
    """Parse a Kite expiry string (``YYYY-MM-DD``) to a date, or None."""
    if not value:
        return None
    import datetime
    try:
        return datetime.datetime.strptime(value[:10], "%Y-%m-%d").date()
    except ValueError:
        return None
