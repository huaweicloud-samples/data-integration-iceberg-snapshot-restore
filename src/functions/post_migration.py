"""
函数: 迁移后从快照恢复 Iceberg 表的 metadata_location 指针.

事件格式:
{
  "database": "my_database",
  "input_key": "iceberg-metadata/my_database/20260917T103045Z.json"
}

环境变量 (通过 FunctionGraph user_data 注入):
  METADATA_BUCKET - manifest 与快照所在的 OBS 桶名

行为:
  - 读快照文件 (由 pre-migration 函数生成)
  - 对快照中每张表, 把 manifest.json 的 metadata_location 回填到快照值
  - 每张表的 outcome = restored / unchanged / skipped / failed

IAM 委托所需权限:
  - obs:object:GetObject / PutObject / ListBucket
  - 仅限于 manifest 前缀
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
# OBS 客户端与 trace_id
# ----------------------------------------------------------------------------

def _make_obs_client(context=None) -> ObsClient:
    """构造 ObsClient, 按以下顺序解析凭证:

    1. user_data 注入的永久 AK/SK (HW_AK / HW_SK) —— 业务首选, owner 身份
       绕过 OBS 私有桶 STS 临时凭证的 ACL owner 校验差异.
    2. context IAM 委托临时凭证 —— fallback.
    3. 标准 HUAWEICLOUD_* 环境变量 (本地调试).

    关键: 强制 signature='v4' + region + is_signature_negotiation=False,
    适配 esdk-obs-python 3.26 的 V2 默认协商陷阱.
    """
    ak = sk = token = None
    ak_source = None

    # 路径 1: user_data 注入的永久凭证 (业务首选)
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
        signature="v4",
        region=os.environ.get("REGION", "cn-north-4"),
        is_signature_negotiation=False,
    )


def _trace_id(event: dict) -> str:
    return event.get("trace_id") or str(uuid.uuid4())


def _manifest_key(database: str, table: str) -> str:
    return f"manifests/{database}/{table}.json"


def _read_manifest(obs: ObsClient, bucket: str, key: str) -> dict:
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


def _write_manifest(obs: ObsClient, bucket: str, key: str, manifest: dict) -> None:
    body = json.dumps(manifest, indent=2, ensure_ascii=False).encode("utf-8")
    resp = obs.putContent(bucket, key, body)
    if resp.status >= 300:
        raise RuntimeError(
            f"PutObject {key} failed: status={resp.status} errorCode={resp.errorCode}"
        )


def _load_snapshot(obs: ObsClient, bucket: str, key: str) -> dict:
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


# ----------------------------------------------------------------------------
# 业务逻辑
# ----------------------------------------------------------------------------

def _restore_table(
    obs: ObsClient,
    metadata_bucket: str,
    database: str,
    table: str,
    target_metadata_location: str,
    trace_id: str,
) -> dict:
    """把单张表的 manifest.metadata_location 回填到 target."""
    key = _manifest_key(database, table)

    try:
        manifest = _read_manifest(obs, metadata_bucket, key)
    except Exception as e:
        logger.warning(f"[trace={trace_id}] manifest not found: {key} ({e})")
        return {"name": table, "status": "skipped", "reason": "manifest not found"}

    current = manifest.get("metadata_location")

    if current == target_metadata_location:
        return {
            "name": table,
            "status": "unchanged",
            "metadata_location": current,
        }

    manifest["metadata_location"] = target_metadata_location
    manifest["updated_at"] = datetime.now(timezone.utc).isoformat()
    manifest["trace_id"] = trace_id

    _write_manifest(obs, metadata_bucket, key, manifest)
    return {
        "name": table,
        "status": "restored",
        "from": current,
        "to": target_metadata_location,
    }


def handler(event, context):
    """FunctionGraph 入口."""
    trace_id = _trace_id(event)

    database = event.get("database")
    input_key = event.get("input_key")
    if not database:
        raise ValueError("Missing required input: 'database'")
    if not input_key:
        raise ValueError("Missing required input: 'input_key'")

    metadata_bucket = os.environ.get("METADATA_BUCKET")
    if not metadata_bucket:
        raise ValueError("Missing required env var: METADATA_BUCKET")

    obs = _make_obs_client(context)

    saved = _load_snapshot(obs, metadata_bucket, input_key)
    if saved.get("database") != database:
        raise ValueError(
            f"Database mismatch: snapshot is for '{saved.get('database')}' "
            f"but event requested '{database}'"
        )

    tables = saved.get("tables", [])
    logger.info(f"[trace={trace_id}] Restoring {len(tables)} tables in '{database}'")

    results = []
    counts = {"restored": 0, "unchanged": 0, "skipped": 0, "failed": 0}

    for entry in tables:
        name = entry.get("name")
        target = entry.get("metadata_location")
        if not name or not target:
            results.append(
                {
                    "name": name,
                    "status": "skipped",
                    "reason": "missing name or metadata_location",
                }
            )
            counts["skipped"] += 1
            continue

        try:
            result = _restore_table(
                obs, metadata_bucket, database, name, target, trace_id
            )
        except Exception as e:
            logger.exception(
                f"[trace={trace_id}] Failed to restore {database}.{name}"
            )
            result = {"name": name, "status": "failed", "error": str(e)}

        results.append(result)
        counts[result["status"]] = counts.get(result["status"], 0) + 1
        logger.info(f"[trace={trace_id}]   {name}: {result['status']}")

    logger.info(f"[trace={trace_id}] Summary: {counts}")

    return {
        "statusCode": 200,
        "trace_id": trace_id,
        "database": database,
        "source_file": f"obs://{metadata_bucket}/{input_key}",
        "summary": counts,
        "results": results,
    }
