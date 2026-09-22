# 多税区发票金额分摊 API

基于 **FastAPI + SQLite** 的发票金额分摊服务：接收含折扣、税率、税区比例的发票明细，
用 `Decimal` 精确计算折扣与各税区税额，并用**最大余数法**把舍入差额稳定分配到明细行；
比例或总额不一致的请求会被拒绝。每个发票返回逐行计算依据与总额校验结果。

## 一键启动（Docker Compose）

```bash
docker compose up --build
```

启动后访问：

| 地址 | 说明 |
| --- | --- |
| http://localhost:8000/docs | Swagger 交互式文档 |
| http://localhost:8000/health | 健康检查 |
| http://localhost:8000/api/v1/invoices | 发票接口 |

SQLite 数据保存在名为 `invoice-data` 的卷中（容器内 `/data/invoices.db`），重启不丢失。
容器启动时会先执行 `python -m app.init_db` 自动建表/迁移（含 `void_events` 表与
`invoices.status` 列），对已有数据卷可安全重复执行，随后才启动 API 服务。

也可以不用 Compose，直接构建运行：

```bash
docker build -t invoice-allocation-api .
docker run -p 8000:8000 -v invoice-data:/data invoice-allocation-api
```

## 本地开发

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
uvicorn app.main:app --reload --port 8000
pytest                 # 运行测试（36 个用例）
```

数据库路径通过环境变量 `DATABASE_PATH` 配置（默认 `data/invoices.db`）。

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/api/v1/invoices` | 创建发票：计算分摊、校验、落库，返回完整结果（201） |
| `POST` | `/api/v1/invoices/batch` | 原子批量开票：整批先计算校验，再同事务写入；任一失败整批回滚（201） |
| `GET` | `/api/v1/invoices/{invoice_id}` | 按 ID 查询发票结果；已作废发票附带 `voided_at` / `voided_by` / `void_reason` |
| `POST` | `/api/v1/invoices/{invoice_id}/void` | 作废发票：必填 `reason` 与 `operator`，仅 `validated` 状态可作废（200） |
| `GET` | `/api/v1/invoices` | 发票列表（`limit` / `offset` 分页）；默认排除已作废，`include_voided=true` 可包含 |
| `GET` | `/health` | 健康检查 |

### 创建发票示例

```bash
curl -X POST http://localhost:8000/api/v1/invoices \
  -H 'Content-Type: application/json' \
  -d @examples/create_invoice.json
```

请求体（金额字段建议传字符串以保证精度）：

```json
{
  "invoice_number": "INV-2026-0001",
  "currency": "CNY",
  "lines": [
    {
      "description": "Consulting services",
      "quantity": "2",
      "unit_price": "100.00",
      "discount_rate": "0.10",
      "tax_regions": [
        {"region_code": "CN-SH", "tax_rate": "0.13", "proportion": "0.6"},
        {"region_code": "CN-BJ", "tax_rate": "0.13", "proportion": "0.4"}
      ]
    }
  ],
  "expected_totals": {"grand_total": "203.40"}
}
```

字段说明：

- `discount_rate` / `discount_amount`：行折扣，按比例、按固定额均可（可叠加，折扣不得超过行总额）。
- `tax_regions[].proportion`：该行净额在各税区间的分摊比例，**每行比例之和必须等于 1**（容差 1e-6）。
- `tax_regions[].tax_rate`：税区税率（0~1 的小数）。
- `expected_totals`（可选）：调用方预期的 `net` / `tax` / `grand_total`，与计算结果不一致时**拒绝创建**（400）。

### 响应内容

- `lines[]`：逐行计算依据 —— 总额、折扣、净额，以及每个税区的 `taxable_amount`（计税基础）与 `tax_amount`（分摊后税额）。
- `totals`：发票级合计（总额/折扣/净额/税额/含税总额）及分税区合计。
- `validation`：总额校验报告，逐项列出比例合计、净额勾稽、税区分摊合计、价税合计等检查结果。

### 原子批量开票

`POST /api/v1/invoices/batch` 接收 `{"invoices": [...]}`（1~100 张），每张发票的字段、折扣与税额分摊规则与单张开票完全一致。处理分三个阶段：

1. **整批计算与校验**：先对每张发票完成分摊计算与 `expected_totals` 校验，此阶段不写库；
2. **发票号查重**：检查批内互重及与库中已有发票号冲突；
3. **同事务写入**：全部通过后在**同一事务**中落库。

任一发票校验失败或发票号重复时**整批回滚、一张不写**，错误响应按请求中的下标（`index`）定位问题发票。

```bash
curl -X POST http://localhost:8000/api/v1/invoices/batch \
  -H 'Content-Type: application/json' \
  -d @examples/create_invoices_batch.json
```

成功响应（201）按顺序返回每张发票的 `invoice_id`、`invoice_number` 与 `grand_total`：

```json
{
  "invoices": [
    {"index": 0, "invoice_id": "f3c9…", "invoice_number": "INV-2026-0001", "grand_total": 203.40},
    {"index": 1, "invoice_id": "8a1b…", "invoice_number": "INV-2026-0002", "grand_total": 33.04}
  ]
}
```

失败响应示例（第 2 张发票 `expected_totals` 不匹配，整批回滚，400）：

```json
{
  "detail": {
    "error": "batch_rejected",
    "message": "one or more invoices failed validation; nothing was written",
    "errors": [
      {
        "index": 1,
        "invoice_number": "INV-2026-0002",
        "error": "totals_mismatch",
        "mismatches": ["grand_total: expected 999.00, computed 33.04"]
      }
    ]
  }
}
```

发票号重复（批内互重或已存在于库中）返回 409，`errors[]` 同样按 `index` 标出冲突的发票。

### 作废发票

`POST /api/v1/invoices/{invoice_id}/void` 作废一张发票，请求体必填 `reason`（作废原因）与 `operator`（操作人）：

```bash
curl -X POST http://localhost:8000/api/v1/invoices/<invoice_id>/void \
  -H 'Content-Type: application/json' \
  -d @examples/void_invoice.json
```

行为规则：

- 仅 `status` 为 `validated` 的发票允许作废；其他状态（如 `inconsistent`）返回 409（`invalid_status`）。
- 服务端在**同一 SQLite 事务**中写入不可变的作废事件（`void_events` 表，每张发票至多一条、永不更新或删除）并将发票状态更新为 `voided`，两者要么同时生效要么同时回滚。
- **重复作废返回 409**（`already_voided`，响应中附带首次作废的事件信息），且不会产生第二条作废事件。
- 成功响应（200）返回完整发票，`status` 变为 `voided`，并携带 `voided_at`（作废时间，UTC ISO-8601）、`voided_by`（操作人）、`void_reason`（原因）；之后 `GET /api/v1/invoices/{invoice_id}` 同样返回这三个字段。
- 列表接口默认排除已作废发票，加 `?include_voided=true` 可一并查询（每条摘要含 `status` 字段）。

```json
{
  "invoice_id": "f3c9…",
  "invoice_number": "INV-2026-0001",
  "status": "voided",
  "voided_at": "2026-09-22T08:30:00.123456Z",
  "voided_by": "finance-zhang",
  "void_reason": "Duplicate invoice issued for the same order",
  "lines": ["…"], "totals": {"…": "…"}, "validation": {"…": "…"}
}
```

## 计算规则

1. **全程 Decimal**：所有金额以 `decimal.Decimal` 计算，按 `ROUND_HALF_UP` 精确到分（0.01），不使用浮点。
2. **行内计算**：`总额 = 数量 × 单价` → `折扣 = 总额 × 折扣率 + 固定折扣` → `净额 = 总额 − 折扣`。
3. **税区计税基础**：行净额按各税区比例拆分，用最大余数法保证各行税区计税基础之和恰好等于行净额。
4. **税额分摊（最大余数法）**：先按 `计税基础 × 税率` 得到各行的**未舍入精确税额**，按税区汇总并对总额一次性舍入；再把该税区总额按各行精确税额的最大余数法分配到明细行 —— 余数最大者优先分得 1 分，余数相同按行顺序（结果稳定、可复现）。因此**各行税额之和恒等于税区总额**，不会出现尾差。
5. **总额勾稽**：`含税总额 = 净额合计 + 税额合计`，响应中的 `validation.checks` 逐项给出校验结果。

## 拒绝规则

| 场景 | 状态码 |
| --- | --- |
| 行内税区比例之和 ≠ 1、税区编码重复、折扣超过总额、参数非法（负数量等） | 422 |
| `expected_totals` 与计算结果不一致 | 400（返回各项差异明细） |
| `invoice_number` 重复 | 409 |
| 批量请求中任一发票校验失败 / 发票号重复（整批回滚，按 `index` 返回错误） | 400 / 409 |
| 作废请求缺少 `reason` / `operator` 或为空白 | 422 |
| 作废不存在的发票 | 404 |
| 重复作废 / 作废非 `validated` 状态的发票 | 409 |
| 查询不存在的发票 | 404 |

## 项目结构

```
app/
  main.py        FastAPI 入口与路由
  schemas.py     请求/响应模型（含比例一致性校验）
  allocation.py  Decimal 精确计算与最大余数法
  service.py     发票计算：折扣、税区分摊、总额校验
  db.py          SQLite 持久化（Decimal 以字符串无损存储；作废事件同事务写入）
  init_db.py     容器启动时建表/迁移（python -m app.init_db）
tests/           pytest 单元测试与 API 集成测试
examples/        示例请求
Dockerfile / docker-entrypoint.sh / docker-compose.yml
```
