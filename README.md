# dsh-feishu-bot

`MIT 协议` ｜ `Python 3.9+` ｜ `已在 DSH 0.1.5-rc.1 上验证`

把 DSH (DeepSeek Harness) 接到飞书上，让你**在外面用手机就能接着聊家里那台机器上的项目**。

> **主仓在 Gitee**：<https://gitee.com/soar_ccie/dsh-feishu-bot>
> GitHub 是自动同步的**只读镜像**（<https://github.com/soar-ccie/dsh-feishu-bot>）。
> **Issue / PR 请提到 Gitee** —— 镜像是覆盖式同步的，在 GitHub 上改的东西下次同步会被覆盖掉。

不是"再开一个 AI 聊天窗口"，而是：**飞书 = 移动端入口，DSH Web GUI = 桌面端工作台，两边按项目对齐、互相读得到对方在说什么。**

```
        ┌─────────────┐                    ┌──────────────────────┐
        │  飞书 App    │                    │  DSH Web GUI (浏览器) │
        │  （手机）    │                    │      （桌面）         │
        └──────┬──────┘                    └──────────┬───────────┘
               │ 长连接（不用公网IP）                  │
               ▼                                      │
        ┌─────────────────────┐                       │
        │   feishu_bot.py     │                       │
        │  收消息 / 回消息     │                       │
        └──────┬──────────────┘                       │
               │ 子进程                                │
               ▼                                      │
        ┌─────────────────────┐   项目记录（fbctl）    │
        │ dsh --profile       │◀──────────────────────┤
        │      headless       │                       │
        └─────────────────────┘                       │
               │                                      │
               ▼                                      ▼
        ┌─────────────────────────────────────────────────────┐
        │   DSH 会话（sessions/）＋ workspace.json            │
        └─────────────────────────────────────────────────────┘
```

两边上下文**不是同一个会话**（DSH 的会话是独占写入的，做不到），但通过 `fbctl` 管起来的"项目记录"，两边都能读到对方最近说了什么。

---

## 它长什么样

在飞书里发一句话，机器人转给 DSH 处理，把结果回给你：

```
你：  帮我看看 APK 编出来没有
Bot： 编好了，在 ~/project/apk/app-debug.apk（12.4MB）
      📎 正在发送附件: ~/project/apk/app-debug.apk     ← 自动补发文件
```

几个本地指令（不花 token、秒回）：

```
你：  查看会话
Bot： 📋 会话列表（共 4 个）
      1. 默认（飞书默认会话）
      2. 安卓开发 【当前】
      3. 服务器巡检
      4. 论文助手
      回复数字进入会话；也可以直接说「切到 XXX」。

      🗄 已归档 1 个：
      · 旧项目
      发「查看归档」看全部，并可回数字恢复。
```

直接发张截图也行 —— **它不会无脑复述画面，而是先结合上下文判断你想干什么，再带着目的看图**：

```
你：  [截图]（什么都不打）
Bot： 这张是编译报错的截图。结合你刚才说的"APK 打不出来"，
      看着是缺了 signingConfig —— 要我帮你补上吗？
```

---

## 能力

| 能力 | 说明 |
|---|---|
| **文本对话** | 转发给 `dsh --profile headless`，带当前项目在两侧的最近记录 |
| **图片** | 先落盘，再带着上下文看图；**先发图后打字会自动合并成一轮** |
| **文件 / 视频 / 音频** | 落盘到 `file/` 对应分类，回执本地路径；超过 100MB 自动改 Range 分片下载 |
| **附件回传** | 回复里出现本地文件路径 → 自动作为附件发回飞书；也可以 `发文件 <路径>` 显式发 |
| **多项目** | 一个项目 = 一个飞书侧记录 + 一个绑定的 DSH 会话，`查看会话` 切换 |
| **在 GUI 建会话** | 飞书里 `新建会话 名称` → 真的在 Web GUI 里建一个会话并绑定（靠一个 DSH 插件） |
| **归档 / 恢复** | `归档会话` → 回数字 → 在 GUI 里归档；`查看归档` → 回数字 → 恢复 |
| **自救** | `重启DSH` / `启动DSH`；`重启飞书` 重启 Bot 自己；断线自动重连 + 补收漏掉的消息 |
| **侧栏自动保洁** | Bot 自己产生的 headless 会话会自动归档，不让 GUI 侧栏堆出一坨「未分组」 |

---

## 快速开始

### 0. 前置

- **DSH**：已安装并能跑 `dsh web`。本项目在 **DSH `0.1.5-rc.1`** 上开发验证。
- **Python 3.9+**（开发环境用的是 3.14）。
- 一台**能被飞书连上**的机器 —— 用的是飞书**长连接**模式，**不需要公网 IP、不用备案域名、不用内网穿透**。

### 1. 飞书那边先建应用

见 **[docs/feishu-app-setup.md](docs/feishu-app-setup.md)**（开哪些权限、怎么开长连接事件订阅、怎么拿 app_id/app_secret）。

**这一步不做，后面全跑不起来** —— 主要是三个权限和一条事件订阅。

### 2. 装

主仓在 **Gitee**，GitHub 是自动同步的镜像（国内用 Gitee 更快）：

```bash
# 主仓（推荐）
git clone https://gitee.com/soar_ccie/dsh-feishu-bot.git && cd dsh-feishu-bot

# 或镜像
# git clone https://github.com/soar-ccie/dsh-feishu-bot.git && cd dsh-feishu-bot

bash install.sh
```

脚本会：建 venv 装依赖 → 生成 `.env` → 把 DSH 插件拷进 `$DSH_HOME/plugins/` → 往 web profile 追加挂载配置（改前备份）→ 把技能软链到 `$DSH_HOME/skills/` → 生成 systemd 单元。

### 3. 填凭据

```bash
nano .env       # 填 FEISHU_APP_ID / FEISHU_APP_SECRET
```

### 4. 起服务

```bash
sudo cp feishu_bot.service /etc/systemd/system/feishu_bot.service
sudo systemctl daemon-reload
sudo systemctl enable --now feishu_bot
sudo systemctl restart dsh-web        # 让 DSH 加载刚挂上的插件
tail -f logs/feishu_bot.log
```

看到 `飞书 Bot 启动` + `[Lark] connected to wss://...` 就成了。在飞书里给机器人发 `查看会话` 试试。

---

## 飞书里的指令

**匹配规则：下面这些必须"整句就是这几个字"**（首尾空白除外），不做包含匹配、不容忍尾随标点。原因见下一节。

| 你说 | 它做什么 |
|---|---|
| `查看会话` | 列出会话（编号）+ 已归档预览。回数字进入对应会话 |
| `归档会话` | 列出会话。**回数字**把那个会话在 GUI 里归档（3 分钟内有效） |
| `查看归档` | 列出已归档会话。**回数字**把它恢复出来（3 分钟内有效） |
| `新建会话 名称` | 在 Web GUI 里真建一个会话，并绑成飞书侧项目、切过去 |
| `发文件 <路径>` | 把本地文件/图片作为附件发到飞书 |
| `重启DSH` / `启动DSH` | 重启 / 拉起 `dsh web` |
| `重启飞书` | 重启 Bot 自己（systemd 会拉起） |
| `1` `2` `3` … | 纯数字 = 进入对应编号的会话（若刚发过归档/查看归档，则是它的目标） |

其余消息一律交给 DSH 模型处理 —— 自然语言说法它也能做（技能里教了怎么调 `fbctl`），只是要花 token、慢几秒。

### 收到的各类消息怎么处理

| 飞书消息类型 | 处理 |
|---|---|
| 文本 `text` | 先过本地指令，否则转给模型 |
| 富文本 `post` | 解析成纯文本（含标题、链接、@、表情、代码块、内嵌图片/视频）；**"一次发多个文件"也是这个类型**，文件挂在顶层 `files[]` 上 |
| 图片 `image` | 落盘 → **等 8 秒看有没有后续文字**（`FB_IMAGE_MERGE_SEC`）→ 有就图文合并成一轮，没有就带着上下文单独处理 |
| 视频 `media` | 落盘 + 回执路径（不喂模型） |
| 语音 `audio` | 落盘 + 回执路径（**不转写**，原因见"已知限制"） |
| 文件 `file` | 落盘 + 回执路径；>100MB 自动 Range 分片 |
| 文件夹 `folder` / 表情包 `sticker` | 飞书平台限制拿不到内容，只回执提示 |

---

## 为什么全是"字面匹配"

这是踩出来的。早期版本用 `关键词 in 文本` 判断，结果：

| 你说 | 早期版本的后果 |
|---|---|
| 别重启DSH | **真的把 DSH 重启了** |
| 不要重启DSH | 同上 |
| 重启DSH了吗 | 又重启一次 |
| 新建会话是干嘛的 | **真建了一个名叫「是干嘛的」的会话** |
| 新建会话要小心重名 | 建了一个名叫「要小心重名」的会话 |
| 针对返回的会话列表，默认会话放第一个 | 被当成"查看会话"命令，答非所问 |
| 发文件的时候记得先压缩 | 回了"❌ 文件不存在: <家目录>/的时候记得先压缩" |

现在所有本地指令都是**字面相等**（`t == "查看会话"`），从构造上就不可能被否定句/疑问句/引用句误触发。`新建会话` 和 `发文件` 因为要带参数，用前缀匹配 + 后缀合法性判定（`_looks_like_name()` / `_looks_like_path()`）。

**代价**：写法必须精确。`查看会话。`（带句号）不会被识别 —— 这是刻意的取舍，宁可漏判交给模型，也不要误判去执行破坏性动作。

---

## 数据都在哪

```
<仓库>/
├── feishu_bot.py                    主程序
├── .env                             凭据（gitignore）
├── session/                         ★ 项目记录、已恢复的飞书历史（gitignore）
├── logs/                            ★ 运行日志（gitignore）
├── file/                            ★ 飞书收到的图片/视频/音频/文档（gitignore）
├── temp/                            ★ 下载中转，自动清理（gitignore）
├── plugins/feishu-bridge/           技能（DSH 通过软链加载）
│   ├── SKILL.md
│   └── scripts/fbctl                项目管理 + 读 DSH 会话（纯标准库）
├── dsh-plugin/feishu-session.mjs    DSH web profile 插件（零依赖）
└── docs/
```

**代码与数据可以分离**：设 `FB_DATA_DIR=/wherever`，`session/ logs/ file/ temp/` 就都跑到那儿去，仓库目录永远干净。

---

## 已知限制 / 不做什么

| 限制 | 说明 |
|---|---|
| **DSH 版本敏感** | 插件用的是 DSH 内部接口（`ctx.webServer` / `ctx.webhookRuntime` / `ctx.workspaceRegistry`），`fbctl` 读的是 DSH 私有会话格式（`session.v3.jsonl.zstd`）。**DSH 升级后可能失效**，见 [docs/gui-ungrouped.md](docs/gui-ungrouped.md) |
| **两边不是同一个会话** | DSH 的会话独占写入（GUI 开着时别的进程拿不到写句柄），所以做不到"飞书和 GUI 共用一个会话"。这是运行时约束，配置绕不过去 —— 完整证据见 [docs/acp-investigation.md](docs/acp-investigation.md) |
| **只归档，不删除** | DSH 本身没有"删除会话"的功能（GUI 里也只有"归档"）。本项目所有"清理"都只是归档：GUI 里不显示、文件完整保留，随时可恢复 |
| **语音不转写** | 飞书音频消息的内容里**没有**识别文字（只有 `file_key` + `duration`）；官方 ASR 接口要付费版且只收 PCM。所以语音只落盘 + 回执 |
| **飞书出站 30MB 上限** | 发回的文件超过 30MB（图片 10MB）飞书会拒收，只会回一句明确提示 |
| **文件夹 / 表情包拿不到** | 飞书平台限制，API 只能拿到 key 和名字 |

---

## 常见问题

**Q：机器人不回话？**
先看 `logs/feishu_bot.log`。常见原因：`FEISHU_APP_ID/SECRET` 没填对、应用没开权限、事件订阅没开长连接、机器人没被加进会话。

**Q：`新建会话` 报"没能在 GUI 里创建会话"？**
DSH 插件没挂上或没生效。检查 `$DSH_HOME/profiles/web/cordis.patch.yml` 有没有 `feishu-session` 那段，然后 `sudo systemctl restart dsh-web`（**插件代码改动不会热重载**）。

**Q：`归档会话` 回 409？**
插件版本旧了（没有 `force` 支持）。重新 `bash install.sh` 并重启 `dsh-web`。

**Q：GUI 侧栏里堆了一堆没名字的会话？**
那是 Bot 每次调用 headless 产生的会话。正常情况下 Bot 会自动把它们归档掉（`查看会话` 里看不到）；关掉这个行为可以设 `FB_HIDE_HEADLESS=0`。手动打扫用 `fbctl gui-hide`。

**Q：想让飞书侧也看不到某个已归档会话？**
已经是了 —— 归档的会话在飞书列表里同样不显示（`fbctl` 的 `project_hidden()`）。恢复用 `查看归档` → 数字，或 `fbctl gui-restore <session_id>`。

---

## 开发

```bash
# 语法检查
python3 -m py_compile feishu_bot.py
python3 -m py_compile plugins/feishu-bridge/scripts/fbctl
node --check dsh-plugin/feishu-session.mjs

# 项目管理工具（所有命令）
plugins/feishu-bridge/scripts/fbctl --help

# 看 GUI 侧栏现状 / 打扫
fbctl gui-status
fbctl gui-hide            # 归档 Bot 自己产生的散客会话
fbctl archived-list       # 列出已归档（不含 Bot 噪音）
fbctl gui-restore <id>    # 恢复指定会话
```

代码里刻意保留了**大量"教训注释"**（为什么用字面匹配、为什么只归档不删除、为什么 `isdigit()` 会炸……）。改之前建议先读一遍 —— 很多坑踩过一次了。

---

## License

[MIT](LICENSE)
