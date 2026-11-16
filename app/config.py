import os
from pathlib import Path

APP_VERSION = os.getenv("APP_VERSION", "FC-2026.11")
DATABASE_PATH = Path(os.getenv("DATABASE_PATH", "data/runs.sqlite3"))
EVIDENCE_DATABASE_PATH = Path(os.getenv("EVIDENCE_DATABASE_PATH", "data/evidence.sqlite3"))
RULE_VERSION = "flightclock-rules-1"
TIMEZONE = "Asia/Shanghai"
AUDIT_ROLE = "audit"
MAX_CONNECTION_MINUTES = 720
