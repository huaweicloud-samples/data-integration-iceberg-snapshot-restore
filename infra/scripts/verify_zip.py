#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
验证 zip 包内容:
- 无 Crypto/ 目录
- 无 obs/obs_cipher_suite.py
- 无 pycryptodome / crypto 残留 .pyd/.so
- obs/__init__.py 中 cipher_suite 引用被注释
- 业务模块 pre_migration / post_migration / rollback_metadata 仍存在
"""
import os
import sys
import zipfile

BUILD_DIR = "D:/workbuddy/huaweicloud/data-integration-iceberg-snapshot-restore/infra/.build"
ZIPS = ["pre_migration.zip", "post_migration.zip", "rollback_metadata.zip"]


def main():
    all_pass = True
    for zname in ZIPS:
        zpath = os.path.join(BUILD_DIR, zname)
        if not os.path.exists(zpath):
            print(f"[FAIL] {zname} not found")
            all_pass = False
            continue
        print(f"=== {zname} ===")
        with zipfile.ZipFile(zpath, "r") as z:
            names = z.namelist()
            print(f"  total entries: {len(names)}")

            # 1. 检查无 Crypto/
            crypto_entries = [n for n in names if n.startswith("Crypto/") or n.startswith("Crypto-") or n.startswith("pycryptodome")]
            if crypto_entries:
                print(f"  [FAIL] Crypto 残留: {crypto_entries[:5]}...")
                all_pass = False
            else:
                print(f"  [PASS] 无 Crypto/pycryptodome 残留")

            # 2. 检查无 obs_cipher_suite
            cipher_entries = [n for n in names if "cipher_suite" in n.lower()]
            if cipher_entries:
                print(f"  [FAIL] cipher_suite 残留: {cipher_entries}")
                all_pass = False
            else:
                print(f"  [PASS] 无 cipher_suite 残留")

            # 3. 检查 obs/__init__.py 中 cipher_suite 引用被注释
            init_py = next((n for n in names if n == "obs/__init__.py"), None)
            if init_py is None:
                print(f"  [FAIL] obs/__init__.py 不存在")
                all_pass = False
            else:
                content = z.read(init_py).decode("utf-8")
                # 找未注释的 cipher_suite 引用
                bad_lines = []
                for i, line in enumerate(content.split("\n"), 1):
                    if "cipher_suite" in line and not line.lstrip().startswith("#"):
                        bad_lines.append((i, line))
                if bad_lines:
                    print(f"  [FAIL] obs/__init__.py 仍有未注释的 cipher_suite 引用:")
                    for ln, txt in bad_lines:
                        print(f"     L{ln}: {txt}")
                    all_pass = False
                else:
                    print(f"  [PASS] obs/__init__.py 中 cipher_suite 引用已注释")

            # 4. 检查无 .pyd 残留 (Windows native module)
            pyd_entries = [n for n in names if n.endswith(".pyd")]
            if pyd_entries:
                print(f"  [FAIL] .pyd 残留: {pyd_entries}")
                all_pass = False
            else:
                print(f"  [PASS] 无 .pyd 残留")

            # 5. 检查业务代码仍在
            fn_names = [n for n in ZIPS if n == zname]
            expected_fn = zname.replace(".zip", ".py")
            if expected_fn in names:
                print(f"  [PASS] 业务代码 {expected_fn} 存在")
            else:
                print(f"  [FAIL] 业务代码 {expected_fn} 缺失")
                all_pass = False

            # 6. 检查 obs/ObsClient 模块齐备
            obs_files = [n for n in names if n.startswith("obs/") and n.endswith(".py")]
            if "obs/client.py" in obs_files and "obs/__init__.py" in obs_files:
                print(f"  [PASS] obs SDK 关键模块齐备 ({len(obs_files)} 个 .py)")
            else:
                print(f"  [WARN] obs SDK 关键模块不完整")

            # 7. 检查 esdk-obs-python 传递依赖是否齐备 (历史踩坑: crcmod 漏掉导致 import 失败)
            REQUIRED_DEPS = ["crcmod/__init__.py", "urllib3/__init__.py",
                             "certifi/__init__.py", "idna/__init__.py",
                             "charset_normalizer/__init__.py", "requests/__init__.py"]
            missing_deps = [d for d in REQUIRED_DEPS if d not in names]
            if missing_deps:
                print(f"  [FAIL] 传递依赖缺失 (运行时 import 会失败): {missing_deps}")
                all_pass = False
            else:
                print(f"  [PASS] esdk-obs-python 传递依赖齐备 (crcmod/urllib3/certifi/idna/charset_normalizer/requests)")

    print()
    print("=" * 50)
    if all_pass:
        print("ALL PASS ✓")
        return 0
    else:
        print("SOME FAIL ✗")
        return 1


if __name__ == "__main__":
    sys.exit(main())
