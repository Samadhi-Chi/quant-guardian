# Quant Guardian v0.5.0-beta.2

本预览版包含 v0.5.0-beta.1 的 Quantclass Client 4.2.1 / Fusion 适配，并修正周末与休市日的 QMT 状态误报。

## 适配内容

- 自动识别 Fusion 选股内核，同时保留 Aqua、Zeus 的显式兼容模式。
- 通过 `fusion.exe`、增量日志和每日 UI 状态文件联合判断运行、成功、失败与空闲状态。
- 旧版默认 Zeus 配置在检测到 Fusion 安装后安全迁移为自动识别。

## 休市状态修正

- 仅在确认休市日、QMT 进程健康、网络正常且没有人工登录要求时，将 XTQuant 的精确 `connect returned -1` 结果解释为券商会话休市，显示为空闲且不触发恢复。
- 交易日出现同样结果仍按关键链路故障处理。
- QMT 进程缺失、网络失败或需要人工登录时仍不会被休市规则掩盖。

## 安全边界

- 自动恢复仍严格限制为 QMT。
- Quantclass、Fuel、Fusion、SCM、Aqua、Zeus 与 Rocket 不会被自动启动、停止或修复。
- 本版本不会下单、撤单或修改策略与交易计划。

本版本未进行代码签名，Windows SmartScreen 可能提示未知发布者。Release ZIP 不包含 QMT、XTQuant、Quantclass、Fusion、真实配置、凭据、日志或监控数据库。
