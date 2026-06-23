"""common — cross-module shared infrastructure for Project Oracle.

Holds code reused by multiple feature modules (``bees``, ``downloader``):
the SQLAlchemy database/models, the enctoken-based Zerodha/Kite client, the
cached broker token-validity check, and email notifications.
"""
