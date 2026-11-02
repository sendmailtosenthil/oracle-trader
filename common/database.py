import os
from sqlalchemy import create_engine, event, Column, Integer, String, Float, Boolean, DateTime, ForeignKey, UniqueConstraint
from sqlalchemy.orm import declarative_base, sessionmaker
import datetime
import hashlib

Base = declarative_base()

class User(Base):
    """A login. Users are managed in the app (Setup › User Management), not in
    config: ``permissions`` is the JSON map described in
    :mod:`common.permissions`, and ``is_admin`` overrides it with full access.
    """
    __tablename__ = 'users'
    id = Column(Integer, primary_key=True)
    username = Column(String, unique=True, nullable=False)
    password_hash = Column(String, nullable=False)
    is_admin = Column(Boolean, default=False)
    permissions = Column(String, nullable=True)   # JSON: {page_key: none|read|edit}
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

class Strategy(Base):
    __tablename__ = 'strategies'
    id = Column(Integer, primary_key=True)
    name = Column(String, unique=True, nullable=False)
    window = Column(Integer, default=25)
    asset1 = Column(String, nullable=False)
    asset2 = Column(String, nullable=False)
    current_signal_target = Column(String, nullable=True) # e.g. 'ASSET1' or 'ASSET2'
    
class Portfolio(Base):
    __tablename__ = 'portfolio'
    id = Column(Integer, primary_key=True)
    strategy_id = Column(Integer, ForeignKey('strategies.id'))
    asset = Column(String, nullable=False) # e.g. 'ASSET1' or 'ASSET2'
    units = Column(Float, default=0.0)
    invested_amount = Column(Float, default=0.0) # Used for ROI calculation

class CashFlow(Base):
    __tablename__ = 'cash_flows'
    id = Column(Integer, primary_key=True)
    strategy_id = Column(Integer, ForeignKey('strategies.id'))
    date = Column(DateTime, default=datetime.datetime.utcnow)
    amount = Column(Float, nullable=False) # Negative for investments (cash out), positive for withdrawals (cash in)
    flow_type = Column(String) # 'INITIAL', 'SIP'

class Trade(Base):
    __tablename__ = 'trades'
    id = Column(Integer, primary_key=True)
    strategy_id = Column(Integer, ForeignKey('strategies.id'))
    date = Column(DateTime, default=datetime.datetime.utcnow)
    asset = Column(String, nullable=False) # 'ASSET1' or 'ASSET2'
    trade_type = Column(String, nullable=False) # 'BUY' or 'SELL'
    units = Column(Float, nullable=False)
    price = Column(Float, nullable=False)
    charges = Column(Float, default=0.0) # total Zerodha charges (STT, txn, GST, stamp, DP, pledge)
    charges_breakdown = Column(String, nullable=True) # JSON itemization of `charges`
    pledge = Column(Boolean, default=False) # user-flagged: add pledge request charge to this trade

class PendingSwitch(Base):
    __tablename__ = 'pending_switches'
    id = Column(Integer, primary_key=True)
    strategy_id = Column(Integer, ForeignKey('strategies.id'))
    from_asset = Column(String, nullable=False) # 'ASSET1' or 'ASSET2'
    to_asset = Column(String, nullable=False) # 'ASSET1' or 'ASSET2'
    total_units_to_sell = Column(Float, nullable=False)
    units_sold_so_far = Column(Float, default=0.0)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    status = Column(String, default='PENDING') # 'PENDING', 'COMPLETED'

class AuthSession(Base):
    # A remembered browser login. Only the SHA-256 of the token is stored, so a
    # copy of the database does not hand over live sessions.
    __tablename__ = 'auth_sessions'
    id = Column(Integer, primary_key=True)
    token_hash = Column(String, unique=True, nullable=False, index=True)
    username = Column(String, nullable=False)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    expires_at = Column(DateTime, nullable=False)


class BrokerConfig(Base):
    """One Zerodha login. See :mod:`common.broker` for the multi-account layout.

    ``owner`` is the app username that added the account: only that user (and
    administrators) may see or change its password / TOTP secret, though anyone
    with access to the Setup pages can see that the account exists.

    ``password_enc`` and ``totp_enc`` hold ciphertext from
    :mod:`common.secrets` — never the raw values. The nightly backup ships this
    whole database to Google Drive, so the key lives outside it.
    """
    __tablename__ = 'broker_config'
    id = Column(Integer, primary_key=True)
    broker_name = Column(String, unique=True, default='ZERODHA')
    user_id = Column(String, nullable=False, default='PC8006')
    enctoken = Column(String, nullable=False)
    owner = Column(String, nullable=True)          # users.username, NULL = unclaimed
    password_enc = Column(String, nullable=True)   # Kite login password (ciphertext)
    totp_enc = Column(String, nullable=True)       # base32 TOTP secret (ciphertext)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

class DownloadJob(Base):
    __tablename__ = 'download_jobs'
    id = Column(Integer, primary_key=True)
    job_type = Column(String, default='manual')  # 'manual' or 'auto'
    status = Column(String, default='pending')    # pending/running/completed/failed
    start_date = Column(String, nullable=False)   # ISO date string
    end_date = Column(String, nullable=False)
    symbols = Column(String, nullable=True)       # comma-separated, e.g. 'NIFTY,BANKNIFTY'
    message = Column(String, nullable=True)       # summary / error text
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

class DownloadStat(Base):
    # Mirrors quant-downloader's `stats` table (date+symbol keyed), with a few
    # additions: futures split out from options, and the ATM strike recorded.
    __tablename__ = 'download_stats'
    __table_args__ = (UniqueConstraint('date', 'symbol', name='uq_download_stats_date_symbol'),)
    id = Column(Integer, primary_key=True)
    date = Column(String, nullable=False)         # ISO date string
    symbol = Column(String, nullable=False)       # 'NIFTY' or 'BANKNIFTY'
    index_status = Column(String, default='skipped')
    vix_status = Column(String, default='skipped')
    futures_status = Column(String, default='skipped')
    options_status = Column(String, default='skipped')
    ce_instruments = Column(Integer, default=0)   # count of CE option contracts in the chain
    pe_instruments = Column(Integer, default=0)   # count of PE option contracts in the chain
    ce_rows = Column(Integer, default=0)          # ATM call rows
    pe_rows = Column(Integer, default=0)          # ATM put rows
    atm_strike = Column(Float, default=0.0)
    file_size_mb = Column(Float, default=0.0)
    upload_status = Column(String, default='skipped')
    created_at = Column(DateTime, default=datetime.datetime.utcnow)


# Singleton setup
engine = None
SessionLocal = None

# SQLite's default rollback journal gives a writer an exclusive lock on the
# whole file, so every reader blocks until the write finishes — and with
# synchronous=FULL each commit waits on an fsync. The cron jobs and the enctoken
# API write while the web app reads the same file on its own timers, so on a slow
# disk they spend their time queueing behind each other and the UI stalls (up to
# busy_timeout) for something that should take no time.
#
# WAL fixes it properly: writers append to a side log, readers are never blocked,
# and one writer at a time is all this app ever has. synchronous=NORMAL is the
# documented safe pairing — a crash can lose the tail of the last transaction but
# cannot corrupt the database.
#
# journal_mode is persisted in the file header, so it only takes once; the other
# two are per-connection and must be set on every connect.
_PRAGMAS = (
    "PRAGMA journal_mode=WAL",
    "PRAGMA synchronous=NORMAL",
    "PRAGMA busy_timeout=10000",
)


def _apply_pragmas(dbapi_connection, _record):
    cursor = dbapi_connection.cursor()
    try:
        for pragma in _PRAGMAS:
            cursor.execute(pragma)
    finally:
        cursor.close()


def init_db(db_path='sqlite:///oracle.db'):
    global engine, SessionLocal
    engine = create_engine(db_path, connect_args={"check_same_thread": False})
    event.listen(engine, "connect", _apply_pragmas)
    Base.metadata.create_all(bind=engine)
    _ensure_columns()
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    seed_data()

def _ensure_columns():
    """Add columns introduced after a table was first created (SQLite create_all
    won't alter existing tables). Idempotent and additive."""
    from sqlalchemy import text
    wanted = {
        # Logins predating per-page permissions were all full-access admins.
        'users': [('is_admin', 'BOOLEAN DEFAULT 1'),
                  ('permissions', 'VARCHAR'),
                  ('created_at', 'DATETIME')],
        # Bees trades gained charge tracking after the first databases were built.
        'trades': [('charges', 'FLOAT DEFAULT 0.0'),
                   ('charges_breakdown', 'VARCHAR'),
                   ('pledge', 'BOOLEAN DEFAULT 0')],
        'broker_config': [('owner', 'VARCHAR'),
                          ('password_enc', 'VARCHAR'),
                          ('totp_enc', 'VARCHAR'),
                          ('created_at', 'DATETIME')],
    }
    with engine.begin() as conn:
        for table, cols in wanted.items():
            existing = {row[1] for row in conn.execute(text(f"PRAGMA table_info({table})"))}
            for name, decl in cols:
                if name not in existing:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {decl}"))

def get_db():
    global SessionLocal
    if SessionLocal is None:
        init_db()
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def hash_password(password):
    return hashlib.sha256(password.encode()).hexdigest()

def seed_data():
    db = SessionLocal()
    # Logins live in this table and are managed in the app (Setup › User
    # Management). The environment only bootstraps the very first administrator
    # on an empty database — it is not the source of truth, so a password
    # changed in the app is never silently reverted on the next restart.
    # Locked out? `python -m scripts.manage_users passwd <user>` on the host.
    if not db.query(User).first():
        admin_user = os.environ.get('ORACLE_ADMIN_USER', 'senthil')
        admin_pass = os.environ.get('ORACLE_ADMIN_PASSWORD')
        if admin_pass:
            db.add(User(username=admin_user, password_hash=hash_password(admin_pass),
                        is_admin=True))
        else:
            print("WARNING: no users exist and ORACLE_ADMIN_PASSWORD is not set — "
                  "no admin user created. Set it in .env and restart to enable login.")


    # Create NIFTY vs GOLD Strategy
    nifty_gold = db.query(Strategy).filter(Strategy.name == 'NIFTY vs GOLD').first()
    if not nifty_gold:
        nifty_gold = Strategy(name='NIFTY vs GOLD', window=25, asset1='NIFTYBEES.NS', asset2='GOLDBEES.NS', current_signal_target='ASSET1')
        db.add(nifty_gold)
        db.commit() # commit to get ID
        
        # Init portfolio
        db.add(Portfolio(strategy_id=nifty_gold.id, asset='ASSET1', units=0.0, invested_amount=0.0))
        db.add(Portfolio(strategy_id=nifty_gold.id, asset='ASSET2', units=0.0, invested_amount=0.0))

    db.commit()
    db.close()

def recalculate_portfolio_from_ledger(db, strategy_id):
    trades = db.query(Trade).filter(Trade.strategy_id == strategy_id).order_by(Trade.date.asc()).all()
    
    asset_state = {
        'ASSET1': {'units': 0.0, 'invested_amount': 0.0, 'average_buy_price': 0.0},
        'ASSET2': {'units': 0.0, 'invested_amount': 0.0, 'average_buy_price': 0.0}
    }
    
    for t in trades:
        state = asset_state[t.asset]
        if t.trade_type == 'BUY':
            total_cost = t.units * t.price
            new_total_units = state['units'] + t.units
            if new_total_units > 0:
                current_value_at_cost = state['units'] * state['average_buy_price']
                state['average_buy_price'] = (current_value_at_cost + total_cost) / new_total_units
            state['units'] = new_total_units
            state['invested_amount'] += total_cost
        elif t.trade_type == 'SELL':
            if state['units'] > 0:
                proportion_sold = t.units / state['units']
                state['invested_amount'] -= (state['invested_amount'] * proportion_sold)
            state['units'] -= t.units
            if state['units'] <= 0.0001:
                state['units'] = 0.0
                state['invested_amount'] = 0.0
                state['average_buy_price'] = 0.0

    portfolios = db.query(Portfolio).filter(Portfolio.strategy_id == strategy_id).all()
    for p in portfolios:
        if p.asset in asset_state:
            p.units = asset_state[p.asset]['units']
            p.invested_amount = asset_state[p.asset]['invested_amount']
            
    db.commit()

if __name__ == "__main__":
    init_db()
    print("Database initialized successfully.")
