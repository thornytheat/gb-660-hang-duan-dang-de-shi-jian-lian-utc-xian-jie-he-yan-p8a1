"""M6 批次服务：接入真实航链结果，核对航段守恒并驱动清除/留存。"""
import json

from app.manifest import build_manifest
from app.modules.m1_legs.parser import parse_legs
from app.modules.m4_itinerary.builder import build_leg, order_legs
from app.modules.m4_itinerary.validate import validate_chain
from app.modules.m5_connections.rules import required_minutes
from app.modules.m5_connections.windows import waiting_minutes
from app.config import MAX_CONNECTION_MINUTES
from app.store import record_run
from . import ledger
from .compare import reconcile
from .exporter import itineraries_csv
from .pipeline import identity_hash

DEFAULT_PAYLOAD = b"flightclock-sample"


def _connection_status(prev: dict, leg: dict) -> str:
    from datetime import datetime
    wait = waiting_minutes(datetime.fromisoformat(prev["arrival_utc"]),
                           datetime.fromisoformat(leg["departure_utc"]))
    try:
        required = required_minutes(leg["origin"], "T1", "T1")
    except ValueError:
        return "UNKNOWN_REFERENCE"
    if wait < 0:
        return "OVERLAP"
    if wait < required:
        return "TOO_SHORT"
    if wait > MAX_CONNECTION_MINUTES:
        return "TOO_LONG"
    return "VALID"


def ingest_batch(payload: dict | None = None, raw_bytes: bytes = DEFAULT_PAYLOAD) -> dict:
    """把一批航段按外部行程号分组，构建真实 UTC 航链并落库（幂等）。"""
    ledger.init_ledger()
    payload = payload or _demo_payload()
    rows = payload["legs"]
    external_id = str(payload["external_id"]).strip()  # TRAP-09：始终按字符串保形
    ext_hash = identity_hash(external_id)

    # 已清除行程：重跑旧批次也不复活
    if ledger.is_tombstoned(ext_hash):
        return {"run_id": build_manifest(raw_bytes, 0, 0)["run_id"], "external_id": external_id,
                "skipped": "TOMBSTONED", "chains": [], "quarantine": []}

    manifest = build_manifest(raw_bytes, accepted=0, quarantined=0)
    run_id = manifest["run_id"]
    accepted, bad = parse_legs(rows)
    built, build_errors = [], []
    for row in accepted:
        try:
            leg = build_leg(row)
            leg["flight_date"] = row["flight_date"]
            built.append(leg)
        except ValueError as exc:
            build_errors.append({"line_no": row.get("line_no"), "reason_code": str(exc), "raw": row})

    ordered = order_legs(built)
    chain_valid = validate_chain(ordered)
    chain_rows, seq = [], 0
    for index, leg in enumerate(ordered):
        seq += 1
        status, reason = "VALID", ""
        if not chain_valid:
            status, reason = "INVALID_CHAIN", "CHAIN_UNCONNECTABLE"
        elif index:
            connection = _connection_status(ordered[index - 1], leg)
            if connection != "VALID":
                status, reason = connection, "CHAIN_UNCONNECTABLE"
        chain_rows.append({
            "flight_key": "|".join((leg["flight_no"], leg["flight_date"], leg["origin"], leg["destination"])),
            "departure_utc": leg["departure_utc"], "arrival_utc": leg["arrival_utc"],
            "chain_status": status, "reason_code": reason,
            "disputed": status != "VALID",
        })
        ledger.upsert_batch_leg(run_id, external_id, seq, chain_rows[-1])

    quarantine = []
    for item in bad + build_errors:
        entry = {"line_no": item["line_no"], "reason_code": item["reason_code"], "raw": item["raw"]}
        quarantine.append(entry)
        ledger.upsert_quarantine_leg(run_id, external_id, item["line_no"],
                                     item["reason_code"], json.dumps(item["raw"], ensure_ascii=False))

    record_run(build_manifest(raw_bytes, len(chain_rows), len(quarantine)))
    account = reconcile(len(rows), len(chain_rows),
                        sum(1 for row in chain_rows if row["chain_status"] != "VALID"),
                        len(quarantine))
    return {"run_id": run_id, "external_id": external_id, "chains": chain_rows,
            "quarantine": quarantine, **account}


def batch_snapshot() -> dict:
    """页面数据：待处理、隔离留存、已清除三态 + 航段守恒差异。"""
    ledger.init_ledger()
    requests = ledger.list_requests()
    counts = {"pending": 0, "retained": 0, "cleared": 0}
    for item in requests:
        counts[{"PENDING": "pending", "RETAINED": "retained", "CLEARED": "cleared"}[item["status"]]] += 1
    batches = []
    for batch in ledger.list_batches():
        batches.append({**batch, "conservation": ledger.conservation(batch["run_id"])})
    return {"batches": batches, "requests": requests, "counts": counts,
            "tombstones": _tombstone_views(requests), "credentials": _credential_views()}


def _tombstone_views(requests: list[dict]) -> list[dict]:
    # 墓碑只保存加盐哈希，输出不含任何行程号明文
    result = []
    for item in requests:
        if item["status"] != "CLEARED":
            continue
        row = ledger.tombstone(identity_hash_lookup(item["request_id"]))
        if row:
            result.append({"request_id": row["request_id"], "leg_count": row["leg_count"],
                           "quarantine_count": row["quarantine_count"], "cleared_at": row["cleared_at"]})
    return result


def identity_hash_lookup(request_id: str) -> str:
    row = ledger.get_request(request_id)
    return row["external_id_hash"] if row else ""


def _credential_views() -> list[dict]:
    from . import vault
    return vault.list_credentials()


def live_export_rows(run_id: str | None = None) -> list[dict]:
    """业务导出口径：已清除/已隔离删除的行程不再出现。"""
    rows = []
    for leg in ledger.list_itinerary_legs(run_id):
        rows.append({"flight_key": leg["flight_key"], "departure_utc": leg["departure_utc"],
                     "arrival_utc": leg["arrival_utc"], "status": leg["chain_status"],
                     "reason_code": leg["reason_code"]})
    return rows


def export_csv(run_id: str | None = None) -> str:
    return itineraries_csv(live_export_rows(run_id))


def _demo_payload() -> dict:
    return {
        "external_id": "90071992547409931234",
        "legs": [
            {"flight_no": "MU5101", "flight_date": "2026-11-16", "origin": "PVG", "destination": "JFK",
             "departure_local": "2026-11-16T09:00", "arrival_local": "2026-11-16T10:30"},
            {"flight_no": "BA178", "flight_date": "2026-11-16", "origin": "JFK", "destination": "LHR",
             "departure_local": "2026-11-16T18:00", "arrival_local": "2026-11-17T06:00"},
        ],
    }


def run() -> dict:
    return ingest_batch()


def demo() -> dict:
    return run()
