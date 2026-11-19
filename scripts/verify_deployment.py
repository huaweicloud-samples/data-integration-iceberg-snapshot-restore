#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""部署业务验证: 捕获 -> 恢复 -> 回滚 三阶段端到端验证.

凭证从环境变量读取 (HW_ACCESS_KEY / HW_SECRET_KEY / HW_REGION_NAME).
验证目标工程: data-integration-iceberg-snapshot-restore (iceberg-mig-* 桶).

流程:
  A. 准备测试数据 (OBS): 2 张表的 metadata.json 多版本 + manifest 指针(指向 v5)
  B. 调用 pre_migration  -> 应写出快照, 捕获 v5 指针
  C. 篡改 manifest 指向 v1(模拟迁移破坏) -> 调用 post_migration -> 应恢复为 v5
  D. 调用 rollback -> 应重置为第 4 新版本(偏移 3 => v2)
"""

import os
import sys
import json
import time
import hashlib
import hmac
import datetime
import urllib.parse

import requests
from obs import ObsClient

# ---------------------------------------------------------------------------
# 配置 (从环境变量 + 本部署常量)
# ---------------------------------------------------------------------------
REGION = os.environ.get("HW_REGION_NAME", "cn-north-4")
PROJECT_ID = "cc2cb3d01b0c46798d77c4cd8ecf6b7e"
AK = os.environ.get("HW_ACCESS_KEY")
SK = os.environ.get("HW_SECRET_KEY")
if not AK or not SK:
    raise SystemExit("缺少 HW_ACCESS_KEY / HW_SECRET_KEY 环境变量")

OBS_ENDPOINT = "https://obs.%s.myhuaweicloud.com" % REGION
# 注意: 正确的 FGS 同步调用端点为 x-functiongraph.{region}.myhuaweicloud.com
# (本沙箱代理仅放行 iam/obs 等端点, 拦截 x-functiongraph, 故沙箱内无法 API 调用;
#  用户本地 PC 无此代理限制, 可直接运行本脚本. 若你的环境也走代理, 请确认代理放行该主机.)
FGS_ENDPOINT = "https://x-functiongraph.%s.myhuaweicloud.com" % REGION

META_BUCKET = "iceberg-mig-md-7700d1"
DATA_BUCKET = "iceberg-mig-dt-7700d1"
OUTPUT_PREFIX = "iceberg-metadata"
DB = "verification_db"
TABLES = ["orders", "events"]
N_VERSIONS = 5  # rollback 偏移=3 => 需要 >=4 个版本

URN_PRE = ("urn:fss:%s:%s:function:default:iceberg-migration-pre-migration:latest"
           % (REGION, PROJECT_ID))
URN_POST = ("urn:fss:%s:%s:function:default:iceberg-migration-post-migration:latest"
            % (REGION, PROJECT_ID))
URN_ROLLBACK = ("urn:fss:%s:%s:function:default:iceberg-migration-rollback:latest"
                % (REGION, PROJECT_ID))


# ---------------------------------------------------------------------------
# 华为云签名 (SDK-HMAC-SHA256) + FunctionGraph 同步调用
# ---------------------------------------------------------------------------

def _hmac_sha256(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sign_request(method, path, body, service="functiongraph"):
    now = datetime.datetime.utcnow()
    amzdate = now.strftime("%Y%m%dT%H%M%SZ")
    datestamp = now.strftime("%Y%m%d")
    payload_hash = _sha256_hex(body)
    host = urllib.parse.urlparse(FGS_ENDPOINT).netloc
    headers = {
        "host": host,
        "content-type": "application/json; charset=utf-8",
        "x-sdk-date": amzdate,
        "x-project-id": PROJECT_ID,
    }
    sorted_keys = sorted(headers.keys())
    canonical_headers = "".join(
        "%s:%s\n" % (k, headers[k].strip()) for k in sorted_keys
    )
    signed_headers = ";".join(sorted_keys)

    segments = path.split("/")
    canonical_uri = "/" + "/".join(
        urllib.parse.quote(s, safe="") for s in segments[1:]
    )
    canonical_request = "\n".join(
        [
            method.upper(),
            canonical_uri,
            "",  # 无 query
            canonical_headers,
            signed_headers,
            payload_hash,
        ]
    )

    credential_scope = "%s/%s/%s/sdk_request" % (datestamp, REGION, service)
    string_to_sign = "\n".join(
        [
            "SDK-HMAC-SHA256",
            amzdate,
            credential_scope,
            _sha256_hex(canonical_request.encode("utf-8")),
        ]
    )
    k_date = _hmac_sha256(SK.encode("utf-8"), datestamp)
    k_region = _hmac_sha256(k_date, REGION)
    k_service = _hmac_sha256(k_region, service)
    k_signing = _hmac_sha256(k_service, "sdk_request")
    signature = hmac.new(
        k_signing, string_to_sign.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    headers["Authorization"] = (
        "SDK-HMAC-SHA256 Access=%s, SignedHeaders=%s, Signature=%s"
        % (AK, signed_headers, signature)
    )
    return headers


def invoke_fgs(urn: str, event: dict) -> dict:
    path = "/v2/%s/functions/%s/invocations" % (
        PROJECT_ID,
        urllib.parse.quote(urn, safe=""),
    )
    body = json.dumps(event, ensure_ascii=False).encode("utf-8")
    headers = _sign_request("POST", path, body)
    headers["X-Function-Execution-Type"] = "sync"
    url = FGS_ENDPOINT + path
    resp = requests.post(url, headers=headers, data=body, timeout=180)
    print("    HTTP %s (x-fgs-request-id: %s)"
          % (resp.status_code,
             resp.headers.get("x-fgs-request-id", "-")))
    if resp.status_code >= 300:
        raise RuntimeError("FGS 调用失败 %s: %s" % (resp.status_code, resp.text[:500]))
    try:
        return resp.json()
    except ValueError:
        return {"raw": resp.text}


# ---------------------------------------------------------------------------
# OBS 辅助 (用 AK/SK 直连, 仅用于准备测试数据 + 读取结果)
# ---------------------------------------------------------------------------

def make_obs() -> ObsClient:
    return ObsClient(
        access_key_id=AK,
        secret_access_key=SK,
        server=OBS_ENDPOINT,
    )


def obs_put_json(obs, bucket, key, obj) -> int:
    body = json.dumps(obj, indent=2, ensure_ascii=False).encode("utf-8")
    resp = obs.putContent(bucket, key, body)
    return resp.status


def obs_get_json(obs, bucket, key):
    resp = obs.getObject(bucket, key, loadStreamInMemory=True)
    if resp.status >= 300:
        raise RuntimeError("GetObject %s/%s 失败 status=%s" % (bucket, key, resp.status))
    return json.loads(resp.body.buffer.read().decode("utf-8"))


def obs_list(obs, bucket, prefix):
    keys = []
    marker = ""
    while True:
        resp = obs.listObjects(bucket, prefix=prefix, marker=marker)
        if resp.status >= 300:
            raise RuntimeError("ListObjects 失败 status=%s" % resp.status)
        for c in resp.body.contents or []:
            keys.append(c.key)
        if not resp.body.is_truncated:
            break
        marker = resp.body.next_marker or ""
    return keys


# ---------------------------------------------------------------------------
# 测试数据准备
# ---------------------------------------------------------------------------

def setup_test_data(obs):
    print("[setup] 准备测试数据 ...")
    for table in TABLES:
        for v in range(1, N_VERSIONS + 1):
            key = "%s/%s/metadata/v%d.metadata.json" % (DB, table, v)
            # 写一份最小 Iceberg metadata 占位内容
            content = {
                "format-version": 2,
                "table-uuid": "%s-%s-uuid" % (DB, table),
                "version": v,
                "snapshots": [],
            }
            st = obs_put_json(obs, DATA_BUCKET, key, content)
            if st >= 300:
                raise RuntimeError("写 data 桶失败 %s status=%s" % (key, st))
            time.sleep(1.2)  # 保证 LastModified 单调递增, 使回滚排序确定

        # manifest 指向最新版本 v5
        manifest_key = "manifests/%s/%s.json" % (DB, table)
        manifest = {
            "database": DB,
            "table": table,
            "format": "iceberg",
            "metadata_location": "obs://%s/%s/%s/metadata/v%d.metadata.json"
            % (DATA_BUCKET, DB, table, N_VERSIONS),
            "updated_at": datetime.datetime.utcnow().isoformat(),
        }
        st = obs_put_json(obs, META_BUCKET, manifest_key, manifest)
        if st >= 300:
            raise RuntimeError("写 manifest 失败 %s status=%s" % (manifest_key, st))
    print("[setup] 测试数据就绪: 2 表 x %d 版本, manifest -> v%d"
          % (N_VERSIONS, N_VERSIONS))


# ---------------------------------------------------------------------------
# 验证主流程
# ---------------------------------------------------------------------------

def main():
    obs = make_obs()

    print("\n=== A. 准备测试数据 ===")
    setup_test_data(obs)

    print("\n=== B. pre_migration(捕获) ===")
    resp_pre = invoke_fgs(URN_PRE, {"database": DB})
    print("    返回: " + json.dumps(resp_pre, ensure_ascii=False))
    assert resp_pre.get("statusCode") == 200, "pre_migration 返回非 200"
    assert resp_pre["summary"]["ok"] == resp_pre["database_count"], \
        "pre_migration 数据库级未全部成功: %s" % json.dumps(resp_pre["summary"], ensure_ascii=False)
    snap_key = resp_pre["results"][0]["output_uri"].replace(
        "obs://%s/" % META_BUCKET, "")
    print("    快照 key: " + snap_key)
    snap = obs_get_json(obs, META_BUCKET, snap_key)
    assert snap["table_count"] == len(TABLES), "快照表数不符"
    for t in snap["tables"]:
        assert t["metadata_location"].endswith("v%d.metadata.json" % N_VERSIONS), \
            "快照应捕获 v%d 指针, 实为 %s" % (N_VERSIONS, t["metadata_location"])
    print("    PASS: 快照捕获 v%d 指针, 表数=%d" % (N_VERSIONS, snap["table_count"]))

    print("\n=== C. post_migration(恢复) - 先模拟迁移破坏 manifest ===")
    for table in TABLES:
        mkey = "manifests/%s/%s.json" % (DB, table)
        m = obs_get_json(obs, META_BUCKET, mkey)
        m["metadata_location"] = "obs://%s/%s/%s/metadata/v1.metadata.json" % (
            DATA_BUCKET, DB, table)
        obs_put_json(obs, META_BUCKET, mkey, m)
        print("    破坏 %s -> v1" % table)
    resp_post = invoke_fgs(URN_POST, {"database": DB, "input_key": snap_key})
    print("    返回: " + json.dumps(resp_post, ensure_ascii=False))
    assert resp_post.get("statusCode") == 200
    assert resp_post["summary"].get("restored", 0) == len(TABLES), \
        "post_migration 应全部 restored"
    for table in TABLES:
        m = obs_get_json(obs, META_BUCKET, "manifests/%s/%s.json" % (DB, table))
        assert m["metadata_location"].endswith("v%d.metadata.json" % N_VERSIONS), \
            "post_migration 未恢复为 v%d: %s" % (N_VERSIONS, m["metadata_location"])
    print("    PASS: 全部 %d 张表 restored 回 v%d" % (len(TABLES), N_VERSIONS))

    print("\n=== D. rollback(回滚) - 重置到第 4 新版本(offset=3 => v2) ===")
    resp_rb = invoke_fgs(URN_ROLLBACK, {"database": DB})
    print("    返回: " + json.dumps(resp_rb, ensure_ascii=False))
    assert resp_rb.get("statusCode") == 200
    assert resp_rb["summary"].get("rolled_back", 0) == len(TABLES), \
        "rollback 应全部 rolled_back"
    for table in TABLES:
        m = obs_get_json(obs, META_BUCKET, "manifests/%s/%s.json" % (DB, table))
        assert m["metadata_location"].endswith("v2.metadata.json"), \
            "rollback 应重置为 v2, 实为 %s" % m["metadata_location"]
    print("    PASS: 全部 %d 张表 rolled_back 到 v2" % len(TABLES))

    print("\n=== 结果汇总 ===")
    print("pre_migration  : %s" % json.dumps(resp_pre["summary"], ensure_ascii=False))
    print("post_migration : %s" % json.dumps(resp_post["summary"], ensure_ascii=False))
    print("rollback       : %s" % json.dumps(resp_rb["summary"], ensure_ascii=False))
    print("\nALL PASS: 三阶段端到端业务验证通过.")


if __name__ == "__main__":
    try:
        main()
    except AssertionError as e:
        print("\nFAIL (断言): %s" % e)
        sys.exit(1)
    except Exception as e:  # noqa
        print("\nERROR: %s" % e)
        sys.exit(2)
