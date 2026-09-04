# Shipyard Agent 沙箱（astrbot Computer Use）

astrbot 的 Agent 工具（shell / ipython / 文件系统）在隔离容器内执行，
写操作被限制在沙箱工作区，宿主文件系统不可达。

## 组件
- **Bay**（控制面）：`soulter/shipyard-bay:latest`，管理 Ship 生命周期
- **Ship**（执行环境）：`soulter/shipyard-ship:latest`（本地 tag 为 `ship:latest`），
  每会话一个容器，非特权、仅挂载自己的数据目录 `/root/ship_data/<ship_id>/home`

## 部署（本机）
```bash
# 1. 拉镜像（docker.1ms.run 镜像源可拉；daocloud 白名单没有）
docker pull soulter/shipyard-bay:latest && docker pull soulter/shipyard-ship:latest
docker tag soulter/shipyard-ship:latest ship:latest

# 2. 网络 + token
docker network create shipyard
TOKEN=$(openssl rand -hex 16); echo "token: $TOKEN" > /srv/shipyard.token; chmod 600 /srv/shipyard.token

# 3. 起 Bay（必须 --network shipyard，否则连不上 Ship）
docker run -d --name shipyard-bay --restart unless-stopped --network shipyard \
  -p 127.0.0.1:8156:8156 \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -e ACCESS_TOKEN="$TOKEN" -e MAX_SHIP_NUM=3 -e CONTAINER_DRIVER=docker \
  soulter/shipyard-bay:latest
```

## astrbot 配置（cmd_config.json → provider_settings）
```json
"computer_use_runtime": "sandbox",
"sandbox": {
  "booter": "shipyard",
  "shipyard_endpoint": "http://127.0.0.1:8156",
  "shipyard_access_token": "<token>",
  "shipyard_ttl": 3600,
  "shipyard_max_sessions": 3
}
```
改完重启 astrbot。此后聊天 LLM 自动挂载 shell/ipython/fs/上传/下载工具，
执行全部落在 Ship 容器内；FS 工具对工作区外路径返回 403。

## 安全边界
- Ship 容器非特权（Privileged=false、无 CapAdd）、无 docker socket、无宿主路径挂载
- FS API 强制 `path must be within workspace`（403）
- shell 在容器内以 root 运行，但写不穿容器（宿主仅暴露 `/root/ship_data/<id>/home`）
- 宿主机要求：每 Ship 约 512MB，建议 2CPU/4GB+

## 运维
```bash
docker logs -f shipyard-bay        # Bay 日志
docker ps | grep ship-             # 查看沙箱容器
docker rm -f ship-<id>             # 手动回收（TTL 到期自动回收）
```
