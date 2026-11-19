# ============================================================================
# LTS 日志组 + 日志流: 为 3 个 FunctionGraph 函数提供日志归集
# ----------------------------------------------------------------------------
# FunctionGraph 日志默认走 LTS, 一个函数对应一条日志流, 同一日志组下统一管理.
# ============================================================================

resource "huaweicloud_lts_group" "fgs" {
  group_name  = "${var.name_prefix}-lts-group"
  ttl_in_days = var.log_retention_days

  tags = var.tags
}

resource "huaweicloud_lts_stream" "pre_migration" {
  group_id    = huaweicloud_lts_group.fgs.id
  stream_name = "${var.name_prefix}-pre-migration"
  ttl_in_days = var.log_retention_days

  tags = var.tags
}

resource "huaweicloud_lts_stream" "post_migration" {
  group_id    = huaweicloud_lts_group.fgs.id
  stream_name = "${var.name_prefix}-post-migration"
  ttl_in_days = var.log_retention_days

  tags = var.tags
}

resource "huaweicloud_lts_stream" "rollback" {
  group_id    = huaweicloud_lts_group.fgs.id
  stream_name = "${var.name_prefix}-rollback"
  ttl_in_days = var.log_retention_days

  tags = var.tags
}

# ============================================================================
# 函数代码 zip 包: 由部署前脚本 infra/scripts/build_zips.py 预生成到 .build/
# ----------------------------------------------------------------------------
# 不依赖 null_resource / null provider (执行环境 registry 受限);
# apply 前需先在 infra/ 目录执行:
#   python scripts/build_zips.py ../src/functions .build
# 生成 .build/<name>.zip 后, 下面的 func_code = filebase64(...) 才能读到内容.
# ============================================================================

# ============================================================================
# FunctionGraph 函数: pre-migration
# ----------------------------------------------------------------------------
# 捕获指定数据库下所有 Iceberg 表的 metadata_location 写入 metadata 桶的快照;
# 同时为每张表写一份 manifest.json (按库前缀归集), post-migration 据此回填.
# ============================================================================

resource "huaweicloud_fgs_function" "pre_migration" {
  name        = "${var.name_prefix}-pre-migration"
  app         = "default"
  handler     = "pre_migration.handler"
  memory_size = 512
  timeout     = 900
  runtime     = "Python3.10"
  agency      = huaweicloud_identity_agency.pre_migration.name

  code_type     = "zip"
  code_filename = "pre_migration.zip"
  func_code     = filebase64("${path.module}/.build/pre_migration.zip")

  functiongraph_version = "v2"

  user_data = jsonencode({
    METADATA_BUCKET = huaweicloud_obs_bucket.metadata.bucket
    DATA_BUCKET     = huaweicloud_obs_bucket.data.bucket
    OUTPUT_PREFIX   = var.output_prefix
    REGION          = var.region
    # 通过 user_data 注入桶 owner 的永久凭证 (当前 admin AK/SK). 业务函数运行时
    # 优先使用此永久凭证绕过 OBS 私有桶 ACL 在 STS 临时凭证下的 owner 校验差异
    # (STS 颁发的 HST.* 临时凭证即使 IAM user_id 与 owner 一致, OBS 私有桶仍
    # 拒绝访问; 永久凭证为 owner 身份, 直接通过 ACL 校验).
    # 仅注入业务所需的桶名/区域常量; AK/SK 由部署者在 apply 前手动 export 到
    # 函数环境变量 (通过 user_data) —— Terraform state 与 FGS 控制台可见.
    HW_AK     = var.function_ak
    HW_SK     = var.function_sk
    AK_SOURCE = "user_data"
  })

  enable_lts_log = true
  log_group_id    = huaweicloud_lts_group.fgs.id
  log_group_name  = huaweicloud_lts_group.fgs.group_name
  log_stream_id   = huaweicloud_lts_stream.pre_migration.id
  log_stream_name = huaweicloud_lts_stream.pre_migration.stream_name

  tags = var.tags

  depends_on = [
    huaweicloud_identity_agency.pre_migration,
  ]
}

# ============================================================================
# FunctionGraph 函数: post-migration
# ----------------------------------------------------------------------------
# 从 metadata 桶读 pre-migration 写下的快照, 按 manifest 逐表回填 metadata_location;
# 每张表的 outcome = restored / unchanged / skipped / failed.
# ============================================================================

resource "huaweicloud_fgs_function" "post_migration" {
  name        = "${var.name_prefix}-post-migration"
  app         = "default"
  handler     = "post_migration.handler"
  memory_size = 512
  timeout     = 900
  runtime     = "Python3.10"
  agency      = huaweicloud_identity_agency.post_migration.name

  code_type     = "zip"
  code_filename = "post_migration.zip"
  func_code     = filebase64("${path.module}/.build/post_migration.zip")

  functiongraph_version = "v2"

  user_data = jsonencode({
    METADATA_BUCKET = huaweicloud_obs_bucket.metadata.bucket
    REGION          = var.region
    HW_AK           = var.function_ak
    HW_SK           = var.function_sk
    AK_SOURCE       = "user_data"
  })

  enable_lts_log = true
  log_group_id    = huaweicloud_lts_group.fgs.id
  log_group_name  = huaweicloud_lts_group.fgs.group_name
  log_stream_id   = huaweicloud_lts_stream.post_migration.id
  log_stream_name = huaweicloud_lts_stream.post_migration.stream_name

  tags = var.tags

  depends_on = [
    huaweicloud_identity_agency.post_migration,
  ]
}

# ============================================================================
# FunctionGraph 函数: rollback (紧急使用)
# ----------------------------------------------------------------------------
# 列出 data 桶下指定数据库表的 metadata/*.metadata.json, 按 LastModified 排序,
# 把表指针重置到 Nth-latest (offset=3) 的 metadata.json; 同步更新 manifest.
# ============================================================================

resource "huaweicloud_fgs_function" "rollback" {
  name        = "${var.name_prefix}-rollback"
  app         = "default"
  handler     = "rollback_metadata.handler"
  memory_size = 512
  timeout     = 900
  runtime     = "Python3.10"
  agency      = huaweicloud_identity_agency.rollback.name

  code_type     = "zip"
  code_filename = "rollback_metadata.zip"
  func_code     = filebase64("${path.module}/.build/rollback_metadata.zip")

  functiongraph_version = "v2"

  user_data = jsonencode({
    METADATA_BUCKET = huaweicloud_obs_bucket.metadata.bucket
    DATA_BUCKET     = huaweicloud_obs_bucket.data.bucket
    REGION          = var.region
    HW_AK           = var.function_ak
    HW_SK           = var.function_sk
    AK_SOURCE       = "user_data"
  })

  enable_lts_log = true
  log_group_id    = huaweicloud_lts_group.fgs.id
  log_group_name  = huaweicloud_lts_group.fgs.group_name
  log_stream_id   = huaweicloud_lts_stream.rollback.id
  log_stream_name = huaweicloud_lts_stream.rollback.stream_name

  tags = var.tags

  depends_on = [
    huaweicloud_identity_agency.rollback,
  ]
}
