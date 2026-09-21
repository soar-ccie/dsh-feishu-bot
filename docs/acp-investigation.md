# ACP 方案调研记录（结论：不可行）

> 目的：避免以后重复走这条弯路。**结论是"不做"，证据如下。**

## 背景需求

用户希望：**在飞书里继续操作 Web GUI 的项目会话**，两个入口共用一个会话，不再割裂。

候选方案：把飞书 Bot 从 `dsh --profile headless`（一次性）改为
`dsh --profile acp`（Agent Client Protocol，**持久会话、支持 resume**）。

## 实验环境

- DSH：0.1.5-rc.1，`DSH_HOME=<DSH 数据目录>`
- 测试会话：`<session-id>`（GUI 建的「测试ACP会话」，已登记 workspace）
- 对照会话：`<session-id-2>`（headless 建的，未登记）
- 客户端：手写最小 ACP 客户端（换行分帧 JSON-RPC over stdio）

## 实验结果

### 1. ACP 可用，零安装

```
$ dsh --profile acp --help
Usage: dsh --profile acp [options]
Serve automation clients over Agent Client Protocol stdio.
```

跑一次 `--help` 就自动创建了 `profiles/acp`，**不需要安装任何东西**。

`initialize` 返回的能力：

```json
{"sessionCapabilities": {"close": {}, "list": {}, "resume": {}},
 "promptCapabilities": {"image": false, "audio": false, "embeddedContext": false}}
```

### 2. `session/list` 能看到所有持久会话

返回 **28 个**会话 —— 比 GUI 列表多得多（GUI 只有 workspace 里登记的 3 个）。
也就是说 ACP 能看到 GUI 会话和 headless 会话。

### 3. ❌ `session/resume` 恢复 GUI 会话失败

```
{"error": {"code": -32603, "message": "Internal error",
  "data": {"details": "session \"<session-id>\" is already owned by an active write handle"}}}
```

### 4. ✅ 对照：headless（未登记）会话可以 resume

| 会话类型 | `session/resume` |
|---|---|
| 未登记的 headless 会话 | ✅ 成功 |
| **workspace 登记的 GUI 会话** | ❌ `already owned by an active write handle` |

**说明锁是"GUI 登记 + dsh-web 持有写句柄"造成的，不是 ACP 本身的问题。**

### 5. ❌ ACP 建的会话，GUI 看不到

`session/new` 成功（例：`<session-id-3>`），
但：

- 目录名**没有 `session-` 前缀**（GUI 会话是 `session-<uuid>`，ACP 是裸 `<uuid>`）
- **不在 `workspace.json` 的 `sessionIds` 里** → GUI 列表不显示

## 根因

对比两个 profile 的插件树：

| | `acp` | `web` |
|---|---|---|
| 插件总数 | **86** | 152 |
| `@deepseek-ai/dsh-workspace` | ❌ 无 | ✅ 有 |
| `@deepseek-ai/dsh-api-workspace-controller` | ❌ 无 | ✅ 有 |
| `@deepseek-ai/dsh-api-workspace-files` | ❌ 无 | ✅ 有 |

两个硬约束：

1. **会话独占写入** —— 一个会话同时只能有一个进程持有写句柄。
   GUI 开着 → ACP 拿不到；ACP 持有 → GUI 也拿不到。
2. **ACP profile 不含 workspace 插件** —— ACP 建的会话不登记，GUI 列表看不到。

**两个方向都是断的，且都不是配置能绕过的**（第 1 条是运行时约束）。

## 决策

**不采用 ACP。** 理由是它对用户的核心目标（合并）没有任何帮助，
而且会**恶化**现状：

| 目标 | 现有方案（headless + 项目文件夹 + skill） | 改用 ACP |
|---|---|---|
| 飞书对话连续 | 🟡 拼最近 30 条（够用） | ✅ 真持久 |
| **GUI 能看到飞书内容** | ✅ 能（skill/fbctl 去读） | ❌ **看不到** |
| 飞书接进 GUI 会话 | ❌ | ❌（独占写入挡住） |

ACP 唯一的收益（真持久）换来的是"GUI 完全看不到飞书内容" —— 正好背离目标。

## 现有方案（保留）

- 飞书 = 移动端入口，**按项目绑定** GUI 会话（`fbctl`）
- 两边通过 `skill`（feishu-bridge）+ `fbctl` **互相读对方的记录**
- 不是一个会话，但两边都知道对方存在、能读到内容

## 如果将来想再试

先验证这两条，任何一条不成立就不必继续：

1. DSH 是否放宽了会话的独占写入（多客户端共用一个会话）
2. `acp` profile 是否开始挂载 `dsh-workspace`（使 ACP 会话在 GUI 可见）

验证命令（不碰任何现有会话）：

```bash
# 1) 起 ACP 服务
dsh --profile acp            # 然后发 initialize + session/resume <某个GUI会话>
# 期望:成功而不是 "already owned by an active write handle"

# 2) 建一个会话后看是否登记
dsh --profile acp            # 发 session/new
grep -c "<新sessionId>" ~/software/deepseek_harness_agent/user_data/storages/workspace.json
# 期望:>=1
```
