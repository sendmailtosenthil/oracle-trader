"""Market-data downloader: download -> store CSV -> zip -> upload -> notify.

Pure orchestration logic (no Streamlit). Callable from the UI view, the bot
daemon, or a CLI. Downloads per trading day at minute resolution for NIFTY and
BANKNIFTY: spot index, India VIX (NIFTY only), all live futures and all live
option strikes (with OI), writes one CSV per instrument-type per day, zips each
month folder, uploads to Google Drive, persists stats and emails a summary.
"""
import datetime
import os
import shutil
import threading
import time
from dataclasses import dataclass, field

from common.zerodha_client import (
    ZerodhaClient,
    FatalAuthError,
    fetch_with_retry,
    NIFTY_INDEX_TOKEN,
    BANKNIFTY_INDEX_TOKEN,
)

DATA_ROOT = os.environ.get("DOWNLOADER_DATA_ROOT", "data")
DRIVE_ROOT_FOLDER = os.environ.get("DRIVE_ROOT_FOLDER", "QuantData")

# Parallelism + rate limiting (ported from quant-downloader's DownloaderService).
# Start with one worker and ramp up to MAX_CONCURRENCY, adding a worker every
# RAMP_STEP processed contracts. Each worker waits PACE_SECONDS between requests,
# so peak request rate is ~ MAX_CONCURRENCY / PACE_SECONDS (~20 req/s by default).
MAX_CONCURRENCY = int(os.environ.get("DOWNLOADER_MAX_WORKERS", "4"))
RAMP_STEP = int(os.environ.get("DOWNLOADER_RAMP_STEP", "50"))
PACE_SECONDS = float(os.environ.get("DOWNLOADER_PACE_SECONDS", "0.2"))

INDEX_HEADER = "symbol,timestamp,open,high,low,close,volume"
CONTRACT_HEADER = "symbol,expiry,strike,type,timestamp,open,high,low,close,volume,oi"

_MONTHS = ["jan", "feb", "mar", "apr", "may", "jun",
           "jul", "aug", "sep", "oct", "nov", "dec"]

# Per-underlying download config.
TASKS = [
    {"symbol": "NIFTY", "index_token": NIFTY_INDEX_TOKEN,
     "index_symbol": "NIFTY 50", "atm_step": 50, "want_vix": True},
    {"symbol": "BANKNIFTY", "index_token": BANKNIFTY_INDEX_TOKEN,
     "index_symbol": "NIFTY BANK", "atm_step": 100, "want_vix": False},
]


@dataclass
class FileResult:
    name: str
    rows: int = 0
    size_mb: float = 0.0


@dataclass
class DayResult:
    date: str
    symbol: str
    index_status: str = "skipped"
    vix_status: str = "skipped"
    futures_status: str = "skipped"
    options_status: str = "skipped"
    ce_instruments: int = 0
    pe_instruments: int = 0
    ce_rows: int = 0
    pe_rows: int = 0
    atm_strike: float = 0.0
    files: list = field(default_factory=list)  # list[FileResult]


@dataclass
class DownloadReport:
    start_date: str = ""
    end_date: str = ""
    symbols: list = field(default_factory=list)
    days: list = field(default_factory=list)          # list[DayResult]
    uploads: list = field(default_factory=list)        # list[{zip,status,error}]
    errors: list = field(default_factory=list)         # list[str]
    fatal: str = ""

    @property
    def trading_days(self):
        return sorted({d.date for d in self.days})

    @property
    def total_files(self):
        return sum(len(d.files) for d in self.days)

    @property
    def total_size_mb(self):
        return round(sum(f.size_mb for d in self.days for f in d.files), 2)


def run_download(
    enctoken,
    user_id="PC8006",
    start_date=None,
    end_date=None,
    symbols=None,
    skip_upload_today=True,
    upload=True,
    progress_cb=None,
    db=None,
):
    """Run the full download -> upload pipeline and return a ``DownloadReport``.

    ``start_date``/``end_date`` are ``datetime.date``. ``symbols`` filters TASKS
    (e.g. ``["NIFTY"]``). When ``db`` is provided, a ``DownloadStat`` row is
    persisted per (date, symbol). Email is NOT sent here — the caller decides
    (render with ``report_html`` and send via ``common.notifications.send_email``).
    """
    today = datetime.date.today()
    start_date = start_date or today
    end_date = end_date or today
    tasks = [t for t in TASKS if not symbols or t["symbol"] in symbols]

    report = DownloadReport(
        start_date=start_date.isoformat(),
        end_date=end_date.isoformat(),
        symbols=[t["symbol"] for t in tasks],
    )

    def emit(msg):
        if progress_cb:
            try:
                progress_cb(msg)
            except Exception:
                pass

    dates = _trading_days(start_date, end_date)
    if not dates:
        report.errors.append("No trading days in the selected range (weekends only).")
        return report

    try:
        emit("Loading instruments...")
        client = ZerodhaClient(enctoken, user_id=user_id, pace_seconds=0.0)
        if not client.validate():
            raise FatalAuthError("Invalid or expired enctoken.")
        client.load_instruments()
        vix_token = client.vix_token()

        os.makedirs(DATA_ROOT, exist_ok=True)
        touched_folders = {}  # folder_name -> requires_upload(bool)

        for task in tasks:
            for d in dates:
                date_str = d.isoformat()
                folder_name = f"{task['symbol'].lower()}-{_MONTHS[d.month - 1]}-{d.year}"
                folder_path = os.path.join(DATA_ROOT, folder_name)
                os.makedirs(folder_path, exist_ok=True)
                emit(f"{task['symbol']} {date_str}: downloading index...")

                day = _download_day(client, task, d, date_str, vix_token, emit)
                if day is None:
                    continue  # holiday / no data
                report.days.append(day)

                requires_upload = not (skip_upload_today and date_str == today.isoformat())
                touched_folders[folder_name] = touched_folders.get(folder_name, False) or requires_upload

                if db is not None:
                    _persist_stat(db, day, "completed" if requires_upload else "skipped")

        if upload and touched_folders:
            _zip_and_upload(touched_folders, report, emit)

    except FatalAuthError as exc:
        report.fatal = str(exc)
        report.errors.append(f"Fatal: {exc}")
    except Exception as exc:  # noqa: BLE001
        report.errors.append(f"Download failed: {exc}")

    return report


def _download_day(client, task, d, date_str, vix_token, emit):
    """Download all instrument types for one (symbol, day). Returns DayResult or None."""
    frm = datetime.datetime.combine(d, datetime.time(9, 15, 0))
    to = datetime.datetime.combine(d, datetime.time(15, 30, 0))
    sym = task["symbol"]
    day = DayResult(date=date_str, symbol=sym)

    # --- Index ---
    index_file = data_file_path(sym, date_str, "index")
    try:
        candles = fetch_with_retry(
            lambda: client.get_historical(task["index_token"], "minute", frm, to)
        )
        if not candles:
            # No candles for a weekday usually means a market holiday: skip the day.
            day.index_status = "no-data"
            return None
        _write_index_csv(index_file, task["index_symbol"], candles)
        day.atm_strike = round(candles[-1]["close"] / task["atm_step"]) * task["atm_step"]
        day.index_status = "completed"
        day.files.append(_file_result(index_file, len(candles)))
    except FatalAuthError:
        raise
    except Exception as exc:  # noqa: BLE001
        day.index_status = f"error: {exc}"
        return day

    # --- India VIX (NIFTY only) ---
    if task["want_vix"]:
        vix_file = data_file_path(sym, date_str, "vix")
        try:
            emit(f"{sym} {date_str}: downloading India VIX...")
            vix = fetch_with_retry(lambda: client.get_historical(vix_token, "minute", frm, to))
            if vix:
                _write_index_csv(vix_file, "INDIA VIX", vix)
                day.files.append(_file_result(vix_file, len(vix)))
            day.vix_status = "completed"
        except Exception as exc:  # noqa: BLE001
            day.vix_status = f"error: {exc}"

    # --- Futures ---
    fut_file = data_file_path(sym, date_str, "futures")
    futures = client.filter_instruments(sym, instrument_type="FUT", min_expiry=d)
    try:
        emit(f"{sym} {date_str}: downloading {len(futures)} futures...")
        rows, _, _ = _download_contracts(client, futures, frm, to, fut_file)
        day.futures_status = "completed"
        if rows:
            day.files.append(_file_result(fut_file, rows))
    except FatalAuthError:
        raise
    except Exception as exc:  # noqa: BLE001
        day.futures_status = f"error: {exc}"

    # --- Options ---
    opt_file = data_file_path(sym, date_str, "options")
    options = client.filter_instruments(sym, segment="NFO-OPT", min_expiry=d)
    day.ce_instruments = sum(1 for i in options if i["instrument_type"] == "CE")
    day.pe_instruments = sum(1 for i in options if i["instrument_type"] == "PE")
    try:
        emit(f"{sym} {date_str}: downloading {len(options)} option contracts...")
        rows, ce, pe = _download_contracts(client, options, frm, to, opt_file, atm_strike=day.atm_strike)
        day.options_status = "completed"
        day.ce_rows = ce
        day.pe_rows = pe
        if rows:
            day.files.append(_file_result(opt_file, rows))
    except FatalAuthError:
        raise
    except Exception as exc:  # noqa: BLE001
        day.options_status = f"error: {exc}"

    return day


def _download_contracts(client, instruments, frm, to, file_path, atm_strike=0):
    """Download many contracts in parallel and write one CSV. Returns (rows, ce_atm, pe_atm).

    Uses a self-ramping worker pool with per-worker rate limiting (ported from
    quant-downloader): begins with a single worker and adds one every RAMP_STEP
    processed contracts up to MAX_CONCURRENCY, each pausing PACE_SECONDS between
    requests. A fatal auth error stops every worker; per-contract failures are
    tolerated. The CSV is written once, single-threaded, after all fetches.
    """
    if not instruments:
        return 0, 0, 0

    results = {}            # tradingsymbol -> (instrument, candles)
    lock = threading.Lock()
    threads = []
    state = {"index": 0, "processed": 0, "workers": 0, "concurrency": 1, "fatal": None}
    n = len(instruments)

    def spawn_worker():
        t = threading.Thread(target=worker, daemon=True)
        with lock:
            threads.append(t)
        t.start()

    def worker():
        with lock:
            state["workers"] += 1
        try:
            while True:
                with lock:
                    if state["fatal"] is not None or state["index"] >= n:
                        break
                    i = state["index"]
                    state["index"] += 1
                instr = instruments[i]
                try:
                    candles = fetch_with_retry(
                        lambda: client.get_historical(
                            instr["instrument_token"], "minute", frm, to, oi=True
                        )
                    )
                    if candles:
                        with lock:
                            results[instr["tradingsymbol"]] = (instr, candles)
                except FatalAuthError as exc:
                    with lock:
                        state["fatal"] = exc
                    break
                except Exception:
                    # Per-contract failures are tolerated (status reported upstream).
                    pass

                ramp = False
                with lock:
                    state["processed"] += 1
                    if (state["processed"] % RAMP_STEP == 0
                            and state["concurrency"] < MAX_CONCURRENCY):
                        state["concurrency"] += 1
                        ramp = True
                if ramp:
                    spawn_worker()
                time.sleep(PACE_SECONDS)
        finally:
            with lock:
                state["workers"] -= 1

    spawn_worker()
    # Wait for all (including dynamically-spawned) workers to drain.
    while True:
        with lock:
            done = state["workers"] == 0 and (state["index"] >= n or state["fatal"] is not None)
        if done:
            break
        time.sleep(0.2)
    for t in threads:
        t.join()

    if state["fatal"] is not None:
        raise state["fatal"]

    total_rows = 0
    ce_atm = 0
    pe_atm = 0
    with open(file_path, "w") as f:
        f.write(CONTRACT_HEADER + "\n")
        for instr, candles in results.values():
            expiry = (instr.get("expiry") or "")[:10]
            strike = instr.get("strike", 0)
            itype = instr.get("instrument_type", "")
            for c in candles:
                f.write(
                    f"{instr['tradingsymbol']},{expiry},{strike},{itype},"
                    f"{c['timestamp']},{c['open']},{c['high']},{c['low']},"
                    f"{c['close']},{c['volume']},{c.get('oi', 0)}\n"
                )
                total_rows += 1
                if atm_strike and strike == atm_strike:
                    if itype == "CE":
                        ce_atm += 1
                    elif itype == "PE":
                        pe_atm += 1
    return total_rows, ce_atm, pe_atm


def _write_index_csv(file_path, symbol, candles):
    with open(file_path, "w") as f:
        f.write(INDEX_HEADER + "\n")
        for c in candles:
            f.write(
                f"{symbol},{c['timestamp']},{c['open']},{c['high']},"
                f"{c['low']},{c['close']},{c['volume']}\n"
            )


def _file_result(file_path, rows):
    size_mb = 0.0
    if os.path.exists(file_path):
        size_mb = round(os.path.getsize(file_path) / (1024 * 1024), 4)
    return FileResult(name=os.path.basename(file_path), rows=rows, size_mb=size_mb)


def _zip_and_upload(touched_folders, report, emit):
    uploader = None
    try:
        from downloader.services.drive import GoogleDriveUploader
        emit("Connecting to Google Drive...")
        uploader = GoogleDriveUploader()
        root_id = uploader.ensure_folder(DRIVE_ROOT_FOLDER, "root")
    except Exception as exc:  # noqa: BLE001
        report.errors.append(f"Drive auth failed, skipping upload: {exc}")
        return

    for folder_name, requires_upload in touched_folders.items():
        if not requires_upload:
            continue
        folder_path = os.path.join(DATA_ROOT, folder_name)
        zip_base = os.path.join(DATA_ROOT, folder_name)
        zip_path = zip_base + ".zip"
        zip_name = folder_name + ".zip"
        try:
            emit(f"Zipping {folder_name}...")
            shutil.make_archive(zip_base, "zip", root_dir=DATA_ROOT, base_dir=folder_name)
            emit(f"Uploading {zip_name} to Drive...")
            uploader.upload_file(zip_path, zip_name, root_id)
            report.uploads.append({"zip": zip_name, "status": "uploaded", "error": ""})
        except Exception as exc:  # noqa: BLE001
            report.uploads.append({"zip": zip_name, "status": "failed", "error": str(exc)})
            report.errors.append(f"Upload failed for {zip_name}: {exc}")


def _persist_stat(db, day, upload_status):
    """Insert or update a DownloadStat row for (date, symbol)."""
    from common.database import DownloadStat
    row = (
        db.query(DownloadStat)
        .filter(DownloadStat.date == day.date, DownloadStat.symbol == day.symbol)
        .first()
    )
    size_mb = round(sum(f.size_mb for f in day.files), 4)
    if row is None:
        row = DownloadStat(date=day.date, symbol=day.symbol)
        db.add(row)
    row.index_status = day.index_status
    row.vix_status = day.vix_status
    row.futures_status = day.futures_status
    row.options_status = day.options_status
    row.ce_instruments = day.ce_instruments
    row.pe_instruments = day.pe_instruments
    row.ce_rows = day.ce_rows
    row.pe_rows = day.pe_rows
    row.atm_strike = day.atm_strike
    row.file_size_mb = size_mb
    row.upload_status = upload_status
    db.commit()


def month_folder_name(symbol, d):
    """Folder name for a symbol's data in a given month, e.g. ``nifty-jun-2026``."""
    return f"{symbol.lower()}-{_MONTHS[d.month - 1]}-{d.year}"


def data_file_path(symbol, date_str, kind):
    """Resolve the local CSV path for a downloaded artifact.

    ``kind`` is one of ``index``/``vix``/``futures``/``options``. Used by both
    the downloader (when writing) and analytics (when reading) so the naming
    convention lives in one place.
    """
    d = datetime.date.fromisoformat(date_str)
    folder = month_folder_name(symbol, d)
    sym_lc = symbol.lower()
    names = {
        "index": f"{sym_lc}-index-{date_str}.csv",
        "vix": f"india-vix-{date_str}.csv",
        "futures": f"{sym_lc}-futures-{date_str}.csv",
        "options": f"{sym_lc}-options-{date_str}.csv",
    }
    return os.path.join(DATA_ROOT, folder, names[kind])


def atm_step_for(symbol):
    """ATM strike rounding step for a symbol (50 for NIFTY, 100 for BANKNIFTY)."""
    for t in TASKS:
        if t["symbol"] == symbol:
            return t["atm_step"]
    return 50


def _trading_days(start_date, end_date):
    """Weekdays between start and end inclusive (holidays filtered at download time)."""
    days = []
    d = start_date
    one = datetime.timedelta(days=1)
    while d <= end_date:
        if d.weekday() < 5:
            days.append(d)
        d += one
    return days


def report_html(report):
    """Render a :class:`DownloadReport` as an HTML email body."""
    trading_days = report.trading_days
    range_txt = report.start_date if report.start_date == report.end_date \
        else f"{report.start_date} → {report.end_date}"

    status_banner = (
        f"<p style='color:red;font-weight:bold;'>⚠️ FATAL: {report.fatal}</p>"
        if report.fatal else ""
    )

    rows_html = ""
    for d in report.days:
        files_txt = "<br>".join(
            f"{f.name} ({f.rows:,} rows, {f.size_mb:.2f} MB)" for f in d.files
        ) or "—"
        rows_html += f"""
        <tr>
            <td>{d.date}</td>
            <td>{d.symbol}</td>
            <td>{d.index_status}</td>
            <td>{d.vix_status}</td>
            <td>{d.futures_status}</td>
            <td>{d.options_status}</td>
            <td>{d.ce_instruments} / {d.pe_instruments}</td>
            <td>{d.ce_rows} / {d.pe_rows}</td>
            <td style="font-size:11px;">{files_txt}</td>
        </tr>"""

    uploads_html = "".join(
        f"<li>{u['zip']}: <b>{u['status']}</b>{(' — ' + u['error']) if u['error'] else ''}</li>"
        for u in report.uploads
    ) or "<li>No uploads.</li>"

    errors_html = ""
    if report.errors:
        items = "".join(f"<li>{e}</li>" for e in report.errors)
        errors_html = f"<h3 style='color:red;'>Errors</h3><ul>{items}</ul>"

    return f"""
    <h2>📥 Oracle Market-Data Download Report</h2>
    {status_banner}
    <ul>
        <li><b>Date range:</b> {range_txt}</li>
        <li><b>Symbols:</b> {', '.join(report.symbols)}</li>
        <li><b>Trading days downloaded:</b> {len(trading_days)} ({', '.join(trading_days) or '—'})</li>
        <li><b>Files written:</b> {report.total_files} ({report.total_size_mb:.2f} MB total)</li>
    </ul>
    <h3>Uploads to Google Drive</h3>
    <ul>{uploads_html}</ul>
    {errors_html}
    <h3>Per-day detail</h3>
    <table border="1" cellpadding="6" cellspacing="0" style="border-collapse:collapse;font-size:12px;">
        <tr style="background:#f0f0f0;">
            <th>Date</th><th>Symbol</th><th>Index</th><th>VIX</th>
            <th>Futures</th><th>Options</th><th>CE/PE contracts</th>
            <th>ATM CE/PE rows</th><th>Files</th>
        </tr>
        {rows_html}
    </table>
    """
