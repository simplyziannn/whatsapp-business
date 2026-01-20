import os
import psycopg2


def db_conn():
    # Read at call-time so it works after load_dotenv()
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL is not set. Set it in your shell or .env")

    # Railway Postgres commonly requires SSL; local Postgres commonly doesn't.
    # Allow overriding via DB_SSLMODE if needed.
    sslmode = os.getenv("DB_SSLMODE")  # e.g. "require", "disable"
    if sslmode is None:
        # sensible default: require SSL for hosted DBs, disable for localhost
        sslmode = "disable" if "localhost" in database_url or "127.0.0.1" in database_url else "require"

    conn = psycopg2.connect(
        database_url,
        connect_timeout=5,
        sslmode=sslmode,
    )
    conn.autocommit = True
    return conn
