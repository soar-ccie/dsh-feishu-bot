"""飞书 Bot — 转发模式：收消息 → DSH Agent → 回结果

交互指令:
  - 查看会话        → 列出会话列表(编号),回复数字进入
  - 数字(0-9)      → 切换到对应会话
  - 归档会话        → 列出会话列表,回复数字把那个会话在 GUI 里归档
  - 新建会话 主题   → 创建主题会话并开始聊天
  - 启动DSH         → 检测 dsh web,没跑才启动,返回带 token 的地址
  - 重启DSH         → 强制重启 dsh web
  - 发文件 路径      → 把本地文件/图片作为附件发到飞书
  - 其他消息        → 交给 DSH headless 处理(带当前会话记忆);
                       回复里出现的本地文件路径会自动作为附件发出

  ⚠️ 匹配规则:上面这些必须【整句就是这几个字】(首尾空白除外),
     不做包含匹配、不容忍尾随标点 —— 见 handle_message() 里的教训。

收到的消息:
  文本 text / 富文本 post        → 交给 DSH 处理(富文本带图时一并传图)
  图片 image / 视频 media /
  语音 audio / 文档 file        → 存入 file/ 对应分类,并回执路径
  表情包 sticker / 文件夹 folder → 飞书平台限制,无法下载(仅回执提示)

目录(全部集中在 ~/software/feishu-bot/):
  feishu_bot.py   主程序        session/  多会话记忆
  logs/           运行日志      temp/     我自己的临时中转(下载中/处理中)
  file/           你发来的文件(只存入站):
      images/ 图片   videos/ 视频   audios/ 音频
      documents/ 文档   other/ 认不出的
"""
import os, json, logging, re, sys, requests, subprocess, time, glob, shutil, threading
from logging.handlers import RotatingFileHandler
from concurrent.futures import ThreadPoolExecutor
from lark_oapi.event.dispatcher_handler import EventDispatcherHandler
from lark_oapi.api.im.v1.model.p2_im_message_receive_v1 import P2ImMessageReceiveV1
from lark_oapi.ws import Client


def _load_dotenv(path):
    """可选的 .env 支持(方便不走 systemd、直接跑脚本的人)。

    逐行读 KEY=VALUE,**只填环境里还没有的键** —— 已经存在的环境变量优先,
    这样 systemd 的 Environment= 不会被 .env 覆盖。
    """
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip().strip('"').strip("'")
                if k and k not in os.environ:
                    os.environ[k] = v
    except FileNotFoundError:
        return
    except Exception as e:                       # 配置文件坏了不该让 Bot 起不来
        print(f"[warn] 读取 .env 失败(忽略): {e}")


_load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

# 凭据从环境变量读(支持 .env / systemd Environment= / 直接 export);缺失就直接报错退出
try:
    APP_ID = os.environ["FEISHU_APP_ID"]
    APP_SECRET = os.environ["FEISHU_APP_SECRET"]
except KeyError as e:
    raise SystemExit(f"缺少环境变量 {e}。请复制 .env.example 为 .env 并填写飞书自建应用的凭据。")
ENC_KEY = os.environ.get("FEISHU_ENCRYPT_KEY", "")
VERIFY_TOKEN = os.environ.get("FEISHU_VERIFICATION_TOKEN", "")

# -------------------- 路径配置(零硬编码,便于移植/开源) --------------------
# Bot 自己的东西集中在程序所在目录:
#   feishu_bot.py        主程序
#   feishu_bot.service   systemd 单元
#   plugins/             DSH skill 扩展(含 fbctl)
#   session/             项目化记录
#   logs/                运行日志(超过 50MB 自动轮转)
#   file/                你在飞书发来的图片/语音/文件
#   temp/                下载中转
#
# 数据目录可用 FB_DATA_DIR 覆盖(方案B:数据与程序分离);默认方案A:程序目录内。
HOME = os.path.expanduser("~")
BOT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("FB_DATA_DIR") or BOT_DIR
SESSION_DIR = f"{DATA_DIR}/session"
LOG_DIR = f"{DATA_DIR}/logs"
FILE_DIR = f"{DATA_DIR}/file"

FBCTL = os.path.join(BOT_DIR, "plugins", "feishu-bridge", "scripts", "fbctl")
RESTART_SCRIPT = os.path.join(BOT_DIR, "bin", "restart-feishu-bot.sh")

# ---- DSH 安装位置:优先环境变量,其次常见位置探测 ----
def _find_dsh():
    cands = [
        os.environ.get("DSH_APP_DIR"),
        f"{HOME}/software/deepseek_harness_agent",
        f"{HOME}/.dsh-app",
    ]
    for c in cands:
        if c and os.path.isdir(os.path.join(c, "bin")):
            return c
    return cands[1]


APP_DIR = _find_dsh()
# DSH 数据目录:凭据/会话/profile 都在这里。必须显式传给子进程,
# 否则 dsh CLI 会去找已不存在的 ~/.dsh。
DSH_HOME = os.environ.get("DSH_HOME") or f"{APP_DIR}/user_data"
DSH_BIN = f"{APP_DIR}/bin/dsh"
DSH_WEB_SCRIPT = f"{APP_DIR}/start-dsh-web.sh"                 # 启动/重启 dsh web
WORKSPACE = HOME

# -------------------- 日志(自己管理,不依赖 logrotate) --------------------
LOG_FILE = f"{LOG_DIR}/feishu_bot.log"
LOG_MAX_BYTES = 50 * 1024 * 1024     # 单个日志文件上限 50MB
LOG_BACKUP_COUNT = 1                 # 滚动后保留 1 份历史(feishu_bot.log.1)


def _setup_logging():
    """日志同时写文件和 stdout,按大小自动轮转。"""
    for d in (SESSION_DIR, LOG_DIR, FILE_DIR):
        os.makedirs(d, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fmt = logging.Formatter("[%(asctime)s] [%(name)s] %(message)s",
                            datefmt="%Y-%m-%d %H:%M:%S")

    fh = RotatingFileHandler(LOG_FILE, maxBytes=LOG_MAX_BYTES,
                             backupCount=LOG_BACKUP_COUNT, encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)

    # stdout 也留一份:手动前台运行时能直接看,被 systemd 收进 journal 便于排查崩溃
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    root.addHandler(sh)

    # 接管飞书 SDK 自带的 logger(它默认往 stdout 塞一个 handler,会和上面重复输出)
    lark = logging.getLogger("Lark")
    for h in list(lark.handlers):
        lark.removeHandler(h)
    lark.propagate = True
    lark.setLevel(logging.INFO)   # 断线/重连日志是 INFO 级,不放开不会落盘


_setup_logging()
_log = logging.getLogger("Bot")


def _pick_node_bin():
    """取 nvm 里版本号最大的 node bin 目录(和 start-dsh-web.sh 一致)"""
    best, best_key = "", ()
    for d in glob.glob(f"{HOME}/software/nvm/versions/node/*/bin"):
        ver = os.path.basename(os.path.dirname(d)).lstrip("v")
        try:
            key = tuple(int(x) for x in ver.split("."))
        except ValueError:
            continue
        if key > best_key:
            best, best_key = d, key
    if best and os.path.isfile(os.path.join(best, "node")):
        return best
    return os.path.dirname(shutil.which("node") or "")


NODE_BIN_DIR = _pick_node_bin()
# 下面两个纯属"锦上添花":只影响 DSH 能不能编译安卓项目。
# 路径不存在也无害(只是命令找不到),并且都支持用环境变量覆盖。
JDK17_HOME = os.environ.get("JDK17_HOME") or f"{HOME}/software/openjdk17.0.20_8"
ANDROID_HOME = os.environ.get("ANDROID_HOME") or f"{HOME}/software/Android_Sdk"

# 传给 DSH 子进程的工具链 PATH(含 node/安卓/gradle,让 DSH 能编译安卓项目)
# 不存在的目录无害,只是找不到对应命令。
TOOLCHAIN_PATH = ":".join([
    NODE_BIN_DIR,
    f"{APP_DIR}/bin",
    f"{HOME}/software/miniconda3/bin",
    f"{JDK17_HOME}/bin",
    f"{ANDROID_HOME}/cmdline-tools/latest/bin",
    f"{ANDROID_HOME}/platform-tools",
    f"{HOME}/shell/bin",
    "/usr/local/bin", "/usr/bin", "/bin",
])

# -------------------- 项目/上下文(交给 fbctl 统一管理) --------------------
# 存储与项目逻辑集中在 plugins/feishu-bridge/scripts/fbctl,
# Bot 通过命令行调用它 —— 这样 agent(技能)和 Bot 用同一套 API,不会出现两套实现。

def fbctl(*args, timeout=90):
    """调用 fbctl,返回 (是否成功, 输出文本)"""
    env = dict(os.environ)
    env["FB_DATA_DIR"] = DATA_DIR          # 支持方案B:数据目录与程序分离
    env["DSH_HOME"] = DSH_HOME
    try:
        r = subprocess.run([FBCTL, *[str(a) for a in args]],
                           capture_output=True, text=True, timeout=timeout, env=env)
        out = (r.stdout or "").strip()
        if r.returncode != 0:
            log(f"fbctl {' '.join(str(a) for a in args)} 失败: {(r.stderr or out)[:200]}")
            return False, out
        return True, out
    except Exception as e:
        log(f"fbctl 调用异常 {args}: {e}")
        return False, ""


def build_prompt(task, with_image=False):
    """拼提示词:用户在本项目的背景(fbctl 组装) + 用户当前说的话。

    背景里可能同时含 Web GUI 和飞书两侧的记录 —— 用户可能在任意一边接力聊同一个项目。

    with_image=True 时把「不要读取文件」放宽成「只许读下面给的图片」——
    否则模型会老老实实守规则、压根不去看图,截图就白发了。
    """
    ok, ctx = fbctl("context")
    lines = [
        "你是通过飞书和用户对话的助手。请用最精简的方式回复。",
        "规则:尽量用一句话或几个要点回答,不要长篇大论,除非用户明确要求详细。",
        "常用问候/简单确认(如'在吗'、'你好')用一个词回应即可。",
        "",
        "⚠️ 重要:用户当前项目的背景【已经在下文给出】,请直接回答。",
        ("   除了下文明确给出的图片文件之外,不要读取其它文件、不要运行命令 —— 你已经掌握足够信息了。"
         if with_image else
         "   不要加载技能、不要读取文件、不要运行命令 —— 你现在已经掌握足够信息了。"),
        "   只有当用户明确要求「切换项目 / 新建项目 / 查看项目列表 / 翻查旧记录」时,才去执行相应操作。",
    ]
    if ok and ctx:
        lines.append("")
        lines.append("【背景:这是用户当前项目的上下文,可能来自 Web GUI 或飞书任一侧。")
        lines.append(" 用户会在两边接力聊同一件事,请结合它回答,不要问'我们之前聊过什么'。】")
        lines.append(ctx)
    lines.append("")
    lines.append(f"【用户现在说】{task}")
    return "\n".join(lines)

def log(msg):
    _log.info("%s", msg)

# handle_message 的特殊返回值:表示"重启 Bot 自己"
CMD_RESTART_BOT = "__CMD_RESTART_BOT__"

# 每次 headless 调用都会打上这行指纹,便于 fbctl gui-hide 认出 Bot 自己的会话
PROMPT_TAG = "【飞书 Bot 发起】"


# -------------------- 在 GUI 里创建会话(靠 dsh-web 插件) --------------------
# 背景:dsh-web 的会话注册表只在内存里,外部改 workspace.json 无效,ACP 建的也不登记。
# 唯一可行的路是 dsh-web 进程内的插件:user_data/plugins/dsh-feishu-session/feishu-session.mjs
# 它暴露 POST /feishu/new-session,能在 Web Workspace 里创建 GUI 可见的会话。

def _gui_webhook_sessions():
    """读 workspace.json,返回当前所有 webhook- 开头的会话 ID 集合"""
    try:
        with open(os.path.join(DSH_HOME, "storages", "workspace.json"), encoding="utf-8") as f:
            ws = json.load(f)
        out = set()
        for w in ws.get("tables", {}).get("workspaces", {}).values():
            for sid in w.get("sessionIds", []) or []:
                if str(sid).startswith("webhook-"):
                    out.add(sid)
        return out
    except Exception:
        return set()


def create_gui_session(title, wait_seconds=25):
    """调 dsh-web 插件在 GUI 里建一个会话。

    返回 (sessionId, 错误说明):
      - 成功 → (sessionId, "")
      - 失败 → ("", 原因)
    插件是 fire-and-forget,不返回 ID,所以这里轮询 workspace.json 找出新出现的那一个。
    """
    port = get_dsh_port()
    url = f"http://127.0.0.1:{port}/feishu/new-session"
    before = _gui_webhook_sessions()
    try:
        r = _request("POST", url,
                     json={"title": title,
                           "prompt": f"（新会话已建立，主题：{title}。请只回复「已就绪」。）"},
                     timeout=15)
    except Exception as e:
        return "", f"调不通 GUI 接口({url}): {e}"
    if r.status_code != 202:
        return "", f"GUI 接口返回 HTTP {r.status_code}: {r.text[:120]}"

    deadline = time.time() + wait_seconds
    while time.time() < deadline:
        time.sleep(1.5)
        new = _gui_webhook_sessions() - before
        if new:
            return sorted(new)[0], ""
    return "", f"提交成功但 {wait_seconds} 秒内没看到新会话（插件是否已挂载？）"


# -------------------- 飞书 API 调用(带重试 + token 缓存) --------------------
# 实测遇到过:网络/DNS 瞬时抖动导致取 token 失败,回复直接丢失(用户收不到任何提示)。
# 所以这里统一加重试;token 也做缓存(官方有效期约 2 小时,原先每条消息都重新取一次)。
API_RETRIES = 3        # 失败重试次数
API_BACKOFF = 2        # 重试间隔基数(秒):2、4
_TOKEN_CACHE = {"value": "", "expire_at": 0.0}


def _request(method, url, **kw):
    """带重试的 HTTP 请求(应对 DNS/网络瞬时抖动)"""
    last = None
    for i in range(API_RETRIES):
        try:
            return requests.request(method, url, **kw)
        except requests.RequestException as e:
            last = e
            log(f"网络请求失败(第{i+1}/{API_RETRIES}次): {method} {url.split('?')[0]} -> {e}")
            if i < API_RETRIES - 1:
                time.sleep(API_BACKOFF * (i + 1))
    raise last


def get_token():
    """获取 tenant_access_token(缓存复用 + 失败重试)"""
    now = time.time()
    if _TOKEN_CACHE["value"] and now < _TOKEN_CACHE["expire_at"]:
        return _TOKEN_CACHE["value"]
    r = _request("POST",
                 "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
                 json={"app_id": APP_ID, "app_secret": APP_SECRET}, timeout=10)
    j = r.json()
    if j.get("code") != 0:
        raise RuntimeError(f"获取 token 失败: {j}")
    _TOKEN_CACHE["value"] = j["tenant_access_token"]
    # 提前 5 分钟判过期,避免踩边界
    _TOKEN_CACHE["expire_at"] = now + int(j.get("expire", 7200)) - 300
    return _TOKEN_CACHE["value"]


# 判断内容里是否含 Markdown 语法(纯文本发出会显示成一堆星号/竖线,很难看)
_MD_HINT = re.compile(r"(\*\*|^#{1,6} |^\s*\|.*\|\s*$|^```|^\s*[-*] |^\s*\d+\. )", re.M)


def send_to_chat(chat_id, text, as_card=False):
    """主动给某个会话发消息(不依赖 WebSocket,用 API 直接发)"""
    try:
        t = get_token()
        if as_card:
            card = {"config": {"wide_screen_mode": True},
                    "elements": [{"tag": "markdown", "content": text[:4000]}]}
            payload = {"receive_id": chat_id, "msg_type": "interactive",
                       "content": json.dumps(card, ensure_ascii=False)}
        else:
            payload = {"receive_id": chat_id, "msg_type": "text",
                       "content": json.dumps({"text": text})}
        r = _request("POST",
                     "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id",
                     headers={"Authorization": f"Bearer {t}", "Content-Type": "application/json"},
                     json=payload, timeout=10)
        j = r.json()
        if j.get("code") != 0:
            log(f"主动发消息失败: {j.get('code')} {j.get('msg')}")
            return False
        return True
    except Exception as e:
        log(f"主动发消息异常: {e}")
        return False


# 重启前落一个"待通知"标记,新进程起来后据此回报结果
RESTART_MARKER = f"{SESSION_DIR}/.restart_pending.json"


def mark_restart_pending(chat_id, mid):
    try:
        os.makedirs(SESSION_DIR, exist_ok=True)
        with open(RESTART_MARKER, "w", encoding="utf-8") as f:
            json.dump({"chat_id": chat_id, "mid": mid,
                       "requested_at": time.time()}, f, ensure_ascii=False)
    except Exception as e:
        log(f"写重启标记失败: {e}")


def notify_restart_done():
    """启动时若发现"重启待通知"标记 → 给用户发一条重启结果

    设计意图:重启成功就报平安;收不到消息=没起来,本身也是信号。
    """
    try:
        if not os.path.exists(RESTART_MARKER):
            return
        with open(RESTART_MARKER, encoding="utf-8") as f:
            d = json.load(f)
        os.remove(RESTART_MARKER)
        chat_id = d.get("chat_id") or ""
        at = float(d.get("requested_at") or 0)
        if not chat_id:
            return
        down = (_BOT_START - at) if at else 0   # 本进程启动时刻 - 收到重启指令时刻 = 真实停机时长
        if down > 600:          # 超过 10 分钟才起来,说明中间出过问题
            send_to_chat(chat_id, f"⚠️ 飞书 Bot 重启耗时异常（{down/60:.1f} 分钟），"
                                  f"但现在已经起来了。\n启动时间: {time.strftime('%H:%M:%S')}")
            return
        running, pids, port_ok, port = dsh_is_running()
        dsh_line = f"运行中（端口 {port}）" if port_ok else f"⚠️ 端口 {port} 未监听"
        send_to_chat(chat_id,
                     f"✅ 飞书 Bot 重启完成\n"
                     f"- 启动时间: {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
                     f"- 停机时长: {down:.0f} 秒\n"
                     f"- 飞书连接: 正常（能收到这条即已连上）\n"
                     f"- DSH: {dsh_line}")
        log(f"已发送重启完成通知（停机 {down:.0f} 秒）")
    except Exception as e:
        log(f"重启通知失败: {e}")


def reply(msg_id, content, force_text=False):
    """回复消息。

    - 含 Markdown 的内容 → 用【飞书卡片】发(卡片会渲染表格/加粗/代码块)
    - 纯文本          → 用普通文本发(简单、轻量)
    失败会重试,并把失败明确写进日志(不静默丢消息)。

    force_text=True:强制走纯文本。
      快捷指令的回复(会话列表等)全部由我们自己排版,里面那些 "1. xxx" 会让
      _MD_HINT 命中、被当 Markdown 塞进卡片 —— 而卡片是按 Markdown 渲染的,
      有序列表后面的空行会被吃掉,排版跟我们对不上。纯文本才能保住换行。
    """
    text = content[:4800]
    use_card = (not force_text) and bool(_MD_HINT.search(text))

    def _send(payload):
        t = get_token()
        return _request("POST",
                        f"https://open.feishu.cn/open-apis/im/v1/messages/{msg_id}/reply",
                        headers={"Authorization": f"Bearer {t}", "Content-Type": "application/json"},
                        json=payload, timeout=10)

    try:
        if use_card:
            card = {"config": {"wide_screen_mode": True},
                    "elements": [{"tag": "markdown", "content": text[:4000]}]}
            r = _send({"content": json.dumps(card, ensure_ascii=False), "msg_type": "interactive"})
            if r.json().get("code") != 0:
                log(f"卡片发送失败({r.text[:120]}),回退为纯文本")
                r = _send({"content": json.dumps({"text": text}), "msg_type": "text"})
        else:
            r = _send({"content": json.dumps({"text": text}), "msg_type": "text"})
        if r.json().get("code") != 0:
            log(f"❌ 回复被飞书拒绝: {r.text[:200]}")
    except Exception as e:
        log(f"❌ 回复失败(这条回复没能送达,请重发消息): {e}")

# -------------------- 发文件 / 图片 --------------------
MAX_FILE_MB = 30          # 飞书单文件上限 30MB
MAX_IMAGE_MB = 10         # 飞书图片上限 10MB
MAX_FILES_PER_MSG = 5     # 每条回复最多带 5 个附件
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
# 只有完全匹配枚举后缀才用专用类型,其余一律 stream(最稳)
FILE_TYPE_MAP = {".mp4": "mp4", ".pdf": "pdf", ".doc": "doc",
                 ".xls": "xls", ".ppt": "ppt", ".opus": "opus"}
# 匹配回复文本里的本地文件路径(家目录 / 临时目录)
# ⚠️ 这里原来把家目录写死成某个具体用户名 —— 换台机器这个功能会**静默失效**
#    (模型回复里提到文件路径,却不会再自动补发附件)。现在从 HOME 动态拼。
FILE_PATH_RE = re.compile(
    r"(?<![\w/])(?:~|/(?:" + re.escape(HOME.strip("/")) + r"|tmp))"
    r"(?:/[\w\u4e00-\u9fff.@%+\-=]+)*\.[A-Za-z0-9]{1,8}"
)

def guess_file_type(name):
    return FILE_TYPE_MAP.get(os.path.splitext(name)[1].lower(), "stream")

def upload_file(path):
    """上传文件到飞书,返回 file_key 或 None"""
    name = os.path.basename(path)
    try:
        with open(path, "rb") as f:
            r = _request("POST",
                "https://open.feishu.cn/open-apis/im/v1/files",
                headers={"Authorization": f"Bearer {get_token()}"},
                data={"file_type": guess_file_type(name), "file_name": name},
                files={"file": (name, f, "application/octet-stream")},
                timeout=180,
            )
        j = r.json()
        if j.get("code") != 0:
            log(f"上传文件失败: {j.get('code')} {j.get('msg')} ({name})")
            return None
        return j["data"]["file_key"]
    except Exception as e:
        log(f"上传文件异常: {e}")
        return None

def upload_image(path):
    """上传图片到飞书,返回 image_key 或 None"""
    name = os.path.basename(path)
    try:
        with open(path, "rb") as f:
            r = _request("POST",
                "https://open.feishu.cn/open-apis/im/v1/images",
                headers={"Authorization": f"Bearer {get_token()}"},
                data={"image_type": "message"},
                files={"image": (name, f, "application/octet-stream")},
                timeout=180,
            )
        j = r.json()
        if j.get("code") != 0:
            log(f"上传图片失败: {j.get('code')} {j.get('msg')} ({name})")
            return None
        return j["data"]["image_key"]
    except Exception as e:
        log(f"上传图片异常: {e}")
        return None

def send_resource_reply(msg_id, msg_type, content):
    """以指定 msg_type(file/image) 回复消息"""
    try:
        r = _request("POST",
            f"https://open.feishu.cn/open-apis/im/v1/messages/{msg_id}/reply",
            headers={"Authorization": f"Bearer {get_token()}",
                     "Content-Type": "application/json"},
            json={"content": json.dumps(content, ensure_ascii=False), "msg_type": msg_type},
            timeout=120,
        )
        j = r.json()
        if j.get("code") != 0:
            log(f"发送 {msg_type} 失败: {j.get('code')} {j.get('msg')}")
            return False
        return True
    except Exception as e:
        log(f"发送 {msg_type} 异常: {e}")
        return False

def send_file_reply(msg_id, path):
    """上传并发送单个本地文件(图片走 image 类型)"""
    p = os.path.abspath(os.path.expanduser(path))
    if not os.path.isfile(p):
        log(f"待发文件不存在: {p}")
        return False
    size = os.path.getsize(p)
    if size == 0:
        reply(msg_id, f"⚠️ 空文件无法发送: {os.path.basename(p)}")
        return False
    if os.path.splitext(p)[1].lower() in IMAGE_EXTS:
        if size > MAX_IMAGE_MB * 1024 * 1024:
            reply(msg_id, f"⚠️ 图片超过 {MAX_IMAGE_MB}MB 上限: {os.path.basename(p)}")
            return False
        key = upload_image(p)
        if not key:
            reply(msg_id, f"⚠️ 图片上传失败(权限或格式): {os.path.basename(p)}")
            return False
        return send_resource_reply(msg_id, "image", {"image_key": key})
    if size > MAX_FILE_MB * 1024 * 1024:
        reply(msg_id, f"⚠️ 文件 {size/1024/1024:.1f}MB,超过飞书 {MAX_FILE_MB}MB 上限:\n"
                      f"{os.path.basename(p)}")
        return False
    key = upload_file(p)
    if not key:
        reply(msg_id, f"⚠️ 文件上传失败(检查应用「上传文件」权限 im:resource): "
                      f"{os.path.basename(p)}")
        return False
    return send_resource_reply(msg_id, "file", {"file_key": key})

def extract_file_paths(text):
    """从回复文本里提取真实存在的本地文件路径(去重)"""
    found = []
    for m in FILE_PATH_RE.finditer(text or ""):
        raw = m.group(0).rstrip(".,;:)]}，。；：）】")
        p = os.path.abspath(os.path.expanduser(raw))
        if os.path.isfile(p) and p not in found:
            found.append(p)
        if len(found) >= MAX_FILES_PER_MSG:
            break
    return found

def send_files_from_text(msg_id, text):
    """把回复文本中提到的本地文件自动作为附件发出"""
    paths = extract_file_paths(text)
    for p in paths:
        log(f"检测到附件,开始发送: {p}")
        if send_file_reply(msg_id, p):
            log(f"附件已发送: {os.path.basename(p)}")

def gui_hide_bot_sessions(reason=""):
    """把 Bot 自己产生的 GUI 会话从侧栏隐藏（归档）。

    headless 会话不属于任何 workspace，会全部堆进 GUI 侧栏那个没名字的
    「未分组」组；归档掉它们之后这一组会直接消失。
    ⚠️ 只归档、不删除，会话文件原样保留在磁盘上（fbctl gui-restore 可放回来）。
    """
    if os.environ.get("FB_HIDE_HEADLESS", "1") == "0":
        return
    ok, out = fbctl("gui-hide", timeout=180)
    tag = f"({reason})" if reason else ""
    if ok and out:
        first = out.splitlines()[0]
        if "已经是干净的" not in first:
            log(f"GUI 侧栏治理{tag}: {first}")
    elif not ok:
        log(f"GUI 侧栏治理{tag} 失败: {out[:150]}")


def _schedule_gui_hide(reason=""):
    """放到后台线程做，别拖慢给用户的回复"""
    threading.Thread(target=gui_hide_bot_sessions, args=(reason,), daemon=True).start()


def ask_agent(task):
    """调用 DSH headless agent 处理任务"""
    log(f"转发任务给 DSH: {task[:100]}")
    # 统一打上指纹：headless 会话不登记进 workspace，会堆在 GUI 侧栏的「未分组」里。
    # fbctl gui-hide 靠这一行认出"这是 Bot 产生的会话"，从而只归档它们、绝不碰项目会话。
    task = PROMPT_TAG + "\n" + task
    env = dict(os.environ)
    env.pop("DEEPSEEK_API_KEY", None)
    # 完整工具链路径(含 node / 安卓 / gradle),让 DSH 能编译安卓项目
    env["PATH"] = TOOLCHAIN_PATH + ":" + env.get("PATH", "")
    env["HOME"] = HOME
    env["USER"] = os.environ.get("USER") or os.environ.get("LOGNAME") or "user"
    env["DSH_HOME"] = DSH_HOME          # 否则 dsh 会去找不存在的 ~/.dsh
    env["JAVA_HOME"] = JDK17_HOME
    env["ANDROID_HOME"] = ANDROID_HOME
    env["ANDROID_SDK_ROOT"] = ANDROID_HOME
    def _run():
        return subprocess.run(
            [DSH_BIN, "--profile", "headless", task],
            cwd=WORKSPACE,
            capture_output=True,
            text=True,
            timeout=600,
            env=env
        )

    try:
        proc = _run()
        log(f"DSH 返回码: {proc.returncode}")
        log(f"DSH stdout 长度: {len(proc.stdout)}")
        result = proc.stdout.strip()

        # 空输出 = 模型只产出了推理、没写正文(实测遇到过:out=611 全是 reasoning)。
        # 这是模型侧偶发故障,重试一次通常就好了。
        if not result:
            log(f"DSH 无正文(只输出推理),stderr: {(proc.stderr or '')[:200]}")
            log("重试一次…")
            proc = _run()
            result = proc.stdout.strip()
            log(f"重试后 stdout 长度: {len(proc.stdout)}")

        if not result:
            return ("⚠️ 这次没能生成回复（模型只输出了思考过程，没有正文）。\n"
                    "再发一次通常就好了。")
        log(f"DSH 返回: {result[:100]}")
        return result
    except subprocess.TimeoutExpired:
        return "⏰ DSH 处理超时(>10分钟)，任务可能仍在执行中。"
    except Exception as e:
        log(f"DSH 调用异常: {e}")
        return f"❌ DSH 调用失败: {e}"
    finally:
        # 这一轮产生的 headless 会话立刻归档，别让它出现在 GUI 侧栏
        _schedule_gui_hide("消息处理完成")

def get_dsh_port():
    """从 DSH 配置读取端口(默认 8080)"""
    try:
        cfg = os.path.join(DSH_HOME, "profiles", "web", "cordis.patch.yml")
        with open(cfg, encoding="utf-8") as f:
            for line in f:
                s = line.strip()
                if s.startswith("port:"):
                    port = re.findall(r"\d+", s)
                    if port:
                        return int(port[-1])
    except Exception:
        pass
    return 8080

def dsh_is_running():
    """检测 dsh web 进程是否存在,返回 (是否运行, 进程ID列表, 端口是否可用, 端口号)"""
    pids = []
    try:
        # 两种入口都要覆盖:bin/dsh web 与 lib/bin.js web
        r = subprocess.run(["pgrep", "-f", r"(bin/dsh|lib/bin\.js) web"],
                           capture_output=True, text=True, timeout=10)
        pids = [p for p in r.stdout.strip().split("\n") if p]
    except Exception as e:
        log(f"检测 dsh 进程失败: {e}")
    # 端口检测(用配置里的端口)
    port = get_dsh_port()
    port_ok = False
    try:
        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(2)
        port_ok = (s.connect_ex(("127.0.0.1", port)) == 0)
        s.close()
    except Exception:
        pass
    return (len(pids) > 0), pids, port_ok, port

def start_dsh():
    """先检测 DSH 是否在运行:在运行则提示无需启动;不在才启动"""
    log("收到启动DSH指令,先检测进程...")

    running, pids, port_ok, port = dsh_is_running()

    # 情况1: 端口正常监听 → 服务在跑,无需启动
    # (端口是最硬的证据:pgrep 在 PID 隔离环境里可能看不到进程)
    if port_ok:
        log(f"DSH 已在运行 (PID: {','.join(pids) or '未知'}),无需启动")
        msg = "ℹ️ DSH 已在运行,无需启动"
        if pids:
            msg += f"\n进程 PID: {','.join(pids)}"
        return msg + f"\n端口 {port}: 正常监听\n如需强制重启,请回复「重启DSH」"

    # 情况2: 进程存在但端口未监听 → 服务假死
    if running and not port_ok:
        log(f"DSH 进程存在(PID {','.join(pids)})但端口 {port} 未监听,服务可能异常")
        return (f"⚠️ DSH 进程存在(PID: {','.join(pids)}),但端口 {port} 未监听\n"
                f"服务可能已假死。如需重启,请回复「重启DSH」")

    # 情况3: 进程不存在 → 启动
    log("DSH 未运行,开始启动...")
    script = DSH_WEB_SCRIPT
    try:
        proc = subprocess.run(
            ["bash", script],
            capture_output=True,
            text=True,
            timeout=120,
            cwd=WORKSPACE
        )
        out = proc.stdout
        lan = ""
        for m in re.finditer(r"http://(?:\d{1,3}\.){3}\d{1,3}:\d+/\?token=[A-Za-z0-9_-]+", out):
            lan = m.group(0)
            break
        log(f"start-dsh 输出: {out[:200]}")

        # 启动后再验证一次(同样以端口为准)
        running2, pids2, port_ok2, port2 = dsh_is_running()
        if port_ok2 or running2:
            msg = "✅ DSH 启动成功"
            if pids2:
                msg += f"\n进程 PID: {','.join(pids2)}"
            msg += f"\n端口: {port2}"
            if lan:
                msg += f"\n局域网访问: {lan}"
            return msg
        else:
            return (f"❌ DSH 启动异常\n"
                    f"输出: {out[-300:] if out else '(无输出)'}")
    except subprocess.TimeoutExpired:
        return "❌ 启动DSH超时(>90秒)"
    except Exception as e:
        log(f"启动DSH异常: {e}")
        return f"❌ 启动DSH失败: {e}"

def restart_dsh():
    """强制重启 DSH(跳过检测)"""
    log("收到重启DSH指令,强制重启...")
    script = DSH_WEB_SCRIPT
    try:
        proc = subprocess.run(
            ["bash", script, "--restart"],
            capture_output=True, text=True, timeout=120, cwd=WORKSPACE)
        out = proc.stdout
        lan = ""
        for m in re.finditer(r"http://(?:\d{1,3}\.){3}\d{1,3}:\d+/\?token=[A-Za-z0-9_-]+", out):
            lan = m.group(0)
            break
        running2, pids2, port_ok2, port2 = dsh_is_running()
        if port_ok2 or running2:
            msg = "✅ DSH 已重启"
            if pids2:
                msg += f"\n进程 PID: {','.join(pids2)}"
            msg += f"\n端口: {port2}"
            if lan:
                msg += f"\n局域网访问: {lan}"
            return msg
        return f"❌ 重启后检测异常\n输出: {out[-200:] if out else '(无输出)'}"
    except Exception as e:
        log(f"重启DSH异常: {e}")
        return f"❌ 重启DSH失败: {e}"

# 已处理的消息 ID 缓存(去重,防止飞书重复投递事件导致重复回复)
_SEEN_MSGS = set()
_SEEN_LIMIT = 500
# 消息处理线程池:只开 1 个 worker = 串行处理。
# 目的是把耗时操作(尤其大文件分片下载)挪出 asyncio 事件循环,避免掐断 WebSocket 心跳。
_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="msg")


# -------------------- 断线补收(让消息不再丢) --------------------
# 飞书 WebSocket 断线期间发的消息【不会补发】—— 这是协议层特性,改不了。
# 唯一解法:断线恢复后主动去查会话历史,把漏掉的补处理一遍。
# 做法:每条消息都记下处理进度(create_time);重连/启动后从该时间点拉历史,
#       挑出"用户发的、且从没处理过的"补一遍。靠 last_ts + _SEEN_MSGS 去重,幂等。
LAST_SEEN_FILE = f"{SESSION_DIR}/last_seen.json"
CATCHUP_MAX_HOURS = 24      # 最多往回补多久(防呆,避免断线太久一次拉爆)
CATCHUP_MAX_PAGES = 5       # 单次最多翻几页(每页 50 条)
CATCHUP_MAX_MSGS = 20       # 单次最多补处理几条(每条都要调 DSH,防止积压时雪崩)


def _load_last_seen():
    try:
        with open(LAST_SEEN_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _mark_seen(chat_id, create_time_ms):
    """记录"处理到哪了":会话 ID + 最后一条消息的时间戳"""
    try:
        d = _load_last_seen()
        d["chat_id"] = chat_id or d.get("chat_id", "")
        d["last_ts"] = max(int(d.get("last_ts", 0)), int(create_time_ms or 0))
        d["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
        os.makedirs(SESSION_DIR, exist_ok=True)
        with open(LAST_SEEN_FILE, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log(f"记录补收进度失败: {e}")


def fetch_history(chat_id, start_ts_ms):
    """拉取会话历史消息(按时间升序,自动翻页),返回 items 列表"""
    items, page_token = [], ""
    start_s = max(0, int(start_ts_ms // 1000) - 1)   # 接口按【秒】,减 1 秒防边界漏
    try:
        t = get_token()
        for _ in range(CATCHUP_MAX_PAGES):
            url = ("https://open.feishu.cn/open-apis/im/v1/messages"
                   f"?container_id_type=chat&container_id={chat_id}"
                   f"&start_time={start_s}&page_size=50&sort_type=ByCreateTimeAsc")
            if page_token:
                url += f"&page_token={page_token}"
            r = _request("GET", url, headers={"Authorization": f"Bearer {t}"}, timeout=30)
            j = r.json()
            if j.get("code") != 0:
                log(f"补收: 拉历史失败 code={j.get('code')} msg={j.get('msg')}")
                break
            data = j.get("data") or {}
            items.extend(data.get("items") or [])
            if not data.get("has_more"):
                break
            page_token = data.get("page_token") or ""
            if not page_token:
                break
    except Exception as e:
        log(f"补收: 拉历史异常 {e}")
    return items


def catch_up_missed(reason="重连"):
    """断线恢复后把漏掉的消息补回来,返回补了几条"""
    st = _load_last_seen()
    chat_id = st.get("chat_id") or ""
    last_ts = int(st.get("last_ts", 0) or 0)
    if not chat_id or not last_ts:
        log(f"补收({reason}): 还没有进度基准,跳过")
        return 0
    floor = int((time.time() - CATCHUP_MAX_HOURS * 3600) * 1000)
    items = fetch_history(chat_id, max(last_ts, floor))

    # 先筛出真正需要补的:用户发的、从没处理过的、未删除的
    todo = [it for it in items
            if (it.get("message_id") or "")
            and int(it.get("create_time", 0) or 0) > last_ts
            and it.get("message_id") not in _SEEN_MSGS
            and not it.get("deleted")
            and (it.get("sender") or {}).get("sender_type") == "user"]

    # 积压太多时只补最近的几条:最新的问题最重要,也避免一次性把 DSH 调爆
    if len(todo) > CATCHUP_MAX_MSGS:
        log(f"补收({reason}): 积压 {len(todo)} 条,只补最近的 {CATCHUP_MAX_MSGS} 条")
        todo = todo[-CATCHUP_MAX_MSGS:]

    n = 0
    for it in todo:
        mid = it.get("message_id") or ""
        ct = int(it.get("create_time", 0) or 0)
        _SEEN_MSGS.add(mid)
        if len(_SEEN_MSGS) > _SEEN_LIMIT:
            _SEEN_MSGS.pop()
        n += 1
        log(f"补收({reason}): 发现漏掉的消息 {mid},补处理")
        try:
            _process(chat_id, mid, it.get("msg_type") or "",
                     (it.get("body") or {}).get("content") or "{}", ct, "补收")
        except Exception as e:
            log(f"补收处理失败 {mid}: {e}")
    log(f"补收({reason}): {'共补回 %d 条' % n if n else '没有漏掉的消息'}")
    return n


def schedule_catch_up(delay=0, reason="重连"):
    """后台线程里做补收 —— 绝不能在 SDK 的 asyncio 线程里直接跑,否则阻塞心跳"""

    def runner():
        if delay:
            time.sleep(delay)
        _EXECUTOR.submit(catch_up_missed, reason)

    threading.Thread(target=runner, daemon=True).start()


# -------------------- 快捷指令的"像不像命令"辅助判断 --------------------
# 原则(和 handle_message 里写的一致)：能用【完全匹配】就不用"包含"。
#
# 教训：自救命令原来用 `"重启DSH" in t and len(t) <= 15` 判断，结果
#   「别重启DSH」/「不要重启DSH」/「先别启动DSH」  → 真的执行了重启/启动
#   「重启DSH了吗」/「我昨天说重启DSH你做了吗」    → 也照做
# 现在只认【字面就是这两个词】，前后什么都不带、一个变体都不留 ——
# 认的词越少，误判越不可能（用户说他只会发「重启DSH」这种裸命令，
# 后面不会加任何标点或语气词）。唯一例外：DSH 的大小写不敏感。

# 名字后缀的黑名单：命中任一 → 判定为"一句话"，交给模型理解
_NAME_BAD_HEAD = set("的时中后前是要会能需不没意怎为如例注记请把用先再")
_NAME_BAD_TAIL = set("吗呢吧么的了吗啊呀")
_NAME_BAD_MID = ("什么", "怎么", "为什么", "如何", "怎样", "哪", "吗", "呢")
# 评论性说法（"这个东西挺好用"这种闲聊），冒号后面不可能是这种词
_NAME_BAD_COMMENT = ("挺", "很", "太", "真", "不错", "好用", "可以", "应该",
                     "需要", "已经", "一直", "有点", "比较", "非常", "要是", "如果")


def _looks_like_name(name):
    """「新建会话」后面的后缀像"名字"还是像"一句话"？

    教训：黑名单只有 的时中后前+标点 时，这些都会真的建出一个怪名字的会话：
      「新建会话要小心重名」   → 建了「要小心重名」
      「新会话是什么意思」     → 建了「是什么意思」
      「新建会话功能改好了吗」 → 建了「功能改好了吗」
    """
    if not name:
        return False
    if name[0] in _NAME_BAD_HEAD or name[0] in "，,。.、；;！!？?…":
        return False
    if name[-1] in _NAME_BAD_TAIL:
        return False
    if any(w in name for w in _NAME_BAD_MID):
        return False
    if any(w in name for w in _NAME_BAD_COMMENT):
        return False
    if re.search(r"[，。！？；]", name) or len(name) > 30 or "\n" in name:
        return False
    return True


def _looks_like_path(p):
    """「发文件」后面的东西像不像一个路径？

    教训：「发文件的时候记得先压缩」被当成命令，回了
    「❌ 文件不存在: <家目录>/的时候记得先压缩」。

    这里用【正面证据】而不是黑名单：要有 / ~ . \\ 这类路径特征，
    或者这个文件**真的存在**。否则一律当成一句话交给模型。
    """
    if not p or len(p) > 200 or "\n" in p:
        return False
    if re.search(r"[，。！？；]", p):
        return False
    if any(c in p for c in "/~.\\"):
        return True
    # 光秃秃一个名字（没有 / . ~）：只有真存在才认
    try:
        return os.path.isfile(os.path.join(WORKSPACE, os.path.expanduser(p)))
    except Exception:
        return False


# 「归档会话」/「查看归档」都是两段式:先回列表,【下一条数字】才是目标。
# 用 chat_id -> 截止时间 记住这个状态,过期自动失效 ——
# 否则以后随手发个数字就可能把某个项目归档掉 / 恢复掉。
_ARCHIVE_PENDING = {}      # 待归档目标(来自「归档会话」)
_ARCHIVE_RESTORE = {}      # 待恢复目标(来自「查看归档」)
ARCHIVE_PENDING_TTL = int(os.environ.get("FB_ARCHIVE_TTL_SEC", "180"))


def handle_message(text, chat_id=""):
    """处理快捷指令,返回(是否已处理, 回复内容)。非快捷消息返回(False, '')

    快捷指令 = 本地处理、不调模型、0 token。
    自然语言的说法由模型通过 feishu-bridge 技能调 fbctl 完成,效果一样。

    ⚠️ 匹配原则:能用【完全匹配】就不用"包含",能用【长度限制】就加上。
    教训:曾经用 "会话列表" in 文本 判断,导致用户说
    「针对返回的会话列表,默认会话放第一个…」这种**需求描述**被当成命令,答非所问。
    后来又出过更狠的:「别重启DSH」真的去重启了、「新建会话是干嘛的」真的建了个
    叫「是干嘛的」的会话 —— 所以现在这些命令一律【字面相等】。
    """
    t = text.strip()
    # 去掉首尾标点和空白,便于完全匹配
    core = t.strip("。！？!?…~～ \u3000\"'`")

    # ---- 完全匹配类:整句话就是命令(防止长句里出现关键词被误判) ----
    # 飞书那边已经做了按钮,按钮发过来的就是「查看会话」这 4 个字,
    # 所以只认它一个,而且【字面必须一样】—— 连尾随句号都不认
    # (原来那 8 个同义词:会话列表/项目列表/查看项目/有哪些项目/… 已全部删掉)
    if t == "查看会话":
        # 回到普通模式:取消两个待定状态,免得后面的数字被当成归档/恢复目标
        _ARCHIVE_PENDING.pop(chat_id, None)
        _ARCHIVE_RESTORE.pop(chat_id, None)
        fbctl("sync")                      # 先跟 DSH 同步(标题可能变过)
        ok2, lst = fbctl("list")
        tips = "\n回复数字进入会话；也可以直接说「切到 XXX」。"
        msg = lst + tips if ok2 else "❌ 读不到会话列表,请查看日志"
        # 顺带把"已归档"的首几个贴在后面（顶格、单独空一行）—— 只给标题不给序号,
        # 因为这里的数字已经被"进入会话"占用了,不能同时当恢复序号。
        ok3, prev = fbctl("archived-list", "--preview", "--limit", "3")
        if ok3 and prev.strip():
            msg += "\n\n" + prev.strip()
        return True, msg

    # ---- 归档会话:先给列表,下一条数字就是归档目标 ----
    if t == "归档会话":
        _ARCHIVE_RESTORE.pop(chat_id, None)
        fbctl("sync")
        _ARCHIVE_PENDING[chat_id] = time.time() + ARCHIVE_PENDING_TTL
        ok2, lst = fbctl("list")
        tip = (f"\n回复数字，我就把对应的会话在 GUI 里归档"
               f"（{ARCHIVE_PENDING_TTL // 60} 分钟内有效，发「查看会话」可取消）。")
        return True, (lst + tip if ok2 else "❌ 读不到项目列表,请查看日志")

    # ---- 查看归档:完整列出已归档会话,下一条数字就是恢复目标 ----
    if t == "查看归档":
        _ARCHIVE_PENDING.pop(chat_id, None)
        ok2, out = fbctl("archived-list")
        if not ok2:
            return True, "❌ 读不到归档列表,请查看日志"
        _ARCHIVE_RESTORE[chat_id] = time.time() + ARCHIVE_PENDING_TTL
        tip = (f"\n回复数字，我就把它从归档里恢复"
               f"（{ARCHIVE_PENDING_TTL // 60} 分钟内有效，发「查看会话」可取消）。")
        return True, (out + tip if out.strip() else out)

    # 纯数字:切换项目；如果刚发过「归档会话」/「查看归档」,这个数字是它的目标
    if core.isdigit():
        if _ARCHIVE_PENDING.pop(chat_id, 0) > time.time():
            ok, out = fbctl("archive-project", core, timeout=60)
            return True, (out if ok else f"❌ {out or '归档失败,请查看日志'}")
        if _ARCHIVE_RESTORE.pop(chat_id, 0) > time.time():
            ok, out = fbctl("archived-restore", core, timeout=60)
            return True, (out if ok else f"❌ {out or '恢复失败,请查看日志'}")
        ok, _ = fbctl("switch", core)
        if ok:
            ok2, cur = fbctl("current")
            return True, f"✅ 已切换\n{cur}" if ok2 else "✅ 已切换"
        return True, "数字超出范围,请先发「查看会话」看列表"

    # 注:原来这里有个「检查连接/连接状态/查看连接」快捷指令(调 health_report()),
    #     那是早期飞书会莫名断连时加的。现在 Bot 有 self_check_loop() 自动每 5 分钟
    #     体检、断线还有补收,手动查询已无必要,按用户要求删掉。
    #     health_report() 函数本身保留着,要恢复就在这加一行:
    #         if t == "检查连接": return True, health_report()

    # ---- 自救命令:字面就是这两个词,前后什么都不带 ----
    # 「别重启DSH」「重启DSH了吗」「重启一下DSH」「强制重启」「重启DSH。」
    # 现在统统不匹配,统一交给模型理解(见上面的教训)
    if t.lower() == "重启dsh":
        return True, restart_dsh()
    if t.lower() == "启动dsh":
        return True, start_dsh()

    # 重启飞书 Bot 自己(systemd Restart=always 会自动拉起)
    # 只认这 4 个字、【字面一样】,连尾随句号都不认
    # (原来那 7 个同义词:重启bot/重启机器人/重启飞书bot/飞书重启/重启你/重起飞书 已删)
    if t == "重启飞书":
        return True, CMD_RESTART_BOT

    # 发文件 <路径>
    if t.startswith("发文件"):
        p = t[len("发文件"):].strip().strip('`"\'')
        if not p:
            return True, "用法: 发文件 文件路径\n如: 发文件 ~/project/apk/app-debug.apk"
        if not _looks_like_path(p):
            return False, ""          # 像一句话不像路径 → 交给模型理解
        if not os.path.isabs(p):
            p = os.path.join(WORKSPACE, p)
        p = os.path.abspath(os.path.expanduser(p))
        if not os.path.isfile(p):
            return True, f"❌ 文件不存在: {p}"
        return True, f"📎 正在发送附件: {p}"

    # 新建项目:前缀匹配,但名称必须【像名字】而不是【一句话】
    for prefix in ("新建会话", "新会话", "创建会话", "开新会话", "新增会话",
                   "新建项目", "新项目"):
        if t.startswith(prefix):
            name = t[len(prefix):].strip().strip(":：-—,，.。 ")
            if not name:
                # 只发了「新建会话」没带名字 —— 明确告诉用户格式:空一格 + 名称
                return True, (f"⚠️ 还差一个会话名称，格式是：\n"
                              f"　　{prefix} 会话名称\n"
                              f"比如：{prefix} 安卓开发\n"
                              f"（「{prefix}」后面空一格，再接名字）")
            # 排除"新建会话的时候要注意…"这类需求描述（判定逻辑见 _looks_like_name）
            if not _looks_like_name(name):
                break        # 当普通消息交给模型理解

            # ① 先在 GUI 里真建一个会话(这样两边都有,不会串台)
            sid, err = create_gui_session(name)
            if sid:
                # ② 再把它绑定成一个飞书侧项目,并设为当前
                ok, out = fbctl("new", name, "--session", sid)
                if ok:
                    return True, (f"✅ 已新建会话「{name}」\n"
                                  f"· GUI 里已出现同名会话\n"
                                  f"· 飞书已绑定并切换过去，现在可以直接聊了")
                return True, f"⚠️ GUI 会话已建（{sid}），但飞书侧绑定失败：{out}"
            # 建不出来就退回"只建飞书侧项目",并说明原因
            ok, out = fbctl("new", name)
            if ok:
                return True, (f"⚠️ 已在飞书侧新建项目「{name}」，但**没能在 GUI 里创建会话**。\n"
                              f"原因: {err}\n"
                              f"（这个项目暂时没有 GUI 绑定）")
            return True, "❌ 新建失败,请查看日志"

    return False, ""

# -------------------- 收到的文件:分类存放 --------------------
# 你在飞书发来的文件统一存到 ~/software/feishu-bot/file/ 下(只存入站,不含我发给你的)。
# 注:表情包(sticker)与文件夹(folder)是飞书平台限制,无法下载,不会落盘。
RECV_SUBDIRS = {
    "image": "images",      # 图片
    "media": "videos",      # 视频
    "audio": "audios",      # 语音
    "file": "documents",    # 文档/压缩包/apk 等
}
RECV_OTHER_SUBDIR = "other"     # 认不出来的统统进这里

# 飞书把"以文件形式发送的音视频"也标成 type=file,不给任何类型标记,
# 所以还要按【扩展名】再纠正一次归类(实测:以"文件"形式发出来的 .mp4 / .m4a
# 都被标成了 file,如果不看后缀就会全都堆进 documents/)。
RECV_EXT_SUBDIRS = {}
for _e in (".mp4", ".mov", ".avi", ".mkv", ".wmv", ".flv", ".webm",
           ".m4v", ".3gp", ".mpg", ".mpeg", ".ts", ".rmvb"):
    RECV_EXT_SUBDIRS[_e] = "videos"
for _e in (".m4a", ".mp3", ".opus", ".ogg", ".oga", ".amr", ".wav",
           ".aac", ".flac", ".wma", ".aiff", ".caf", ".mid"):
    RECV_EXT_SUBDIRS[_e] = "audios"
for _e in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp",
           ".heic", ".heif", ".tif", ".tiff", ".svg", ".ico"):
    RECV_EXT_SUBDIRS[_e] = "images"

# 能认出是"文档"的后缀;不在这个表里、也不是上面音视频图片的 → other/
DOC_EXTS = {
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".txt", ".md", ".rst", ".csv", ".tsv", ".rtf", ".odt", ".ods", ".odp",
    ".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz", ".iso",
    ".apk", ".ipa", ".exe", ".msi", ".deb", ".rpm", ".dmg",
    ".json", ".xml", ".yaml", ".yml", ".toml", ".ini", ".conf", ".log",
    ".html", ".htm", ".css", ".js", ".ts", ".py", ".sh", ".bat",
    ".java", ".kt", ".c", ".cpp", ".h", ".go", ".rs", ".php", ".sql",
}

# 无原始文件名时的中文前缀(图片/语音/表情包飞书不给名字)
RECV_PREFIX = {"image": "图片", "audio": "语音", "media": "视频", "file": "文件"}

# 常见 Content-Type → 扩展名(飞书语音等不给文件名,靠响应头补)
CT_EXT = {
    "image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif",
    "image/webp": ".webp", "image/bmp": ".bmp",
    "audio/opus": ".opus", "audio/ogg": ".ogg", "audio/mpeg": ".mp3",
    "audio/amr": ".amr", "audio/wav": ".wav", "audio/x-wav": ".wav",
    "audio/mp4": ".m4a", "audio/x-m4a": ".m4a",
    "video/mp4": ".mp4", "video/quicktime": ".mov",
    "application/pdf": ".pdf", "application/zip": ".zip",
    "text/plain": ".txt",
}


def subdir_for(type_hint, ext):
    """决定文件该进哪个分类目录

    优先级:
      1. 扩展名能认出是 图片/视频/音频 → 对应目录(最可靠,飞书的类型标记不准)
      2. 扩展名能认出是文档         → documents/
      3. 飞书消息类型明确(image/media/audio) → 对应目录
      4. 全都认不出                 → other/
    """
    e = (ext or "").lower()
    if e in RECV_EXT_SUBDIRS:
        return RECV_EXT_SUBDIRS[e]
    if e in DOC_EXTS:
        return "documents"
    if type_hint in ("image", "media", "audio"):
        return RECV_SUBDIRS[type_hint]
    return RECV_OTHER_SUBDIR


# -------------------- 临时工作目录 --------------------
# 下载先落到 temp/,成功后再搬到 file/<分类>/,这样正式目录永远不会出现半截文件;
# 万一进程被杀,残留也只在 temp/ 里,启动时自动清理。
# 跟着 DATA_DIR 走(以前写死 BOT_DIR,配了 FB_DATA_DIR 时它仍落在程序目录里,不一致)。
TEMP_DIR = f"{DATA_DIR}/temp"
TEMP_KEEP_HOURS = 24          # temp/ 里超过这个时长的残留自动清掉


def _temp_file(suffix=".part"):
    os.makedirs(TEMP_DIR, exist_ok=True)
    stamp = f"{time.strftime('%Y%m%d-%H%M%S')}_{os.getpid()}_{int(time.time() * 1000) % 100000}"
    return os.path.join(TEMP_DIR, f"{stamp}{suffix}")


def _remove_quietly(path):
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except Exception:
        pass


def cleanup_temp():
    """清掉 temp/ 里超期的残留(崩溃/中断留下的半截文件)"""
    try:
        os.makedirs(TEMP_DIR, exist_ok=True)
        cutoff = time.time() - TEMP_KEEP_HOURS * 3600
        n = 0
        for f in glob.glob(os.path.join(TEMP_DIR, "*")):
            try:
                if os.path.isfile(f) and os.path.getmtime(f) < cutoff:
                    os.remove(f)
                    n += 1
            except Exception:
                pass
        if n:
            log(f"temp/ 已清理 {n} 个残留文件")
        return n
    except Exception as e:
        log(f"temp/ 清理异常: {e}")
        return 0


def _finalize(tmp_path, type_hint, ext, orig_name):
    """把 temp/ 里的临时文件搬到 file/<分类>/ 的最终位置"""
    dest = recv_path(type_hint, ext, orig_name)
    shutil.move(tmp_path, dest)
    return dest



def _safe_name(name):
    """把飞书给的文件名洗成安全的单层文件名(去路径分隔符/控制字符)"""
    name = os.path.basename(str(name or "").replace("\\", "/"))
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    return name[:120]


def _ext_from_headers(r, fallback_ext):
    """优先用响应头里的文件名/类型判断扩展名,失败再用兜底值"""
    cd = r.headers.get("Content-Disposition", "")
    m = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^\";]+)', cd)
    if m:
        ext = os.path.splitext(m.group(1))[1]
        if ext:
            return ext.lower()
    ct = (r.headers.get("Content-Type") or "").split(";")[0].strip().lower()
    return CT_EXT.get(ct, fallback_ext)


def recv_path(type_hint, ext, orig_name=""):
    """生成收到文件的保存路径:file/<分类>/<名字>_<日期-时间><扩展名>

    分类由 subdir_for() 决定:扩展名优先,其次看飞书给的消息类型。
    - 有原始文件名(文档/视频) → 保留原名的主干部分,便于日后辨认
    - 无原始文件名(图片/语音) → 用中文前缀 + 时间戳
    - 同名自动加 _2/_3,绝不覆盖
    """
    d = os.path.join(FILE_DIR, subdir_for(type_hint, ext))
    os.makedirs(d, exist_ok=True)
    clean = _safe_name(orig_name)
    stem = os.path.splitext(clean)[0] if clean else ""
    if not stem:
        stem = RECV_PREFIX.get(type_hint, "文件")
    stamp = time.strftime("%Y%m%d-%H%M%S")
    path = os.path.join(d, f"{stem}_{stamp}{ext}")
    n = 2
    while os.path.exists(path):
        path = os.path.join(d, f"{stem}_{stamp}_{n}{ext}")
        n += 1
    return path


CHUNK_BYTES = 32 * 1024 * 1024           # 飞书规定:分片下载单次不得超过 32MB
MAX_DOWNLOAD_BYTES = 3 * 1024 ** 3       # 本地保护上限 3GB,避免异常情况撑爆磁盘


def _download_by_range(url, headers, type_hint, fallback_ext, orig_name):
    """Range 分片下载:飞书对 ≥100MB 的资源只允许分片获取。

    各片先顺序写进 temp/ 里的临时文件,全部拿齐后再搬到 file/<分类>/。
    返回 (路径, 错误说明)。
    """
    pos, tmp, fh, ok, ext = 0, _temp_file(), None, False, fallback_ext
    try:
        while True:
            h = dict(headers)
            h["Range"] = f"bytes={pos}-{pos + CHUNK_BYTES - 1}"
            r = _request("GET", url, headers=h, timeout=180)
            if r.status_code not in (200, 206):
                raise RuntimeError(f"HTTP {r.status_code}: {r.text[:160]}")
            if not r.content:
                break
            if fh is None:
                ext = _ext_from_headers(r, fallback_ext)
                fh = open(tmp, "wb")
            fh.write(r.content)
            pos += len(r.content)
            if pos > MAX_DOWNLOAD_BYTES:
                raise RuntimeError(f"文件超过本地上限 {MAX_DOWNLOAD_BYTES // 1024 ** 3}GB,已中止")
            if len(r.content) < CHUNK_BYTES:
                break                      # 最后一片
            log(f"分片下载中: 已完成 {pos / 1048576:.1f} MB")
        if fh is None:
            raise RuntimeError("服务端没有返回任何数据")
        fh.close()
        fh = None
        dest = _finalize(tmp, type_hint, ext, orig_name)   # temp/ → file/<分类>/
        ok = True
        log(f"分片下载完成: {dest} ({pos}字节)")
        return dest, ""
    except Exception as e:
        return "", f"分片下载失败: {e}"
    finally:
        if fh is not None:
            fh.close()
        if not ok:
            _remove_quietly(tmp)           # 失败的半截文件只留在 temp/,直接删掉


def download_file(file_key, msg_id, type_hint, orig_name=""):
    """下载飞书消息资源(图片/视频/语音/文件)并存入 file/

    返回 (本地路径, 错误说明):
      成功 -> (path, "")
      失败 -> ("", 原因)
    超过 100MB 的 type=file 会自动改用 Range 分片下载。
    """
    # 飞书下载资源 API 需要 type 参数: image/file
    # (音频、视频都归在 type=file 下)
    url = (f"https://open.feishu.cn/open-apis/im/v1/messages/{msg_id}/resources/{file_key}"
           f"?type={type_hint}")
    fallback = os.path.splitext(_safe_name(orig_name))[1] or (
        ".png" if type_hint == "image" else ".bin")
    try:
        headers = {"Authorization": f"Bearer {get_token()}"}
        r = _request("GET", url, headers=headers, timeout=60)
    except Exception as e:
        log(f"下载资源异常 type={type_hint}: {e}")
        return "", f"网络异常: {e}"

    if r.status_code == 200:
        tmp = _temp_file()
        try:
            with open(tmp, "wb") as f:
                f.write(r.content)
            path = _finalize(tmp, type_hint, _ext_from_headers(r, fallback), orig_name)
        except Exception as e:
            _remove_quietly(tmp)
            return "", f"保存失败: {e}"
        log(f"资源已下载: {path} ({len(r.content)}字节)")
        return path, ""

    # 234037 = 超过 100MB,需要改走 Range 分片下载
    if "234037" in r.text:
        if type_hint != "file":
            return "", f"资源超过 100MB,且 {type_hint} 类型不支持分片下载"
        log(f"资源超过 100MB,改用 Range 分片下载 (type={type_hint})")
        return _download_by_range(url, headers, type_hint, fallback, orig_name)

    log(f"下载资源失败 type={type_hint} HTTP {r.status_code}: {r.text[:160]}")
    return "", f"飞书返回 HTTP {r.status_code}: {r.text[:120]}"



# -------------------- 富文本 post 解析 --------------------
def parse_post(content):
    """解析富文本消息,返回 (纯文本, 图片key列表, 视频key列表, 文件条目列表)

    优先用 content_v2 的 md 标签(保留原文格式);没有则按标签逐个拼。
    file_items 是 [(file_key, file_name, is_folder), ...],交给调用方下载。

    ⚠️ 实测踩过的坑:飞书把"一次发多个文件"投递成
       post + **顶层 files[]**,此时 content / content_v2 全都是空的 [[]]。
       老代码只走 content/content_v2 → 整批文件静默蒸发,
       Bot 只回了句"富文本内容没显示出来,请重发",只能一个个重发。
       原生 JSON 长这样:
         {"title":"","content":[[]],"content_v2":[[]],
          "files":[{"file_key":"file_v3_…","file_name":"x.mp4","is_folder":false}, …]}
    """
    texts, img_keys, media_keys, file_items = [], [], [], []

    def walk_rows(rows):
        for row in rows or []:
            for tag in (row if isinstance(row, list) else [row]):
                if not isinstance(tag, dict):
                    continue
                t = tag.get("tag")
                if t == "md":
                    md = tag.get("text", "")
                    texts.append(md)
                    # 富文本里内嵌的图片是 ![img](img_xxx) 形式,要单独抓出来下载
                    img_keys.extend(re.findall(r"!\[[^\]]*\]\((img_[A-Za-z0-9_\-]+)\)", md))
                elif t == "text":
                    texts.append(tag.get("text", ""))
                elif t == "a":
                    texts.append(f"{tag.get('text','')}({tag.get('href','')})")
                elif t == "at":
                    texts.append(f"@{tag.get('user_name') or tag.get('user_id','')}")
                elif t == "img":
                    if tag.get("image_key"):
                        img_keys.append(tag["image_key"])
                    texts.append("[图片]")
                elif t == "media":
                    if tag.get("file_key"):
                        media_keys.append(tag["file_key"])
                    if tag.get("image_key"):
                        img_keys.append(tag["image_key"])
                    texts.append("[视频]")
                elif t == "emotion":
                    texts.append(f"[表情:{tag.get('emoji_type','')}]")
                elif t == "code_block":
                    # 保留语言标记,否则拼出来是个裸代码块,模型看不出是什么语言
                    lang = str(tag.get("language") or "").strip().lower()
                    body = tag.get("text", "") or ""
                    texts.append(f"```{lang}\n{body}\n```" if lang else f"```\n{body}\n```")
                elif t == "hr":
                    texts.append("---")

    # content_v2 里的 md 更完整,优先
    if content.get("content_v2"):
        walk_rows(content["content_v2"])
    else:
        walk_rows(content.get("content"))

    # ★ 顶层 files[]:"一次发多个文件"走这里,和上面的 content 是并列的
    for f in content.get("files") or []:
        if not isinstance(f, dict):
            continue
        fn = str(f.get("file_name") or "")
        if f.get("is_folder"):
            texts.append(f"[文件夹] {fn} (飞书不支持下载文件夹内容)".strip())
        elif f.get("file_key"):
            file_items.append((f["file_key"], fn, False))
            texts.append(f"[文件] {fn}".strip())

    body = "\n".join(x for x in texts if x)
    title = content.get("title") or ""
    return ((f"{title}\n{body}".strip() if title else body),
            img_keys, media_keys, file_items)


def on_msg(data_event: P2ImMessageReceiveV1):
    """飞书 SDK 回调 —— 在 asyncio 事件循环里【同步】执行。

    这里只做去重,然后把真正的处理丢给工作线程:
    下载大文件(>100MB 要分片)可能耗时几分钟,一旦占住事件循环,
    WebSocket 心跳就发不出去,连接会被服务端掐断。
    单线程池 = 消息串行处理,顺带避免记忆文件读改写竞争。
    """
    mid = data_event.event.message.message_id

    # 去重:同一 message_id 只处理一次(必须在主线程同步做)
    if mid in _SEEN_MSGS:
        log(f"跳过重复消息 (mid={mid})")
        return
    _SEEN_MSGS.add(mid)
    if len(_SEEN_MSGS) > _SEEN_LIMIT:
        _SEEN_MSGS.pop()

    _EXECUTOR.submit(_handle_msg, data_event)


def _handle_msg(data_event: P2ImMessageReceiveV1):
    """飞书事件回调 → 归一成普通字段,交给 _process"""
    msg = data_event.event.message
    _process(
        chat_id=getattr(msg, "chat_id", "") or "",
        chat_type=getattr(msg, "chat_type", "") or "",
        mid=msg.message_id,
        mtype=msg.message_type,
        content_str=msg.content or "{}",
        create_time=int(getattr(msg, "create_time", 0) or 0),
    )


# -------------------- 图片合并(方案 A:先攥住,等后续文字) --------------------
# 背景:飞书把"图片 + 文字"拆成两条独立消息。老逻辑里图片会立刻被一轮孤立的
#       "描述这张图" 处理掉,紧随其后的文字只能看到那段描述文本、看不到图本身,
#       于是出现"我发截图是有目的的,你却复述了一遍画面"。
#
# 做法:收到【纯图片】先不回,攥在手里 IMAGE_MERGE_WINDOW 秒;
#   - 窗口内来了文字 → 把图片接到那条文字上,图文合成一轮,只回一次
#   - 窗口期满还没文字 → 按"带上下文的看图"处理(见 _process 里的纯图片分支)
# 这个窗口设 0 就完全关闭合并,退回"图片立刻处理"的老行为。
IMAGE_MERGE_WINDOW = float(os.environ.get("FB_IMAGE_MERGE_SEC", "8"))
_PENDING_IMAGES = {}                       # chat_id -> holder
_PENDING_LOCK = threading.Lock()


def _hold_image_for_merge(chat_id, mid, text, image_paths, create_time, chat_type):
    """把纯图片暂存起来等后续文字。连续发多张图会合并成一批。"""
    with _PENDING_LOCK:
        old = _PENDING_IMAGES.get(chat_id)
        if old:
            if old.get("timer"):
                old["timer"].cancel()
            image_paths = old["paths"] + image_paths
            text = old["text"] + "\n" + text
        holder = {"paths": image_paths, "text": text, "mid": mid,
                  "create_time": create_time, "chat_type": chat_type}
        timer = threading.Timer(IMAGE_MERGE_WINDOW, _flush_pending_image, args=(chat_id,))
        timer.daemon = True
        holder["timer"] = timer
        _PENDING_IMAGES[chat_id] = holder
        timer.start()
    log(f"图片已暂存,等 {IMAGE_MERGE_WINDOW:g} 秒看有没有后续文字 (mid={mid})")


def _take_pending_images(chat_id):
    """取走暂存的图片(如果窗口内来了文字就调用它)。返回路径列表。"""
    with _PENDING_LOCK:
        holder = _PENDING_IMAGES.pop(chat_id, None)
    if not holder:
        return []
    if holder.get("timer"):
        holder["timer"].cancel()
    log(f"接上了刚才暂存的 {len(holder['paths'])} 张图,与这条文字合并处理")
    return holder["paths"]


def _flush_pending_image(chat_id):
    """窗口到期还没等到文字 —— 交给处理线程单独处理这张图"""
    with _PENDING_LOCK:
        holder = _PENDING_IMAGES.pop(chat_id, None)
    if not holder:
        return
    log(f"图片合并窗口到期,单独处理 (mid={holder['mid']})")
    _EXECUTOR.submit(_process_pure_image, holder)


def _process_pure_image(holder):
    """纯图片(没等到文字)的处理:带上完整上下文,带着目的看图"""
    image_paths = holder["paths"]
    img_list = "\n".join(f"- {p}" for p in image_paths)
    prompt = build_prompt("（我发了一张图片，没有配文字）", with_image=True)
    prompt += (f"\n\n【用户发来图片】\n{img_list}\n"
               f"(先结合上面的上下文判断用户发这张图想干什么,再带着这个目的去看图并直接回答。"
               f"上下文里看不出目的时,用一句话说明你看到了什么,并问一句他想让你做什么。"
               f"不要做没有目的的逐项复述。)")
    result = ask_agent(prompt)
    _finish(holder["mid"], "image", holder["text"], result)


def _finish(mid, mtype, text, result):
    """回复 + 补发附件 + 记进项目。图片合并的延迟路径也走这里,保证行为一致。"""
    reply(mid, result)
    # 回复里出现本地文件路径(如 apk/png/pdf)→ 自动作为附件补发
    if mtype in ("text", "post"):
        send_files_from_text(mid, result)
    # 记到当前项目(没有项目就进 default),这样另一边也看得到
    fbctl("log", text)
    fbctl("reply", result)
    fbctl("maintain")          # 顺带做滚动/归档(开销很小)
    log(f"已回复: {result[:50]}")


def _process(chat_id, mid, mtype, content_str, create_time=0, chat_type=""):
    """处理一条消息 —— 实时收到的和断线后补收的都走这里"""
    log(f"收到消息类型: {mtype} (mid={mid}, chat={chat_id}, chat_type={chat_type})")
    _LAST_MSG_TIME["ts"] = time.time()   # 更新最近消息时间(健康检查用)
    _mark_seen(chat_id, create_time)     # 记录进度,断线补收要用

    # 解析不同消息类型的 content
    text = ""
    image_paths = []
    saved = []        # 已落盘的文件路径(用于回执)
    failed = []       # 下载失败的文件(用于明确告知用户,不静默吞掉)
    try:
        content = json.loads(content_str or "{}")
    except Exception:
        content = {}

    def fetch(key, thint, name=""):
        """下载并登记结果,返回本地路径或空串"""
        p, err = download_file(key, mid, thint, name)
        if p:
            saved.append(p)
        else:
            failed.append(f"{name or thint}: {err}")
        return p

    if mtype == "text":
        text = content.get("text", "")
    elif mtype == "post":
        # 富文本(图文混排):手机上粘贴长内容常发成这个
        # 也包含"一次发多个文件"的形态(content 为空 + 顶层 files[])
        text, img_keys, media_keys, file_items = parse_post(content)
        for k in img_keys:
            p = fetch(k, "image")
            if p:
                image_paths.append(p)
        for k in media_keys:
            fetch(k, "media")
        for fk, fn, _folder in file_items:
            fetch(fk, "file", fn)
        if not text:
            # ⚠️ 兜底之前先把原始 content 记下来 —— 以前兜底赋值在前,
            #    把下面那句"未能解析出内容"的诊断整个挡掉了,出事时无从查证。
            log(f"⚠️ post 未能解析出内容,原始 content: {(content_str or '')[:1200]}")
            text = "[富文本]"
    elif mtype == "image":
        # content: {"image_key": "xxx"}
        key = content.get("image_key")
        if key:
            p = fetch(key, "image")
            if p:
                image_paths.append(p)
        text = "[图片]"
    elif mtype == "media":
        # 视频: {"file_key": "...", "file_name": "...", "duration": N}
        key = content.get("file_key")
        if key:
            fetch(key, "media", content.get("file_name", ""))
        text = "[视频]"
    elif mtype == "audio":
        # 语音: {"file_key": "xxx", "duration": N} — 暂不转写
        key = content.get("file_key")
        if key:
            fetch(key, "audio")
        text = "[语音]"
    elif mtype == "file":
        # 文档/音视频文件: {"file_key": "...", "file_name": "..."}
        key = content.get("file_key")
        if key:
            fetch(key, "file", content.get("file_name", ""))
        text = f"[文件] {content.get('file_name') or ''}".strip()
    elif mtype == "folder":
        # 飞书平台限制:只能拿到文件夹名字,无法下载其内容
        text = f"[文件夹] {content.get('file_name') or ''} (飞书不支持下载文件夹内容)".strip()
    elif mtype == "sticker":
        # 飞书平台限制:表情包不支持下载,只能收到 file_key
        text = "[表情包] (飞书不支持下载表情包,无法保存)"
    else:
        text = f"[{mtype}] 收到,此消息类型暂不处理"

    # 没解析出任何内容时,把原始 content 打出来,便于定位飞书的特殊结构
    if not text.strip() and not image_paths:
        log(f"⚠️ 未能解析出内容,原始 content: {(content_str or '')[:800]}")

    # 非文字类消息:把落盘位置(或失败原因)明确回执给用户
    if saved:
        text = f"{text}\n已保存到: " + "、".join(
            os.path.relpath(p, HOME) for p in saved)
    if failed:
        text = f"{text}\n❌ 保存失败: " + "; ".join(failed)
        log(f"下载失败明细: {failed}")

    log(f"消息内容: {text[:80]} (图片{len(image_paths)}张, 落盘{len(saved)}个, 失败{len(failed)}个)")

    # 快捷指令(仅对文本):本地处理,不调模型,0 token
    if mtype == "text":
        handled, cmd_reply = handle_message(text, chat_id)
        if handled:
            # 「重启飞书」:调用本地脚本重启自己(和「重启DSH」一个套路)
            if cmd_reply == CMD_RESTART_BOT:
                reply(mid, "🔄 飞书 Bot 正在重启，起来后我会再发一条结果给你…")
                mark_restart_pending(chat_id, mid)   # 让新进程知道要回报
                log(f"收到「重启飞书」指令，调用脚本: {RESTART_SCRIPT}")
                try:
                    # 独立会话启动,避免杀 Bot 时连带中断脚本自己。
                    # 脚本输出写进 logs/restart.log,方便事后核对走的是哪条路径。
                    with open(os.path.join(LOG_DIR, "restart.log"), "a", encoding="utf-8") as lf:
                        lf.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} 收到「重启飞书」指令 =====\n")
                        lf.flush()
                        subprocess.Popen(["bash", RESTART_SCRIPT],
                                         start_new_session=True,
                                         stdout=lf, stderr=lf)
                except Exception as e:
                    log(f"调用重启脚本失败({e}),改为直接退出让 systemd 拉起")
                time.sleep(3)
                os._exit(1)      # 兜底:即使脚本没生效,退出也能被 Restart=always 拉起
            # 快捷指令的回复一律走纯文本:内容是我们自己排版的(会话列表等),
            # 送进 markdown 卡片反而会被渲染器改掉换行/空行(见 reply 的说明)
            reply(mid, cmd_reply, force_text=True)
            log(f"快捷指令: {cmd_reply[:60]}")
            send_files_from_text(mid, cmd_reply)   # 发文件指令:自动带附件
            return

    # ---- 图片合并(方案 A)----
    # 如果刚才有张纯图片还在等,把它接到这条文字上,图文合成一轮。
    # (放在快捷指令判断【之后】:如果是「查看会话」这类命令,不该把图吃掉,
    #  让它的定时器到期后自己单独处理)
    adopted = []
    if mtype in ("text", "post"):
        adopted = _take_pending_images(chat_id)
        if adopted:
            image_paths = adopted + image_paths

    # 纯图片先攥住 IMAGE_MERGE_WINDOW 秒,等用户接着打的那句话;
    # 到期没等到才单独处理(见 _flush_pending_image → _process_pure_image)
    if IMAGE_MERGE_WINDOW > 0 and mtype == "image" and image_paths and not adopted:
        _hold_image_for_merge(chat_id, mid, text, image_paths, create_time, chat_type)
        return

    # 普通消息:交给 DSH(提示词里带上该项目在 GUI/飞书两侧的背景)
    if mtype in ("text", "post"):
        prompt = build_prompt(text, with_image=bool(image_paths))
        if image_paths:
            img_list = "\n".join(f"- {p}" for p in image_paths)
            prompt += (f"\n\n【用户发来图片】\n{img_list}\n"
                       f"(结合上面的上下文和用户这句话去看图,直接回答他要的,保持简洁)")
        result = ask_agent(prompt)
    elif image_paths:
        # 纯图片消息(没有配文字)。
        # ❌ 老做法:甩一个孤立的提示词"请用一句话简洁描述你看到了什么" ——
        #    模型看不到任何上下文,只能复述画面。用户发截图通常是"有目的"的
        #    (针对它做事 / 接着上文说),结果答非所问。
        # ✅ 现在:先带上完整项目上下文,让模型结合上文判断意图,再带着目的看图;
        #    判断不出来才说明看到了什么并问一句,而不是无脑复述。
        img_list = "\n".join(f"- {p}" for p in image_paths)
        prompt = build_prompt("（我发了一张图片，没有配文字）", with_image=True)
        prompt += (f"\n\n【用户发来图片】\n{img_list}\n"
                   f"(先结合上面的上下文判断用户发这张图想干什么,再带着这个目的去看图并直接回答。"
                   f"上下文里看不出目的时,用一句话说明你看到了什么,并问一句他想让你做什么。"
                   f"不要做没有目的的逐项复述。)")
        result = ask_agent(prompt)
    else:
        result = text  # 视频/语音/文档等:直接回执落盘位置,不消耗 token

    _finish(mid, mtype, text, result)



# -------------------- 连接健康检查 --------------------
# 断线/重连次数直接从自己的日志里统计(飞书 SDK 的 ws 客户端以 INFO 级打印
# "trying to reconnect for the Nth time" / "disconnected to ..." / "connected to ..."),
# 不再单独维护一个 health.json。
_BOT_START = time.time()
_LAST_MSG_TIME = {"ts": time.time()}   # 最近收到消息的时间


def _count_in_logs(needle):
    """在日志(含轮转后的 .1)里数出现次数"""
    total = 0
    for f in (LOG_FILE, f"{LOG_FILE}.1"):
        try:
            with open(f, encoding="utf-8", errors="ignore") as fh:
                total += sum(1 for line in fh if needle in line)
        except Exception:
            pass
    return total

def health_report():
    """生成健康报告(断线次数来自日志统计)"""
    now = time.time()
    up = now - _BOT_START
    idle = now - _LAST_MSG_TIME["ts"]
    reconnects = _count_in_logs("trying to reconnect for the")
    def fmt(sec):
        if sec < 60: return f"{int(sec)}秒"
        if sec < 3600: return f"{int(sec//60)}分钟"
        return f"{int(sec//3600)}小时{int(sec%3600//60)}分"
    return (f"🩺 连接状态\n"
            f"运行时长: {fmt(up)}\n"
            f"最近消息: {fmt(idle)}前\n"
            f"断线重连: {reconnects}次(已自动恢复)\n"
            f"服务: 正常")

# -------------------- 自监控线程 --------------------
# 定期检查与飞书的连通性;连续失败则主动退出,由 systemd(Restart=always)拉起
SELFCHECK_INTERVAL = 300      # 每 5 分钟检查一次
SELFCHECK_MAX_FAIL = 3        # 连续失败 3 次则退出重启

# ⚠️ 会话【永久保留】—— 这里曾经有一个 cleanup_sessions() 会自动删除
#    "未登记的 headless 会话",已彻底移除。
#    理由:飞书每条消息产生的 DSH 会话是该次交互的完整记录,属于要长期保存的数据;
#    自动删除会造成不可逆的数据丢失,且与"会话长久保持"的设计目标相悖。
#    磁盘占用很小(每次约 30KB),不需要清理。要清理请人工确认后手动操作。

def self_check_loop():
    """后台自监控:定期验证飞书连通性 + 清理 temp/ 中转残留

    注意:【绝不清理 DSH 会话】。会话是永久保留的数据。
    """
    fails = 0
    while True:
        time.sleep(SELFCHECK_INTERVAL)
        try:
            r = _request("POST",
                "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
                json={"app_id": APP_ID, "app_secret": APP_SECRET}, timeout=15)
            ok = (r.status_code == 200 and r.json().get("code") == 0)
        except Exception as e:
            ok = False
            log(f"自监控: 连通性检查异常 {e}")
        if ok:
            if fails > 0:
                # 只在"从失败恢复"时记录,避免每次成功都写日志造成噪音
                log(f"自监控: 连接已恢复(之前失败{fails}次)")
            fails = 0
            # 只清理 temp/ 里自己的下载中转残留(超过24小时的);
            # 绝不动 DSH 的会话文件 —— 会话永久保留
            cleanup_temp()
        else:
            fails += 1
            log(f"自监控: 连通性检查失败({fails}/{SELFCHECK_MAX_FAIL})")
            if fails >= SELFCHECK_MAX_FAIL:
                log("自监控: 连续失败达阈值,主动退出由 systemd 重启")
                os._exit(1)   # 退出进程,systemd Restart=always 会拉起


h = EventDispatcherHandler.builder(ENC_KEY, VERIFY_TOKEN).register_p2_im_message_receive_v1(on_msg).build()

if __name__ == "__main__":
    log("飞书 Bot 启动（转发 → DSH headless, 多会话+启动DSH）")
    cleanup_temp()   # 清掉上次崩溃/中断留在 temp/ 的残留
    # 启动时先治理一次 GUI 侧栏(把之前攒下的 headless 会话归档掉)
    _schedule_gui_hide("启动")
    # 启动自监控线程(守护线程,随主进程退出)
    threading.Thread(target=self_check_loop, daemon=True).start()
    log(f"自监控已启动(每{SELFCHECK_INTERVAL}秒检查一次)")

    client = Client(app_id=APP_ID, app_secret=APP_SECRET, event_handler=h)
    # 断线重连成功 → 立刻补收断线期间漏掉的消息
    # (SDK 只在"重连"时回调;首次连接不会触发,下面用延迟线程兜底)
    client.on_reconnecting = lambda: log("⚠️ 连接断开,SDK 正在重连...")
    client.on_reconnected = lambda: schedule_catch_up(0, "重连")
    schedule_catch_up(delay=20, reason="启动")

    # 若是"重启飞书"起来的,连上之后回报一条结果(约 15 秒后 WS 已就绪)
    def _notify_later():
        time.sleep(15)
        notify_restart_done()
    threading.Thread(target=_notify_later, daemon=True).start()

    try:
        client.start()
    except Exception as e:
        log(f"Bot 异常退出: {e}")
        raise

