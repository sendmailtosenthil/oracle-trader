from database import get_db, CashFlow

def main():
    db = next(get_db())
    cfs = db.query(CashFlow).filter(CashFlow.flow_type == 'IMPORTED').all()
    count = 0
    for cf in cfs:
        cf.flow_type = 'INVESTMENT'
        count += 1
    db.commit()
    print(f"Updated {count} records.")

if __name__ == '__main__':
    main()
