import datetime
import database
from database import CashFlow, Strategy

def main():
    db = next(database.get_db())
    strat = db.query(Strategy).filter(Strategy.name == 'NIFTY vs GOLD').first()
    if strat:
        date_obj = datetime.datetime.strptime('2026-06-12', '%Y-%m-%d')
        # Check if already exists
        exists = db.query(CashFlow).filter(CashFlow.strategy_id == strat.id, CashFlow.amount == -52.52).first()
        if not exists:
            db.add(CashFlow(strategy_id=strat.id, date=date_obj, amount=-52.52, flow_type='RESIDUAL'))
            db.commit()
            print("Added missing cash flow of -52.52")
        else:
            print("Cash flow already exists.")
    db.close()

if __name__ == '__main__':
    main()
