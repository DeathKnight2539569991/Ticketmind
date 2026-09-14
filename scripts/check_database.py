from sqlalchemy import text
def main():
    try:
        from ticketmind.db.engine import engine

        with engine.connect() as connection:
            database_name,user_name=connection.execute(
                text("SELECT current_database(), current_user")
            ).one()
    except Exception as error:
        print(f"Database connection check failed: {type(error).__name__}: {error}")
        return 1
    print(f"Connected to database: {database_name}, user: {user_name}")
    return 0
if __name__ == "__main__":
    raise SystemExit(main())