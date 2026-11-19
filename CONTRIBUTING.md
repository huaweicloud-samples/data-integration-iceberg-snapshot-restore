# 贡献指南

欢迎对本实践提出改进建议。

## 提交方式

- 通过仓库 Issue 反馈问题或功能建议。
- 通过 Pull Request 提交代码改进；PR 描述请说明改动目的与验证方式。

## 本地校验

提交前请确认：

```bash
cd infra
terraform init
terraform validate
```

函数代码改动后请重新打包，确认 `infra/.build/*.zip` 包含最新代码：

```bash
python3 infra/scripts/build_zips.py src/functions infra/.build
```

## 代码风格

- Terraform：资源按 `huaweicloud_` 类型分组，变量与输出配有中文 `description`。
- Python：遵循 PEP 8，函数入口统一为 `def handler(event, context)`。
