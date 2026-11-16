"""业务库：批次航链、清除请求与清除墓碑。

证据原件从不落在本库；墓碑仅保存外部行程号的加盐哈希与不可还原计数，
因此业务库任何位置都无法还原已清除的行程号明文。
"""
import sqlite3
from app.config import DATABASE_PATH
from app.store import connect as _connect_store

SCHEMA = """
CREATE TABLE IF NOT EXISTS batch_itinerary_legs (
    run_id TEXT NOT NULL,
    external_id TEXT NOT NULL,
    chain_seq INTEGER NOT NULL,
    flight_key TEXT NOT NULL,
    departure_utc TEXT NOT NULL,
    arrival_utc TEXT NOT NULL,
    chain_status TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    disputed INTEGER NOT NULL,
    PRIMARY KEY (run_id, external_id, chain_seq)
);
CREATE TABLE IF NOT EXISTS batch_quarantine_legs (
    run_id TEXT NOT NULL,
    external_id TEXT NOT NULL,
    line_no INTEGER NOT NULL,
    reason_code TEXT NOT NULL,
    raw_json TEXT NOT NULL,
    PRIMARY KEY (run_id, external_id, line_no)
);
CREATE TABLE IF NOT EXISTS clearance_requests (
    request_id TEXT PRIMARY KEY,
    external_id TEXT,
    external_id_hash TEXT NOT NULL UNIQUE,
    reason_code TEXT NOT NULL,
    disputed INTEGER NOT NULL,
    stage TEXT NOT NULL,
    status TEXT NOT NULL,
    hold_id TEXT,
    legs_removed INTEGER NOT NULL DEFAULT 0,
    quarantine_removed INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS clearance_tombstones (
    external_id_hash TEXT PRIMARY KEY,
    request_id TEXT NOT NULL,
    leg_count INTEGER NOT NULL,
    quarantine_count INTEGER NOT NULL,
    cleared_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ledger_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def connect() -> sqlite3.Connection:
    connection = _connect_store()
    connection.row_factory = sqlite3.Row
    connection.create_function("sha256_hex", 2, _sql_sha256_hex, deterministic=True)
    return connection


def _sql_sha256_hex(salt_hex: str, external_id: str) -> str:
    import hashlib
    return hashlib.sha256(bytes.fromhex(salt_hex) + str(external_id).encode("utf-8")).hexdigest()


def init_ledger() -> None:
    with connect() as db:
        db.executescript(SCHEMA)


def _salt(db: sqlite3.Connection) -> bytes:
    row = db.execute("SELECT value FROM ledger_meta WHERE key = 'id_salt'").fetchone()
    if row:
        return bytes.fromhex(row["value"])
    import secrets
    salt = secrets.token_bytes(32)
    db.execute("INSERT INTO ledger_meta(key, value) VALUES ('id_salt', ?)", (salt.hex(),))
    return salt


def hash_external_id(external_id: str) -> str:
    """加盐哈希：同库稳定可比对，跨库不可还原明文。"""
    import hashlib
    with connect() as db:
        salt = _salt(db)
    return hashlib.sha256(salt + external_id.encode("utf-8")).hexdigest()


# ---------- 批次写入（幂等：同 run_id 重跑复用，已清除行程不复活） ----------

def upsert_batch_leg(run_id: str, external_id: str, seq: int, leg: dict) -> None:
    with connect() as db:
        db.execute(
            "INSERT OR IGNORE INTO batch_itinerary_legs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (run_id, external_id, seq, leg["flight_key"], leg["departure_utc"],
             leg["arrival_utc"], leg["chain_status"], leg["reason_code"], int(leg["disputed"])),
        )


def upsert_quarantine_leg(run_id: str, external_id: str, line_no: int, reason_code: str, raw_json: str) -> None:
    with connect() as db:
        db.execute(
            "INSERT OR IGNORE INTO batch_quarantine_legs VALUES (?, ?, ?, ?, ?)",
            (run_id, external_id, line_no, reason_code, raw_json),
        )


def is_tombstoned(external_id_hash: str) -> bool:
    with connect() as db:
        return db.execute(
            "SELECT 1 FROM clearance_tombstones WHERE external_id_hash = ?", (external_id_hash,)
        ).fetchone() is not None


def find_request(external_id_hash: str) -> sqlite3.Row | None:
    with connect() as db:
        return db.execute(
            "SELECT * FROM clearance_requests WHERE external_id_hash = ?", (external_id_hash,)
        ).fetchone()


def get_request(request_id: str) -> sqlite3.Row | None:
    with connect() as db:
        return db.execute("SELECT * FROM clearance_requests WHERE request_id = ?", (request_id,)).fetchone()


def insert_request(record: dict) -> None:
    with connect() as db:
        db.execute(
            "INSERT OR IGNORE INTO clearance_requests VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (record["request_id"], record.get("external_id"), record["external_id_hash"],
             record["reason_code"], int(record["disputed"]), record["stage"], record["status"],
             record.get("hold_id"), record["legs_removed"], record["quarantine_removed"],
             record["created_at"], record["updated_at"]),
        )


_UNSET = object()


def update_request_stage(request_id: str, stage: str, status: str, updated_at: str,
                         external_id=_UNSET, hold_id=_UNSET,
                         legs_removed=_UNSET, quarantine_removed=_UNSET) -> None:
    with connect() as db:
        assignments = ["stage = ?", "status = ?", "updated_at = ?"]
        values: list = [stage, status, updated_at]
        if external_id is not _UNSET:
            assignments.append("external_id = ?"); values.append(external_id)
        if hold_id is not _UNSET:
            assignments.append("hold_id = ?"); values.append(hold_id)
        if legs_removed is not _UNSET:
            assignments.append("legs_removed = ?"); values.append(legs_removed)
        if quarantine_removed is not _UNSET:
            assignments.append("quarantine_removed = ?"); values.append(quarantine_removed)
        values.append(request_id)
        db.execute(f"UPDATE clearance_requests SET {', '.join(assignments)} WHERE request_id = ?", values)


def _salt_hex(db: sqlite3.Connection) -> str:
    return _salt(db).hex()


def delete_legs_for_hash(external_id_hash: str) -> int:
    """按加盐哈希在 SQL 内定位业务行并删除，返回实际删除行数（重试时为 0，不重复扣减）。"""
    with connect() as db:
        salt_hex = _salt_hex(db)
        cursor = db.execute(
            "DELETE FROM batch_itinerary_legs WHERE sha256_hex(?, external_id) = ?",
            (salt_hex, external_id_hash),
        )
        return cursor.rowcount


def delete_quarantine_for_hash(external_id_hash: str) -> int:
    with connect() as db:
        salt_hex = _salt_hex(db)
        cursor = db.execute(
            "DELETE FROM batch_quarantine_legs WHERE sha256_hex(?, external_id) = ?",
            (salt_hex, external_id_hash),
        )
        return cursor.rowcount


def insert_tombstone(external_id_hash: str, request_id: str, leg_count: int,
                     quarantine_count: int, cleared_at: str) -> None:
    with connect() as db:
        db.execute("INSERT OR IGNORE INTO clearance_tombstones VALUES (?, ?, ?, ?, ?)",
                   (external_id_hash, request_id, leg_count, quarantine_count, cleared_at))


def tombstone(external_id_hash: str) -> sqlite3.Row | None:
    with connect() as db:
        return db.execute("SELECT * FROM clearance_tombstones WHERE external_id_hash = ?",
                          (external_id_hash,)).fetchone()


# ---------- 查询（已清除行程不出现在任何业务结果中） ----------

def list_batches() -> list[dict]:
    with connect() as db:
        rows = db.execute(
            """SELECT run_id,
                      COUNT(*) AS leg_rows,
                      SUM(chain_status = 'VALID') AS valid_legs,
                      SUM(chain_status <> 'VALID') AS bad_legs,
                      SUM(disputed) AS disputed_legs
               FROM batch_itinerary_legs GROUP BY run_id ORDER BY run_id"""
        ).fetchall()
        return [dict(row) for row in rows]


def list_itinerary_legs(run_id: str | None = None) -> list[dict]:
    with connect() as db:
        if run_id:
            rows = db.execute(
                "SELECT * FROM batch_itinerary_legs WHERE run_id = ? ORDER BY run_id, external_id, chain_seq",
                (run_id,)).fetchall()
        else:
            rows = db.execute(
                "SELECT * FROM batch_itinerary_legs ORDER BY run_id, external_id, chain_seq").fetchall()
        return [dict(row) for row in rows]


def list_quarantine(run_id: str | None = None) -> list[dict]:
    with connect() as db:
        if run_id:
            rows = db.execute(
                "SELECT * FROM batch_quarantine_legs WHERE run_id = ? ORDER BY run_id, external_id, line_no",
                (run_id,)).fetchall()
        else:
            rows = db.execute(
                "SELECT * FROM batch_quarantine_legs ORDER BY run_id, external_id, line_no").fetchall()
        return [dict(row) for row in rows]


def list_requests() -> list[dict]:
    with connect() as db:
        rows = db.execute(
            """SELECT request_id, external_id, reason_code, disputed, stage, status, hold_id,
                      legs_removed, quarantine_removed, created_at, updated_at
               FROM clearance_requests ORDER BY created_at""").fetchall()
        result = []
        for row in rows:
            item = dict(row)
            if item["status"] == "CLEARED":
                item["external_id"] = None  # 已清除请求不再暴露行程号
            result.append(item)
        return result


def conservation(run_id: str) -> dict:
    with connect() as db:
        leg_total = db.execute(
            "SELECT COUNT(*) FROM batch_itinerary_legs WHERE run_id = ?", (run_id,)).fetchone()[0]
        valid = db.execute(
            "SELECT COUNT(*) FROM batch_itinerary_legs WHERE run_id = ? AND chain_status = 'VALID'",
            (run_id,)).fetchone()[0]
        bad = leg_total - valid
        quarantined = db.execute(
            "SELECT COUNT(*) FROM batch_quarantine_legs WHERE run_id = ?", (run_id,)).fetchone()[0]
    return {"run_id": run_id, "input": leg_total + quarantined,
            "valid": valid, "conflicts_or_unconnectable": bad,
            "quarantined": quarantined,
            "balanced": leg_total + quarantined == valid + bad + quarantined,
            "difference": (leg_total + quarantined) - (valid + bad + quarantined)}
