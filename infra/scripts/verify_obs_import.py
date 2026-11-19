#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
验证 .build/obs 在 import 时不会触发 pycryptodome (Crypto) 加载.
模拟 FGS Linux runtime 启动流程, 确保 OSError 不再发生.
"""
import os
import sys
import builtins

BUILD = os.path.dirname(os.path.dirname(os.path.abspath(__file__))) + "/.build"
sys.path.insert(0, BUILD)

# 记录被加载的模块
loaded_modules = []
original_import = builtins.__import__


def tracking_import(name, *args, **kwargs):
    loaded_modules.append(name)
    return original_import(name, *args, **kwargs)


builtins.__import__ = tracking_import

print("=" * 60)
print("1. 测试 import obs (不触发 Crypto)")
print("=" * 60)
try:
    import obs
    print(f"  [PASS] obs 加载成功, ObsClient = {obs.ObsClient.__name__}")
except Exception as e:
    print(f"  [FAIL] obs 加载失败: {e}")
    sys.exit(1)

# 检查是否触发过 Crypto 加载
crypto_loaded = [m for m in loaded_modules if m.startswith("Crypto") or m.startswith("crypto")]
if crypto_loaded:
    print(f"  [FAIL] import obs 意外触发了 Crypto 模块: {crypto_loaded[:5]}")
    sys.exit(1)
else:
    print(f"  [PASS] import obs 未触发 Crypto 模块加载")

# 检查 cipher_suite 是否被尝试加载
cipher_loaded = [m for m in loaded_modules if "cipher" in m.lower()]
if cipher_loaded:
    print(f"  [FAIL] import obs 意外触发了 cipher 模块: {cipher_loaded}")
    sys.exit(1)
else:
    print(f"  [PASS] import obs 未触发 cipher_suite 加载")

print()
print("=" * 60)
print("2. 测试 pre_migration.py 入口代码")
print("=" * 60)
try:
    from obs import ObsClient
    print(f"  [PASS] from obs import ObsClient 成功")
except Exception as e:
    print(f"  [FAIL] {e}")
    sys.exit(1)

print()
print("=" * 60)
print("3. 列出已加载的所有 obs 相关模块")
print("=" * 60)
obs_mods = [m for m in loaded_modules if m.startswith("obs")][:20]
for m in obs_mods:
    print(f"  {m}")

print()
print("=" * 60)
print("ALL PASS - FGS Linux runtime 不会再因 _cpuid_c 崩溃")
print("=" * 60)
