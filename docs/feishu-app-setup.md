# 飞书应用配置

这一步不做，Bot 起得来但收不到消息。全程在 [飞书开放平台](https://open.feishu.cn/app) 网页上点，大约 10 分钟。

---

## 1. 建一个自建应用

开放平台 → **创建企业自建应用** → 填名字/图标。

建好后进 **「凭证与基础信息」**，能拿到两样东西，等下要填进 `.env`：

```
App ID      cli_xxxxxxxxxxxxxxxx
App Secret  xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

> ⚠️ `App Secret` 等于密码。别提交进 git —— 本项目的 `.gitignore` 已经把 `.env` 和 `*.service` 排除了。

---

## 2. 开权限

进 **「权限管理」**，搜索并添加下面几个（名字以控制台为准，搜索关键词即可）：

| 权限 | 干什么用 | 本项目哪段代码在用 |
|---|---|---|
| `im:message`（获取与发送单聊、群组消息） | 收消息、发消息、读历史消息 | 回复、断线补收漏掉的消息 |
| `im:message:send_as_bot`（以应用身份发消息） | 主动发消息 | 重启完成通知、发附件 |
| `im:resource`（获取与上传图片或文件资源） | 上传/下载图片和文件 | 收发图片、文件、语音、视频 |

**不确定要哪个？** 项目的调用面很小，就这几个接口：

```
POST /open-apis/auth/v3/tenant_access_token/internal     取 token（默认就有）
POST /open-apis/im/v1/messages/{message_id}/reply        回复消息
POST /open-apis/im/v1/messages?receive_id_type=chat_id    主动发消息
GET  /open-apis/im/v1/messages/{message_id}/resources/{key}?type=...   下载图片/文件/语音/视频
POST /open-apis/im/v1/images                             上传图片
POST /open-apis/im/v1/files                              上传文件
GET  /open-apis/im/v1/messages?container_id_type=chat_id 读历史（断线补收）
```

在控制台调接口或用调试台时会直接告诉你缺哪个权限。

---

## 3. 开事件订阅（关键：用长连接）

进 **「事件与回调」** → 订阅方式选 **使用长连接接收事件**。

> 这是这个项目最大的便利：**不需要公网 IP、不需要备案域名、不需要内网穿透或反向代理。** 由 SDK 主动向飞书建 WebSocket，家里/公司内网的机器直接就能用。

然后 **添加事件**：搜索并添加

```
接收消息  im.message.receive_v1
```

如果「事件与回调」页面要求填 加密 Key / 验证 Token，把值也填进 `.env` 的 `FEISHU_ENCRYPT_KEY` / `FEISHU_VERIFICATION_TOKEN`（长连接模式其实用不到加密，留空也能跑，但填上更稳妥）。

---

## 4. 发布应用并加上机器人

1. **「版本管理与发布」** → 创建版本 → 申请发布（企业自建应用需要管理员审核通过）。
2. 发布后，在飞书里**搜索这个机器人，把它加进单聊**（或在群里 @ 它）。

> 没发布 / 没加进会话，机器人收不到消息。

---

## 5. 把凭据填进去

```bash
cp .env.example .env
chmod 600 .env
nano .env      # 填 FEISHU_APP_ID / FEISHU_APP_SECRET
```

---

## 6. 验证

```bash
# 本地检查 token 能不能取到（最简单的连通性验证）
python3 - <<'PY'
import json, os, urllib.request
from pathlib import Path
for line in Path(".env").read_text(encoding="utf-8").splitlines():
    if line.strip() and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1); os.environ.setdefault(k.strip(), v.strip())
r = urllib.request.urlopen(urllib.request.Request(
    "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
    data=json.dumps({"app_id": os.environ["FEISHU_APP_ID"],
                     "app_secret": os.environ["FEISHU_APP_SECRET"]}).encode(),
    headers={"Content-Type": "application/json"}), timeout=15)
print(json.load(r))
PY
```

返回 `{"code":0,"msg":"ok",...}` 就说明凭据没问题。

然后启服务、在飞书里发一句 `查看会话`：

```bash
sudo systemctl start feishu_bot
tail -f logs/feishu_bot.log
```

日志里应该出现：

```
飞书 Bot 启动（转发 → DSH headless, 多会话+启动DSH）
[Lark] connected to wss://msg-frontier.feishu.cn/ws/v2?...
```

---

## 出问题时

| 现象 | 查什么 |
|---|---|
| 起不来、日志里 `缺少环境变量` | `.env` 没填 / 没生效（systemd 的 `Environment=` 会盖住 `.env`） |
| 起来了但发消息没反应 | 应用没发布、机器人没加进会话、事件订阅没开或没加 `im.message.receive_v1` |
| 收到消息但回不出去 | 缺 `im:message` 或 `im:message:send_as_bot`，日志里会有 `code` 和 `msg` |
| 收图片/文件失败 | 缺 `im:resource` |
| **不确定缺哪个权限** | 日志里飞书返回的 `code`（如 `99991672`）会直接告诉你要开哪个权限，并给出申请链接 |
