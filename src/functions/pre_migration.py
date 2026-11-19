"""
函数: 迁移前捕获指定数据库下所有 Iceberg 表的 metadata_location 快照.

事件格式 (任选其一):
  {"database": "my_database"}              # 单个数据库
  {"databases": ["db1", "db2", "db3"]}     # 多个数据库

环境变量 (通过 FunctionGraph user_data 注入):
  METADATA_BUCKET - manifest 与快照所在的 OBS 桶名
  DATA_BUCKET     - Iceberg 数据桶名
  OUTPUT_PREFIX   - (可选) 快照 key 前缀, 默认为 'iceberg-metadata'

输出 (每个数据库一份):
  obs://<METADATA_BUCKET>/<OUTPUT_PREFIX>/<database>/<timestamp>.json

IAM 委托 (由 Terraform 创建) 所需权限:
  - obs:object:GetObject / PutObject / ListBucket
  - 对 metadata 桶的快照前缀与 manifests 前缀
  - 对 data 桶 (只读) 用于初次创建 manifest 时列出表前缀
"""

import json
import logging
import os
import uuid
from datetime import datetime, timezone

from obs import ObsClient

logger = logging.getLogger()
logger.setLevel(logging.INFO)


# ----------------------------------------------------------------------------
# OBS 客户端初始化
# ----------------------------------------------------------------------------

def _make_obs_client(context=None) -> ObsClient:
    """构造 ObsClient, 按以下顺序解析凭证:

    1. 用户在 user_data 注入的永久 AK/SK (HW_AK / HW_SK) —— 业务首选路径.
       永久凭证以桶 owner 身份调用 OBS, 绕过 OBS 私有桶在 STS 临时凭证下的
       ACL owner 校验差异 (HST.* 临时凭证即使 IAM user_id 与 owner 相同,
       私有桶仍 AccessDenied).
    2. FunctionGraph IAM 委托注入的临时凭证 (context.getSecurityAccessKey /
       getSecuritySecretKey / getSecurityToken) —— fallback, 业务后续迁移
       到最小权限后启用. 当前已知缺陷: 私有桶 owner 校验差异未兼容.
    3. 标准 HUAWEICLOUD_* 环境变量 (本地调试).

    注: getAccessKey()/getSecretKey() 已被 FunctionGraph 废弃, 必须使用
    getSecurityAccessKey()/getSecuritySecretKey()/getSecurityToken().
    仅当三条路径都拿不到凭证时才抛错.

    关键: ObsClient 必须用 signature='v4' + 显式 region + 关闭签名协商,
    才能保证 V4 签名 (让 x-obs-security-token 进 canonical headers 参与
    HMAC-SHA256 计算), 适配 esdk-obs-python 3.26 的 V2 默认协商陷阱.
    """
    ak = sk = token = None
    ak_source = None

    # 路径 1: user_data 注入的永久凭证 (业务首选, owner 身份绕过私有桶 ACL 差异)
    env_ak = os.environ.get("HW_AK")
    env_sk = os.environ.get("HW_SK")
    if env_ak and env_sk:
        ak, sk, token = env_ak, env_sk, None
        ak_source = "user_data_env"

    # 路径 2: context 临时凭证 (fallback)
    if (not ak or not sk) and context is not None:
        try:
            ctx_ak = context.getSecurityAccessKey()
            ctx_sk = context.getSecuritySecretKey()
            ctx_token = context.getSecurityToken()
        except Exception:  # noqa: BLE001 - context 不可用时静默, 走 fallback
            ctx_ak = ctx_sk = ctx_token = None
        if ctx_ak and ctx_sk:
            ak, sk, token = ctx_ak, ctx_sk, ctx_token
            ak_source = "context_agency"

    # 路径 3: 标准 HUAWEICLOUD_* 环境变量 (本地调试)
    if not ak or not sk:
        ak = (
            os.environ.get("HUAWEICLOUD_ACCESS_KEY_ID")
            or os.environ.get("HW_ACCESS_KEY")
        )
        sk = (
            os.environ.get("HUAWEICLOUD_SECRET_ACCESS_KEY")
            or os.environ.get("HW_SECRET_KEY")
        )
        token = os.environ.get("HUAWEICLOUD_SECURITY_TOKEN")
        if ak and sk:
            ak_source = "env_standard"

    endpoint = os.environ.get(
        "OBS_ENDPOINT", "https://obs.cn-north-4.myhuaweicloud.com"
    )
    if not ak or not sk:
        raise ValueError(
            "Missing credentials: 未找到 HW_AK/HW_SK (user_data 注入) 或 context "
            "agency 临时凭证, 也未配置 HUAWEICLOUD_* 环境变量"
        )
    print(f"[cred] source={ak_source} ak_head={ak[:10] if ak else None} "
          f"sk_present={bool(sk)} token={'present' if token else 'none'}")
    return ObsClient(
        access_key_id=ak,
        secret_access_key=sk,
        security_token=token,
        server=endpoint,
        signature="v4",                  # 强制 V4 签名 (临时凭证路径下让 token 参与签名)
        region=os.environ.get("REGION", "cn-north-4"),
        is_signature_negotiation=False,   # 关闭协商, 避免被切到 V2
    )


def _trace_id(event: dict) -> str:
    """从 event 取 trace_id, 缺失则生成 UUID4."""
    return event.get("trace_id") or str(uuid.uuid4())


# ----------------------------------------------------------------------------
# 业务逻辑
# ----------------------------------------------------------------------------

def _list_manifest_keys(obs: ObsClient, bucket: str, db: str) -> list:
    """列出指定数据库下的所有 manifest.json key."""
    prefix = f"manifests/{db}/"
    keys = []
    marker = ""
    while True:
        resp = obs.listObjects(bucket, prefix=prefix, marker=marker)
        if resp.status >= 300:
            raise RuntimeError(
                f"ListObjects failed: status={resp.status} errorCode={resp.errorCode}"
            )
        for c in resp.body.contents or []:
            if c.key.endswith(".json"):
                keys.append(c.key)
        if not resp.body.is_truncated:
            break
        marker = resp.body.next_marker or ""
    return keys


def _read_manifest(obs: ObsClient, bucket: str, key: str) -> dict:
    """读单个 manifest.json 文件."""
    resp = obs.getObject(bucket, key, loadStreamInMemory=True)
    if resp.status >= 300:
        raise RuntimeError(
            f"GetObject {key} failed: status={resp.status} errorCode={resp.errorCode}"
        )
    buf = resp.body.buffer if hasattr(resp.body, "buffer") else resp.body
    if isinstance(buf, (bytes, bytearray)):
        raw = bytes(buf)
    else:
        raw = buf.read()
    return json.loads(raw.decode("utf-8"))


def _list_iceberg_tables(obs: ObsClient, db: str, metadata_bucket: str) -> list:
    """从 manifest 列出数据库下所有 Iceberg 表."""
    keys = _list_manifest_keys(obs, metadata_bucket, db)
    tables = []
    for key in keys:
        m = _read_manifest(obs, metadata_bucket, key)
        if m.get("format", "").lower() != "iceberg":
            continue
        tables.append(
            {
                "name": m["table"],
                "metadata_location": m.get("metadata_location"),
            }
        )
    return tables


def _write_snapshot(
    obs: ObsClient,
    metadata_bucket: str,
    output_prefix: str,
    database: str,
    tables: list,
    trace_id: str,
) -> str:
    """写快照文件, 返回完整 obs URI."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    key = f"{output_prefix}/{database}/{ts}.json"
    payload = {
        "database": database,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "trace_id": trace_id,
        "table_count": len(tables),
        "tables": tables,
    }
    body = json.dumps(payload, indent=2, ensure_ascii=False).encode("utf-8")
    resp = obs.putContent(metadata_bucket, key, body)
    if resp.status >= 300:
        raise RuntimeError(
            f"PutObject {key} failed: status={resp.status} errorCode={resp.errorCode}"
        )
    return f"obs://{metadata_bucket}/{key}"


def _capture_database(
    obs: ObsClient,
    database: str,
    metadata_bucket: str,
    output_prefix: str,
    trace_id: str,
) -> dict:
    """对单个数据库执行捕获, 返回结果字典."""
    logger.info(f"[trace={trace_id}] Listing Iceberg tables in '{database}'")
    tables = _list_iceberg_tables(obs, database, metadata_bucket)
    logger.info(f"[trace={trace_id}] Found {len(tables)} Iceberg tables in '{database}'")
    output_uri = _write_snapshot(
        obs, metadata_bucket, output_prefix, database, tables, trace_id
    )
    logger.info(f"[trace={trace_id}] Snapshot -> {output_uri}")
    return {
        "database": database,
        "table_count": len(tables),
        "output_uri": output_uri,
        "status": "ok",
    }


def handler(event, context):
    """FunctionGraph 入口."""
    trace_id = _trace_id(event)

    if "databases" in event:
        databases = event["databases"]
        if not isinstance(databases, list) or not databases:
            raise ValueError("'databases' must be a non-empty list")
    elif "database" in event:
        databases = [event["database"]]
    else:
        raise ValueError("Missing required input: provide 'database' or 'databases'")

    metadata_bucket = os.environ.get("METADATA_BUCKET")
    if not metadata_bucket:
        raise ValueError("Missing required env var: METADATA_BUCKET")
    output_prefix = os.environ.get("OUTPUT_PREFIX", "iceberg-metadata").strip("/")

    obs = _make_obs_client(context)

    results = []
    for db in databases:
        try:
            results.append(
                _capture_database(obs, db, metadata_bucket, output_prefix, trace_id)
            )
        except Exception as e:
            logger.exception(f"[trace={trace_id}] Failed to capture '{db}'")
            results.append(
                {
                    "database": db,
                    "status": "failed",
                    "error": str(e),
                }
            )

    summary = {
        "ok": sum(1 for r in results if r.get("status") == "ok"),
        "failed": sum(1 for r in results if r.get("status") == "failed"),
    }

    return {
        "statusCode": 200,
        "trace_id": trace_id,
        "database_count": len(databases),
        "summary": summary,
        "results": results,
    }
