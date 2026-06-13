import json
import datetime
from database import SessionLocal, Strategy, Trade, recalculate_portfolio_from_ledger

STATE_FILE = '/Users/senthil/Documents/Personal/projects/quant-donchian-reporter/state.json'

def fix_ledger():
    import database
    db = next(database.get_db())
    with open(STATE_FILE, 'r') as f:
        state = json.load(f)
        
    for strat_key, strat_data in state.get('strategies', {}).items():
        name = strat_data['pair_name']
        strat = db.query(Strategy).filter(Strategy.name == name).first()
        
        if not strat:
            continue
            
        units1 = strat_data.get('units_1', 0.0)
        units2 = strat_data.get('units_2', 0.0)
        invested = strat_data.get('invested_amount', 0.0)
        
        # Determine the initial date from cash_flows
        cfs = strat_data.get('cash_flows', [])
        initial_date = datetime.datetime.strptime(cfs[0]['date'], "%Y-%m-%d") if cfs else datetime.datetime.now()
        
        # Check if an initial trade already exists
        existing_trades = db.query(Trade).filter(Trade.strategy_id == strat.id, Trade.trade_type == 'INITIAL_IMPORT').count()
        if existing_trades == 0:
            if units1 > 0:
                price = invested / units1
                db.add(Trade(strategy_id=strat.id, date=initial_date, asset='ASSET1', trade_type='BUY', units=units1, price=price))
            if units2 > 0:
                price = invested / units2
                db.add(Trade(strategy_id=strat.id, date=initial_date, asset='ASSET2', trade_type='BUY', units=units2, price=price))
                
        db.commit()
        recalculate_portfolio_from_ledger(db, strat.id)
        print(f"Fixed {name}. Current units in Portfolio recalculated.")

    db.close()
    print("Ledger perfectly restored.")

if __name__ == '__main__':
    fix_ledger()
