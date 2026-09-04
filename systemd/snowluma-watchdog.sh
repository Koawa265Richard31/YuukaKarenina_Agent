#!/usr/bin/env bash
# SnowLuma 状态 + 登录窗二维码提取 + 过期自动换码（每 5s 由 timer 调用）
set -u
STATUS_FILE=/var/run/snowluma-watchdog/status.json
SHOT=/var/run/snowluma/screen.png
CROP=/var/run/snowluma/qr-crop.png
CROPSRC=/tmp/tiles/login.png

# 强制换新码：账密登录⇄扫码登录 切换。
# （QQ 过期后有时不显示“刷新”链接，此方式任何状态都有效）
force_new_qr() {
  docker exec snowluma bash -c 'DISPLAY=:1 xdotool mousemove 931 716 click 1; sleep 1.5; xdotool mousemove 910 717 click 1' >/dev/null 2>&1
}

running=$(docker inspect -f '{{.State.Running}}' snowluma 2>/dev/null || echo false)
if [ "$running" != "true" ]; then
  echo '{"qq_online":false,"time":"容器未运行"}' > "$STATUS_FILE"
  exit 0
fi

# 1. 全屏截图
docker exec -u snowluma -e DISPLAY=:1 snowluma sh -c 'scrot -o -q 75 /tmp/sl-shot.png' >/dev/null 2>&1
docker cp snowluma:/tmp/sl-shot.png "$SHOT.tmp" >/dev/null 2>&1 && mv -f "$SHOT.tmp" "$SHOT"

# 2. QQ 在线判定：snowluma → astrbot:6199 WS 是否 ESTABLISHED
PREV=$(cat /var/run/snowluma-watchdog/prev_state 2>/dev/null || echo 0)
OFF_SINCE=$(cat /var/run/snowluma-watchdog/offline_since 2>/dev/null || echo 0)
if [ -n "$(ss -tn state established '( sport = :6199 )' 2>/dev/null | tail -n +2)" ]; then
  echo "{\"qq_online\":true,\"time\":\"$(date +%F\ %T)\"}" > "$STATUS_FILE"
  echo 1 > /var/run/snowluma-watchdog/prev_state
  if [ "$PREV" != "1" ] && [ $(( $(date +%s) - OFF_SINCE )) -gt 60 ]; then
    # 上线通知（token/admin_qq 运行时从插件配置读，脚本内无明文）
    python3 - <<'PYEOF2' 2>/dev/null
import json,urllib.request
cfg=json.load(open('/srv/astrbot/data/config/astrbot_dsh_config.json',encoding='utf-8-sig'))
req=urllib.request.Request('http://127.0.0.1:6200/dsh/send',
  data=json.dumps({'token':cfg.get('bridge_token',''),'qq':str(cfg.get('admin_qq','')),'text':'🔌 QQ 已重新上线（SnowLuma）'}).encode(),
  headers={'Content-Type':'application/json'})
try: urllib.request.urlopen(req,timeout=10)
except Exception: pass
PYEOF2
  fi
  exit 0
fi
echo 0 > /var/run/snowluma-watchdog/prev_state
[ "$PREV" = "1" ] && echo "$(date +%s)" > /var/run/snowluma-watchdog/offline_since

# 3. 未登录：裁剪登录窗（约 800,290 起 340x470）放大 2x
python3 - "$SHOT" "$CROP" "$CROPSRC" <<'PYEOF' >/dev/null 2>&1
import sys
from PIL import Image
src, crop, cropsrc = sys.argv[1], sys.argv[2], sys.argv[3]
img = Image.open(src)
w, h = img.size
box = (int(w*0.42), int(h*0.28), int(w*0.59), int(h*0.71))
c = img.crop(box).resize(((box[2]-box[0])*2, (box[3]-box[1])*2), Image.LANCZOS)
c.save(crop)
c.save(cropsrc)
PYEOF

# 4. 二维码新鲜度管理：
#    - OCR 出现“过期/刷新” → 立即换码
#    - 同一个码超过 90s → 强制换码（QQ 有时过期不变灰，zbar 仍能读旧码）
#    - 无码且登录窗在 → 60s 无码后换码
QR=$(zbarimg --raw -q "$CROPSRC" 2>/dev/null | head -1)
TEXT=$(tesseract "$CROPSRC" - -l chi_sim --psm 11 2>/dev/null | tr -d ' ')
NOW=$(date +%s)

if echo "$TEXT" | grep -qE '刷新|过期'; then
  force_new_qr
  echo "{\"qq_online\":false,\"time\":\"$(date +%F\ %T)\",\"qr\":\"expired-refreshing\"}" > "$STATUS_FILE"
elif [ -n "$QR" ]; then
  LASTQR=$(cat /var/run/snowluma/qr-url.txt 2>/dev/null || true)
  SEEN=$(cat /var/run/snowluma/qr-seen.txt 2>/dev/null || echo 0)
  if [ "$QR" != "$LASTQR" ]; then
    echo "$QR" > /var/run/snowluma/qr-url.txt
    echo "$NOW" > /var/run/snowluma/qr-seen.txt
    echo "{\"qq_online\":false,\"time\":\"$(date +%F\ %T)\",\"qr\":\"fresh\"}" > "$STATUS_FILE"
  elif [ $((NOW - SEEN)) -gt 90 ]; then
    force_new_qr
    echo "$NOW" > /var/run/snowluma/qr-seen.txt
    echo "{\"qq_online\":false,\"time\":\"$(date +%F\ %T)\",\"qr\":\"rotating\"}" > "$STATUS_FILE"
  else
    echo "{\"qq_online\":false,\"time\":\"$(date +%F\ %T)\",\"qr\":\"fresh\"}" > "$STATUS_FILE"
  fi
else
  if echo "$TEXT" | grep -qE '扫码登录|账密登录'; then
    NOSEEN=$(cat /var/run/snowluma/noqr-seen.txt 2>/dev/null || echo "$NOW")
    if [ $((NOW - NOSEEN)) -gt 60 ]; then
      force_new_qr
      echo "$NOW" > /var/run/snowluma/noqr-seen.txt
      echo "{\"qq_online\":false,\"time\":\"$(date +%F\ %T)\",\"qr\":\"none-refreshing\"}" > "$STATUS_FILE"
    else
      echo "{\"qq_online\":false,\"time\":\"$(date +%F\ %T)\",\"qr\":\"none\"}" > "$STATUS_FILE"
    fi
  else
    echo "{\"qq_online\":false,\"time\":\"$(date +%F\ %T)\",\"qr\":\"none\"}" > "$STATUS_FILE"
  fi
fi
