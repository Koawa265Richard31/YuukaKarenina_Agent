#!/usr/bin/env bash
# 重启 astrbot 前的安全检查：检测 bot 内置 agent（tool_loop_agent_runner）是否在跑任务。
# 用法: tools/check-agent-idle.sh [窗口分钟数，默认 10]
# 退出码: 0 = idle 可安全重启; 1 = busy 不建议重启; 2 = 无法判断/astrbot 未运行
set -u

WIN=${1:-10}
SVC=astrbot

if ! systemctl is-active --quiet "$SVC"; then
  echo "ASTRBOT_NOT_RUNNING: $SVC 未运行，无需检查"
  exit 2
fi

SINCE="$WIN minutes ago"
# 只统计当前 astrbot 进程（MainPID）的日志，排除被 kill 的旧进程残留
PID=$(systemctl show -p MainPID --value "$SVC" 2>/dev/null | tr -d ' ')
if [ -z "$PID" ] || [ "$PID" = "0" ]; then
  echo "ASTRBOT_NO_PID: 无法获取 $SVC 主进程 PID"
  exit 2
fi
LOG=$(journalctl -u "$SVC" --since "$SINCE" --no-pager 2>/dev/null | grep -F "astrbot[${PID}]")

# 工具调用开始标记（403: "使用工具：xxx，参数：..." 是实际执行；368 是决定调用）
START=$(echo "$LOG" | grep -c "使用工具：")
# 工具调用完成标记（580: "Tool \`xxx\` Result: ..."）
DONE=$(echo "$LOG" | grep -c "Tool .* Result:")
# 任何 agent 运行器活动
ACTIVITY=$(echo "$LOG" | grep -c "tool_loop_agent_runner")
# 最近一条 agent 日志时间（取最后一条匹配的时间戳）
LAST_TS=$(echo "$LOG" | grep "tool_loop_agent_runner" | tail -1 | grep -oE "^[A-Z][a-z]{2} [ 0-9][0-9]:[0-9]{2}:[0-9]{2}" || echo "")

echo "=== astrbot 内置 agent 任务检查（窗口 ${WIN} 分钟） ==="
echo "agent 活动日志条数: ${ACTIVITY}"
echo "工具调用开始次数: ${START}"
echo "工具调用完成次数: ${DONE}"
[ -n "$LAST_TS" ] && echo "最近一条 agent 活动时间: ${LAST_TS}"

if [ "${ACTIVITY}" -eq 0 ]; then
  echo "状态: IDLE ✅  窗口内无 agent 任务活动，可安全重启"
  exit 0
fi

if [ "${START}" -gt "${DONE}" ]; then
  echo "状态: BUSY ⚠️  ${START} 次工具调用中有 $((START - DONE)) 次未返回结果，agent 正在执行任务，不建议重启！"
  echo "建议: 等工具调用全部返回（或任务结束）再重启。如需强制重启请确认可接受中断。"
  exit 1
fi

echo "状态: IDLE（窗口内有 agent 活动但均已返回结果）✅  可安全重启"
exit 0
