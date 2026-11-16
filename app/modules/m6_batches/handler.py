from fastapi import APIRouter, Request, Header
from fastapi.responses import HTMLResponse, PlainTextResponse
from pydantic import BaseModel

from app.errors import DomainError
from app.web import page
from . import service, pipeline, vault
from .pipeline import identity_hash

router = APIRouter()


def audit_reason(raw: str | None) -> str | None:
    """HTTP 头为 Latin-1，前端按 UTF-8 转义后传入，这里还原。"""
    if not raw:
        return raw
    try:
        return raw.encode("latin-1").decode("utf-8")
    except (UnicodeDecodeError, UnicodeEncodeError):
        return raw


class IngestBody(BaseModel):
    external_id: str | None = None
    legs: list[dict] | None = None


class ClearBody(BaseModel):
    external_id: str
    reason: str | None = None
    disputed: bool | None = None


class ResumeBody(BaseModel):
    request_id: str


class ReleaseBody(BaseModel):
    request_id: str
    reason: str


@router.get("/batches", response_class=HTMLResponse)
def view(request: Request):
    snapshot = service.batch_snapshot()
    return page(request, "m6.html", "批次导出", "/api/reconcile",
                {"snapshot": snapshot, "identity_hash": identity_hash})


@router.post("/api/reconcile")
def execute(body: IngestBody | None = None):
    payload = None
    if body and (body.external_id or body.legs):
        payload = {"external_id": body.external_id, "legs": body.legs or []}
    return service.ingest_batch(payload)


@router.get("/api/batches")
def batches():
    return service.batch_snapshot()


@router.post("/api/clearance")
def open_clearance(body: ClearBody):
    return pipeline.clear_external_id(body.external_id, body.reason, body.disputed)


@router.post("/api/clearance/resume")
def resume_clearance(body: ResumeBody):
    return pipeline.resume_clearance(body.request_id)


@router.post("/api/clearance/release")
def release_retention(body: ReleaseBody, x_role: str = Header(default="operator")):
    return pipeline.release_retention(body.request_id, x_role, body.reason)


@router.get("/api/evidence/{hold_id}")
def read_evidence(hold_id: str, x_role: str = Header(default="operator"),
                  x_audit_reason: str | None = Header(default=None)):
    vault.require_auditor(x_role, audit_reason(x_audit_reason), "evidence_read", hold_id=hold_id)
    row = vault.get_evidence(hold_id)
    if row is None:
        raise DomainError("CLEARANCE_NOT_FOUND", "留存证据不存在或已解除", status=404)
    return dict(row)


@router.get("/api/evidence")
def list_evidence(x_role: str = Header(default="operator"),
                  x_audit_reason: str | None = Header(default=None)):
    vault.require_auditor(x_role, audit_reason(x_audit_reason), "evidence_list")
    return vault.list_evidence()


@router.get("/api/credentials")
def list_credentials():
    # 计数凭据不含行程号，业务角色可查
    return vault.list_credentials()


@router.get("/api/audit-log")
def read_audit_log(x_role: str = Header(default="operator"),
                   x_audit_reason: str | None = Header(default=None)):
    vault.require_auditor(x_role, audit_reason(x_audit_reason), "audit_read")
    return vault.list_audit()


@router.get("/api/export/itineraries.csv")
def export_itineraries(run_id: str | None = None):
    body = service.export_csv(run_id)
    return PlainTextResponse(
        body, media_type="text/csv",
        headers={"content-disposition": "attachment; filename=itineraries.csv"})
