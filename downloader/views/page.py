"""Downloader page: trigger market-data downloads and view history."""
import datetime

import pandas as pd
import streamlit as st

from common.database import BrokerConfig, DownloadJob, DownloadStat
from common.broker import is_zerodha_token_valid
from common.notifications import send_email
from common.timez import today_ist, to_ist
from downloader.services import core


def render(db):
    st.title("📥 Market Data Downloader")
    st.write(
        "Download minute-level NIFTY / BANKNIFTY index, India VIX, futures and "
        "options from Zerodha, upload to Google Drive, and email a summary."
    )

    broker_config = db.query(BrokerConfig).filter(BrokerConfig.broker_name == 'ZERODHA').first()
    if not broker_config or not is_zerodha_token_valid(broker_config.enctoken, broker_config.user_id):
        st.error("🚨 Zerodha enctoken is missing or expired. Set it in **Broker Setup** first.")
        return

    st.success("Zerodha token is valid.")

    with st.form(key="download_form"):
        today = today_ist()
        default_start = today - datetime.timedelta(days=7)
        col1, col2 = st.columns(2)
        start_date = col1.date_input("Start date", value=default_start)
        end_date = col2.date_input("End date", value=today)

        symbols = st.multiselect(
            "Symbols", options=["NIFTY", "BANKNIFTY"], default=["NIFTY", "BANKNIFTY"]
        )
        col3, col4 = st.columns(2)
        do_upload = col3.checkbox("Upload to Google Drive", value=True)
        do_email = col4.checkbox("Email summary after run", value=True)
        skip_today = st.checkbox(
            "Skip uploading today's data (partial session before 3:30 PM)", value=True
        )

        submitted = st.form_submit_button("Download & Upload", type="primary")

    if submitted:
        if start_date > end_date:
            st.error("Start date must be on or before end date.")
            return
        if not symbols:
            st.error("Select at least one symbol.")
            return

        job = DownloadJob(
            job_type="manual",
            status="running",
            start_date=start_date.isoformat(),
            end_date=end_date.isoformat(),
            symbols=",".join(symbols),
        )
        db.add(job)
        db.commit()

        status_box = st.empty()
        progress_log = st.empty()
        messages = []

        def progress_cb(msg):
            messages.append(msg)
            progress_log.code("\n".join(messages[-12:]))

        with st.spinner("Downloading..."):
            report = core.run_download(
                enctoken=broker_config.enctoken,
                user_id=broker_config.user_id,
                start_date=start_date,
                end_date=end_date,
                symbols=symbols,
                skip_upload_today=skip_today,
                upload=do_upload,
                progress_cb=progress_cb,
                db=db,
            )

        # Update job + send email
        job.status = "failed" if (report.fatal or report.errors) else "completed"
        job.message = report.fatal or (report.errors[0] if report.errors else
                                       f"{len(report.trading_days)} day(s), {report.total_files} files")
        db.commit()

        if do_email:
            subject = ("📥 Oracle Download "
                       + ("FAILED" if report.fatal else "Report")
                       + f" — {report.start_date}..{report.end_date}")
            send_email(core.report_html(report), subject)

        if report.fatal:
            status_box.error(f"Fatal: {report.fatal}")
        elif report.errors:
            status_box.warning("Completed with errors: " + "; ".join(report.errors))
        else:
            status_box.success(
                f"Done — {len(report.trading_days)} trading day(s), "
                f"{report.total_files} files ({report.total_size_mb:.2f} MB)."
            )
        if report.uploads:
            st.write("**Uploads:**")
            st.table(pd.DataFrame(report.uploads))

    st.divider()
    _render_history(db)


def _render_history(db):
    st.subheader("Download History")

    stats = (
        db.query(DownloadStat)
        .order_by(DownloadStat.date.desc(), DownloadStat.symbol.asc())
        .limit(60)
        .all()
    )
    if stats:
        df = pd.DataFrame([{
            "Date": s.date, "Symbol": s.symbol, "Index": s.index_status,
            "VIX": s.vix_status, "Futures": s.futures_status, "Options": s.options_status,
            "ATM CE": s.ce_rows, "ATM PE": s.pe_rows, "Size (MB)": round(s.file_size_mb, 2),
            "Upload": s.upload_status,
        } for s in stats])
        st.dataframe(df, use_container_width=True, hide_index=True)
    else:
        st.info("No downloads recorded yet.")

    jobs = db.query(DownloadJob).order_by(DownloadJob.created_at.desc()).limit(10).all()
    if jobs:
        st.caption("Recent jobs")
        jdf = pd.DataFrame([{
            "Created": to_ist(j.created_at).strftime("%Y-%m-%d %H:%M") if j.created_at else "",
            "Type": j.job_type, "Range": f"{j.start_date}..{j.end_date}",
            "Symbols": j.symbols, "Status": j.status, "Message": j.message,
        } for j in jobs])
        st.dataframe(jdf, use_container_width=True, hide_index=True)
