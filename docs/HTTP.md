# 接口

- `GET /`：工作台。
- `GET /api/catalog`：阶段、字段和样例。
- `POST /api/stages/{stage}`：按阶段返回处理结果。
- `POST /api/runs`：执行完整批次并保存草稿。
- `GET /api/runs/{id}`：读取批次。
- `POST /api/runs/{id}/review`：签发或暂缓，提交 `decision`、`comment`、`expected_revision`。
- `GET /api/runs/{id}/evidence`：下载已签发证据。
- `POST /api/agent-vouchers`：按代理生成批次凭证和能力下载链接；请求可传 `batch_id`、`run_id`、`payments` 或 `assignments`。
- `POST /api/agents/{agent_id}/vouchers`：生成单个代理的批次凭证。
- `GET /api/agent-vouchers/{token}/download`：下载代理凭证；同一链接重复下载保持核对号不变。
- `POST /api/agents/{agent_id}/revoke`：撤回代理权限，该代理已有链接立即返回 `403`。
- `GET /api/examples/{profile}`：下载样例。
- `GET /healthz`：健康检查。

代理凭证只包含该代理经手且已签发的款。没有代理归属的旧票不会进入凭证；票号、旅客号等可交叉串联的原始标识不输出。核对号由代理和款项身份稳定派生，同代理跨批次一致，不同代理之间不可直接关联。凭证合计只汇总实际列出的款项。
