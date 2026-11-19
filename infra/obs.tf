# ============================================================================
# OBS 桶: metadata 桶与 data 桶
# ----------------------------------------------------------------------------
# metadata 桶: 存储 pre-migration 函数的快照 (manifest + Iceberg 表元数据索引)
# data 桶:     存储 Iceberg 表的真实数据 + metadata.json 历史快照
# 两个桶物理隔离, 符合最小特权原则 (rollback 函数只能读 data 桶, 不能写).
# ============================================================================

# metadata 桶: 接收 pre-migration 写入, 提供 post-migration 读取
resource "huaweicloud_obs_bucket" "metadata" {
  bucket        = var.metadata_bucket_name
  acl           = "private"
  force_destroy = true
  versioning    = true

  tags = var.tags
}

# data 桶: 存放 Iceberg 表数据, rollback 函数据此列历史 metadata.json
resource "huaweicloud_obs_bucket" "data" {
  bucket        = var.data_bucket_name
  acl           = "private"
  force_destroy = true
  versioning    = true

  tags = var.tags
}
