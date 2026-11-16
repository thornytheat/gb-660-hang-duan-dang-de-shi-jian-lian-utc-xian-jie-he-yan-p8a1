"""外部行程号清除管线。

阶段化设计，任一阶段失败后从断点恢复；每个阶段幂等：
- 已写入的证据/凭据/墓碑不重复写；
- 扣减只统计实际删除的业务行，重试得到 0 行，不重复扣减；
- 墓碑只保存加盐哈希，重跑旧批次时已清除行程不会复活。
"""
import json
import secrets
from datetime import datetime, timezone

from app.reasons import REASONS
from . import ledger, vault

STAGES = ("CREATED", "EVIDENCE", "BUSINESS_DELETE", "CREDENTIAL", "TOMBSTONE", "DONE")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def identity_hash(external_id: str) -> str:
    return ledger.hash_external_id(external_id)


def _snapshot(external_id: str) -> dict:
    """收集同一外部行程号在所有批次中的业务原件（覆盖所有批次及旧导出口径）。"""
    legs = [row for row in ledger.list_itinerary_legs() if row["external_id"] == external_id]
    quarantined = [row for row in ledger.list_quarantine() if row["external_id"] == external_id]
    return {"external_id": external_id, "legs": legs, "quarantine": quarantined,
            "captured_at": now_iso()}


def open_clearance(external_id: str, reason: str | None = None, disputed: bool | None = None) -> dict:
    """登记清除请求；已存在则原样返回，不重复建单。"""
    ledger.init_ledger()
    external_id = str(external_id).strip()
    ext_hash = identity_hash(external_id)
    existing = ledger.find_request(ext_hash)
    if existing:
        return _request_view(existing)

    if disputed is None:
        # 航链本身处于争议状态（无效/不可衔接/含隔离航段）即视为涉争议
        legs = [row for row in ledger.list_itinerary_legs() if row["external_id"] == external_id]
        quarantined = [row for row in ledger.list_quarantine() if row["external_id"] == external_id]
        disputed = bool(quarantined) or any(row["chain_status"] != "VALID" or row["disputed"] for row in legs)
    reason_code = reason or ("CLEARANCE_DISPUTED" if disputed else "CLEARANCE_APPROVED")
    record = {
        "request_id": f"CLR-{secrets.token_hex(8)}",
        "external_id": external_id,
        "external_id_hash": ext_hash,
        "reason_code": reason_code,
        "disputed": bool(disputed),
        "stage": "CREATED",
        "status": "PENDING",
        "hold_id": None,
        "legs_removed": 0,
        "quarantine_removed": 0,
        "created_at": now_iso(),
        "updated_at": now_iso(),
    }
    ledger.insert_request(record)
    return _request_view(ledger.get_request(record["request_id"]))


def process_clearance(request_id: str) -> dict:
    """从断点继续推进清除管线，直到 DONE 或进入 RETAINED 等待解除。"""
    ledger.init_ledger()
    request = ledger.get_request(request_id)
    if request is None:
        from app.errors import DomainError
        raise DomainError("CLEARANCE_NOT_FOUND", REASONS["CLEARANCE_NOT_FOUND"], status=404)
    if request["status"] in ("RETAINED", "CLEARED"):
        return _request_view(request)

    ext_hash = request["external_id_hash"]
    external_id = request["external_id"]
    hold_id = request["hold_id"]
    legs_removed = request["legs_removed"]
    quarantine_removed = request["quarantine_removed"]

    # 阶段 1：涉争议 → 原件先入隔离证据库；无争议 → 无原件可留，直接进入清除
    if request["stage"] == "CREATED":
        if request["disputed"]:
            hold_id = f"HOLD-{secrets.token_hex(8)}"
            snapshot = _snapshot(external_id)
            vault.retain_evidence(
                hold_id, request_id, external_id, request["reason_code"],
                json.dumps(snapshot, ensure_ascii=False, sort_keys=True),
                len(snapshot["legs"]), len(snapshot["quarantine"]), now_iso())
            vault.audit("retain", "system", True, request["reason_code"],
                        hold_id=hold_id, request_id=request_id)
            ledger.update_request_stage(request_id, "EVIDENCE", "PENDING", now_iso(), hold_id=hold_id)
        else:
            ledger.update_request_stage(request_id, "EVIDENCE", "PENDING", now_iso())

    # 阶段 2：业务库删除——覆盖同一行程号的所有批次及旧导出行，按实际行数扣减
    request = ledger.get_request(request_id)
    if request["stage"] == "EVIDENCE":
        if request["legs_removed"] == 0 and request["quarantine_removed"] == 0:
            legs_removed = ledger.delete_legs_for_hash(ext_hash)
            quarantine_removed = ledger.delete_quarantine_for_hash(ext_hash)
            ledger.update_request_stage(request_id, "BUSINESS_DELETE", "PENDING", now_iso(),
                                        legs_removed=legs_removed,
                                        quarantine_removed=quarantine_removed)
        else:
            ledger.update_request_stage(request_id, "BUSINESS_DELETE", "PENDING", now_iso())

    # 阶段 3：写入不含行程号的计数凭据（证据库独立保存）
    request = ledger.get_request(request_id)
    if request["stage"] == "BUSINESS_DELETE":
        vault.write_credential(request_id, request["hold_id"], request["legs_removed"],
                               request["quarantine_removed"], request["reason_code"], now_iso())
        ledger.update_request_stage(request_id, "CREDENTIAL", "PENDING", now_iso())

    # 阶段 4：墓碑（只含加盐哈希），阻止重跑旧批次复活已清除行程
    request = ledger.get_request(request_id)
    if request["stage"] == "CREDENTIAL":
        ledger.insert_tombstone(ext_hash, request_id, request["legs_removed"],
                                request["quarantine_removed"], now_iso())
        ledger.update_request_stage(request_id, "TOMBSTONE", "PENDING", now_iso())

    # 阶段 5：涉争议保持 RETAINED（原件在证据库可核验）；无争议直接 CLEARED
    request = ledger.get_request(request_id)
    if request["stage"] == "TOMBSTONE":
        if request["disputed"]:
            ledger.update_request_stage(request_id, "DONE", "RETAINED", now_iso(),
                                        external_id=None)  # 业务库不再保留行程号明文
        else:
            ledger.update_request_stage(request_id, "DONE", "CLEARED", now_iso(),
                                        external_id=None)

    return _request_view(ledger.get_request(request_id))


def clear_external_id(external_id: str, reason: str | None = None,
                      disputed: bool | None = None) -> dict:
    """登记并推进清除（失败后可凭返回的 request_id 调用 process_clearance 续跑）。"""
    request = open_clearance(external_id, reason, disputed)
    return process_clearance(request["request_id"])


def resume_clearance(request_id: str) -> dict:
    return process_clearance(request_id)


def release_retention(request_id: str, role: str, reason: str) -> dict:
    """解除留存：审计角色凭理由销毁原件，仅保留不含行程号的计数凭据。"""
    request = ledger.get_request(request_id)
    if request is None:
        from app.errors import DomainError
        raise DomainError("CLEARANCE_NOT_FOUND", REASONS["CLEARANCE_NOT_FOUND"], status=404)
    # RBAC 优先：任何角色状态错误都不得绕过审计校验
    vault.require_auditor(role, reason, "release",
                          hold_id=request["hold_id"], request_id=request_id)
    if request["status"] != "RETAINED" or not request["hold_id"]:
        from app.errors import DomainError
        raise DomainError("RETENTION_NOT_HELD", REASONS["RETENTION_NOT_HELD"], status=409)
    hold_id = request["hold_id"]
    if vault.release_evidence(hold_id, now_iso()):
        vault.purge_released_evidence(hold_id)
    # 凭据已在清除时写入；此处幂等确认，且凭据中没有任何行程号字段
    vault.write_credential(request_id, None, request["legs_removed"],
                           request["quarantine_removed"], "CLEARANCE_RELEASED", now_iso())
    ledger.update_request_stage(request_id, "DONE", "CLEARED", now_iso(), hold_id=None)
    return _request_view(ledger.get_request(request_id))


def _request_view(row) -> dict:
    item = dict(row)
    if item["status"] == "CLEARED":
        item["external_id"] = None
    return item
