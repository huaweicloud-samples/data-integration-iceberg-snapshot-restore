# data-integration-iceberg-snapshot-restore 华为云实践

[![License: MIT No Attribution](https://img.shields.io/badge/License-MIT%20No%20Attribution-blue.svg)](LICENSE)
![Huawei Cloud](https://img.shields.io/badge/Huawei%20Cloud-verified-green.svg)
![Build](https://img.shields.io/badge/build-passing-brightgreen.svg)

## 1. 简介

Apache Iceberg 是一种支持 ACID 事务、模式演进与时间旅行的开放表格式，常作为数据湖的表存储格式。在华为云数据湖仓栈（DLI / MRS / DataArts Studio）中，Iceberg 表的元数据文件存储在对象存储上，每张表对应一个元数据指针指向最新版本的元数据文件。

当数据工程师对承载 Iceberg 表的基础设施执行版本升级时，会出现表指针被重置的风险：升级过程中元数据被改写到新版本（空数据），若不主动回填指针，下游查询引擎会读空元数据，表现为「表数据丢失」。实际上数据文件与历史元数据始终安全存储，只要把指针恢复到升级前的位置，所有历史快照即可恢复。

本实践提供一个**三阶段无数据丢失工作流**：捕获 → 升级 → 恢复，覆盖 Iceberg 表基础设施升级的典型场景。

## 2. 目录

- [简介](#简介)
- [架构图](#架构图)
- [方案亮点](#方案亮点)
- [涉及云服务](#涉及云服务)
- [前置条件](#前置条件)
- [快速开始](#快速开始)
- [分步部署](#分步部署)
- [使用方法](#使用方法)
- [清理资源](#清理资源)
- [详细说明](#详细说明)
- [依赖](#依赖)
- [常见问题](#常见问题)
- [贡献指南](#贡献指南)
- [许可证](#许可证)
- [联系方式](#联系方式)

## 3. 架构图

```
                  ┌────────────────────────────────────────────┐
                  │   FunctionGraph (Python 3.10)               │
                  │                                            │
                  │   pre_migration   post_migration   rollback │
                  │   (捕获)          (恢复)          (回滚)     │
                  └─────────────┬──────────────────────────────┘
                                │ IAM 委托（最小特权）
                                ▼
              ┌──────────────────────────────────────────────┐
              │            对象存储 OBS（2 个独立桶）          │
              │  ┌─────────────────┐  ┌─────────────────────┐ │
              │  │ <metadata-bucket>│  │  <data-bucket>      │ │
              │  │   snapshots/     │  │   <db>/             │ │
              │  │   manifests/     │  │     metadata/       │ │
              │  └─────────────────┘  │     data/           │ │
              │                       └─────────────────────┘ │
              └──────────────────────────────────────────────┘
                                │
                                ▼
                  ┌────────────────────────────────────────────┐
                  │   云日志服务 LTS（日志组 + 日志流）         │
                  └────────────────────────────────────────────┘
```

## 4. 方案亮点

- **Serverless 无服务器架构**：函数工作流按需触发，无需长期运行的计算资源。
- **两个 OBS 桶物理隔离**：元数据桶与数据桶分离，避免误操作影响 Iceberg 真实数据。
- **最小特权 IAM 委托**：每个函数对应一个 IAM 委托，权限严格限定到桶与前缀。
- **trace_id 调用链追踪**：每个函数在日志与响应中透传 `trace_id`，运维侧按 `trace_id` 关联同一调用链的所有日志。
- **幂等设计**：恢复与回滚操作可重复执行，不产生副作用。

## 5. 涉及云服务

本实践涉及以下华为云服务：

| 服务 | 用途 |
|---|---|
| 对象存储服务 OBS | 存储元数据快照、manifest 索引与 Iceberg 表数据 |
| 函数工作流 FunctionGraph | 承载捕获 / 恢复 / 回滚三个函数 |
| 统一身份认证 IAM | 为函数提供最小特权的访问委托 |
| 云日志服务 LTS | 归集函数运行日志，支持按 `trace_id` 检索 |

> 使用完毕后请按「清理资源」章节释放资源，避免环境残留。

## 6. 前置条件

- 华为云账号及访问密钥：在华为云控制台的「我的凭证」页面获取 Access Key 与 Secret Key，并配置到本地环境。
- Terraform `>= 1.5.0`。
- Python `>= 3.7`（用于本地打包函数代码）。
- huaweicloud Provider `~> 1.97` 与 null Provider `~> 3.2`。
- 区域：本实践以 `cn-north-4` 为例，可通过 `region` 变量调整。

## 7. 快速开始

```bash
# 1. 进入基础设施目录
cd data-integration-iceberg-snapshot-restore/infra

# 2. 准备变量文件（按需修改两个 OBS 桶名）
cp terraform.tfvars.example terraform.tfvars

# 3. 本地打包函数代码（生成 infra/.build/*.zip）
python3 scripts/build_zips.py ../src/functions .build

# 4. 初始化并部署
terraform init
terraform apply
```

## 8. 分步部署

### 步骤 1：准备变量

复制示例变量文件并填入两个全局唯一的 OBS 桶名：

```bash
cd data-integration-iceberg-snapshot-restore/infra
cp terraform.tfvars.example terraform.tfvars
```

`terraform.tfvars` 关键字段：

| 字段 | 说明 |
|---|---|
| `metadata_bucket_name` | 元数据桶名，由本实践创建 |
| `data_bucket_name` | 数据桶名，由本实践创建（持有 Iceberg 表数据） |
| `name_prefix` | 资源命名前缀 |
| `region` | 华为云区域 |
| `output_prefix` | 快照文件 key 前缀 |
| `log_retention_days` | LTS 日志保留天数 |

### 步骤 2：打包函数

```bash
python3 scripts/build_zips.py ../src/functions .build
```

预期输出：

```
[+] Built <path>/.build/pre_migration.zip
[+] Built <path>/.build/post_migration.zip
[+] Built <path>/.build/rollback_metadata.zip
```

> 该脚本会一并安装 `esdk-obs-python` 到构建目录并打进 zip，保证函数运行时自包含。

### 步骤 3：初始化与部署

```bash
terraform init
terraform apply
```

`terraform apply` 完成后会输出三个函数的 URN / 名称以及快照与 manifest 的 OBS 前缀。

## 9. 使用方法

三个函数均通过 FunctionGraph 触发，事件为 JSON 结构。

### 阶段 1 · 捕获（升级前）

```json
{
  "databases": ["database_a", "database_b"]
}
```

调用 `pre_migration` 函数，捕获每个数据库下所有 Iceberg 表的元数据指针，写入元数据桶的快照文件，返回快照的 OBS URI。

### 阶段 2 · 升级

由本实践之外的升级流程执行基础设施版本升级。Iceberg 表数据文件与历史元数据始终保存在数据桶中。

### 阶段 3 · 恢复（升级后）

```json
{
  "database": "database_a",
  "input_key": "iceberg-metadata/database_a/20260917T103045Z.json"
}
```

调用 `post_migration` 函数，将快照中记录的指针逐表回填到 manifest，返回每个表的 `restored` / `unchanged` / `skipped` / `failed` 状态。

### 阶段 4 · 紧急回滚

```json
{
  "database": "database_a"
}
```

调用 `rollback` 函数，根据数据桶中历史元数据文件的时间顺序，将指针重置到倒数第 4 个版本，返回每个表的 `rolled_back` / `unchanged` / `skipped` 状态。

> **验证提示**：部署完成后，请在本地 PC 按本节步骤依次触发三个函数，确认捕获、恢复、回滚流程按预期工作，方可视为交付完成。

## 10. 清理资源

```bash
cd data-integration-iceberg-snapshot-restore/infra
terraform destroy
```

该命令释放本实践创建的所有资源（函数、IAM 委托、LTS 日志、OBS 桶），避免环境残留。

## 11. 详细说明

- 架构设计、工作流细节、trace_id 机制与限制条件详见 [docs/architecture.md](docs/architecture.md)。
- 部署参数、权限边界与故障排查详见 [docs/deployment-guide.md](docs/deployment-guide.md)。

## 12. 依赖

| 依赖 | 版本 | 说明 |
|---|---|---|
| Terraform | `>= 1.5.0` | 基础设施编排 |
| huaweicloud Provider | `~> 1.97` | 华为云资源编排 |
| null Provider | `~> 3.2` | 本地打包函数代码 |
| Python | `>= 3.7` | 函数运行时与打包脚本 |
| esdk-obs-python | 最新 | 函数访问 OBS 的 SDK（打包时自动安装） |

## 13. FAQ

**Q：函数部署时报 `Missing credentials`？**
A：确认函数已绑定 IAM 委托（由 Terraform 自动创建），且委托已授予 OBS 相应权限。委托模式下运行时会自动注入临时凭证。

**Q：`terraform plan` 报找不到 `.build/*.zip`？**
A：请先执行「分步部署 · 步骤 2」的打包命令生成 zip 文件，再执行 `terraform init` / `apply`。

**Q：回滚步长能否调整？**
A：修改 `src/functions/rollback_metadata.py` 中的 `ROLLBACK_OFFSET` 常量（默认 3，即倒数第 4 个版本）后重新打包并部署。

## 14. 贡献指南

欢迎通过 Issue 与 Pull Request 提交改进建议。提交前请确认本地 `terraform validate` 通过，且函数代码已重新打包。详见 [CONTRIBUTING.md](CONTRIBUTING.md)。

## 15. 许可证

本实践采用 MIT No Attribution 许可证。详见 [LICENSE](LICENSE)。

## 16. 联系方式

- 交流渠道：通过仓库 Issue 反馈问题与建议。

## 17. 创建者与维护者

| 角色 | GitHub 账号 |
|---|---|
| **创建者** | `dgc-git` |
| **维护者** | `dgc-git` |
