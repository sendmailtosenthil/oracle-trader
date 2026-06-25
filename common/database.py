import os
from sqlalchemy import create_engine, Column, Integer, String, Float, Boolean, DateTime, ForeignKey, UniqueConstraint
from sqlalchemy.orm import declarative_base, sessionmaker
import datetime
import hashlib

Base = declarative_base()

class User(Base):
    __tablename__ = 'users'
    id = Column(Integer, primary_key=True)
    username = Column(String, unique=True, nullable=False)
    password_hash = Column(String, nullable=False)

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

class BrokerConfig(Base):
    __tablename__ = 'broker_config'
    id = Column(Integer, primary_key=True)
    broker_name = Column(String, unique=True, default='ZERODHA')
    user_id = Column(String, nullable=False, default='PC8006')
    enctoken = Column(String, nullable=False)

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

class MomentumConfig(Base):
    # Single-row configuration for the momentum strategy (Nifty500 weighted
    # momentum). Mirrors quant-momentum's config.js — factors stored as JSON.
    __tablename__ = 'momentum_config'
    id = Column(Integer, primary_key=True)
    investment = Column(Float, default=100000.0)        # initial capital (₹)
    num_stocks = Column(Integer, default=15)            # equal-weight holdings
    factors_json = Column(String, default='[{"months": 3, "weight": 0.40}, {"months": 6, "weight": 0.32}, {"months": 9, "weight": 0.28}]')
    vol_enabled = Column(Boolean, default=True)         # risk-adjust by volatility
    vol_months = Column(Integer, default=3)             # volatility lookback
    min_history_coverage = Column(Float, default=0.8)   # min fraction of expected bars
    replace_rank_threshold = Column(Integer, default=40)  # sell when rank > this
    reinvest_idle_cash = Column(Boolean, default=True)  # redeploy all cash on rebalance
    cash = Column(Float, default=100000.0)              # idle cash in hand
    capital_injected = Column(Float, default=0.0)       # extra capital from min-1 top-ups

class MomentumHolding(Base):
    # A currently-held position (one row per symbol). Recalculated from
    # MomentumTrade on every executed rebalance.
    __tablename__ = 'momentum_holdings'
    id = Column(Integer, primary_key=True)
    symbol = Column(String, unique=True, nullable=False)   # NSE ticker, e.g. 'MARUTI.NS'
    shares = Column(Integer, default=0)
    avg_cost = Column(Float, default=0.0)
    entry_date = Column(String, nullable=True)             # ISO date first bought

class MomentumTrade(Base):
    # Append-only trade ledger. Sells carry realized pnl + holding metadata.
    __tablename__ = 'momentum_trades'
    id = Column(Integer, primary_key=True)
    date = Column(String, nullable=False)                  # ISO trade date
    symbol = Column(String, nullable=False)
    side = Column(String, nullable=False)                  # 'BUY' or 'SELL'
    shares = Column(Integer, nullable=False)
    price = Column(Float, nullable=False)
    value = Column(Float, nullable=False)                  # shares * price (turnover)
    charges = Column(Float, default=0.0)                   # Zerodha charges (STT, txn, GST, stamp, DP)
    rank = Column(Integer, nullable=True)                  # momentum rank at trade time
    avg_cost = Column(Float, nullable=True)                # for sells: cost basis sold
    pnl = Column(Float, nullable=True)                     # realized pnl, net of charges (sells)
    reason = Column(String, nullable=True)                 # 'deploy' / 'rank>40' / 'left-index' ...
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

class MomentumRanking(Base):
    # Snapshot of a computed ranking run, keyed by (as_of, symbol). Persisted so
    # the dashboard can show the latest ranking without recomputing every load.
    __tablename__ = 'momentum_rankings'
    __table_args__ = (UniqueConstraint('as_of', 'symbol', name='uq_momentum_rankings_asof_symbol'),)
    id = Column(Integer, primary_key=True)
    as_of = Column(String, nullable=False)                 # ISO ranking date
    symbol = Column(String, nullable=False)
    rank = Column(Integer, nullable=False)                 # vol-adjusted rank
    raw_rank = Column(Integer, nullable=True)              # raw-momentum rank
    score = Column(Float, default=0.0)                     # final (vol-adjusted) score
    blended = Column(Float, default=0.0)                   # blended factor return
    r3m = Column(Float, nullable=True)
    r6m = Column(Float, nullable=True)
    r9m = Column(Float, nullable=True)
    vol = Column(Float, nullable=True)
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

def init_db(db_path='sqlite:///oracle.db'):
    global engine, SessionLocal
    engine = create_engine(db_path, connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    _ensure_columns()
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    seed_data()

def _ensure_columns():
    """Add columns introduced after a table was first created (SQLite create_all
    won't alter existing tables). Idempotent and additive."""
    from sqlalchemy import text
    wanted = {
        'momentum_trades': [('charges', 'FLOAT DEFAULT 0.0')],
        'momentum_rankings': [('raw_rank', 'INTEGER')],
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
    # Create Default User
    if not db.query(User).filter(User.username == 'senthil').first():
        user = User(username='senthil', password_hash=hash_password('L@ngTerm2026'))
        db.add(user)
        
    # Create NIFTY vs GOLD Strategy
    nifty_gold = db.query(Strategy).filter(Strategy.name == 'NIFTY vs GOLD').first()
    if not nifty_gold:
        nifty_gold = Strategy(name='NIFTY vs GOLD', window=25, asset1='NIFTYBEES.NS', asset2='GOLDBEES.NS', current_signal_target='ASSET1')
        db.add(nifty_gold)
        db.commit() # commit to get ID
        
        # Init portfolio
        db.add(Portfolio(strategy_id=nifty_gold.id, asset='ASSET1', units=0.0, invested_amount=0.0))
        db.add(Portfolio(strategy_id=nifty_gold.id, asset='ASSET2', units=0.0, invested_amount=0.0))
        
    # Create BANK vs IT Strategy
    bank_it = db.query(Strategy).filter(Strategy.name == 'BANK vs IT').first()
    if not bank_it:
        bank_it = Strategy(name='BANK vs IT', window=15, asset1='BANKBEES.NS', asset2='ITBEES.NS', current_signal_target='ASSET1')
        db.add(bank_it)
        db.commit()
        
        db.add(Portfolio(strategy_id=bank_it.id, asset='ASSET1', units=0.0, invested_amount=0.0))
        db.add(Portfolio(strategy_id=bank_it.id, asset='ASSET2', units=0.0, invested_amount=0.0))

    # Default momentum config (15-stock Nifty500 weighted-momentum strategy).
    if not db.query(MomentumConfig).first():
        db.add(MomentumConfig(investment=225000.0, num_stocks=15, cash=225000.0))

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
