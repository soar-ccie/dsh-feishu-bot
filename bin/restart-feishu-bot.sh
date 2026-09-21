#!/bin/bash
# ============================================================
# 重启飞书 Bot
#
# 用法:
#   bash restart-feishu-bot.sh          # 重启
#   bash restart-feishu-bot.sh --status # 只看状态,不重启
#
# 两种重启路径:
#   1) systemd  -- 有免密 sudo 时优先用(优雅停止再启动)
#   2) pkill    -- 无免密 sudo 时退回:杀掉进程,靠 systemd 的
#                  Restart=always 自动拉起(不需要任何权限)
#
# 说明:本脚本被"重启飞书"指令调用时,是以独立会话(新进程组)
#       启动的,所以杀掉 Bot 进程不会连带中断脚本自己。
# ============================================================
set -uo pipefail

SERVICE=feishu_bot
# 注意:模式里用 feishu_bot.py(下划线),不要用本脚本名,否则会误杀自己
PATTERN=feishu_bot.py
WAIT_SECONDS=90

status() {
    local act
    act=$(systemctl is-active "$SERVICE" 2>/dev/null || true)
    echo "服务: ${act:-unknown}"
    if [ "$act" = "active" ]; then
        echo "启动时间: $(systemctl show "$SERVICE" -p ActiveEnterTimestamp --value 2>/dev/null)"
        echo "累计重启: $(systemctl show "$SERVICE" -p NRestarts --value 2>/dev/null)"
    fi
}

if [ "${1:-}" = "--status" ]; then
    status
    exit 0
fi

echo "=============================================="
echo "  重启飞书 Bot"
echo "=============================================="
BEFORE_RESTARTS=$(systemctl show "$SERVICE" -p NRestarts --value 2>/dev/null || echo "?")

if systemctl is-active --quiet "$SERVICE" 2>/dev/null; then
    if sudo -n systemctl restart "$SERVICE" 2>/dev/null; then
        echo "[1/2] 已通过 systemd 重启(优雅停止)"
        MODE=systemd
    else
        echo "[1/2] 无免密 sudo,改用 pkill + Restart=always"
        pkill -f "$PATTERN" 2>/dev/null || true
        MODE=pkill
    fi
else
    echo "[1/2] 服务未在运行,直接拉起"
    if ! sudo -n systemctl start "$SERVICE" 2>/dev/null; then
        pkill -f "$PATTERN" 2>/dev/null || true
    fi
    MODE=pkill
fi

echo "[2/2] 等待服务就绪(最多 ${WAIT_SECONDS}s)..."
for _ in $(seq 1 $WAIT_SECONDS); do
    if systemctl is-active --quiet "$SERVICE" 2>/dev/null; then
        sleep 1
        if systemctl is-active --quiet "$SERVICE" 2>/dev/null; then
            echo ""
            echo "✅ 飞书 Bot 已重启 (方式: $MODE)"
            echo "   重启前累计: $BEFORE_RESTARTS"
            status
            exit 0
        fi
    fi
    sleep 1
done

echo ""
echo "❌ 等待超时(>${WAIT_SECONDS}s)，服务仍是: $(systemctl is-active "$SERVICE" 2>/dev/null || echo unknown)"
echo "   排查: systemctl status $SERVICE --no-pager"
echo "         tail -50 \$(dirname "$0")/../logs/feishu_bot.log"
exit 1
