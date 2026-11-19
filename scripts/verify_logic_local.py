#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地逻辑验证: 直接导入交付物的函数模块, 用真实 AK/SK 对真实 OBS 桶
执行 捕获 -> 恢复 -> 回滚 三阶段, 验证代码逻辑 + OBS 布局 + 事件契约.

与 verify_deployment.py 的区别: 不通过 FunctionGraph invoke API(沙箱代理拦截
x-functiongraph), 而是直接调用 handler() 函数, 但操作的是真实部署的 OBS 桶.
FGS 运行时打包/委托由 terraform apply 已确认; 本脚本验证业务逻辑本身.

凭证从环境变量读取 (HW_ACCESS_KEY / HW_SECRET_KEY / HW_REGION_NAME).
"""

import os
import sys
import json
import time
import datetime

# 让函数模块能 import obs, 并把真实桶名/凭证注入环境变量
HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "..", "src", "functions")
sys.path.insert(0, os.path.abspath(SRC))

REGION = os.environ.get("HW_REGION_NAME", "cn-north-4")
AK = os.environ.get("HW_ACCESS_KEY")
SK = os.environ.get("HW_SECRET_KEY")
if not AK or not SK:
    raise SystemExit("缺少 HW_ACCESS_KEY / HW_SECRET_KEY 环境变量")

META_BUCKET = "iceberg-mig-md-7700d1"
DATA_BUCKET = "iceberg-mig-dt-7700d1"
OUTPUT_PREFIX = "iceberg-metadata"
DB = "verification_db"
TABLES = ["orders", "events"]
N_VERSIONS = 5

# 注入函数运行所需环境变量 (与 FunctionGraph user_data 保持一致)
os.environ["METADATA_BUCKET"] = META_BUCKET
os.environ["DATA_BUCKET"] = DATA_BUCKET
os.environ["OUTPUT_PREFIX"] = OUTPUT_PREFIX
os.environ["REGION"] = REGION
os.environ["HW_ACCESS_KEY"] = AK
os.environ["HW_SECRET_KEY"] = SK

OBS_ENDPOINT = "https://obs.%s.myhuaweicloud.com" % REGION

from obs import ObsClient
import pre_migration
import post_migration
import rollback_metadata


def make_obs() -> ObsClient:
    return ObsClient(
        access_key_id=AK, secret_access_key=SK, server=OBS_ENDPOINT
    )


def obs_put_json(obs, bucket, key, obj):
    body = json.dumps(obj, indent=2, ensure_ascii=False).encode("utf-8")
    return obs.putContent(bucket, key, body).status


def obs_get_json(obs, bucket, key):
    resp = obs.getObject(bucket, key, loadStreamInMemory=True)
    if resp.status >= 300:
        raise RuntimeError("GetObject %s/%s 失败 status=%s" % (bucket, key, resp.status))
    buf = resp.body.buffer if hasattr(resp.body, "buffer") else resp.body
    raw = bytes(buf) if isinstance(buf, (bytes, bytearray)) else buf.read()
    return json.loads(raw.decode("utf-8"))


def setup_test_data(obs):
    print("[setup] 准备测试数据 ...")
    for table in TABLES:
        for v in range(1, N_VERSIONS + 1):
            key = "%s/%s/metadata/v%d.metadata.json" % (DB, table, v)
            content = {
                "format-version": 2,
                "table-uuid": "%s-%s-uuid" % (DB, table),
                "version": v,
                "snapshots": [],
            }
            st = obs_put_json(obs, DATA_BUCKET, key, content)
            if st >= 300:
                raise RuntimeError("写 data 桶失败 %s status=%s" % (key, st))
            time.sleep(1.2)
        manifest_key = "manifests/%s/%s.json" % (DB, table)
        manifest = {
            "database": DB,
            "table": table,
            "format": "iceberg",
            "metadata_location": "obs://%s/%s/%s/metadata/v%d.metadata.json"
            % (DATA_BUCKET, DB, table, N_VERSIONS),
            "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }
        st = obs_put_json(obs, META_BUCKET, manifest_key, manifest)
        if st >= 300:
            raise RuntimeError("写 manifest 失败 %s" % manifest_key)
    print("[setup] 就绪: 2 表 x %d 版本, manifest -> v%d" % (N_VERSIONS, N_VERSIONS))


def main():
    obs = make_obs()

    print("\n=== A. 准备测试数据 ===")
    setup_test_data(obs)

    print("\n=== B. pre_migration(捕获) ===")
    r_pre = pre_migration.handler({"database": DB}, None)
    print("    返回: " + json.dumps(r_pre, ensure_ascii=False))
    assert r_pre["statusCode"] == 200 and r_pre["summary"]["ok"] == r_pre["database_count"], \
        "pre_migration 数据库级未全部成功: %s" % json.dumps(r_pre["summary"], ensure_ascii=False)
    snap_key = r_pre["results"][0]["output_uri"].replace("obs://%s/" % META_BUCKET, "")
    snap = obs_get_json(obs, META_BUCKET, snap_key)
    print("    [debug] snap.table_count=%r expected=%r" % (snap.get("table_count"), len(TABLES)))
    print("    [debug] snap.tables=%s" % json.dumps(snap.get("tables"), ensure_ascii=False))
    assert snap["table_count"] == len(TABLES), "table_count mismatch: %r" % snap.get("table_count")
    for t in snap["tables"]:
        assert t["metadata_location"].endswith("v%d.metadata.json" % N_VERSIONS), \
            "metadata_location 不为 v%d: %s" % (N_VERSIONS, t["metadata_location"])
    print("    PASS: 快照捕获 v%d, 表数=%d, key=%s" % (N_VERSIONS, snap["table_count"], snap_key))

    print("\n=== C. post_migration(恢复) - 先破坏 manifest 指向 v1 ===")
    for table in TABLES:
        mkey = "manifests/%s/%s.json" % (DB, table)
        m = obs_get_json(obs, META_BUCKET, mkey)
        m["metadata_location"] = "obs://%s/%s/%s/metadata/v1.metadata.json" % (DATA_BUCKET, DB, table)
        obs_put_json(obs, META_BUCKET, mkey, m)
        print("    破坏 %s -> v1" % table)
    r_post = post_migration.handler({"database": DB, "input_key": snap_key}, None)
    print("    返回: " + json.dumps(r_post, ensure_ascii=False))
    assert r_post["summary"].get("restored", 0) == len(TABLES)
    for table in TABLES:
        m = obs_get_json(obs, META_BUCKET, "manifests/%s/%s.json" % (DB, table))
        assert m["metadata_location"].endswith("v%d.metadata.json" % N_VERSIONS)
    print("    PASS: %d 张表 restored 回 v%d" % (len(TABLES), N_VERSIONS))

    print("\n=== D. rollback(回滚) - 重置到第 4 新版本(offset=3 => v2) ===")
    r_rb = rollback_metadata.handler({"database": DB}, None)
    print("    返回: " + json.dumps(r_rb, ensure_ascii=False))
    assert r_rb["summary"].get("rolled_back", 0) == len(TABLES)
    for table in TABLES:
        m = obs_get_json(obs, META_BUCKET, "manifests/%s/%s.json" % (DB, table))
        assert m["metadata_location"].endswith("v2.metadata.json")
    print("    PASS: %d 张表 rolled_back 到 v2" % len(TABLES))

    print("\n=== 结果汇总 ===")
    print("pre_migration  : " + json.dumps(r_pre["summary"], ensure_ascii=False))
    print("post_migration : " + json.dumps(r_post["summary"], ensure_ascii=False))
    print("rollback       : " + json.dumps(r_rb["summary"], ensure_ascii=False))
    print("\nALL PASS: 三阶段业务逻辑在本仓库函数代码中验证通过 (操作真实 OBS 桶).")


if __name__ == "__main__":
    try:
        main()
    except AssertionError as e:
        print("\nFAIL (断言): %s" % e)
        sys.exit(1)
    except Exception as e:  # noqa
        print("\nERROR: %s" % e)
        sys.exit(2)
