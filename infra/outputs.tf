# ============================================================================
# 输出: 供外部编排系统 (CI/CD / 调度) 使用的关键信息
# ============================================================================

output "pre_migration_function_urn" {
  description = "pre-migration 函数的 URN (用于通过 FunctionGraph API 触发)"
  value       = huaweicloud_fgs_function.pre_migration.urn
}

output "pre_migration_function_name" {
  description = "pre-migration 函数名 (用于 invoke)"
  value       = huaweicloud_fgs_function.pre_migration.name
}

output "post_migration_function_urn" {
  description = "post-migration 函数的 URN"
  value       = huaweicloud_fgs_function.post_migration.urn
}

output "post_migration_function_name" {
  description = "post-migration 函数名 (用于 invoke)"
  value       = huaweicloud_fgs_function.post_migration.name
}

output "rollback_function_urn" {
  description = "rollback 函数的 URN"
  value       = huaweicloud_fgs_function.rollback.urn
}

output "rollback_function_name" {
  description = "rollback 函数名 (用于 invoke)"
  value       = huaweicloud_fgs_function.rollback.name
}

output "snapshot_obs_prefix" {
  description = "OBS metadata 桶内快照文件 key 前缀"
  value       = "obs://${huaweicloud_obs_bucket.metadata.bucket}/${var.output_prefix}/"
}

output "manifest_obs_prefix" {
  description = "OBS metadata 桶内 manifest 文件 key 前缀 (post/rollback 据此回填)"
  value       = "obs://${huaweicloud_obs_bucket.metadata.bucket}/manifests/"
}
