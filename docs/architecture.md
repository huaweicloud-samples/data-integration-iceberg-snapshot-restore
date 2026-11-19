# 架构说明

## 1. 业务背景

Apache Iceberg 是一种支持 ACID 事务、模式演进、隐藏分区、时间旅行的开放表格式，常作为数据湖的表存储格式。在华为云数据湖仓栈（DLI / MRS / DataArts Studio）中，Iceberg 表的元数据 `metadata.json` 文件存储在 OBS 上，每张表对应一个 `metadata_location` 指针指向最新版本的 `metadata.json`。

当数据工程师对承载 Iceberg 表的基础设施执行版本升级（如升级 DLI 队列版本、调整 Iceberg 客户端依赖、调整 Hadoop/Spark 客户端兼容性）时，会出现表指针被重置的风险：

- 升级前的指针指向 `obs://<data-bucket>/<table-prefix>/metadata/v5.metadata.json`（含全部历史快照）
- 升级过程中元数据被改写到 `obs://<data-bucket>/<table-prefix>/metadata/v6.metadata.json`（空数据）
- 如果不主动回填指针，下游查询引擎会读空元数据，表现为「表数据丢失」

实际上数据文件（Parquet）和历史 `metadata.json` 始终安全存储在 OBS 数据桶里，**只要把指针恢复到升级前的位置，所有历史快照即可恢复**。

本实践提供一个**三阶段无数据丢失工作流**：捕获 → 升级 → 恢复，覆盖 Iceberg 表基础设施升级的典型场景。

## 2. 目标架构

```
                        ┌─────────────────────────────────────┐
                        │  部署自动化（Terraform）             │
                        │   infra/versions.tf + variables.tf   │
                        │   infra/obs.tf (2 桶)                │
                        │   infra/iam.tf (3 委托 + 3 策略)     │
                        │   infra/functions.tf (3 函数 + LTS)   │
                        │   infra/scripts/build_zips.py       │
                        └────────────────┬────────────────────┘
                                         │
                                         ▼
                  ┌──────────────────────────────────────────┐
                  │   华为云 FunctionGraph（Python 3.10）     │
                  │                                          │
                  │   pre_migration   post_migration   rollback│
                  │   (512MB, 900s)   (512MB, 900s)   (512MB, │
                  │                                    900s) │
                  └────────────────────┬─────────────────────┘
                                       │ IAM 委托 + LTS 日志
                                       ▼
              ┌────────────────────────────────────────────────┐
              │         华为云 OBS（2 个独立桶）                 │
              │  ┌────────────────────┐ ┌─────────────────────┐│
              │  │ <metadata-bucket>  │ │  <data-bucket>      ││
              │  │                    │ │                     ││
              │  │ snapshots/<db>/    │ │ <db>/               ││
              │  │   <ts>.json        │ │   metadata/         ││
              │  │                    │ │     v*.metadata.json││
              │  │ manifests/<db>/    │ │   data/             ││
              │  │   <table>.json     │ │     *.parquet       ││
              │  └────────────────────┘ └─────────────────────┘│
              └────────────────────────────────────────────────┘
                                       │
                                       ▼
                  ┌──────────────────────────────────────────┐
                  │   华为云 LTS（日志组 + 3 条日志流）        │
                  │   <prefix>-lts-group                     │
                  │     ├─ <prefix>-pre-migration            │
                  │     ├─ <prefix>-post-migration           │
                  │     └─ <prefix>-rollback                 │
                  └──────────────────────────────────────────┘
```

## 3. 核心组件

| 组件 | 数量 | 职责 |
|---|---|---|
| OBS 桶（metadata） | 1 | 存储捕获阶段写入的快照（`snapshots/<db>/<ts>.json`）与 manifest 表索引（`manifests/<db>/<table>.json`） |
| OBS 桶（data） | 1 | 存储 Iceberg 表的真实 Parquet 数据与 `metadata/*.metadata.json` 历史快照 |
| IAM 委托 | 3 | `pre-migration-agency` / `post-migration-agency` / `rollback-agency`，分别挂自定义策略实现最小特权 |
| FunctionGraph 函数 | 3 | `pre-migration`（捕获） / `post-migration`（恢复） / `rollback`（紧急回滚到 Nth-latest） |
| LTS 日志流 | 3 | 每函数一条日志流；日志字段含 trace_id，运维侧按 trace_id 关联同一调用链的所有日志 |
| Terraform `null_resource` | 1 | 在 apply 时本地执行 `python3 scripts/build_zips.py`，把 Python 源文件 + `obs` 依赖打为 zip |

## 4. 工作流

### 阶段 1 · 捕获（Pre-migration）

**触发**：升级 Iceberg 表基础设施之前调用 `pre-migration` 函数。

**事件**：

```json
{
  "databases": ["db_a", "db_b"]
}
```

**行为**：

1. 对每个数据库，从 `manifests/<db>/` 读取所有 manifest.json 文件
2. 过滤出 `format == "iceberg"` 的表，收集 `name` 与 `metadata_location`
3. 把结果写入 `snapshots/<db>/<timestamp>.json`（时间戳按 UTC ISO 8601）
4. 返回快照的 obs URI

**输出**：

```json
{
  "statusCode": 200,
  "trace_id": "550e8400-e29b-41d4-a716-446655440000",
  "database_count": 2,
  "summary": {"ok": 2, "failed": 0},
  "results": [
    {
      "database": "db_a",
      "table_count": 5,
      "output_uri": "obs://<metadata-bucket>/iceberg-metadata/db_a/20260917T103045Z.json",
      "status": "ok"
    }
  ]
}
```

### 阶段 2 · 升级（Apply）

**触发**：CI/CD 流水线触发 terraform apply 升级 Iceberg 基础设施。

**行为**：

- 表指针在升级过程中被改写到新 `metadata.json`，但 Parquet 数据 + 历史 `metadata.json` 在 OBS 数据桶里**始终完整保留**
- 此阶段由本实践外的升级流程负责，本实践不参与

### 阶段 3 · 恢复（Post-migration）

**触发**：升级完成后调用 `post-migration` 函数，传阶段 1 写下的快照 URI。

**事件**：

```json
{
  "database": "db_a",
  "input_key": "iceberg-metadata/db_a/20260917T103045Z.json"
}
```

**行为**：

1. 读快照文件，校验 `database` 字段匹配
2. 对快照中每张表，读当前 manifest.json
3. 若 `metadata_location` 与快照值不同 → 改写 manifest.json 字段；相同则记 `unchanged`
4. 每张表的 outcome = `restored` / `unchanged` / `skipped` / `failed`

**输出**：

```json
{
  "statusCode": 200,
  "trace_id": "550e8400-e29b-41d4-a716-446655440001",
  "database": "db_a",
  "source_file": "obs://<metadata-bucket>/iceberg-metadata/db_a/20260917T103045Z.json",
  "summary": {"restored": 4, "unchanged": 1, "skipped": 0, "failed": 0},
  "results": [
    {"name": "events", "status": "restored", "from": "obs://.../v6.metadata.json", "to": "obs://.../v5.metadata.json"}
  ]
}
```

### 阶段 4 · 紧急回滚（Rollback）

**触发**：阶段 3 落地后发现指针错误，或后续 commit 需要回退时调用 `rollback` 函数。

**事件**：

```json
{
  "database": "db_a"
}
```

**行为**：

1. 从 `manifests/<db>/` 列出所有 Iceberg 表
2. 对每张表，从 data 桶的 `<table-prefix>/metadata/*.metadata.json` 列出全部历史 metadata 文件
3. 按 `LastModified` 排序，选**倒数第 4 个**（即 `ROLLBACK_OFFSET=3`）作为回滚目标
4. 更新 manifest.json 的 `metadata_location` 字段

## 5. 设计要点

### 5.1 两个 OBS 桶物理隔离

- **metadata 桶**：仅 pre/post/rollback 函数读写；存放快照与 manifest
- **data 桶**：pre 函数只读（用于列出表前缀），rollback 函数只读；post 函数不接触
- 物理隔离保证「一次误删 metadata 桶不会影响 Iceberg 真实数据」

### 5.2 manifest.json 是元数据指针的唯一真实来源

manifest.json 的 schema：

```json
{
  "database": "db_a",
  "table": "events",
  "format": "iceberg",
  "metadata_location": "obs://<data-bucket>/db_a/events/metadata/v5.metadata.json",
  "updated_at": "2026-09-17T10:30:45Z",
  "trace_id": "550e8400-e29b-41d4-a716-446655440000"
}
```

下游查询引擎通过 DLI SQL / MRS Spark 等读取 `metadata_location` 指向的最新 `metadata.json`，再依据 Iceberg 规范加载 manifest list 与 Parquet 数据文件。manifest.json 由本实践三个函数统一维护。

### 5.3 trace_id 贯穿调用链

每个函数在日志与响应中透传 `trace_id`：

- 调用方传入：`{"trace_id": "..."}` —— 可选
- 函数生成：未传入则生成 UUID4
- 写入日志：`logger.info(f"[trace={trace_id}] ...")` —— LTS 日志可按 trace_id 字段过滤
- 写入 manifest：`_write_manifest()` 时记录到 `trace_id` 字段，便于审计

### 5.4 幂等性保证

post-migration 函数对每张表的处理是幂等的：

- 若当前 manifest 的 `metadata_location` 与快照值一致 → 返回 `unchanged`，**不重写**
- 若快照值与数据库不匹配 → 抛 `ValueError`，整个函数拒绝执行（避免部分表已恢复、部分表未恢复的不一致状态）

rollback 函数同样幂等：若 manifest 已是 Nth-latest 值 → 返回 `unchanged`。

## 6. 限制

| 限制 | 说明 |
|---|---|
| 单次调用 900s 超时 | FunctionGraph 单次执行最长 900s，对 500+ 表的数据库建议拆分多次调用 |
| 单数据库对应一次 post-migration | 函数按 database 维度调用，跨 database 需分别调用 |
| ROLLBACK_OFFSET 写死为 3 | 若需变更偏移，修改 `rollback_metadata.py` 中的 `ROLLBACK_OFFSET` 常量并重新打包 |
| 元数据目录服务依赖 manifest.json | 放弃外部元数据目录抽象；不依赖外部元数据服务，迁移成本低但失去列级权限 / 审计能力 |
