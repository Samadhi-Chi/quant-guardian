# Quant Guardian v0.3.0-beta.1

首个公开预览版，面向 Windows x64。

## 重要提示

- 本版本未进行代码签名，可能触发 Windows SmartScreen。
- 默认观察模式，不会自动重启。
- 自动恢复只处理 QMT；不会自动启动、停止或修复 Quantclass、Fuel、Aqua、Zeus 或 Rocket。
- Quant Guardian 不会下单、撤单或生成交易计划。
- 请先在非交易环境和至少一个完整观察日中验证。

## 下载与校验

下载 Windows ZIP、SHA256SUMS 和 CycloneDX SBOM。使用 SHA256SUMS 校验 ZIP 后再解压运行。

发行 ZIP 包含：

- PyInstaller one-folder 程序目录；
- 加固后的安装与卸载脚本；
- Apache-2.0、Qt/PySide6 LGPL/GPL 与第三方许可材料；
- 嵌入式 CycloneDX SBOM。

发行 ZIP 不包含 QMT、XTQuant、Quantclass、真实配置、日志、账户信息或监控数据库。

## 已知限制

- QMT、Quantclass 和 XTQuant 的兼容性仅对 README 中的基线版本完成验证。
- 某些券商登录、升级、风险提示或确认对话框仍需要人工处理。
- 在获得代码签名证书并完成更长观察期前，不发布 Stable。
