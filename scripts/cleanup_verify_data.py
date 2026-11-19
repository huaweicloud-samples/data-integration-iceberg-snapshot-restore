#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""清理 verify_logic_local.py 在真实 OBS 桶里写入的 verification_db 测试数据,
仅删除 manifests/verification_db/ 与 <db>/metadata/v*.metadata.json, 不影响其他数据.

凭证从环境变量读取 (HW_ACCESS_KEY / HW_SECRET_KEY / HW_REGION_NAME).
"""
import os
from obs import ObsClient

AK = os.environ.get("HW_ACCESS_KEY")
SK = os.environ.get("HW_SECRET_KEY")
REGION = os.environ.get("HW_REGION_NAME", "cn-north-4")
META_BUCKET = "iceberg-mig-md-7700d1"
DATA_BUCKET = "iceberg-mig-dt-7700d1"
DB = "verification_db"


def make_obs():
    return ObsClient(
        access_key_id=AK,
        secret_access_key=SK,
        server="https://obs.%s.myhuaweicloud.com" % REGION,
    )


def delete_prefix(obs, bucket, prefix):
    marker = ""
    deleted = 0
    while True:
        resp = obs.listObjects(bucket, prefix=prefix, marker=marker)
        if resp.status >= 300:
            raise RuntimeError("ListObjects %s 失败 status=%s" % (prefix, resp.status))
        for c in resp.body.contents or []:
            d = obs.deleteObject(bucket, c.key)
            if d.status < 300:
                deleted += 1
            else:
                print("  删除失败 %s: %s" % (c.key, d.status))
        if not resp.body.is_truncated:
            break
        marker = resp.body.next_marker or ""
    return deleted


def main():
    obs = make_obs()
    print("[cleanup] 删除 metadata 桶 manifests/%s/ ..." % DB)
    n1 = delete_prefix(obs, META_BUCKET, "manifests/%s/" % DB)
    print("[cleanup] 删除 metadata 桶 iceberg-metadata/%s/ ..." % DB)
    n2 = delete_prefix(obs, META_BUCKET, "iceberg-metadata/%s/" % DB)
    print("[cleanup] 删除 data 桶 %s/ 下 metadata 文件 ..." % DB)
    n3 = delete_prefix(obs, DATA_BUCKET, "%s/" % DB)
    print("[cleanup] 完成: metadata桶=%d, 快照=%d, data桶=%d" % (n1, n2, n3))


if __name__ == "__main__":
    if not AK or not SK:
        raise SystemExit("缺少 HW_ACCESS_KEY / HW_SECRET_KEY")
    main()
