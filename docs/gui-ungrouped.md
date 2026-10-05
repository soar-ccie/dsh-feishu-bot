# GUI 侧栏「未分组」调研记录

> 结论：**改名字做不到；让它整组消失可以做**，做法是"归档"散客会话。
> 全程不删任何文件。

## 1. 需求

用户要求二选一：

1. 把「未分组」改成一个有表示性的名字
2. 在 GUI 里把它隐藏

## 2. 「未分组」是什么

`dsh-client-ui-workspace/lib/client.js`：

```js
"group.ungrouped": "未分组"

function groupByWorkspace(list, workspaces, archived, ungroupedOrder) {
    ...
    const stray = list.ids.map(id => list.byId[id])
        .filter(s => s && !accounted.has(s.id) && sessionVisible(s, list.current, archived));
    if (stray.length > 0) groups.push(buildGroup("", ..., "" /* 无标题 */, stray, ...));
    return groups;
}
```

- 它不是一个真正的 workspace，而是"**不在任何 workspace 里的会话**"的兜底分组。
- **关键**：只有 `stray.length > 0` 时才 push。散客清空 → 这一组**根本不渲染**
  （不是变成空组，是消失）。
- 该组没有 title，显示名直接取自 locale 的 `group.ungrouped`。

## 3. 为什么"改名"做不到（干净地）

DSH 的客户端本地化是 `dsh-client-locale` 提供的，插件通过字典注册：

```js
register(ns, localeOrDicts, dict) {
    ...
    for (const [locale] of pairs)
        if (locales.has(localeKey(locale)))
            throw new Error(`locale namespace "${ns}" already has locale "${locale}"`);
    ...
}
```

- `"未分组"` 属于 **`dsh-client-ui-workspace` 这个内置客户端插件**的私有命名空间。
- 注册 API 对**同一 namespace + 同一语言重复注册直接抛错**，外部插件无法覆盖。
- 客户端插件还需要前端构建产物，不是一个文件能搞定的。
- 唯一可行路径是改打包好的 `lib/client.js` —— **升级即丢**，不可接受。

**所以「改名」放弃。**

## 4. 「隐藏」怎么做：归档

`dsh-workspace/lib/index.js` 的注册表有一份**全局归档集合**：

```js
/** The registry-global archive set: sessions hidden from every grouping
 *  surface. Archiving never touches workspace accounting — an archived
 *  session keeps its `sessionIds` slot so unarchiving restores its position. */
get archivedSessionIds() { ... }
archiveSession(sessionId) { ... }   // 落盘到 storages/workspace.json 的 global.archivedSessionIds
```

客户端三处派生**一律排除归档会话**：

- `deriveGroups()` → 分组视图
- `deriveFlat()`  → 「单列表」视图
- `deriveSearchResults()` → 搜索

所以归档后：`stray` 为空 → 「未分组」整组消失。GUI 里本来就有右键
「归档会话」，这是官方行为，不是 hack。

**归档 ≠ 删除**：只往 `storages/workspace.json` 的
`global.archivedSessionIds` 记一笔；会话目录
（`sessions/<workspace>/<sessionId>/session.v?.jsonl.zstd`）原样保留。
用户要求"会话长久保持"，所以任何清理都只能是归档。

## 5. 为什么必须走 dsh-web 进程内的插件

`dsh-web` 启动时把 `storages/workspace.json` 读进内存，**之后不再看文件**。
外部程序直接改这个 JSON 不会生效，而且下一次内存状态落盘时会被覆盖。

唯一的入口是在 dsh-web 进程里注册一个插件：
`user_data/plugins/dsh-feishu-session/feishu-session.mjs`，它暴露

```
POST /feishu/new-session
  {"title","prompt"}                               建会话（登记进 workspace，GUI 可见）
  {"action":"list"}                                查现状
  {"action":"archive","sessionId":S}               归档一个
  {"action":"unarchive","sessionId":S}             取消归档
  {"action":"archive-strays","sessionIds":[S...]}  只归档未登记的散客
```

> ⚠️ 插件必须 `inject: ['webServer','webhookRuntime','workspaceRegistry']`。
> 少了 `workspaceRegistry`，`ctx.workspaceRegistry` 是 `undefined`，
> 归档会直接 HTTP 500 —— **这个坑踩过一次**。
>
> ⚠️ 插件代码改动**不会热重载**（ESM 模块缓存），改完必须
> `sudo systemctl restart dsh-web`。改文件名也没用，实测过。

## 6. 怎么识别"哪些会话是 Bot 产生的"

不能靠文件名、也不能靠 cwd（GUI 会话和 headless 会话都是
`session-<uuid>`、cwd 都是 `<家目录>`）。用**提示词指纹**：

- 现在：`ask_agent()` 在每条 headless 提示词开头统一加一行 `【飞书 Bot 发起】`
- 历史：两条旧提示词开头兜底 —— `你是通过飞书和用户对话的助手`、
  `用户通过飞书发来一张图片`

再叠加一道保险：**已登记进任何 workspace 的会话一律不碰**。

## 7. 日常操作

```bash
FB_DATA_DIR=~/software/feishu-bot \
DSH_HOME=$DSH_HOME \
~/software/feishu-bot/plugins/feishu-bridge/scripts/fbctl gui-status     # 看现状
... fbctl gui-hide                                                       # 隐藏 Bot 会话
... fbctl gui-restore                                                    # 放回来
```

Bot 每次处理完消息会自动跑一次 `gui-hide`，所以侧栏会一直保持干净。
关掉自动隐藏：给 Bot 进程设 `FB_HIDE_HEADLESS=0`。

**实测**：一批 Bot 会话归档后 `gui-status` 显示散客 0；
被归档会话的目录与 `session.v?.jsonl.zstd` 全部还在磁盘上（一个没少）；
`gui-restore <ID>` 能放回侧栏，再 `gui-hide` 又能藏回去。
客户端**没有**「已归档」这样的分组或入口（`sessionVisible()` 直接把归档会话
排除在所有视图之外），所以侧栏不会换成另一个已归档列表，就是干净地少了一块。

> 也正因为 GUI 没有取消归档的入口，`fbctl gui-restore` 是唯一的撤销路径。

## 8. 复现/复核命令

```bash
# 未分组的分组逻辑（只有 stray>0 才出现）
grep -n 'if (stray.length > 0)' \
  $DSH_HOME/../../lib/node_modules/@deepseek-ai/dsh/node_modules/@deepseek-ai/dsh-client-ui-workspace/lib/client.js

# locale 拒绝重复注册
grep -n 'already has locale' \
  .../dsh-client-locale/lib/client.js

# 归档集合与落盘
grep -n 'archivedSessionIds' .../dsh-workspace/lib/index.js | head
python3 -c "import json;print(json.load(open('$DSH_HOME/storages/workspace.json'))['global']['archivedSessionIds'])"
```
