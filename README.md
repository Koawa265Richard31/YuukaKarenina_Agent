# dsh-astrbot-bridge

AstrBot **DSH 桥插件** + DSH 桥服务：通过 QQ 私聊操作 DSH agent。

## 内容
- `astrbot-plugin/astrbot_dsh/` — DSH 桥插件（`/dsh` 只读/管理员、任务/审批/订阅、`/dsh/send` 推送端点 6200）
- `dsh-admin-bridge/` — DSH 管理员桥（主 DSH，任务排队/审批）
- `dsh-qqbridge/` — DSH 只读桥（read-only 沙箱）
- `tools/dsh-session-repair.mjs` — DSH 会话日志修复工具

## 相关仓库（插件拆分后各自独立）
- `astrbot-router` — 自定义路由插件（/chatmode /character /persona /provider）
- `astrbot-screen-watch` — 屏幕监控插件 + Windows 客户端
- `astrbot-memory` — 记忆管理插件（私有）
- `astrbot-deploy` — 部署/运维资产（snowluma/nginx/systemd/web/shipyard）
- `Yuuka-memory` — 记忆数据归档 + 记忆工具（私有）

## 配置
敏感项走环境变量（DSH_WHITELIST_QQ / DSH_ADMIN_QQ / DSH_QQBRIDGE_TOKEN），不入库。
