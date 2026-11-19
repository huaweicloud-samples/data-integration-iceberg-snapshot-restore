# ============================================================================
# IAM: FunctionGraph 委托 (agency) + 自定义角色 (最小特权)
# ----------------------------------------------------------------------------
# 3 个函数各对应一个 IAM 委托, 委托挂载一个只含必要 OBS 权限的自定义角色.
# 自定义角色用内联 JSON 策略 (type = "AX"), 不依赖 policy_document 数据源.
# 委托类型 delegated_service_name = "op_svc_cff" (FunctionGraph 服务).
# 注: huaweicloud_identity_agency 不支持 tags 参数; 角色挂载通过 project_role 块.
#
# 动作名与资源格式遵循 OBS 细粒度策略规范:
#   动作: obs:object:GetObject / obs:object:PutObject / obs:bucket:ListBucket
#   资源: obs:*:*:object:<桶名>/*  (对象) ; obs:*:*:bucket:<桶名>  (桶)
# ============================================================================

# ----------------------------------------------------------------------------
# 自定义角色: pre-migration (仅 metadata 桶 读写)
# ----------------------------------------------------------------------------
resource "huaweicloud_identity_role" "pre_migration" {
  name        = "${var.name_prefix}-pre-migration-role"
  description = "Iceberg 迁移前捕获: 读写 metadata 桶快照与 manifest."
  type        = "AX"
  policy = jsonencode({
    Version   = "1.1"
    Statement = [{
      Effect = "Allow"
      Action = [
        "obs:object:GetObject",
        "obs:object:PutObject",
        "obs:bucket:ListBucket",
      ]
      Resource = [
        "obs:*:*:object:${huaweicloud_obs_bucket.metadata.bucket}/*",
        "obs:*:*:bucket:${huaweicloud_obs_bucket.metadata.bucket}",
      ]
    }]
  })
}

resource "huaweicloud_identity_agency" "pre_migration" {
  name                   = "${var.name_prefix}-pre-migration"
  description            = "FunctionGraph 执行 pre-migration 函数的委托."
  delegated_service_name = "op_svc_cff"
  project_role {
    project = var.region
    roles   = [huaweicloud_identity_role.pre_migration.name]
  }
  # 临时加上系统策略 OBS OperateAccess (domain 级, 立即生效).
  # 原因: FGS agency 颁发的临时凭证对自定义 role 的 custom policy
  # 不收敛 (10 分钟缓存 / 跨服务 STS 不评估), 导致 ListObjects 403.
  # 后续验证业务闭环后可移除或换成更窄的系统策略.
  domain_roles = ["OBS OperateAccess"]
}

# ----------------------------------------------------------------------------
# 自定义角色: post-migration (仅 metadata 桶 读写)
# ----------------------------------------------------------------------------
resource "huaweicloud_identity_role" "post_migration" {
  name        = "${var.name_prefix}-post-migration-role"
  description = "Iceberg 迁移后恢复: 读写 metadata 桶 manifest."
  type        = "AX"
  policy = jsonencode({
    Version   = "1.1"
    Statement = [{
      Effect = "Allow"
      Action = [
        "obs:object:GetObject",
        "obs:object:PutObject",
        "obs:bucket:ListBucket",
      ]
      Resource = [
        "obs:*:*:object:${huaweicloud_obs_bucket.metadata.bucket}/*",
        "obs:*:*:bucket:${huaweicloud_obs_bucket.metadata.bucket}",
      ]
    }]
  })
}

resource "huaweicloud_identity_agency" "post_migration" {
  name                   = "${var.name_prefix}-post-migration"
  description            = "FunctionGraph 执行 post-migration 函数的委托."
  delegated_service_name = "op_svc_cff"
  project_role {
    project = var.region
    roles   = [huaweicloud_identity_role.post_migration.name]
  }
  domain_roles = ["OBS OperateAccess"]
}

# ----------------------------------------------------------------------------
# 自定义角色: rollback (metadata 桶 读写 + data 桶 只读列举)
# ----------------------------------------------------------------------------
resource "huaweicloud_identity_role" "rollback" {
  name        = "${var.name_prefix}-rollback-role"
  description = "Iceberg 紧急回滚: 读写 metadata 桶 manifest, 只读列举 data 桶 metadata.json."
  type        = "AX"
  policy = jsonencode({
    Version = "1.1"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "obs:object:GetObject",
          "obs:object:PutObject",
          "obs:bucket:ListBucket",
        ]
        Resource = [
          "obs:*:*:object:${huaweicloud_obs_bucket.metadata.bucket}/*",
          "obs:*:*:bucket:${huaweicloud_obs_bucket.metadata.bucket}",
        ]
      },
      {
        Effect = "Allow"
        Action = [
          "obs:bucket:ListBucket",
        ]
        Resource = [
          "obs:*:*:bucket:${huaweicloud_obs_bucket.data.bucket}",
        ]
      },
    ]
  })
}

resource "huaweicloud_identity_agency" "rollback" {
  name                   = "${var.name_prefix}-rollback"
  description            = "FunctionGraph 执行 rollback 函数的委托."
  delegated_service_name = "op_svc_cff"
  project_role {
    project = var.region
    roles   = [huaweicloud_identity_role.rollback.name]
  }
  domain_roles = ["OBS OperateAccess"]
}
