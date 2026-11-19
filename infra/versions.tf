# ============================================================================
# Terraform 与 provider 版本约束
# ----------------------------------------------------------------------------
# 说明: 本仓库使用 Terraform 1.5+ 与 huaweicloud Provider 1.97.x.
#       函数代码 zip 包由部署前脚本 infra/scripts/build_zips.py 生成,
#       仅依赖 huaweicloud Provider, 不引入其它 provider 依赖.
# ============================================================================

terraform {
  required_version = ">= 1.5.0"

  required_providers {
    huaweicloud = {
      source  = "huaweicloud/huaweicloud"
      version = "~> 1.97"
    }
  }
}

provider "huaweicloud" {
  region = var.region
}
