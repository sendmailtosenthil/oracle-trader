import os
from sqlalchemy import create_engine, Column, Integer, String, Float, Boolean, DateTime, ForeignKey
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

# Singleton setup
engine = None
SessionLocal = None

def init_db(db_path='sqlite:///oracle.db'):
    global engine, SessionLocal
    engine = create_engine(db_path, connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    seed_data()

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
