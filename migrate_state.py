import json
import os
import datetime
from database import SessionLocal, Strategy, Portfolio, CashFlow, PendingSwitch, init_db

STATE_FILE = '/Users/senthil/Documents/Personal/projects/quant-donchian-reporter/state.json'
DB_FILE = 'oracle.db'

def migrate():
    print("Initializing fresh database...")
    if os.path.exists(DB_FILE):
        os.remove(DB_FILE)
    init_db()
    import database
    db = next(database.get_db())
    
    print(f"Loading state from {STATE_FILE}...")
    with open(STATE_FILE, 'r') as f:
        state = json.load(f)
        
    for strat_key, strat_data in state.get('strategies', {}).items():
        name = strat_data['pair_name']
        print(f"Migrating {name}...")
        
        # Get strategy from db (seed_data creates them)
        strat = db.query(Strategy).filter(Strategy.name == name).first()
        if not strat:
            print(f"Strategy {name} not found in DB!")
            continue
            
        strat.current_signal_target = strat_data.get('current_signal_target')
        
        # Update Portfolios
        port1 = db.query(Portfolio).filter(Portfolio.strategy_id == strat.id, Portfolio.asset == 'ASSET1').first()
        port2 = db.query(Portfolio).filter(Portfolio.strategy_id == strat.id, Portfolio.asset == 'ASSET2').first()
        
        # Calculate proportional invested amount (assuming equal split or just attributing it to the active asset)
        # Since state.json only tracks a single 'invested_amount', we'll assign it to the one currently held
        port1.units = strat_data.get('units_1', 0.0)
        port2.units = strat_data.get('units_2', 0.0)
        
        if port1.units > 0 and port2.units == 0:
            port1.invested_amount = strat_data.get('invested_amount', 0.0)
            port2.invested_amount = 0.0
        elif port2.units > 0 and port1.units == 0:
            port2.invested_amount = strat_data.get('invested_amount', 0.0)
            port1.invested_amount = 0.0
        else:
            # If both have units, split proportionally or equally
            port1.invested_amount = strat_data.get('invested_amount', 0.0) / 2
            port2.invested_amount = strat_data.get('invested_amount', 0.0) / 2
            
        # Migrate Cash Flows
        db.query(CashFlow).filter(CashFlow.strategy_id == strat.id).delete()
        for cf in strat_data.get('cash_flows', []):
            date_obj = datetime.datetime.strptime(cf['date'], "%Y-%m-%d")
            db.add(CashFlow(
                strategy_id=strat.id,
                date=date_obj,
                amount=cf['amount'],
                flow_type='IMPORTED'
            ))
            
        # Check for pending switch
        if strat_data.get('pending_switch', False):
            print(f"Found pending switch for {name}")
            signal = strat_data.get('current_signal_target')
            from_asset = 'ASSET1' if signal == 'ASSET2' else 'ASSET2'
            to_asset = signal
            
            # The units to sell would be the units currently held in the 'from_asset'
            units_to_sell = port1.units if from_asset == 'ASSET1' else port2.units
            
            # Create a pending switch record
            db.add(PendingSwitch(
                strategy_id=strat.id,
                from_asset=from_asset,
                to_asset=to_asset,
                total_units_to_sell=units_to_sell,
                units_sold_so_far=0.0,
                status='PENDING'
            ))

    db.commit()
    db.close()
    print("Migration complete!")

if __name__ == '__main__':
    migrate()
