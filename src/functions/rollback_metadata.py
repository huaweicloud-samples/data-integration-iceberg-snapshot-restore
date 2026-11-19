"""
函数: 紧急回滚, 把 Iceberg 表的 metadata_location 指针重置到 Nth-latest metadata.json.

事件格式:
{
  "database": "my_database",
  "obs_base": "obs://<data-bucket>/<table-prefix>"   # 可选, 默认按 manifest 中的 metadata_location 解析
}

环境变量 (通过 FunctionGraph user_data 注入):
  METADATA_BUCKET - manifest 所在的 OBS 桶名
  DATA_BUCKET     - Iceberg 数据桶名

行为:
  - 读 manifest 列出数据库下所有 Iceberg 表
  - 对每张表, 列 data 桶 metadata/*.metadata.json 文件
  - 按 LastModified 排序, 选 Nth-latest (offset=3)
  - 更新 manifest 的 metadata_location 字段

IAM 委托所需权限:
  - obs:object:GetObject / PutObject / ListBucket
  - 对 data 桶的 metadata 目录只读
  - 对 metadata 桶的 manifest 前缀可读写
"""

import json
import logging
import os
import re
import uuid
from datetime import datetime, timezone

from obs import ObsClient

logger = logging.getLogger()
logger.setLevel(logging.INFO)


# 回滚步长: 1 = previous (倒数第 2 个), 3 = 三版之前.
ROLLBACK_OFFSET = 3


def _make_obs_client(context=None) -> ObsClient:
    """构造 ObsClient, 按以下顺序解析凭证:

    1. user_data 注入的永久 AK/SK (HW_AK / HW_SK) —— 业务首选, owner 身份
       绕过 OBS 私有桶 STS 临时凭证的 ACL owner 校验差异.
    2. context IAM 委托临时凭证 —— fallback.
    3. 标准 HUAWEICLOUD_* 环境变量 (本地调试).

    关键: 强制 signature='v4' + region + is_signature_negotiation=False.
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


def _parse_obs_uri(uri: str) -> tuple:
    """拆分 obs://bucket/key 为 (bucket, key)."""
    uri = uri.rstrip("/")
    parts = uri.replace("obs://", "").split("/", 1)
    return parts[0], parts[1] if len(parts) > 1 else ""


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


def _list_manifest_keys(obs: ObsClient, bucket: str, db: str) -> list:
    """列出数据库下所有 manifest.json key."""
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


def _find_previous_metadata(
    obs: ObsClient, data_bucket: str, table_prefix: str, offset: int = ROLLBACK_OFFSET
):
    """
    列出 data 桶下 <table_prefix>/metadata/*.metadata.json, 按 LastModified 排序,
    返回 offset 之前的 metadata 文件 URI; 不足 offset+1 个文件则返回 None.
    """
    metadata_prefix = f"{table_prefix}/metadata/"
    metadata_files = []
    marker = ""
    while True:
        resp = obs.listObjects(data_bucket, prefix=metadata_prefix, marker=marker)
        if resp.status >= 300:
            raise RuntimeError(
                f"ListObjects {metadata_prefix} failed: status={resp.status}"
            )
        for c in resp.body.contents or []:
            if c.key.endswith(".metadata.json"):
                metadata_files.append(
                    {
                        "key": c.key,
                        "last_modified": c.last_modified,
                    }
                )
        if not resp.body.is_truncated:
            break
        marker = resp.body.next_marker or ""

    if not metadata_files:
        return None

    def _version_of(key):
        m = re.search(r"v(\d+)\.metadata\.json$", key)
        return int(m.group(1)) if m else -1

    def _sort_key(item):
        # 主排序: 按 Iceberg 元数据版本号 (v1 < v2 < ...); 次排序: last_modified 兜底,
        # 避免 last_modified 为 None 时 sort 抛 TypeError.
        return (_version_of(item["key"]), str(item["last_modified"] or ""))

    metadata_files.sort(key=_sort_key)

    if len(metadata_files) < offset + 1:
        return None

    previous_key = metadata_files[-(offset + 1)]["key"]
    return f"obs://{data_bucket}/{previous_key}"


def _rollback_table(
    obs: ObsClient,
    metadata_bucket: str,
    data_bucket: str,
    database: str,
    table: str,
    trace_id: str,
) -> dict:
    """回滚单张表的 metadata_location 到 Nth-latest."""
    manifest_key = _manifest_key(database, table)

    try:
        manifest = _read_manifest(obs, metadata_bucket, manifest_key)
    except Exception:
        return {"name": table, "status": "skipped", "reason": "manifest not found"}

    current_meta = manifest.get("metadata_location", "")
    if not current_meta:
        return {"name": table, "status": "skipped", "reason": "no metadata_location"}

    _, meta_key = _parse_obs_uri(current_meta)
    table_prefix = re.sub(r"/metadata/.*$", "", meta_key)

    previous = _find_previous_metadata(obs, data_bucket, table_prefix)
    if not previous:
        return {
            "name": table,
            "status": "skipped",
            "reason": f"fewer than {ROLLBACK_OFFSET + 1} metadata files found",
        }

    if current_meta == previous:
        return {
            "name": table,
            "status": "unchanged",
            "metadata_location": current_meta,
        }

    manifest["metadata_location"] = previous
    manifest["updated_at"] = datetime.now(timezone.utc).isoformat()
    manifest["trace_id"] = trace_id

    _write_manifest(obs, metadata_bucket, manifest_key, manifest)
    return {
        "name": table,
        "status": "rolled_back",
        "from": current_meta,
        "to": previous,
    }


def handler(event, context):
    """FunctionGraph 入口."""
    trace_id = _trace_id(event)

    database = event.get("database")
    if not database:
        raise ValueError("Missing required input: 'database'")

    metadata_bucket = os.environ.get("METADATA_BUCKET")
    data_bucket = os.environ.get("DATA_BUCKET")
    if not metadata_bucket or not data_bucket:
        raise ValueError("Missing required env var: METADATA_BUCKET / DATA_BUCKET")

    obs = _make_obs_client(context)

    manifest_keys = _list_manifest_keys(obs, metadata_bucket, database)
    logger.info(
        f"[trace={trace_id}] Rolling back Iceberg tables in '{database}' "
        f"(offset={ROLLBACK_OFFSET})"
    )

    results = []
    counts = {"rolled_back": 0, "unchanged": 0, "skipped": 0, "failed": 0}

    for key in manifest_keys:
        try:
            manifest = _read_manifest(obs, metadata_bucket, key)
        except Exception as e:
            logger.warning(f"[trace={trace_id}] skip {key}: {e}")
            continue

        if manifest.get("format", "").lower() != "iceberg":
            continue

        table = manifest.get("table")
        if not table:
            continue

        try:
            result = _rollback_table(
                obs, metadata_bucket, data_bucket, database, table, trace_id
            )
        except Exception as e:
            logger.exception(
                f"[trace={trace_id}] Failed to rollback {database}.{table}"
            )
            result = {"name": table, "status": "failed", "error": str(e)}

        results.append(result)
        counts[result["status"]] = counts.get(result["status"], 0) + 1
        logger.info(f"[trace={trace_id}]   {table}: {result['status']}")

    logger.info(f"[trace={trace_id}] Summary: {counts}")

    return {
        "statusCode": 200,
        "trace_id": trace_id,
        "database": database,
        "rollback_offset": ROLLBACK_OFFSET,
        "summary": counts,
        "results": results,
    }
