"""downloader — market-data download module.

Downloads NIFTY/BANKNIFTY index, India VIX, futures and options from Zerodha
(via the shared ``common`` Kite client), uploads to Google Drive, backs up the
database, and reports by email. UI lives in ``downloader.views.page``.
"""
