"""通过 FunctionGraph invoke API 直接调用函数, 验证业务逻辑.

纯标准库 + requests, 手写华为云 SDK-HMAC-SHA256 签名.
用法:
  python invoke_fgs.py <function_name> '<event_json>'
  function_name: pre | post | rollback
  event_json: 例如 '{"database": "demo_db"}'
"""
import os
import sys
import json
import hashlib
import hmac
import datetime
from urllib.parse import quote

import requests

REGION = os.environ.get("HW_REGION_NAME", "cn-north-4")
AK = os.environ.get("HW_ACCESS_KEY")
SK = os.environ.get("HW_SECRET_KEY")
PROJECT_ID = "cc2cb3d01b0c46798d77c4cd8ecf6b7e"

NAMES = {
    "pre": "iceberg-migration-pre-migration",
    "post": "iceberg-migration-post-migration",
    "rollback": "iceberg-migration-rollback",
}

SERVICE = "functiongraph"
HOST = os.environ.get("FGS_HOST", f"x-functiongraph.{REGION}.myhuaweicloud.com")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def build_auth(method: str, uri: str, query: str, body: bytes,
                headers: dict, signed_keys, region: str, service: str):
    canonical_headers = "".join(
        f"{k.lower()}:{str(headers[k]).strip()}\n" for k in sorted(signed_keys)
    )
    signed_headers = ";".join(sorted(k.lower() for k in signed_keys))
    canonical_request = "\n".join([
        method.upper(),
        uri,
        query,
        canonical_headers,
        signed_headers,
        sha256_hex(body),
    ])
    now = datetime.datetime.now(datetime.timezone.utc)
    date_stamp = now.strftime("%Y%m%dT%H%M%SZ")
    scope = f"{now.strftime('%Y%m%d')}/{region}/{service}/request"
    string_to_sign = "\n".join([
        "SDK-HMAC-SHA256",
        date_stamp,
        scope,
        sha256_hex(canonical_request.encode("utf-8")),
    ])
    k_date = hmac.new(f"SDK{SK}".encode(), now.strftime("%Y%m%d").encode(),
                      hashlib.sha256).digest()
    k_region = hmac.new(k_date, region.encode(), hashlib.sha256).digest()
    k_service = hmac.new(k_region, service.encode(), hashlib.sha256).digest()
    k_signing = hmac.new(k_service, b"request", hashlib.sha256).digest()
    signature = hmac.new(k_signing, string_to_sign.encode(),
                         hashlib.sha256).hexdigest()
    auth = (f"SDK-HMAC-SHA256 Access={AK}, SignedHeaders={signed_keys_str(signed_keys)}, "
            f"Signature={signature}")
    return auth, date_stamp


def signed_keys_str(keys):
    return ";".join(sorted(k.lower() for k in keys))


def main():
    if len(sys.argv) < 3:
        print("usage: python invoke_fgs.py <pre|post|rollback> '<event_json>'")
        sys.exit(2)
    short = sys.argv[1]
    event = sys.argv[2]
    func_name = NAMES[short]
    urn = (f"urn:fss:{REGION}:{PROJECT_ID}:function:default:{func_name}:latest")
    encoded_urn = quote(urn, safe="")
    uri = f"/v1/{PROJECT_ID}/functions/{encoded_urn}/invocations"
    url = f"https://{HOST}{uri}"

    body = event.encode("utf-8")
    headers = {
        "Host": HOST,
        "Content-Type": "application/json",
        "X-Project-Id": PROJECT_ID,
    }
    signed_keys = ["Host", "Content-Type", "X-Project-Id"]
    auth, date_stamp = build_auth("POST", uri, "", body, headers, signed_keys,
                                  REGION, SERVICE)
    headers["X-Sdk-Date"] = date_stamp
    headers["Authorization"] = auth

    print(f"POST {url}")
    print(f"body: {event}")
    try:
        # 默认绕过代理直连 (x-functiongraph 常被代理 502 屏蔽, 但浏览器/直连可达)
        proxies = {"http": None, "https": None}
        resp = requests.post(url, data=body, headers=headers, timeout=30,
                             proxies=proxies)
    except Exception as e:  # noqa
        print(f"CONNECTION_ERROR: {e}")
        sys.exit(3)
    print(f"HTTP {resp.status_code}")
    print("--- response headers ---")
    for k, v in resp.headers.items():
        print(f"  {k}: {v}")
    print("--- response body ---")
    print(resp.text)


if __name__ == "__main__":
    main()
