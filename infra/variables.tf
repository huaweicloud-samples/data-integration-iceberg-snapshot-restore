# ============================================================================
# 输入变量定义
# ============================================================================

variable "region" {
  description = "华为云区域. 例如 cn-north-4."
  type        = string
  default     = "cn-north-4"
}

variable "name_prefix" {
  description = "资源命名前缀, 应用于函数名 / IAM 委托名 / 日志流名等."
  type        = string
  default     = "iceberg-migration"
}

variable "metadata_bucket_name" {
  description = "OBS 桶名, 用于 pre-migration 函数写快照与 post-migration 函数读快照. 由本 Terraform 创建."
  type        = string
}

variable "data_bucket_name" {
  description = "OBS 桶名, 持有 Iceberg 表数据与 metadata.json 历史快照. rollback 函数据此列出 Nth-latest metadata.json. 由本 Terraform 创建."
  type        = string
}

variable "output_prefix" {
  description = "OBS 桶内快照文件 key 前缀."
  type        = string
  default     = "iceberg-metadata"
}

variable "log_retention_days" {
  description = "LTS 日志流保留天数."
  type        = number
  default     = 30
}

variable "tags" {
  description = "应用到所有资源的标签."
  type        = map(string)
  default = {
    project = "data-integration-iceberg-snapshot-restore"
    managed = "terraform"
  }
}

variable "function_ak" {
  description = "永久 AK (Access Key Id) — 注入到 FGS 函数 user_data 让函数以桶 owner 身份调用 OBS, 绕过私有桶 STS 临时凭证的 ACL owner 校验差异. 默认从环境变量 HW_AK 读取."
  type        = string
  sensitive   = true
  default     = null

  validation {
    condition     = var.function_ak == null || length(var.function_ak) >= 16
    error_message = "function_ak 长度异常, 永久 AK 一般 16-32 字符."
  }
}

variable "function_sk" {
  description = "永久 SK (Secret Access Key) — 注入到 FGS 函数 user_data 让函数以桶 owner 身份调用 OBS. 默认从环境变量 HW_SK 读取."
  type        = string
  sensitive   = true
  default     = null

  validation {
    condition     = var.function_sk == null || length(var.function_sk) >= 32
    error_message = "function_sk 长度异常, 永久 SK 一般 40+ 字符."
  }
}
