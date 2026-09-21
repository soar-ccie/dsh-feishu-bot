# 设计说明

这份文档解释**为什么是这样**，而不是怎么用。里面很多结论是踩坑换来的，代码里也留了对应的"教训注释"。

---

## 1. 要解决的问题

DSH 有两个入口：

- **Web GUI**（浏览器）—— 桌面端工作台，一个会话就是一个项目
- **飞书**（手机）—— 想让 DSH 随时随地可用

问题是这两个入口**上下文不通**：飞书那边发的消息，GUI 里的会话不知道；GUI 里聊到一半，出门在手机上接不上。

理想方案是"两个入口共用一个会话"。**这条路走不通**，证据见 [acp-investigation.md](acp-investigation.md)：

1. **会话独占写入** —— 一个会话同时只能有一个进程持有写句柄。GUI 开着时，别的进程 `session/resume` 会得到
   `already owned by an active write handle`。这是运行时约束，不是配置能绕过的。
2. **ACP profile 不含 workspace 插件** —— 就算用 ACP 建会话，它也不登记进 workspace，GUI 列表里看不到。

所以退一步：**两边不是同一个会话，但按"项目"对齐，并且互相读得到对方最近说了什么。**

---

## 2. 整体结构

```
飞书 ──(长连接)──> feishu_bot.py ──(子进程)──> dsh --profile headless
                        │                              │
                        │                              ▼
                        │                      DSH 会话（一次性）
                        │
                        ├── fbctl ──> session/<项目>/chat_*.jsonl   飞书侧记录
                        │        └──> 读 DSH 会话文件（只读）         GUI 侧记录
                        │
                        └──(HTTP)──> dsh-plugin ──> workspace registry（建/归档会话）
```

### 为什么用 `headless` 而不是常驻进程

`dsh --profile headless "任务"` 是**一次性**的：起进程 → 建一个新会话 → 回答 → 退出。

- 好处：不用管常驻进程的状态、并发、内存；
- 代价：**它不记得上一轮**。所以"记忆"是 `fbctl` 拼出来的上下文（见第 3 节）。

会话本身仍然会落盘（`sessions/`），是完整记录，**永远不删**。

---

## 3. "项目"是这套东西的核心

`fbctl` 维护一份 `session/_index.json`：

```json
{"session_id": "session-<uuid>", "title": "安卓开发",
 "folder": "安卓开发_12345", "last_active": "..."}
```

- **飞书侧记录**：`session/<folder>/chat_YYYYMMDD-NN.jsonl`，每次对话追加一条；
- **绑定关系**：`session_id` 指向 GUI 里那个 DSH 会话；
- **上下文注入**：每轮把"项目摘要 + GUI 侧最近 N 条 + 飞书侧最近 M 条"拼进提示词。

于是两边都能"看到对方"。切换项目 = 换一个 `folder` 和绑定的 `session_id`。

### 为什么 `fbctl` 只用标准库

它要读 DSH 的**私有**会话格式（`session.v3.jsonl.zstd`），是最容易随 DSH 升级而失效的一块。把它集中在一个纯标准库脚本里，**坏了只改一处**，也不用担心依赖冲突。

---

## 4. 为什么需要那个 DSH 插件

外部程序**没法**在 GUI 的会话列表里创建或归档会话：

| 尝试 | 结果 |
|---|---|
| 直接改 `storages/workspace.json` | ❌ 无效 —— dsh-web 启动时读进内存，之后不看文件 |
| ACP 建会话 | ❌ 不登记 workspace，GUI 看不到 |
| dsh-web 的 HTTP 接口 | ❌ 没有公开的"建会话/归档会话"接口 |

唯一可行的路是**在 dsh-web 进程里挂一个插件**，于是有了 `dsh-plugin/feishu-session.mjs`：一个零依赖的单文件插件，暴露 `POST /feishu/new-session`，支持：

```
{"title","prompt"}                        建会话（登记进 workspace → GUI 立刻可见）
{"action":"list"}                         查现状
{"action":"archive","sessionId":S}        归档（不带 force 时拒绝已登记的项目会话）
{"action":"archive","sessionId":S,"force":true}   显式归档项目会话（飞书「归档会话」用）
{"action":"unarchive","sessionId":S}      取消归档
{"action":"archive-strays","sessionIds":[…]}      只归档未登记的散客
{"action":"detach-session",…}             默认停用（见下）
```

### 两个坑

1. **插件必须 `inject: ['webServer','webhookRuntime','workspaceRegistry']`**。
   少了 `workspaceRegistry`，`ctx.workspaceRegistry` 是 `undefined`，归档接口直接 HTTP 500。
2. **插件代码改动不会热重载**（ESM 模块缓存）—— 改完文件名、`touch` 配置文件、等几分钟都没用，**必须重启 dsh-web**。

### 为什么"归档"要加 `force` 保护

DSH **没有删除会话的功能**（GUI 里对会话只有"归档"）。归档之后 GUI 里就找不到入口把它弄回来了（没有"已归档"分组、也没有取消归档的菜单）。

所以插件的归档接口默认**拒绝**操作已登记进 workspace 的会话 —— 防止自动清理误伤项目会话。只有用户在飞书里显式发 `归档会话` 并选数字时，才带 `force=true` 绕开这道保护。

`detach-session`（把会话从 workspace 成员表里摘掉）是唯一能做到"真正删除"的动作，但**代码保留、默认不加载**（配置里 `enableDetach: false`）。

---

## 5. "归档"到底是什么

对 DSH 来说，归档 = 往 `storages/workspace.json` 的 `global.archivedSessionIds` 里记一笔：

- **文件不动、workspace 成员关系不动**（所以取消归档能回到原位）；
- 客户端三处派生**一律排除归档会话**：分组视图、单列表视图、搜索 —— 搜都搜不出来；
- 客户端**没有任何入口能看到已归档的会话**。

客户端 `dsh-client-ui-workspace` 里有个细节：那个「未分组」分组**只有散客数量 > 0 时才渲染**。

```js
const stray = list.ids.map(...).filter(s => ... && sessionVisible(s, list.current, archived));
if (stray.length > 0) groups.push(buildGroup("", ..., "" /* 无标题 */, stray, ...));
```

所以把散客全部归档之后，**「未分组」整组会直接消失**，而不是变成一个空组。Bot 每次处理完消息会自动做这件事（`fbctl gui-hide`）。

> 想改「未分组」这个名字是做不到的：那是内置客户端插件的私有 locale 键，而 DSH 的本地化注册 API 对同一个 namespace + 同一语言**重复注册直接抛错**，外部插件覆盖不了。完整证据见 [gui-ungrouped.md](gui-ungrouped.md)。

### 飞书侧也对齐

`fbctl` 的 `project_hidden()` 判定一个项目的 `session_id` 在不在 `archivedSessionIds` 里；在的话，`list` / `switch` / `gui` / `context` 全都当它不存在。**观感和 GUI 一致**，但数据一行不动，取消归档就恢复。

---

## 6. 指令匹配：为什么全是字面匹配

见 README 的"为什么全是字面匹配"一节。核心一句：**宁可漏判（交给模型，最多慢几秒），也不要误判（去执行破坏性动作）。**

三个档位：

| 档位 | 用在哪 | 归一化 |
|---|---|---|
| **字面相等** | `查看会话` `归档会话` `查看归档` `重启DSH` `启动DSH` `重启飞书` | 只 `strip()` 首尾空白 |
| **前缀 + 后缀合法性** | `新建会话 <名字>`（7 个前缀）、`发文件 <路径>` | 后缀过 `_looks_like_name()` / `_looks_like_path()` |
| **纯数字** | 切项目 / 归档目标 / 恢复目标 | 额外剥首尾标点 |

两段式指令（`归档会话` → 数字、`查看归档` → 数字）用**带超时的待定状态**记住"你刚发了什么"：3 分钟内有效，发 `查看会话` 取消，用掉就清空。过期状态自动失效，不会以后随手一个数字就触发。

### 一个 Python 冷知识

判序号**不能用 `str.isdigit()`**：`'²'`、`'①②③'` 的 `isdigit()` 都是 `True`，但 `int()` 会抛 `ValueError`。项目里改成了 `try: int(k) except ValueError`。

---

## 7. 图片为什么要"等 8 秒"

飞书把"图片 + 文字"拆成**两条独立消息**发的（至少手机端是这样）。老逻辑里图片会立刻被一轮孤立的"描述这张图"处理掉，紧随其后的文字只能看到那段描述文本、**看不到图本身** —— 于是出现"我发截图是有目的的，你却复述了一遍画面"。

现在的做法：

```
收到纯图片
  ├─ 8 秒内来了文字 → 把图片接到那条文字上，图文合成一轮，只回一次
  └─ 8 秒内没文字   → 带上完整项目上下文，让模型"先判断你想干什么，再带着目的看图"
```

窗口是 `FB_IMAGE_MERGE_SEC`，设 0 关闭合并。

顺带还有个隐藏坑：`build_prompt()` 里原本写着"**不要读取文件**"，而看图恰恰要读文件 —— 图文混排那条路上模型会被自己的规则挡住。现在用 `with_image=True` 把这句话放宽成"除了下面给出的图片文件，不要读其它文件"。

---

## 8. 还有一些小设计

- **提示词指纹**：`ask_agent()` 在每条 headless 提示词开头统一加一行 `【飞书 Bot 发起】`。这是识别"哪些会话是 Bot 自己产生的"的唯一可靠办法（文件名、cwd 都区分不出来），配合 `_gui_hygiene.json` 缓存，让自动保洁只归档 Bot 自己的会话。
- **富文本顶层 `files[]`**：飞书把"一次发多个文件"投递成 `post` + 顶层 `files[]`，此时 `content`/`content_v2` 全是空的。**官方文档里没有这个字段**，是真实消息暴露出来的 —— 不处理的话整批文件会静默蒸发。
- **下载先落 `temp/` 再搬进 `file/`**：万一进程被杀，残留只在 `temp/` 里，正式目录永远不会出现半截文件。
- **单线程池处理消息**：`>100MB` 的分片下载可能跑几分钟，占住 asyncio 事件循环会让 WebSocket 心跳发不出去、被服务端掐断。单线程池顺带避免了记忆文件的读写竞争。
- **全部可执行的"教训注释"**：代码里保留了大量"以前这么写，结果出了什么事"的注释。删代码前建议读一遍。
