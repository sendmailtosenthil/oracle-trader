import database
from database import init_db, Base

def main():
    print("Creating new tables...")
    Base.metadata.create_all(bind=database.engine)
    print("Done.")

if __name__ == '__main__':
    init_db()
    main()
