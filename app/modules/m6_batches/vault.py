"""证据库（vault）：与业务库物理隔离的独立 SQLite 文件。

- retained_evidence：涉争议航链的可核验原件（含外部行程号），仅审计角色可查。
- clearance_credentials：解除留存后留下的计数凭据，绝不含外部行程号或可还原原值。
- vault_audit_log：每次证据访问与留存变更的审计轨迹。
"""
import sqlite3
from app.config import EVIDENCE_DATABASE_PATH
from app.errors import DomainError

SCHEMA = """
CREATE TABLE IF NOT EXISTS retained_evidence (
    hold_id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL,
    external_id TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    leg_count INTEGER NOT NULL,
    quarantine_count INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    released_at TEXT
);
CREATE TABLE IF NOT EXISTS clearance_credentials (
    request_id TEXT PRIMARY KEY,
    hold_id TEXT,
    leg_count INTEGER NOT NULL,
    quarantine_count INTEGER NOT NULL,
    reason_code TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS vault_audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    action TEXT NOT NULL,
    role TEXT NOT NULL,
    hold_id TEXT,
    request_id TEXT,
    reason TEXT,
    allowed INTEGER NOT NULL,
    at TEXT NOT NULL
);
"""


def connect() -> sqlite3.Connection:
    EVIDENCE_DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(EVIDENCE_DATABASE_PATH)
    connection.row_factory = sqlite3.Row
    return connection


def init_vault() -> None:
    with connect() as db:
        db.executescript(SCHEMA)


# ---------- 审计日志（越权访问也留痕） ----------

def audit(action: str, role: str, allowed: bool, reason: str | None = None,
          hold_id: str | None = None, request_id: str | None = None) -> None:
    from datetime import datetime, timezone
    init_vault()
    with connect() as db:
        db.execute(
            "INSERT INTO vault_audit_log(action, role, hold_id, request_id, reason, allowed, at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (action, role, hold_id, request_id, reason, int(allowed),
             datetime.now(timezone.utc).isoformat()),
        )


def list_audit(limit: int = 100) -> list[dict]:
    init_vault()
    with connect() as db:
        rows = db.execute("SELECT * FROM vault_audit_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(row) for row in rows]


# ---------- 角色控制：审计角色 + 理由，二者缺一即拒绝 ----------

def require_auditor(role: str, reason: str | None, action: str,
                    hold_id: str | None = None, request_id: str | None = None) -> None:
    allowed = role == "audit" and bool(reason and reason.strip())
    audit(action, role or "anonymous", allowed, reason, hold_id, request_id)
    if not allowed:
        raise DomainError("EVIDENCE_FORBIDDEN",
                          "证据仅限审计角色凭查阅理由访问", status=403)


# ---------- 隔离留存原件 ----------

def retain_evidence(hold_id: str, request_id: str, external_id: str, reason_code: str,
                    payload_json: str, leg_count: int, quarantine_count: int, created_at: str) -> None:
    init_vault()
    with connect() as db:
        db.execute(
            "INSERT OR IGNORE INTO retained_evidence VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)",
            (hold_id, request_id, external_id, reason_code, payload_json,
             leg_count, quarantine_count, created_at),
        )


def get_evidence(hold_id: str) -> sqlite3.Row | None:
    init_vault()
    with connect() as db:
        return db.execute("SELECT * FROM retained_evidence WHERE hold_id = ?", (hold_id,)).fetchone()


def list_evidence() -> list[dict]:
    init_vault()
    with connect() as db:
        rows = db.execute("SELECT * FROM retained_evidence WHERE released_at IS NULL ORDER BY created_at").fetchall()
        return [dict(row) for row in rows]


# ---------- 计数凭据（不含行程号） ----------

def write_credential(request_id: str, hold_id: str | None, leg_count: int,
                     quarantine_count: int, reason_code: str, created_at: str) -> None:
    init_vault()
    with connect() as db:
        db.execute(
            "INSERT OR IGNORE INTO clearance_credentials VALUES (?, ?, ?, ?, ?, ?)",
            (request_id, hold_id, leg_count, quarantine_count, reason_code, created_at),
        )


def get_credential(request_id: str) -> sqlite3.Row | None:
    init_vault()
    with connect() as db:
        return db.execute("SELECT * FROM clearance_credentials WHERE request_id = ?",
                          (request_id,)).fetchone()


def list_credentials() -> list[dict]:
    init_vault()
    with connect() as db:
        rows = db.execute("SELECT * FROM clearance_credentials ORDER BY created_at").fetchall()
        return [dict(row) for row in rows]


# ---------- 解除留存：销毁原件，只留计数凭据 ----------

def release_evidence(hold_id: str, released_at: str) -> bool:
    init_vault()
    with connect() as db:
        cursor = db.execute(
            "UPDATE retained_evidence SET released_at = ? WHERE hold_id = ? AND released_at IS NULL",
            (released_at, hold_id),
        )
        return cursor.rowcount > 0


def purge_released_evidence(hold_id: str) -> int:
    """解除后物理删除含行程号的原件行。"""
    init_vault()
    with connect() as db:
        cursor = db.execute(
            "DELETE FROM retained_evidence WHERE hold_id = ? AND released_at IS NOT NULL", (hold_id,))
        return cursor.rowcount
