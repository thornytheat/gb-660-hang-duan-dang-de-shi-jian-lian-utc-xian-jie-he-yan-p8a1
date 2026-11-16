import json
import pytest
from fastapi.testclient import TestClient

import app.store as store
from app.config import DATABASE_PATH, EVIDENCE_DATABASE_PATH
from app.errors import DomainError
from app.main import app
from app.modules.m6_batches import ledger, vault, pipeline, service


@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    main_db = tmp_path / "runs.sqlite3"
    evidence_db = tmp_path / "evidence.sqlite3"
    monkeypatch.setattr(store, "DATABASE_PATH", main_db)
    monkeypatch.setattr(ledger, "DATABASE_PATH", main_db)
    monkeypatch.setattr(vault, "EVIDENCE_DATABASE_PATH", evidence_db)
    ledger.init_ledger()
    vault.init_vault()
    return tmp_path


VALID_PAYLOAD = {
    "external_id": "90071992547409931234",  # 超大行程号，按字符串保形 TRAP-09
    "legs": [
        {"flight_no": "MU5101", "flight_date": "2026-11-16", "origin": "PVG", "destination": "JFK",
         "departure_local": "2026-11-16T09:00", "arrival_local": "2026-11-16T10:30"},
        {"flight_no": "BA178", "flight_date": "2026-11-16", "origin": "JFK", "destination": "LHR",
         "departure_local": "2026-11-16T18:00", "arrival_local": "2026-11-17T06:00"},
    ],
}

DISPUTED_PAYLOAD = {
    "external_id": "EXT-DISPUTE-7",
    "legs": [
        {"flight_no": "UA857", "flight_date": "2026-11-01", "origin": "JFK", "destination": "SFO",
         "departure_local": "2026-11-01T01:30", "arrival_local": "2026-11-01T05:00"},
        {"flight_no": "UA857", "flight_date": "2026-11-01", "origin": "JFK", "destination": "SFO",
         "departure_local": "2026-11-01T01:30", "arrival_local": "2026-11-01T05:30"},
    ],
}


def _ingest(payload):
    return service.ingest_batch(payload, raw_bytes=json.dumps(payload, sort_keys=True).encode())


def test_ingest_builds_real_utc_chain_and_balances(isolated_db):
    result = _ingest(VALID_PAYLOAD)
    assert result["balanced"] is True and result["difference"] == 0
    assert len(result["chains"]) == 2
    assert all(leg["departure_utc"].endswith("+00:00") or leg["departure_utc"].endswith("Z")
               for leg in result["chains"])
    stored = ledger.list_itinerary_legs(result["run_id"])
    assert stored[0]["external_id"] == VALID_PAYLOAD["external_id"]


def test_clearance_removes_from_business_query_and_export(isolated_db):
    _ingest(VALID_PAYLOAD)
    outcome = pipeline.clear_external_id(VALID_PAYLOAD["external_id"])
    assert outcome["status"] == "CLEARED"
    assert ledger.list_itinerary_legs() == []
    csv_text = service.export_csv()
    assert VALID_PAYLOAD["external_id"] not in csv_text
    assert "external_id" not in csv_text.splitlines()[0]


def test_clearance_covers_all_batches_for_same_external_id(isolated_db):
    _ingest(VALID_PAYLOAD)
    second = dict(VALID_PAYLOAD, legs=[VALID_PAYLOAD["legs"][0]])
    service.ingest_batch(second, raw_bytes=b"second-batch-bytes")
    assert len(ledger.list_itinerary_legs()) == 3
    pipeline.clear_external_id(VALID_PAYLOAD["external_id"])
    assert ledger.list_itinerary_legs() == []
    header = service.export_csv().splitlines()[0]
    assert "external_id" not in header
    assert len(service.export_csv().strip().splitlines()) == 1  # 仅剩表头


def test_retry_resumes_without_double_counting(isolated_db):
    _ingest(VALID_PAYLOAD)
    opened = pipeline.open_clearance(VALID_PAYLOAD["external_id"], disputed=False)
    first = pipeline.process_clearance(opened["request_id"])
    assert first["legs_removed"] == 2
    # 管线幂等：重复处理不报错，扣减不翻倍
    again = pipeline.process_clearance(opened["request_id"])
    assert again["legs_removed"] == 2 and again["status"] == "CLEARED"
    assert ledger.delete_legs_for_hash(identity(VALID_PAYLOAD["external_id"])) == 0


def test_resume_from_interrupted_stage(isolated_db, monkeypatch):
    _ingest(VALID_PAYLOAD)
    opened = pipeline.open_clearance(VALID_PAYLOAD["external_id"], disputed=False)
    # 模拟在 BUSINESS_DELETE 之后崩溃：阶段已推进但凭据/墓碑未写
    ledger.delete_legs_for_hash(identity(VALID_PAYLOAD["external_id"]))
    ledger.update_request_stage(opened["request_id"], "BUSINESS_DELETE", "PENDING",
                                pipeline.now_iso(), legs_removed=2, quarantine_removed=0)
    resumed = pipeline.resume_clearance(opened["request_id"])
    assert resumed["stage"] == "DONE" and resumed["status"] == "CLEARED"
    assert resumed["legs_removed"] == 2  # 断点恢复不重复扣减
    assert vault.get_credential(opened["request_id"]) is not None


def test_rerun_batch_does_not_resurrect_cleared_itinerary(isolated_db):
    _ingest(VALID_PAYLOAD)
    pipeline.clear_external_id(VALID_PAYLOAD["external_id"])
    rerun = _ingest(VALID_PAYLOAD)
    assert rerun.get("skipped") == "TOMBSTONED"
    assert ledger.list_itinerary_legs() == []


def test_disputed_chain_is_retained_in_isolated_vault(isolated_db):
    result = _ingest(DISPUTED_PAYLOAD)
    assert result["quarantine"], "冲突航段应进入隔离"
    outcome = pipeline.clear_external_id(DISPUTED_PAYLOAD["external_id"])
    assert outcome["status"] == "RETAINED" and outcome["external_id"] is None
    # 业务库已无该行程号明文
    assert all(row["external_id"] != DISPUTED_PAYLOAD["external_id"]
               for row in ledger.list_itinerary_legs())
    assert ledger.list_quarantine() == []
    # 证据原件在独立证据库，可核验
    hold = vault.get_evidence(outcome["hold_id"])
    assert hold is not None and hold["external_id"] == DISPUTED_PAYLOAD["external_id"]
    payload = json.loads(hold["payload_json"])
    assert payload["external_id"] == DISPUTED_PAYLOAD["external_id"]


def test_evidence_rbac_allows_auditor_and_denies_operator(isolated_db):
    _ingest(DISPUTED_PAYLOAD)
    outcome = pipeline.clear_external_id(DISPUTED_PAYLOAD["external_id"])
    hold_id = outcome["hold_id"]
    # 普通角色越权 → 403
    with pytest.raises(DomainError):
        vault.require_auditor("operator", "peek without permission", "evidence_read", hold_id=hold_id)
    # 审计角色缺理由 → 403
    with pytest.raises(DomainError):
        vault.require_auditor("audit", "  ", "evidence_read", hold_id=hold_id)
    # 审计角色 + 理由 → 放行
    vault.require_auditor("audit", "争议工单 DISP-2026-018 复核", "evidence_read", hold_id=hold_id)
    log = vault.list_audit()
    assert {row["allowed"] for row in log} == {0, 1}
    assert all("external_id" not in row for row in log)


def test_release_retention_destroys_original_keeps_count_credential(isolated_db):
    _ingest(DISPUTED_PAYLOAD)
    outcome = pipeline.clear_external_id(DISPUTED_PAYLOAD["external_id"])
    hold_id, request_id = outcome["hold_id"], outcome["request_id"]
    # 非审计角色不能解除
    with pytest.raises(DomainError):
        pipeline.release_retention(request_id, "operator", "无权操作")
    released = pipeline.release_retention(request_id, "audit", "争议结案，批准解除留存")
    assert released["status"] == "CLEARED"
    assert vault.get_evidence(hold_id) is None  # 含行程号原件已销毁
    credential = vault.get_credential(request_id)
    assert credential is not None
    raw = dict(credential)
    assert "external_id" not in raw and DISPUTED_PAYLOAD["external_id"] not in json.dumps(raw)
    assert raw["leg_count"] + raw["quarantine_count"] >= 1


def test_tombstone_contains_no_plaintext_identifier(isolated_db):
    _ingest(VALID_PAYLOAD)
    outcome = pipeline.clear_external_id(VALID_PAYLOAD["external_id"])
    row = ledger.tombstone(identity(VALID_PAYLOAD["external_id"]))
    assert row is not None and row["request_id"] == outcome["request_id"]
    assert VALID_PAYLOAD["external_id"] not in json.dumps(dict(row))
    snapshot = service.batch_snapshot()
    assert VALID_PAYLOAD["external_id"] not in json.dumps(snapshot, ensure_ascii=False)


def test_idempotent_open_does_not_duplicate_request(isolated_db):
    _ingest(VALID_PAYLOAD)
    first = pipeline.open_clearance(VALID_PAYLOAD["external_id"])
    second = pipeline.open_clearance(VALID_PAYLOAD["external_id"])
    assert first["request_id"] == second["request_id"]
    assert len(ledger.list_requests()) == 1


def test_conservation_difference_visible(isolated_db):
    _ingest(VALID_PAYLOAD)
    snapshot = service.batch_snapshot()
    assert snapshot["batches"][0]["conservation"]["difference"] == 0
    from app.modules.m6_batches.compare import reconcile
    assert reconcile(14, 10, 2, 1)["difference"] == 1


def identity(external_id: str) -> str:
    return ledger.hash_external_id(external_id)


# ---------- HTTP 层 RBAC ----------

def test_http_evidence_endpoint_rbac(isolated_db):
    client = TestClient(app)
    _ingest(DISPUTED_PAYLOAD)
    outcome = pipeline.clear_external_id(DISPUTED_PAYLOAD["external_id"])
    denied = client.get(f"/api/evidence/{outcome['hold_id']}", headers={"x-role": "operator"})
    assert denied.status_code == 403
    denied_no_reason = client.get(f"/api/evidence/{outcome['hold_id']}", headers={"x-role": "audit"})
    assert denied_no_reason.status_code == 403
    allowed = client.get(f"/api/evidence/{outcome['hold_id']}",
                         headers={"x-role": "audit", "x-audit-reason": "DISP-2026-018 review"})
    assert allowed.status_code == 200 and allowed.json()["external_id"] == DISPUTED_PAYLOAD["external_id"]
    # 中文理由经 UTF-8 转义后同样可还原
    from urllib.parse import quote
    encoded = quote("争议工单复核")
    cn = client.get(f"/api/evidence/{outcome['hold_id']}",
                    headers={"x-role": "audit", "x-audit-reason": encoded})
    assert cn.status_code == 200


def test_http_release_and_credentials_flow(isolated_db):
    client = TestClient(app)
    _ingest(DISPUTED_PAYLOAD)
    outcome = pipeline.clear_external_id(DISPUTED_PAYLOAD["external_id"])
    denied = client.post("/api/clearance/release",
                         json={"request_id": outcome["request_id"], "reason": "x"},
                         headers={"x-role": "operator"})
    assert denied.status_code == 403
    ok = client.post("/api/clearance/release",
                     json={"request_id": outcome["request_id"], "reason": "争议结案"},
                     headers={"x-role": "audit"})
    assert ok.status_code == 200 and ok.json()["status"] == "CLEARED"
    credentials = client.get("/api/credentials").json()
    assert credentials and all("external_id" not in item for item in credentials)


def test_batches_page_renders_three_states(isolated_db):
    client = TestClient(app)
    _ingest(VALID_PAYLOAD)
    pipeline.clear_external_id(VALID_PAYLOAD["external_id"])
    _ingest(DISPUTED_PAYLOAD)
    pipeline.clear_external_id(DISPUTED_PAYLOAD["external_id"])
    html = client.get("/batches").text
    assert "隔离留存" in html and "已清除" in html and "待处理" in html
    assert "航段守恒差异" in html


def test_export_endpoint_excludes_cleared_rows(isolated_db):
    client = TestClient(app)
    _ingest(VALID_PAYLOAD)
    pipeline.clear_external_id(VALID_PAYLOAD["external_id"])
    text = client.get("/api/export/itineraries.csv").text
    assert "MU5101" not in text and "external_id" not in text


def test_resume_after_evidence_stage_crash(isolated_db):
    # 争议单在证据已入 vault、业务尚未删除时崩溃，续跑应完成且不丢证据
    _ingest(DISPUTED_PAYLOAD)
    opened = pipeline.open_clearance(DISPUTED_PAYLOAD["external_id"])
    request = ledger.get_request(opened["request_id"])
    pipeline.process_clearance(opened["request_id"])
    # 手工把状态拉回 EVIDENCE 模拟崩溃后重放
    ledger.update_request_stage(opened["request_id"], "EVIDENCE", "PENDING",
                                pipeline.now_iso(), legs_removed=0, quarantine_removed=0)
    resumed = pipeline.resume_clearance(opened["request_id"])
    assert resumed["status"] == "RETAINED"
    hold = vault.get_evidence(resumed["hold_id"])
    assert hold is not None  # 证据仍可核验
    assert ledger.list_quarantine() == [] and ledger.list_itinerary_legs() == []


def test_tombstone_blocks_ingest_under_different_run_id(isolated_db):
    first = _ingest(VALID_PAYLOAD)
    pipeline.clear_external_id(VALID_PAYLOAD["external_id"])
    # 同行程号、不同批次字节 → 不同 run_id，仍被墓碑拦截
    other = service.ingest_batch(VALID_PAYLOAD, raw_bytes=b"different-batch-input-bytes")
    assert other["run_id"] != first["run_id"]
    assert other.get("skipped") == "TOMBSTONED"
    assert ledger.list_itinerary_legs() == []


def test_clearance_without_business_rows_completes_and_is_idempotent(isolated_db):
    outcome = pipeline.clear_external_id("NO-SUCH-ID")
    assert outcome["status"] == "CLEARED" and outcome["stage"] == "DONE"
    assert outcome["legs_removed"] == 0 and outcome["quarantine_removed"] == 0
    again = pipeline.resume_clearance(outcome["request_id"])
    assert again["legs_removed"] == 0 and again["status"] == "CLEARED"


def test_release_rbac_checked_before_state(isolated_db):
    # 已 CLEARED 的请求，普通角色调用解除仍应得到 403 而非 409
    _ingest(VALID_PAYLOAD)
    outcome = pipeline.clear_external_id(VALID_PAYLOAD["external_id"])
    with pytest.raises(DomainError) as exc:
        pipeline.release_retention(outcome["request_id"], "operator", "no permission")
    assert exc.value.status == 403


def test_invalid_duration_leg_is_quarantined_and_disputed(isolated_db):
    payload = {
        "external_id": "EXT-BAD-DURATION",
        "legs": [{"flight_no": "MU5101", "flight_date": "2026-11-16", "origin": "PVG",
                  "destination": "JFK", "departure_local": "2026-11-16T09:00",
                  "arrival_local": "2026-11-17T10:30"}],  # 超 18 小时
    }
    result = _ingest(payload)
    assert result["quarantine"][0]["reason_code"] == "INVALID_FLIGHT_DURATION"
    outcome = pipeline.clear_external_id("EXT-BAD-DURATION")
    assert outcome["status"] == "RETAINED"
    assert vault.get_evidence(outcome["hold_id"]) is not None
