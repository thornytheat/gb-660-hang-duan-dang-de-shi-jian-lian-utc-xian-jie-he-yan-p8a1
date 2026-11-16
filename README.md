# 航班时刻运行台

面向真实数据复核的中型数据工程工具，包含六个可独立调用的业务模块。规则、失败原因与证据字段见 docs/DATA-DICTIONARY.md，陷阱和测试映射见 docs/DEPTH.md。

## 启动

docker compose up --build

浏览器访问 http://localhost:18219。测试：docker compose exec -T app pytest -q

## 模块

- M1 航段采信：页面 /legs，接口 /api/legs/import
- M2 机场时区：页面 /airports，接口 /api/airports
- M3 当地时刻：页面 /local-time，接口 /api/local-time/resolve
- M4 UTC 航链：页面 /itineraries，接口 /api/itineraries/build
- M5 衔接判定：页面 /connections，接口 /api/connections/check
- M6 批次导出：页面 /batches，接口 /api/reconcile

## 运行规则

同一输入内容生成相同运行标识，重复执行复用已有批次。隔离记录包含原因码、原始行号与原值；业务导出只保留一份证据清单。

## M6 外部行程号清除与证据隔离

- 业务库（`DATABASE_PATH`）保存批次航链、清除请求与墓碑；证据库（`EVIDENCE_DATABASE_PATH`）是独立文件，保存涉争议航链原件、不含行程号的计数凭据与审计日志，两库物理隔离。
- 清除覆盖同一外部行程号的所有批次及旧导出口径；管线按阶段推进，失败后凭 `request_id` 调 `/api/clearance/resume` 从断点恢复，重试只统计实际删除行，不重复扣减。
- 墓碑仅存加盐哈希，重跑旧批次不会复活已清除行程；业务查询与 `/api/export/itineraries.csv` 不再出现该行程号。
- 涉争议航链进入 `RETAINED`：业务侧已删除，原件在证据库可核验。`/api/evidence` 与 `/api/clearance/release` 要求请求头 `X-Role: audit` 且提供 `X-Audit-Reason` 查阅理由，普通角色返回 403 并写入审计日志。
- 解除留存后原件物理销毁，仅保留不含行程号的计数凭据（`/api/credentials`）。
- 批次页（/batches）核对待处理、隔离留存、已清除三态，并展示航段守恒差异。

