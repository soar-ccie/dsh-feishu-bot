# DSH 升级兼容性记录

本项目用到 DSH 的两处**内部**东西，是升级时最容易断的地方：

1. **dsh-web 插件接口** —— `ctx.webServer` / `ctx.webhookRuntime` / `ctx.workspaceRegistry`
2. **会话文件格式** —— `sessions/<workspace>/<id>/session.v?.jsonl.zstd`（`fbctl` 靠它读 GUI 侧对话）

所以每次 DSH 升级后，建议照下面这份清单跑一遍。

---

## 0.1.5-rc.1 → 0.2.0-rc.2（已实测，无需改代码 ✅）

### 实际变化

| 项 | 0.1.5 | 0.2.0 |
|---|---|---|
| 会话文件 | `session.v3.jsonl.zstd` | **`session.v4.jsonl.zstd`**（旧会话仍是 v3） |
| headless 输出 | 推理可能混进 stdout | **推理走 stderr**，stdout 只有正文 ✅ |
| `workspace.json` | `global.archivedSessionIds` / `workspaceIds` | **完全一样** |
| `start-dsh-web.sh` | 支持 `--restart` | 一样 |
| 插件挂载 | `profiles/web/cordis.patch.yml` | 一样（升级会重写这个文件，但自定义段保留） |

### 为什么没断

- `fbctl._session_file()` 从第一天就是「**取版本号最高的** `session*.jsonl.zstd`」，
  所以 v4 一出现就自动被选中，不需要改。
- v3 和 v4 的 **JSONL 行结构完全一致**（`data` / `seq` / `time` / `type`，
  部分行多 `surfaceOp` / `sourceEventSeqs`；首行都是会话元信息）。
  `fbctl` 本来就是宽松解析，所以直接能读。

### 实测清单（全部通过）

```bash
DSH=/path/to/deepseek_harness_agent
U=$DSH/user_data
FB=/path/to/feishu-bot
FBCTL="$FB/plugins/feishu-bridge/scripts/fbctl"
```

| # | 检查 | 命令 / 方式 |
|---|---|---|
| 1 | `headless` profile 还在 | `$DSH/bin/dsh --profile headless "只回复两个字：正常"` |
| 2 | **用 Bot 的真实 PATH 也能跑** | 见下方"PATH 要一起测" |
| 3 | stdout / stderr 分离 | 上面那条命令，看 stdout 是否只有正文 |
| 4 | 插件在 dsh-web 里活着 | `curl -X POST 127.0.0.1:8080/feishu/new-session -d '{"action":"list"}'` |
| 5 | 插件能**建**会话 | 同上 `-d '{"title":"自测","prompt":"请只回复：已就绪"}'` → 202 |
| 6 | 插件能**归档**（含 force） | 对已归档会话发 `{"action":"archive","sessionId":X,"force":true}` → 应回 `skippedArchived` 且**零副作用** |
| 7 | Bot 调用指纹识别 | `session_is_bot_call(<带【飞书 Bot 发起】的会话>)` → `True` |
| 8 | `fbctl list` / `current` / `show` | 直接跑 |
| 9 | `fbctl gui`（**解析会话文件**） | `$FBCTL gui <项目> 5` → 应读出真实对话 |
| 10 | `fbctl context` | 直接跑（Bot 每条消息都走这条） |
| 11 | `fbctl sync` | 直接跑（会建项目，先备份 `session/_index.json`） |
| 12 | `fbctl gui-status` / `gui-hide` | 直接跑 |
| 13 | `fbctl archived-list` / `archived-restore` | 直接跑 |
| 14 | `workspace.json` 结构 | 看有没有 `global.archivedSessionIds` |
| 15 | `start-dsh-web.sh` 还在、还认 `--restart` | **只读脚本，别执行**（`--restart` 会杀掉 GUI 会话） |
| 16 | 端口在监听 | `socket.connect_ex(("127.0.0.1", 8080)) == 0` |

### PATH 要一起测

Bot 给 dsh 传的是自己拼的 `TOOLCHAIN_PATH`（nvm 里的 node + JDK + Android SDK + `~/shell/bin`），
**不是**登录 shell 的 PATH。所以排查时要用同样的 PATH：

```bash
NODE_BIN=$(ls -d ~/software/nvm/versions/node/*/bin | sort -V | tail -1)
env -i HOME=$HOME USER=$(id -un) DSH_HOME=$U \
  PATH="$NODE_BIN:$DSH/bin:$HOME/shell/bin:/usr/local/bin:/usr/bin:/bin" \
  $DSH/bin/dsh --profile headless "只回复三个字：可用"
```

### 顺带发现的行为变化（不是故障）

- `dsh --profile headless` 产生的会话**仍然不登记进 workspace** → 依旧会堆进 GUI 的「未分组」，
  Bot 的 `gui-hide` 依旧负责清理（用提示词指纹 `【飞书 Bot 发起】` 识别）。
- `fbctl sync` 会给**所有**已登记但还没绑项目的 GUI 会话建项目 —— 包括你手建的。
  这是设计行为，看到多出一个项目先别慌，去 GUI 里认一下是不是自己建的。

---

## 下次升级的排查顺序

1. **先看 Bot 还回不回复** —— 不回就 `tail -f logs/feishu_bot.log`，看 `DSH 返回码` / `stdout 长度`。
2. **`fbctl gui` 读不出内容** → 会话格式变了。只改两个函数：
   `fbctl` 的 `read_gui_messages()` 和 `session_title()`（这是当初把解析集中在一处的目的）。
3. **插件接口 404 / 500** → 插件没被加载或内部接口改名了。
   先确认 `profiles/web/cordis.patch.yml` 里那段还在（升级会重写这个文件，理论上保留自定义段），
   然后 `sudo systemctl restart dsh-web`（**插件代码改动不会热重载**）。
   历史坑：`inject` 里少 `workspaceRegistry` 会让归档接口直接 500。
4. **`启动DSH` / `重启DSH` 报错** → 看 `start-dsh-web.sh` 是否改名、是否还认 `--restart`，
   以及它打印的 `?token=` 行格式有没有变（`feishu_bot.py` 用正则从输出里捞局域网地址）。
