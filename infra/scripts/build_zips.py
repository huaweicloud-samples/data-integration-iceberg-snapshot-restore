#!/usr/bin/env python3
# ============================================================================
# 把 src/functions/ 下的 Python 文件打成 zip, 供 FunctionGraph 函数部署使用.
# ----------------------------------------------------------------------------
# 同时安装 esdk-obs-python 到 build 目录, 一起打进 zip,
# 保证函数运行时无需依赖 FGS runtime 内置包, 自包含运行.
#
# 用法: build_zips.py <src_dir> <build_dir>
# ============================================================================

import os
import shutil
import subprocess
import sys
import zipfile


FUNCTIONS = [
    ("pre_migration.py", "pre_migration.zip"),
    ("post_migration.py", "post_migration.zip"),
    ("rollback_metadata.py", "rollback_metadata.zip"),
]


def install_obs_into(build_dir: str) -> None:
    """用 pip 把 esdk-obs-python 装到 build_dir, 函数代码内可直接 import obs."""
    # pip install --target <dir> 会把包解到 <dir>/<package_name>/...
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--quiet",
            "--disable-pip-version-check",
            "--target",
            build_dir,
            "esdk-obs-python",
        ],
        check=True,
    )


def strip_unused_obs_cipher(build_dir: str) -> None:
    """
    移除 obs SDK 中未使用的客户端加密模块, 解决 FGS Linux 运行时加载失败问题.

    问题链:
    - esdk-obs-python 3.26.x 依赖 pycryptodome, Windows pip install 装出的是 .pyd
    - FGS runtime 是 Linux x86_64 Python 3.10, 需要 .cpython-310-x86_64-linux-gnu.so
    - obs/__init__.py 在 import 时会触发 `from obs.obs_cipher_suite import CTRCipherGenerator`
    - cipher_suite 顶部 `from Crypto.Cipher import AES, PKCS1_OAEP` 加载 _cpuid_c.so
    - Linux runtime 找不到 .so, function entry 直接抛 OSError, 整个函数无法启动

    解决:
    - 本仓库 3 个函数 (pre_migration / post_migration / rollback_metadata) 都不使用客户端加密
    - 删除 obs/obs_cipher_suite.py + 整个 Crypto/ 包, 并 patch obs/__init__.py 不再 import 它
    - 压缩后体积约 4.5 MB, 不影响功能
    """
    # 1. 删除整个 Crypto 包 + pycryptodome 相关的 dist-info
    crypto_dir = os.path.join(build_dir, "Crypto")
    if os.path.isdir(crypto_dir):
        shutil.rmtree(crypto_dir, ignore_errors=True)
    for entry in os.listdir(build_dir):
        if entry.lower().startswith(("pycryptodome", "crypto-")) and entry.endswith(".dist-info"):
            shutil.rmtree(os.path.join(build_dir, entry), ignore_errors=True)

    # 2. 删除 obs/obs_cipher_suite.py
    cipher_py = os.path.join(build_dir, "obs", "obs_cipher_suite.py")
    if os.path.exists(cipher_py):
        os.remove(cipher_py)

    # 3. Patch obs/__init__.py, 把 cipher_suite 的 import 注释掉
    init_py = os.path.join(build_dir, "obs", "__init__.py")
    if os.path.exists(init_py):
        with open(init_py, "r", encoding="utf-8") as f:
            content = f.read()
        new_content = content
        new_content = new_content.replace(
            "from obs.obs_cipher_suite import CTRCipherGenerator",
            "# from obs.obs_cipher_suite import CTRCipherGenerator  # patched: 客户端加密未使用, 移除以避开 pycryptodome 依赖",
        )
        new_content = new_content.replace(
            "from obs.obs_cipher_suite import CtrRSACipherGenerator",
            "# from obs.obs_cipher_suite import CtrRSACipherGenerator  # patched: 客户端加密未使用, 移除以避开 pycryptodome 依赖",
        )
        if new_content != content:
            with open(init_py, "w", encoding="utf-8") as f:
                f.write(new_content)

    # 4. 删除所有 __pycache__ (避免打包 .pyc 文件, 它们是 Python 2/3 字节码缓存, runtime 不需要)
    for root, dirs, _files in os.walk(build_dir):
        for d in list(dirs):
            if d == "__pycache__":
                shutil.rmtree(os.path.join(root, d), ignore_errors=True)
                dirs.remove(d)

    # 5. 删除所有 .pyd 文件 (Windows native module 扩展名, FGS Linux runtime 不识别)
    # Python 会按 PEP 3149 规则忽略不匹配版本的 .pyd, 但保留 .py 兜底; 删掉只为减小 zip 体积.
    for root, _dirs, files in os.walk(build_dir):
        for f in files:
            if f.endswith(".pyd"):
                os.remove(os.path.join(root, f))


def build_one(src_dir: str, src_name: str, build_dir: str, zip_name: str) -> str:
    src_path = os.path.join(src_dir, src_name)
    dst_path = os.path.join(build_dir, zip_name)
    # 顶级目录白名单: 跳过 bin (CLI 入口 exe) 与 __pycache__ (Python 字节码缓存).
    # 早期版本只复制 "obs" + "*.dist-info", 把 esdk-obs-python 的传递依赖
    # (crcmod / urllib3 / certifi / idna / charset_normalizer / requests)
    # 全部漏掉, 运行时 import obs 即 ModuleNotFoundError.
    SKIP_TOP_LEVEL = {"bin", "__pycache__"}
    with zipfile.ZipFile(dst_path, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(src_path, arcname=src_name)
        # 把 build_dir 下所有顶级 Python 包目录 + *.dist-info 都打进 zip 根目录.
        for entry in sorted(os.listdir(build_dir)):
            if entry == zip_name:
                continue
            full = os.path.join(build_dir, entry)
            if not os.path.isdir(full):
                continue
            if entry in SKIP_TOP_LEVEL:
                continue
            for root, dirs, files in os.walk(full):
                # 运行时不需要 __pycache__/.
                dirs[:] = [d for d in dirs if d != "__pycache__"]
                for f in files:
                    fp = os.path.join(root, f)
                    arc = os.path.relpath(fp, build_dir)
                    z.write(fp, arcname=arc)
    return dst_path


def main() -> None:
    if len(sys.argv) != 3:
        print("用法: build_zips.py <src_dir> <build_dir>", file=sys.stderr)
        sys.exit(2)

    src_dir = sys.argv[1]
    build_dir = sys.argv[2]

    os.makedirs(build_dir, exist_ok=True)
    install_obs_into(build_dir)
    strip_unused_obs_cipher(build_dir)

    for src_name, zip_name in FUNCTIONS:
        dst = build_one(src_dir, src_name, build_dir, zip_name)
        print(f"[+] Built {dst}")


if __name__ == "__main__":
    main()
