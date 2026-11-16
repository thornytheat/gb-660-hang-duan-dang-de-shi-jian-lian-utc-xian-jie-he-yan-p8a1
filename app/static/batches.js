// M6 批次页：真实航链接入、清除三态与受控证据查阅
const result = document.querySelector("#result");
const feedback = document.querySelector("#feedback");
const evidenceFeedback = document.querySelector("#evidence-feedback");

function headers(extra = {}) {
  const role = document.querySelector("#role-select")?.value || "operator";
  const reason = document.querySelector("#audit-reason")?.value?.trim() || "";
  // HTTP 头仅支持 Latin-1，中文理由按 UTF-8 转义后由后端还原
  const encoded = reason ? encodeURIComponent(reason).replace(/%([0-9A-F]{2})/g, (_, hex) => String.fromCharCode(parseInt(hex, 16))) : "";
  return Object.assign({"content-type": "application/json", "x-role": role},
    encoded ? {"x-audit-reason": encoded} : {}, extra);
}

async function call(url, options = {}) {
  const response = await fetch(url, options);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.detail?.message || "请求失败");
  return data;
}

async function runSample() {
  feedback.textContent = "运行中";
  try {
    const data = await call("/api/reconcile", {method: "POST", headers: headers(), body: JSON.stringify({})});
    result.textContent = JSON.stringify(data, null, 2);
    feedback.textContent = "航链已接入";
    setTimeout(() => window.location.reload(), 600);
  } catch (error) {
    result.textContent = error.message;
    feedback.textContent = "失败，可重试";
  }
}

async function readEvidence() {
  evidenceFeedback.textContent = "查阅中…";
  const h = headers();
  try {
    const data = await call("/api/evidence", {method: "GET", headers: {"x-role": h["x-role"], "x-audit-reason": h["x-audit-reason"] || ""}});
    result.textContent = JSON.stringify(data, null, 2);
    evidenceFeedback.textContent = `审计查阅通过，返回 ${data.length} 份留存证据`;
  } catch (error) {
    result.textContent = error.message;
    evidenceFeedback.textContent = "越权已拒绝并记录审计日志";
  }
}

async function readCredentials() {
  try {
    const data = await call("/api/credentials", {method: "GET"});
    result.textContent = JSON.stringify(data, null, 2);
    evidenceFeedback.textContent = "计数凭据仅含数量与原因码，不含任何行程号";
  } catch (error) {
    result.textContent = error.message;
  }
}

document.querySelector("#run")?.addEventListener("click", runSample);
document.querySelector("#btn-evidence")?.addEventListener("click", readEvidence);
document.querySelector("#btn-credentials")?.addEventListener("click", readCredentials);
