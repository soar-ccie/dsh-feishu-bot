---
name: feishu-bridge
description: 用户在「飞书 Bot」和「Web GUI」有两个独立对话入口，上下文互不相通。当用户提到「另一边」「飞书上」「界面上」「刚才说的」「之前聊的」等跨渠道指代，或需要查看/切换/继续某个项目、查询项目进度时，加载本技能。
whenToUse: 用户提到跨渠道（飞书/GUI）指代，或要查看项目列表、项目进度、切换/继续某个项目时。
---

# 飞书 ↔ DSH 桥接

用户有**两个独立**的对话入口，上下文不会自动同步：

| 入口 | 特点 |
|---|---|
| **飞书 Bot** | 手机上随手沟通；每条消息走一次性的 `dsh --profile headless` |
| **Web GUI** | 桌面上干重活；持久会话，一个会话 = 一个项目 |

用户会在两边接力聊**同一个项目**。你的职责：**主动把两边串起来，不要让用户重复说一遍。**

## 工具：fbctl

本技能自带工具，路径为 `<本技能目录>/scripts/fbctl`（把相对路径按技能基目录展开）。

```bash
FB=<本技能目录>/scripts/fbctl

$FB list                     # 列出全部项目（含序号、【当前】标记）
                             #   序号 1 永远是「默认」= 飞书自己的会话，不对应 GUI
                             #   序号 2 起才是 GUI 项目，顺序与 GUI 一致
$FB current                  # 当前项目
$FB switch <序号|默认|文件夹|标题>  # 切换当前项目
$FB show [项目]               # 项目摘要 + 记录条数
$FB gui [项目] [条数]         # 读该项目在 GUI 里的最近对话原文（默认 30 条）
$FB log "<内容>"              # 把用户说的话/进展记到当前项目
$FB summary "<文本>"          # 更新当前项目摘要
$FB sync                     # 从 DSH 同步项目列表（标题会随 DSH 变化）
$FB new "<名字>" [--session <会话ID>]  # 新建项目；带 --session 时直接绑定该 GUI 会话
$FB bind <会话ID> [--title "<名字>"]   # 把一个**已有**的 GUI 会话绑定成项目（兜底）
$FB stats / maintain         # 存储统计 / 滚动归档
```

### 新建一个"两边都有"的会话

`fbctl new` 只建飞书侧项目；要**同时在 GUI 里建会话**，用 dsh-web 插件的接口：

```bash
curl -sS -X POST http://127.0.0.1:8080/feishu/new-session \
  -H "Content-Type: application/json" \
  -d '{"title":"会话名","prompt":"（新会话已建立，主题：会话名。请只回复「已就绪」。）"}'
# 返回 202；插件是 fire-and-forget，不返回 ID
# 之后从 <数据目录>/../../storages/workspace.json 里找新的 webhook- 开头会话 ID
# 再用 `fbctl new "<名字>" --session <该ID>` 绑定
```

> 插件位置：`$DSH_HOME/plugins/dsh-feishu-session/feishu-session.mjs`
> （dsh-web 进程内插件，见 `docs/acp-investigation.md` 里为什么必须是插件）

## 行为要求

1. **用户出现跨渠道指代时**（"我刚才在飞书上说的"、"界面上那个"、"接着上次"）——
   **先去看另一边的记录，再回答**。不要回答"我不知道"、"这是新会话"。

2. **问项目进度时**：`fbctl show <项目>` 看摘要；摘要为空或过期时，
   用 `fbctl gui <项目>` 读 GUI 会话原文，自己总结后再用 `fbctl summary` 写回。

3. **用户提了新要求/有进展时**：用 `fbctl log "<内容>"` 记到对应项目，
   这样另一边也看得到。

4. **不要要求用户用固定词汇**。"我有哪些项目"、"安卓那个搞得怎样了"、
   "切到飞书项目" —— 这些都直接用 fbctl 实现，不要让用户打数字或背命令。

5. **记录以项目为单位**。用户没指明项目时，先 `fbctl current` 看当前项目；
   若明显在说别的项目，先 `fbctl switch` 再操作。

## 记录存在哪

```
<数据目录>/session/
├── _index.json                    项目索引 + 当前项目
├── notes.md                       长期记忆（跨会话/跨渠道有效的用户事实、偏好、暗号）
├── default.jsonl                  未绑定项目时的对话
└── <项目名>_<id>/
    ├── guisession.json            项目身份证（session_id / 标题 / 摘要）
    ├── chat_YYYYMMDD-NN.jsonl     飞书侧对话（热，滚满 5MB/2000 条自动分片）
    └── archive/<年>/              冷数据（文件名日期超 3 个月），默认不读
```

- **长期记忆（`notes.md`）**：用户说"记住…"、提到暗号、或表达长期偏好时，
  写进这里并**先读一遍**，避免和已有内容冲突。
- **热**：可读，注入上下文时取最近 30 条
- **冷**：默认不读；用户明确要"翻旧账"时才去 `archive/` 里找
- **GUI 会话**：DSH 自己管理，**只读不写**（fbctl 只解析、绝不修改）

## ⚠️ 已知架构约束（别再重复调研）

**"飞书和 GUI 合并成同一个会话"在 DSH 当前架构下做不到**，别再提这个方案：

1. **会话是独占写入的** —— 一个会话同时只能有一个进程持有写句柄。
   实测 `session/resume` 恢复 GUI 会话会报
   `already owned by an active write handle`（headless 会话则能恢复）。
2. **ACP profile 不含 `dsh-workspace` 插件** —— ACP 建的会话不登记 workspace，
   GUI 列表看不到（目录名是裸 UUID，没有 `session-` 前缀）。

**两个方向都是断的，配置绕不过去。** 完整证据见 `docs/acp-investigation.md`。

**能做到的天花板**就是本技能：两边按项目对齐 + 互相读对方的记录。

## GUI 侧栏的「未分组」是怎么来的、怎么治

飞书每来一条消息，Bot 都跑一次 `dsh --profile headless`，而 headless 会话
**不登记进任何 workspace**，于是全部堆进 GUI 侧栏那个没名字的「未分组」组。

**「未分组」整组消失的条件**（客户端 `dsh-client-ui-workspace` 的
`groupByWorkspace()`：只有 `stray.length > 0` 才 push 那个无标题分组）：
把散客全部**归档**即可 —— 不是变成空组，是根本不出现。

- `fbctl gui-status` 看现状（多少散客、多少是 Bot 调用）
- `fbctl gui-hide` 把 Bot 产生的会话归档（**只归档、绝不删除**，
  会话文件原样留在磁盘；已登记进 workspace 的项目会话一律不碰）
- `fbctl gui-restore [ID...]` 取消隐藏

Bot 每次消息处理完会自动跑一次 `gui-hide`（`FB_HIDE_HEADLESS=0` 可关掉），
所以侧栏会一直保持干净。识别靠提示词指纹 `【飞书 Bot 发起】`
（`ask_agent()` 统一加在开头），历史会话靠两个旧的提示词开头兜底。

> ⚠️ **归档 ≠ 删除**。用户明确要求会话永久保留，
> 所以任何"清理"都只能是归档，且必须走 dsh-web 插件的
> `POST /feishu/new-session {"action":"archive"}`（外部改
> `storages/workspace.json` 无效：dsh-web 启动时读进内存，之后不看文件）。

## 归档的会话，飞书侧同样不显示

DSH 的归档语义：只往 `storages/workspace.json` 的 `global.archivedSessionIds`
记一笔，**文件不动、workspace 成员关系不动**，但 GUI 里所有视图都不显示
（分组 / 单列表 / 搜索都过滤）。飞书侧保持同样的观感：

| 命令 | 对一个已归档会话所绑定的项目 |
|---|---|
| `fbctl list` | 不显示（序号跟着重排），末尾提示还有几个已归档 |
| `fbctl list --all` | 显示，标 `🗄` |
| `fbctl switch` / 按序号 | 拒绝，并告诉你怎么恢复 |
| `fbctl current` | 照实显示，但加一行 ⚠️ 说明它已归档 |
| `fbctl show` | 允许（显式查询），标注 `🗄 状态` |
| `fbctl gui` | 拒绝 |
| `fbctl context` | **不再注入 GUI 侧内容**，只保留飞书侧记录，并加一行说明 |
| `fbctl sync` | 不为已归档的会话建项目；也不更新它的标题 |

判定只看一个字段：项目的 `session_id` 在不在 `archivedSessionIds` 里。
**只影响展示，不动数据** —— `_index.json` 的记录、`session/<项目>/` 下的
`chat_*.jsonl` 全部原样保留，取消归档就自动恢复显示。

恢复方式（GUI 里没有取消归档的入口，只有这一条路）：
在 GUI 里归档的就用 `fbctl gui-restore <session_id>` 放回来。

> **关于"删除会话"**：DSH **没有**这个功能（GUI 里对会话只有归档），
> 想在登记表里彻底摘掉一条会话只能走注册表的 `detachSession`。
> 用户已确认**不需要**这个能力，所以插件里那个 `detach-session` 动作
> **默认停用**（`cordis.patch.yml` 里 `enableDetach: false`，调用返回 403），
> 代码保留备用。**不要再自己去给会话做删除/摘除。**

## 注意

- 读 DSH 会话依赖其内部文件格式（当前 `session.v3.jsonl.zstd`）。
  `fbctl` 已做了宽松解析 + 自动取最高版本；**将来 DSH 升级若格式变化，
  只需修改 `fbctl` 的 `read_gui_messages()` / `session_title()` 一处**。
- 读不到时**降级**：用 `_index.json` 和 `guisession.json` 的缓存信息回答，
  不要报错卡住。
