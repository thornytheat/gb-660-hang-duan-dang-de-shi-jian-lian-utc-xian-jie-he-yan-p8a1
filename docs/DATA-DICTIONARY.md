# 数据字典

- run_id：规则版本与输入 SHA-256 前 12 位组成的稳定批次标识。
- line_no：从 1 开始的物理来源行号，不因隔离或排序改变。
- business_key：本项目声明的业务唯一键，始终按字符串存储。
- reason_code：隔离原因的稳定英文编码。
- accepted：进入业务计算的记录数。
- quarantined：保留证据但不参与业务计算的记录数。
- 数值：金额、税额、用量和权重使用 Decimal；对外序列化为字符串。
- 时间：存储为带偏移的 ISO 8601，分桶使用半开区间。
- 空值：缺失键、空串和业务哨兵分别记录。
- 幂等：完整输入字节与规则版本相同则复用 run_id，不新增结果。
- external_id：外部行程号，始终按字符串保形（TRAP-09）；获准清除后业务库不再保留明文。
- external_id_hash：外部行程号的加盐 SHA-256，墓碑与去重只使用哈希，不可还原明文。
- clearance 状态：PENDING 待处理 / RETAINED 隔离留存（争议航链原件在证据库）/ CLEARED 已清除。
- hold_id：证据库留存原件的随机标识，业务库只存该标识而不存行程号。
- 计数凭据：request_id、hold_id、leg_count、quarantine_count、reason_code 与时间戳，不含任何行程号字段。
- 证据库：独立 SQLite 文件（EVIDENCE_DATABASE_PATH），与业务库物理隔离；审计访问需角色 audit 与查阅理由。

