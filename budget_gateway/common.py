import hashlib
import os
import sqlite3
from contextlib import contextmanager
from fastapi import Header, HTTPException


def owner(authorization: str = Header(default="")) -> str:
    allowed = os.getenv("API_KEYS", "local-demo-key").split(",")
    token = authorization.removeprefix("Bearer ") if authorization.startswith("Bearer ") else ""
    import hmac
    if not token or not any(hmac.compare_digest(token, key.strip()) for key in allowed if key.strip()):
        raise HTTPException(401, "Valid Bearer API key required")
    return hashlib.sha256(token.encode()).hexdigest()


@contextmanager
def connect(path):
    db = sqlite3.connect(path, timeout=15)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA journal_mode=WAL")
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
