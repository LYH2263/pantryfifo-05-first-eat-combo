import os, sqlite3
from contextlib import contextmanager
from pathlib import Path

def db_path() -> Path:
    d = Path(os.environ.get("DATA_DIR", Path(__file__).resolve().parent.parent / "data"))
    d.mkdir(parents=True, exist_ok=True)
    return d / "pantryfifo.db"

def connect():
    c = sqlite3.connect(db_path(), timeout=10, isolation_level=None)
    c.row_factory = sqlite3.Row
    # WAL allows a writer and readers to coexist; busy_timeout makes would-be
    # writers wait instead of failing with "database is locked".
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA busy_timeout=10000")
    c.execute("PRAGMA foreign_keys=ON")
    return c

@contextmanager
def write_tx(c):
    """Serialized write transaction.

    BEGIN IMMEDIATE acquires the RESERVED lock up front, so concurrent
    mutations (combo confirm vs single-item consume / off-shelf sweep)
    serialize here instead of losing updates inside a deferred transaction.
    """
    c.execute("BEGIN IMMEDIATE")
    try:
        yield
        c.execute("COMMIT")
    except Exception:
        c.execute("ROLLBACK")
        raise
