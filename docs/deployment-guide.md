# 部署指导

> 本文档说明本实践在华为云上的部署参数、权限边界与故障排查，配合 `README.md` 使用。

## 1. 部署架构概览

本实践以 Terraform 编排以下资源：

| 资源类型 | 数量 | 说明 |
|---|---|---|
| OBS 桶 | 2 | 元数据桶（snapshot + manifest）、数据桶（Iceberg 真实数据） |
| FunctionGraph 函数 | 3 | pre_migration / post_migration / rollback |
| IAM 委托 | 3 | 每个函数一个，挂载自定义策略 |
| LTS 日志组 / 日志流 | 1 / 3 | 每函数一条日志流 |
| null_resource | 1 | apply 时本地打包函数 zip |

## 2. 前置环境准备

### 2.1 本地工具

| 工具 | 版本要求 | 用途 |
|---|---|---|
| Terraform | `>= 1.5.0` | 资源编排 |
| Python | `>= 3.7` | 函数运行时 + 打包脚本 |
| pip | 任意 | 安装 `esdk-obs-python` 到构建目录 |
| huaweicloud Provider | `~> 1.97` | Terraform 初始化时自动拉取 |
| null Provider | `~> 3.2` | Terraform 初始化时自动拉取 |

### 2.2 访问凭证

在华为云控制台「我的凭证」页面创建访问密钥（Access Key / Secret Key）。本实践有两种凭证使用方式：

- **IAM 委托（推荐）**：Terraform 为三个函数分别创建 IAM 委托并绑定到 FunctionGraph。函数运行时由平台注入临时凭证，代码中优先读取 `HUAWEICLOUD_ACCESS_KEY_ID` / `HUAWEICLOUD_SECRET_ACCESS_KEY` / `HUAWEICLOUD_SECURITY_TOKEN`。
- **手动配置（回退）**：若委托未注入，代码回退读取 `HW_AK` / `HW_SK`。可在 FunctionGraph 控制台配置到函数环境变量。

> 凭证仅用于本地 `terraform` 执行与函数运行，不在代码中硬编码。

## 3. 变量说明

| 变量 | 类型 | 默认值 | 必填 | 说明 |
|---|---|---|---|---|
| `region` | string | `cn-north-4` | 否 | 华为云区域 |
| `name_prefix` | string | `iceberg-migration` | 否 | 资源命名前缀 |
| `metadata_bucket_name` | string | — | 是 | 元数据桶名（由本实践创建） |
| `data_bucket_name` | string | — | 是 | 数据桶名（由本实践创建） |
| `output_prefix` | string | `iceberg-metadata` | 否 | 快照 key 前缀 |
| `log_retention_days` | number | `30` | 否 | LTS 日志保留天数 |
| `tags` | map | 见变量定义 | 否 | 资源标签 |

## 4. IAM 委托权限边界

| 函数 | 桶 | 允许动作 | 前缀范围 |
|---|---|---|---|
| pre_migration | metadata | PutObject / PutObjectAcl | `output_prefix/*`、`manifests/*` |
| pre_migration | metadata | ListBucket / GetObject | `*` |
| pre_migration | data | ListBucket / GetObject | `*` |
| post_migration | metadata | GetObject | `output_prefix/*`、`manifests/*` |
| post_migration | metadata | ListBucket / GetObject / PutObject | `manifests/*` |
| rollback | data | ListBucket | bucket 级 |
| rollback | data | GetObject | `*` |
| rollback | metadata | ListBucket / GetObject / PutObject | `manifests/*` |

权限严格遵循最小特权：rollback 函数只能**读**数据桶、只能**写** metadata 桶的 manifest 前缀。

## 5. 函数打包机制

`infra/scripts/build_zips.py` 在 `null_resource.build_function_zips` 的 `local-exec` 中执行，也会在「分步部署」步骤由用户手动执行一次：

1. 用 `pip install --target <build_dir> esdk-obs-python` 安装 OBS SDK。
2. 把 `src/functions/*.py` 与 `obs` 包一起打进 `infra/.build/<name>.zip`。

> **关键点**：`huaweicloud_fgs_function` 的 `code` 字段在 `terraform plan` 阶段通过 `filebase64()` 求值，因此 **plan 前 `.build/*.zip` 必须已存在**。请务必先执行打包命令，再执行 `terraform init` / `apply`。

## 6. LTS 日志

- 三个函数共用一个日志组 `<name_prefix>-lts-group`，每函数一条日志流。
- 函数运行日志含 `[trace=<trace_id>]` 前缀，可在 LTS 控制台按 `trace_id` 字段检索，关联同一调用链。
- 日志保留天数由 `log_retention_days` 控制。

## 7. 故障排查

| 现象 | 原因 | 处理 |
|---|---|---|
| `terraform plan` 报 `.build/*.zip` 不存在 | 未先打包 | 执行 `python3 scripts/build_zips.py ../src/functions .build` |
| 函数报 `Missing credentials` | 委托未生效或环境变量缺失 | 检查 IAM 委托绑定；或在函数环境变量配置 `HW_AK` / `HW_SK` |
| 函数报 OBS `AccessDenied` | 委托策略前缀不匹配 | 核对第 4 节权限矩阵与桶名 / 前缀 |
| `rollback` 大量 `skipped` | 数据桶元数据文件不足 4 个 | 确认升级已产生多个 `metadata.json` 历史版本 |
| `post_migration` 全 `unchanged` | 指针未被改写 | 确认升级流程确实重置了元数据指针 |

## 8. 区域与端点

- 默认区域 `cn-north-4`，OBS 端点 `obs.cn-north-4.myhuaweicloud.com`。
- 切换区域时同步调整 `region` 变量与 OBS 端点环境变量 `OBS_ENDPOINT`。
