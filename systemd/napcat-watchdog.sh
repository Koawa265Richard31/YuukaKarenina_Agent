#!/bin/bash
# NapCat 掉线看门狗：检测 QQ 掉线 -> 重启 napcat 刷新二维码；重新上线 -> 推 QQ 提醒。
# 判定：取「登录成功」「掉线」两类日志信号各自最后一条的时间戳，谁新谁生效——
# 防止同一窗口内先登录后掉线时，固定优先级误判成在线（导致不重启、二维码一直过期）。
# 敏感项（QQ 号 / token）一律从 systemd 的 EnvironmentFile 注入，本文件不含任何明文。
set -u

: "${DSH_ADMIN_QQ:=}"          # 管理员 QQ（/srv/astrbot.env 注入）
: "${DSH_QQBRIDGE_TOKEN:=}"    # 桥 token（/srv/dsh-qqbridge.env 注入）
PUSH_URL="http://127.0.0.1:6200/dsh/send"
RUN_DIR="/var/run/napcat-watchdog"
STATE_FILE="$RUN_DIR/state"          # online | offline
RESTART_FILE="$RUN_DIR/restarted"    # 上次重启 napcat 的 epoch
LOG_FILE="/var/log/napcat-watchdog.log"
RESTART_COOLDOWN=600                  # 掉线期间最多每 10 分钟重启一次（防重启风暴）
LOG_WINDOW=600                        # 日志判定窗口（秒）

mkdir -p "$RUN_DIR"
log() { echo "[$(date '+%F %T')] $*" >> "$LOG_FILE"; }

write_status() {
  # 供 /napcat-qr.html 状态页读取（nginx alias 直接暴露本文件）
  printf '{"state":"%s","changedAt":"%s"}\n' "$1" "$(date '+%F %T')" > "$RUN_DIR/status.json"
}

RECENT=$(docker logs napcat --since "${LOG_WINDOW}s" 2>&1 || true)

# 各信号最后一条的时间戳（日志格式 "MM-DD HH:MM:SS"，同格式字符串比较即时间比较）
online_ts=$(echo "$RECENT" | grep 'OneBot11 适配器初始化完成' | tail -1 | awk '{print $1" "$2}')
offline_ts=$(echo "$RECENT" | grep -E 'KickedOffLine|账号状态变更为离线|登录已失效' | tail -1 | awk '{print $1" "$2}')

CUR=""
if [ -n "$offline_ts" ] || [ -n "$online_ts" ]; then
  if [ -n "$offline_ts" ] && { [ -z "$online_ts" ] || [ "$offline_ts" \> "$online_ts" ]; }; then
    CUR="offline"
  elif [ -n "$online_ts" ]; then
    CUR="online"
  fi
fi
if [ -z "$CUR" ] && ! docker ps --filter "name=^/napcat$" --filter "status=running" --format '{{.Names}}' 2>/dev/null | grep -q napcat; then
  # 容器没在跑（崩溃/被停止）：视作掉线并拉起
  CUR="offline"
fi

PREV=""
[ -f "$STATE_FILE" ] && PREV="$(cat "$STATE_FILE")"

if [ -n "$CUR" ] && [ "$CUR" != "$PREV" ]; then
  echo "$CUR" > "$STATE_FILE"
  write_status "$CUR"
  if [ "$CUR" = "offline" ]; then
    log "检测到 QQ 掉线（$PREV -> offline）"
    now=$(date +%s)
    last=$(cat "$RESTART_FILE" 2>/dev/null || echo 0)
    if [ $((now - last)) -ge "$RESTART_COOLDOWN" ]; then
      log "重启 napcat 以刷新扫码二维码"
      docker restart napcat >/dev/null 2>&1 || docker start napcat >/dev/null 2>&1
      date +%s > "$RESTART_FILE"
    fi
  else
    log "检测到 QQ 重新上线（$PREV -> online）"
    rm -f "$RESTART_FILE"
    if [ -n "$DSH_ADMIN_QQ" ] && [ -n "$DSH_QQBRIDGE_TOKEN" ]; then
      curl -s -m 10 -X POST "$PUSH_URL" \
        -H 'Content-Type: application/json' \
        -d "{\"qq\":\"$DSH_ADMIN_QQ\",\"text\":\"🔌 QQ 已重新上线\",\"token\":\"$DSH_QQBRIDGE_TOKEN\"}" \
        >/dev/null 2>&1
    fi
  fi
fi

# 首跑/无变化时兜底：保证状态页文件存在
[ -f "$RUN_DIR/status.json" ] || write_status "$(cat "$STATE_FILE" 2>/dev/null || echo unknown)"
