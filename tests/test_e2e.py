import pytest
from fastapi.testclient import TestClient
import app.store as store
from app.config import EVIDENCE_DATABASE_PATH
from app.modules.m6_batches import ledger, vault
from app.main import app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    main_db = tmp_path / "runs.sqlite3"
    evidence_db = tmp_path / "evidence.sqlite3"
    monkeypatch.setattr(store, "DATABASE_PATH", main_db)
    monkeypatch.setattr(ledger, "DATABASE_PATH", main_db)
    monkeypatch.setattr(vault, "EVIDENCE_DATABASE_PATH", evidence_db)
    ledger.init_ledger()
    vault.init_vault()
    return TestClient(app)


def test_all_module_pages_accessible(client):
    for path in ("/legs", "/airports", "/local-time", "/itineraries", "/connections", "/batches"):
        response = client.get(path)
        assert response.status_code == 200 and "panel" in response.text


def test_full_sample_flow_has_utc_chain(client):
    results = [client.post(path).json() for path in ("/api/legs/import", "/api/airports", "/api/local-time/resolve", "/api/itineraries/build", "/api/connections/check", "/api/reconcile")]
    assert results[0]["accepted"] == 1 and results[2]["utc"].endswith("Z") and results[3]["duration_minutes"] == 870 and results[4]["status"] == "VALID"
    # M6 接入真实航链：样例含两个有效航段且守恒
    assert results[5]["valid"] == 2 and results[5]["balanced"] is True


def test_itinerary_export_downloads(client):
    client.post("/api/reconcile")
    response = client.get("/api/export/itineraries.csv")
    assert response.status_code == 200 and "MU5101" in response.text
    assert "external_id" not in response.text.splitlines()[0]


def test_clearance_lifecycle_end_to_end(client):
    external_id = "90071992547409931234"
    client.post("/api/reconcile")
    # 获准清除：业务查询与导出消失
    cleared = client.post("/api/clearance", json={"external_id": external_id}).json()
    assert cleared["status"] == "CLEARED"
    assert external_id not in client.get("/api/batches").text
    assert external_id not in client.get("/api/export/itineraries.csv").text
    # 重跑旧批次不复活
    rerun = client.post("/api/reconcile").json()
    assert rerun.get("skipped") == "TOMBSTONED"
    # 计数凭据不含行程号
    credentials = client.get("/api/credentials").json()
    assert credentials and all(external_id not in str(item) for item in credentials)
    # 普通角色看审计日志被拒
    assert client.get("/api/audit-log", headers={"x-role": "operator"}).status_code == 403
    assert client.get("/api/audit-log",
                      headers={"x-role": "audit", "x-audit-reason": "E2E check"}).status_code == 200


