#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""仅重新打包函数 zip: 用修复后的 src/functions/*.py + .build 中已有 obs SDK/依赖.
不执行 pip install (沙箱 pypi 被代理拦截时用). .build 须已含 obs 等依赖.
"""
import os
import sys
import zipfile
import shutil

HERE = os.path.dirname(os.path.abspath(__file__))
BUILD = os.path.join(HERE, "..", ".build")
SRC = os.path.join(HERE, "..", "..", "src", "functions")

FUNCTIONS = [
    ("pre_migration.py", "pre_migration.zip"),
    ("post_migration.py", "post_migration.zip"),
    ("rollback_metadata.py", "rollback_metadata.zip"),
]


def collect_deps():
    items = []
    for name in sorted(os.listdir(BUILD)):
        if name.endswith(".zip"):
            continue
        items.append(name)
    return items


def main():
    if not os.path.isdir(os.path.join(BUILD, "obs")):
        raise SystemExit(".build/obs 不存在, 请先联网 pip install esdk-obs-python")
    deps = collect_deps()
    print("依赖条目: %d" % len(deps))
    for src_name, zip_name in FUNCTIONS:
        src_path = os.path.join(SRC, src_name)
        if not os.path.exists(src_path):
            raise SystemExit("缺失源文件: " + src_path)
        zip_path = os.path.join(BUILD, zip_name)
        if os.path.exists(zip_path):
            os.remove(zip_path)
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
            # 1) 函数代码 (arcname 不带路径)
            z.write(src_path, arcname=src_name)
            # 2) 依赖: 递归加入 .build 下除 *.zip 外的所有条目
            for name in deps:
                full = os.path.join(BUILD, name)
                if os.path.isdir(full):
                    for root, _dirs, files in os.walk(full):
                        for f in files:
                            fp = os.path.join(root, f)
                            arc = os.path.relpath(fp, BUILD).replace(os.sep, "/")
                            z.write(fp, arcname=arc)
                else:
                    z.write(full, arcname=name)
        print("已重建 %s (%d bytes)" % (zip_name, os.path.getsize(zip_path)))


if __name__ == "__main__":
    main()
