#!/usr/bin/env bash
# ══════════════════════════════════════════════════════════════
#  dsh-feishu-bot 一键安装
#
#  它做这几件事（每步都会打印在做什么）：
#    1. 建 venv 并装依赖
#    2. 没有 .env 就从 .env.example 复制一份
#    3. 把 DSH 插件拷进 $DSH_HOME/plugins/dsh-feishu-session/
#    4. 往 web profile 的 cordis.patch.yml 追加挂载配置（改前先备份）
#    5. 把技能软链到 $DSH_HOME/skills/feishu-bridge
#    6. 生成 systemd 单元到 ./feishu_bot.service（不自动装，那步要 sudo）
#
#  幂等：重复跑不会重复追加、不会覆盖已有 .env。
# ══════════════════════════════════════════════════════════════
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOME_DIR="${HOME:-$(cd ~ && pwd)}"
USER_NAME="$(id -un)"
GROUP_NAME="$(id -gn)"

say()  { printf '\n\033[1;36m▶ %s\033[0m\n' "$*"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$*"; }
die()  { printf '\n\033[31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

# ── 0. 环境检查 ───────────────────────────────────────────────
say "0/6 环境检查"
command -v python3 >/dev/null 2>&1 || die "找不到 python3（需要 3.9+）"
ok "python3 $(python3 -c 'import sys;print("%d.%d.%d"%sys.version_info[:3])')"

DSH_HOME="${DSH_HOME:-}"
if [ -z "$DSH_HOME" ]; then
  for c in "$HOME_DIR/software/deepseek_harness_agent/user_data" "$HOME_DIR/.dsh"; do
    if [ -d "$c" ]; then DSH_HOME="$c"; break; fi
  done
fi
if [ -z "$DSH_HOME" ] || [ ! -d "$DSH_HOME" ]; then
  die "找不到 DSH 数据目录。先装 DSH，或 export DSH_HOME=<...>/user_data"
fi
ok "DSH_HOME = $DSH_HOME"

WEB_PATCH="$DSH_HOME/profiles/web/cordis.patch.yml"
ok "仓库路径 = $REPO"

# ── 1. venv ──────────────────────────────────────────────────
say "1/6 创建虚拟环境并安装依赖"
if [ -d "$REPO/venv" ]; then
  ok "venv/ 已存在，跳过创建"
else
  python3 -m venv "$REPO/venv"
  ok "已创建 venv/"
fi
"$REPO/venv/bin/pip" install --quiet --upgrade pip
"$REPO/venv/bin/pip" install --quiet -r "$REPO/requirements.txt"
ok "依赖安装完成"

# ── 2. .env ──────────────────────────────────────────────────
say "2/6 准备 .env"
if [ -f "$REPO/.env" ]; then
  ok ".env 已存在，保持原样"
else
  cp "$REPO/.env.example" "$REPO/.env"
  chmod 600 "$REPO/.env"
  warn "已生成 .env —— 还需要填 FEISHU_APP_ID / FEISHU_APP_SECRET"
fi

# ── 3. DSH 插件 ──────────────────────────────────────────────
say "3/6 安装 DSH 插件"
PLUG_DIR="$DSH_HOME/plugins/dsh-feishu-session"
mkdir -p "$PLUG_DIR"
cp -f "$REPO/dsh-plugin/feishu-session.mjs" "$PLUG_DIR/feishu-session.mjs"
PLUG_ABS="$PLUG_DIR/feishu-session.mjs"
ok "$PLUG_ABS"

# ── 4. 挂载到 web profile ────────────────────────────────────
say "4/6 挂载插件到 web profile"
NEED_DSH_RESTART=0
if [ ! -f "$WEB_PATCH" ]; then
  warn "找不到 $WEB_PATCH"
  warn "先跑一次 dsh web 让它生成，然后重新执行本脚本"
elif grep -q "feishu-session" "$WEB_PATCH"; then
  ok "已经挂载过了，跳过"
else
  BACKUP="$WEB_PATCH.bak.$(date +%Y%m%d-%H%M%S)"
  cp "$WEB_PATCH" "$BACKUP"
  cat >> "$WEB_PATCH" <<YAML

# ══════════════════════════════════════════════════════════════
# dsh-feishu-bot：让飞书 Bot 能在 GUI 里创建/归档会话
# 由 install.sh 于 $(date '+%Y-%m-%d %H:%M:%S') 追加；卸载就删掉下面两段
# ══════════════════════════════════════════════════════════════
- insert:
    - id: webhook
      name: '@deepseek-ai/dsh-webhook'

- insert:
    - id: feishu-session
      name: $PLUG_ABS
      config:
        path: /feishu/new-session
        workspacePath: $HOME_DIR
        agentPreset: standard
        permissionPreset: workspace-write
        enableDetach: false
YAML
  ok "已追加挂载配置（原文件备份：$BACKUP）"
  NEED_DSH_RESTART=1
fi

# ── 5. 技能软链 ──────────────────────────────────────────────
say "5/6 链接技能到 DSH"
mkdir -p "$DSH_HOME/skills"
ln -sfn "$REPO/plugins/feishu-bridge" "$DSH_HOME/skills/feishu-bridge"
ok "$DSH_HOME/skills/feishu-bridge -> $REPO/plugins/feishu-bridge"

# ── 6. systemd 单元 ──────────────────────────────────────────
say "6/6 生成 systemd 单元"
UNIT_SRC="$REPO/systemd/feishu_bot.service.example"
UNIT_OUT="$REPO/feishu_bot.service"
sed -e "s|^User=.*|User=$USER_NAME|" \
    -e "s|^Group=.*|Group=$GROUP_NAME|" \
    -e "s|^WorkingDirectory=.*|WorkingDirectory=$REPO|" \
    -e "s|^ExecStart=.*|ExecStart=$REPO/venv/bin/python3 $REPO/feishu_bot.py|" \
    -e "s|^Environment=\"HOME=.*|Environment=\"HOME=$HOME_DIR\"|" \
    -e "s|^Environment=\"USER=.*|Environment=\"USER=$USER_NAME\"|" \
    -e "s|^Environment=\"DSH_HOME=.*|Environment=\"DSH_HOME=$DSH_HOME\"|" \
    "$UNIT_SRC" > "$UNIT_OUT"
# 凭据默认交给 .env 提供；如果写成 systemd 环境变量，它会盖住 .env。
sed -i -E 's|^Environment="FEISHU_|#Environment="FEISHU_|' "$UNIT_OUT"
chmod 600 "$UNIT_OUT"
ok "已生成 $UNIT_OUT（凭据那 4 行默认注释掉，走 .env）"

# ── 收尾提示 ─────────────────────────────────────────────────
cat <<EOF

  ┌────────────────────────────────────────────────────────────┐
  │  还剩 3 步，需要你自己动手                                 │
  └────────────────────────────────────────────────────────────┘

  1) 填飞书应用凭据
       nano $REPO/.env

  2) 装成系统服务
       sudo cp $UNIT_OUT /etc/systemd/system/feishu_bot.service
       sudo systemctl daemon-reload
       sudo systemctl enable --now feishu_bot

  3) 让 DSH 加载插件
EOF
if [ "$NEED_DSH_RESTART" = "1" ]; then
  printf '       sudo systemctl restart dsh-web\n'
else
  printf '       （本次没改 profile，若插件刚更新过才需要）\n'
fi
cat <<EOF

  看日志：   tail -f $REPO/logs/feishu_bot.log
  健康检查： $REPO/plugins/feishu-bridge/scripts/fbctl gui-status

  前置条件（没做过的话先做）：飞书那边要建自建应用、开权限、开长连接事件订阅，
  见 docs/feishu-app-setup.md
EOF
