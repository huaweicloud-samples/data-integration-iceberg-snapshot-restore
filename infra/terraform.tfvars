# 部署变量：区域 / 命名前缀 / 两个 OBS 桶名（必填，全局唯一）

region               = "cn-north-4"
name_prefix          = "iceberg-migration"
metadata_bucket_name = "iceberg-mig-md-7700d1"
data_bucket_name     = "iceberg-mig-dt-7700d1"
output_prefix        = "iceberg-metadata"
log_retention_days   = 30

# ----------------------------------------------------------------------------
# 函数访问 OBS 的凭证不写入本文件（避免明文密钥落盘）。
# 部署前通过环境变量传入：
#   export TF_VAR_function_ak=<永久 AK>
#   export TF_VAR_function_sk=<永久 SK>
# 更推荐：改用 IAM 委托 + context.getSecurityAccessKey() 临时凭证，无需永久密钥。
# ----------------------------------------------------------------------------