# Quant Guardian v0.5.0-beta.1

本预览版适配 Quantclass Client 4.2.1 及新的 Fusion 选股内核，同时保留旧版 Aqua/Zeus 兼容监控。

## 适配内容

- 自动识别当前选股内核：优先使用实时进程，其次使用最新状态文件与增量日志，最后按已安装内核安全回退。
- 新增 `fusion.exe`、`real_trading/logs/fusion.log` 与 `fusion-stats-日期.json` 三路只读证据。
- Fusion 运行中显示健康；完成后显示空闲与最近成功；状态文件仍称运行但进程消失时明确告警。
- 半写 JSON、日志轮转及旧 Zeus/Aqua 历史记录不会让 Guardian 崩溃，也不会污染当前 Fusion 父级状态。
- v0.4 的旧 Zeus 默认配置在检测到真实 Fusion 安装后，会在内存中迁移为自动识别；新版设置仍允许显式锁定 Fusion、Zeus 或 Aqua。

## 安全边界

- Quant Guardian 仍只允许自动恢复 QMT。
- Quantclass、Fuel、Fusion、SCM、Aqua、Zeus 与 Rocket 均不会被自动启动、停止或修复。
- Fusion 与 SCM 已加入消息 Gateway 的远程控制拒绝列表。
- 本版本不会下单、撤单或修改策略、交易计划及 Quantclass 配置。

## 已验证基线

- Quantclass Client 4.2.1
- Fusion 3.0.2 / 3.0.2a
- 保留 Quantclass Client 4.1.1 的 Zeus/Aqua 兼容路径

本版本未进行代码签名，Windows SmartScreen 可能提示未知发布者。Release ZIP 不包含 QMT、XTQuant、Quantclass、Fusion、真实配置、凭据、日志或监控数据库。
