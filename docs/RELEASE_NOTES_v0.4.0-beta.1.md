# Quant Guardian v0.4.0-beta.1

本预览版在既有 QMT 监控与受控恢复之上新增独立消息 Gateway。

## 新增能力

- Telegram Bot API 私聊播报、固定命令与原生确认按钮。
- 个人微信 iLink Bot 私聊文本播报、固定命令与一次性确认码。
- 消息送达、通道连接、远程命令与 QMT 重启结果的统计和明细。
- 两层本机授权、私聊白名单、短时确认、速率限制和每次执行前的安全复核。

远程端只允许查询状态、立即只读检测、查看故障/操作记录，以及二次确认后的 QMT 受控重启。不会远程启动、停止或修复 Quantclass、Fuel、Aqua、Zeus 或 Rocket，也不提供下单、撤单、文件访问、Shell、LLM 或任意命令入口。

## 个人微信说明

个人微信通道是 Quant Guardian 内置的最小文本适配器，不需要安装完整 Hermes Agent。它独立适配了 Hermes Agent MIT 许可的 iLink 协议结构，并保留上游归属。扫码得到的是 iLink Bot 身份，不是可任意操控的普通个人微信客户端；群聊、媒体与文件均不支持。

## 重要提示

- 本版本未进行代码签名，可能触发 Windows SmartScreen。
- Gateway 默认关闭，远程控制也默认关闭；凭据由当前 Windows 用户的 DPAPI 保护。
- 自动恢复与远程恢复都只面向 QMT，且使用彼此独立的授权开关。
- 请先完成通道只读配对和至少两个交易日的观察，再于收市后执行远程 QMT 重启验收。

Release ZIP 包含主程序与 `Quant Guardian Gateway.exe`、许可证、CycloneDX SBOM 和安装脚本；不包含 QMT、XTQuant、Quantclass、真实配置、Token、日志或数据库。
