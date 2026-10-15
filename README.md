# 摸鱼bot · MemoBot

> 基于 **Python + QQ 官方机器人 SDK（botpy）** 的本地记事与文件助手 —— **零网页服务、数据全在本地、支持热插拔私有扩展**。

[![Python](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/)
[![Data](https://img.shields.io/badge/data-local--only-2ea44f)](#核心特性)
[![Tests](https://img.shields.io/badge/tests-690%2B%20passing-brightgreen)](test/run-tests.py)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)

把 QQ 变成随手记事本与文件发端：群里发一句 `待办 xxx` 就记下，随手一句闲聊自动收成灵感，需要时把电脑里的文件（文本类会渲染成图片）直接发到群或私聊。数据落在本机 SQLite，原生桌面客户端与机器人共享同一份存储。

> 💡 **普通用户看这里**：保姆级图文教程、一键安装包与更新说明都在 **QQ 群文件**里（**群号 1129867238**）。
> 本仓库面向**源码运行、二次开发与 Issue 追踪**，文档只写到「能跑起来、能改起来」为止。

---

## 核心特性

- **双流记事**：`memo`（我要做）与 `idea`（我的想法）两类记录走同一张表、同一套处理界面；群里以触发词开头 = 待办，非 @ 的闲聊 = 灵感。
- **原生桌面端（Tkinter，无浏览器）**：「配置中心」十个分区 + 「脑内抽屉」卡片轮播 + 回收站，读写全部复用下面的模块，界面层不重复实现业务。
- **文本类文件渲染成图片**：`.md / .txt / .json / .py / .csv …`（≤150KB）经 `markdown` → HTML → 系统 Chrome 截图后按图片发送，绕开手机端「文件卡片打不开」。
- **纯本地、无对外暴露面**：不监听任何端口、不跑网页服务；凭据只写本机 `.env`，不存在对外接口。
- **热插拔扩展**：往 `extensions/local/` 放一个 `.py` 或 `.zip` 就能加自定义指令，**不改任何公开代码**，也不进公开仓库。
- **单实例 + 进程托管**：OS 级命名互斥体保证同时只跑一个实例；管家进程统一托管 GUI 与机器人，配合计划任务做登录自启与崩溃重启。

## 快速开始（源码运行）

环境：**Python 3.10+**、**Node.js**（仅「渲染成图片发送」需要）。

```bash
git clone https://github.com/phononprismlab/memo-bot.git
cd memo-bot

pip install -r requirements.txt   # qq-botpy / python-dotenv / markdown / pillow
npm install                       # playwright-core（渲染链路用）

python memo-launcher.py --gui     # 打开桌面窗口：配置中心 + 脑内抽屉
```

首次打开后，在 **配置中心 → ① 连接 QQ** 填入 QQ 开放平台申请的 `AppID` / `AppSecret` 并保存，再到 **⑦ 运行方式**点「启动」。
其余设置（触发词、搜索目录、开机自启、忽略表、播报文案…）全在配置中心里改，不需要手写配置文件。

**平台侧两个开关必须开**（在 QQ 群里设置，代码改不了；不开的表现是「机器人像死了一样」）：

| 开关 | 不开会怎样 |
| --- | --- |
| 消息范围 = **获取群内全部消息** | 收不到群里非 @ 的普通发言 ⇒ 灵感全丢 |
| **机器人主动在群聊内发言** = 开 | 发不出启动播报与主动推送 |

截图见 [`docs/qq-group-switches.png`](docs/qq-group-switches.png)。要用**私聊**，还需在 QQ 开放平台 → 功能配置里开启「单聊」。

### 数据目录在哪

`paths.py` 把「只读的代码目录」与「可写的数据目录」分开：

| 运行方式 | 数据目录 |
| --- | --- |
| 源码直跑 | 代码目录本身（`memos.db` / `.env` / `settings.json` 就在仓库里，已 gitignore） |
| 安装版 | `%LOCALAPPDATA%\memo-bot`（靠 `MEMO_DATA_DIR` 指过去） |

### 指令速查

群聊与私聊走**同一套识别逻辑**，私聊里不需要 @。

| 发什么 | 做什么 |
| --- | --- |
| `待办 内容` | 记一条待办（触发词可改） |
| `查待办` / `查灵感` | 查看最近的待办 / 未整理的思绪 |
| `搜索灵感 关键词` | 按正文、标签或结论查找思绪 |
| `发文件 关键词` | 在配置的搜索根里查找并发送文件 |
| `摸鱼` / `回旋镖` | 抽一条放松内容 / 翻回一条旧记录 |
| `导出 [md/txt/json]` | 导出全部待办与思绪 |
| `忽略 内容` / `忽略 列表` | 管理忽略规则（有其他机器人的群用得上） |
| `设置` / `设置 <键> <值>` | 在聊天里查看 / 修改本机设置 |
| `帮助` / `你好` | 指令菜单 / 功能介绍 |

## 架构与核心文件

单一职责、互相解耦：界面层只做渲染，业务逻辑全在下面这些模块里，回归测试直接驱动它们（不依赖 GUI）。

**进程与入口**

| 文件 | 职责 |
| --- | --- |
| `memo-launcher.py` | 统一启动入口：补 `sys.path`、定数据目录，再把参数转交真正的脚本 |
| `hub.py` | 管家：托盘图标 + 托管「桌面窗口 + 机器人」，负责单实例与开机自启 |
| `bot.py` | 机器人本体：网关事件监听、指令路由、落库与回复（`--self-test` / `--list` / `--log`） |
| `memo_gui.py` | 桌面窗口：配置中心 + 脑内抽屉 + 回收站（纯界面） |
| `botctl.py` | 进程控制：启 / 停 / 重启 / 开机自启、`bot.pid` 判活 |
| `single_lock.py` | OS 级命名互斥体，保证同时只有一个实例 |
| `alerts.py` | 账号掉线告警：读 `bot.link` 链路心跳 + 只读探活判在线，走托盘气泡 / webhook 带外通知（`hub.py` 调它） |
| `shortcut.py` | 任务栏图标：AUMID 常量唯一定义处 + 开机在开始菜单建带同一个 AUMID 的快捷方式（`hub.py` 调它） |
| `pylnk3.py` | 写 Windows `.lnk` 的第三方纯 Python 模块（`shortcut.py` 用，随包分发） |

**数据与配置**

| 文件 | 职责 |
| --- | --- |
| `store.py` | SQLite 存储层：事务保护、状态机、结构化查询、首次建库播种 |
| `settings.py` | `settings.json` 的读写、校验与指令解析（三个配置入口共用一份键值） |
| `config_core.py` | 配置后端：`.env` 按行读写、依赖自检、状态快照 `full_state()` |
| `paths.py` | 路径中心：区分代码目录与数据目录 |
| `ignore.py` | 忽略表：命中规则的群 / 私聊消息一律跳过 |
| `push.py` | 会话发现与播报目标（`QQ_PUSH_TARGETS`） |
| `version.py` | 版本号真源 + 「检查更新」的多源探测 |

**能力模块**

| 文件 | 职责 |
| --- | --- |
| `send_file.py` | 发端（纯 HTTP）：文件上传与发送，含降级重试 |
| `md2img.py` + `md2png.mjs` | 渲染链路：Python 转 HTML → Node + `playwright-core` 用系统 Chrome 截图 |
| `media.py` | 收图：远程 URL 落盘、按内容哈希去重 |
| `export.py` | 导出排版（机器人指令与桌面按钮共用这一处） |
| `ideas.py` | 灵感池 CLI：导出 / 列出 / 标记处理结果 |
| `moyu.py` + `moyu-data.json` | 摸鱼库：宽容指令解析、抽签防重复、内置种子 + 用户覆盖双库 |
| `diag.py` | 诊断包：环境 + 脱敏配置摘要 + 日志尾部打成 zip（**不含**用户数据与凭据） |

**扩展与工程**

| 文件 | 职责 |
| --- | --- |
| `extensions/__init__.py` | 扩展加载器；`scan_dir()` 是「什么算一个扩展包」的唯一判定处 |
| `extmgr.py` | 扩展管理：导入 / 列出 / 移出，带路径穿越与体积校验 |
| `extensions/example.py` | 扩展示例模板（不注册任何指令） |
| `extensions/local/` | 私有扩展目录（已 gitignore，不进公开仓库） |
| `test/run-tests.py` | 回归测试套件（纯 stdlib） |
| `packaging/` | 安装脚本、内嵌 Python 运行时与[开发者说明](packaging/摸鱼bot-开发者说明.md) |

## 自定义扩展

不改任何公开代码就能加指令 —— 放进 `extensions/local/` 即可。该目录已 gitignore，本地运行 = 公开代码 + 你的私有扩展。

**三步加一条指令**：新建 `extensions/local/myext.py` → 写一个 `register(reg)` → 重启机器人，`帮助` 菜单会自动带上你写的 `help` 行。

```python
# extensions/local/myext.py
def register(reg):
    def handle(text, mentioned):
        return "👋 你好，这是一条自定义回复"
    reg.add(["打招呼", "hi"], handle, help="打招呼 —— 回你一句")
```

**handler 契约**

```python
def handle(text, mentioned) -> str | list[str] | None
```

| 参数 / 返回 | 含义 |
| --- | --- |
| `text` | 已剥掉 @ 标记的指令全文（如 `"我的指令 参数"`） |
| `mentioned` | 是否 @ 机器人（非 @ 时群里普通发言也会走到这） |
| `None` | 不处理，继续走后面的逻辑 |
| `str` | 回一条消息 |
| `list[str]` | 依次回多条（`msg_seq` 自动递增） |

- handler 是**同步函数**，框架用线程执行 ⇒ 里面可以直接做阻塞操作，不会卡机器人心跳。
- 指令名是**精确匹配**；想只在 @ 时接话，在 handler 里 `if not mentioned: return None`。
- `reg.api` 另提供 `search_files(q, limit=5)` 与 `SEARCH_ROOT`。
- 某个扩展导入失败 / `register()` 抛异常 → **只打 warning，不影响 bot 启动**；后注册者优先，所以私有扩展可以覆盖公开扩展的同名指令。

完整契约（执行顺序、导入闸门、打包与自测清单）见 [`packaging/摸鱼bot-开发者说明.md`](packaging/摸鱼bot-开发者说明.md)，模板见 `extensions/example.py`。

## 实现要点

几处不写下来就会踩坑的地方：

- **两条链路并存**：`bot.py`（收）走 WebSocket 网关常驻；`send_file.py`（发）走纯 HTTP，不建网关连接 —— 互不抢连接，可同时运行。
- **群全量消息要补解析器**：botpy 1.2.1 未注册 `GROUP_MESSAGE_CREATE`，`install_group_message_parser()` 补上，否则事件被网关丢弃；`C2C_MESSAGE_CREATE` 官方已注册。
- **私聊**：记录落同一张 `memos` 表，`group_id` = `c2c:<user_openid>`；群指令回复走**被动回复**（带 `msg_id`），语义更准也不占主动消息额度。
- **发送**：上传必须带 `file_name`（否则卡片显示「未命名」）；`305013`（审核超时）/ `850027` / `40093001` 自动重试 2 次；群与单聊的上传接口相互独立。
- **被动回复的 `msg_seq`** 在 bot（1~3）与 CLI（从 100 起）之间必须错开，否则同一 `msg_id` 会撞「消息被去重」。
- **渲染成图片**：正文里的相对路径图片靠注入 `<base href="{源文件目录}/">` 加载；整篇超过 14000 CSS px 才自动分页。

**平台限制**（都是开关问题，不是 bug）：

- **主动推送**：群需开「机器人主动在群聊内发言」，未开时自动降级为被动回复（需 4 分钟内群里 @ 过 bot）；单聊无需开关。
- **忽略表**：`.env` 里的 `MEMO_IGNORE` 规则删不掉（防误删把循环放出来），只有 `ignore.json` 里的能被指令改。
- 生产存储可把 `store.py` 换成任意数据库后端，接口不变。

## 回归测试

```bash
python test/run-tests.py
```

**纯 stdlib，不依赖 pytest**，690+ 项断言，覆盖指令解析与分发、状态机与老库迁移、`.env` 按行写入、扩展加载容错、真实路由端到端、导出格式、桌面端源码护栏等。

- 所有落盘用例都改写到临时目录，**不碰真实数据**（`memos.db` / `moyu-recent.json` / `moyu-user.json` / `.env`）。
- 仓库里用 AST 扫描锁死几条硬约束（例如子进程必须带 `CREATE_NO_WINDOW`），改坏了会被测试挡住。

本地验证（无需 QQ 凭据）：

```bash
python memo-launcher.py --gui   # 桌面窗口
python bot.py --self-test       # 模拟群消息，验证识别 + 落库
python bot.py --list            # 打印已记录的待办
python send_file.py --self-test # 验证文件类型识别 / 编码 / 凭据
python moyu.py 3                # 摸鱼库抽 3 条
```

## 环境变量参考

桌面配置中心能完成绝大多数设置；下面这些给开发或手动部署用。未填写时走默认值。

**必填**

| 变量 | 说明 |
| --- | --- |
| `QQ_BOT_APPID` | QQ 机器人 AppID（<https://q.qq.com> 创建获取） |
| `QQ_BOT_SECRET` | AppSecret（机器人**密钥**，不是 Token）；兼容旧名 `QQ_BOT_TOKEN` |
| `QQ_PUSH_TARGETS` | 启动播报发到哪些会话：`group:<群openid>;c2c:<用户openid>`，可多个。在配置中心勾选即可写入，不用自己找 openid |
| `QQ_GROUP_OPENID` / `QQ_C2C_OPENID` | 旧版遗留键，仅当 `QQ_PUSH_TARGETS` 没配时回退使用 |

**可选（行为 / 路径）**

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `MEMO_DATA_DIR` | 代码目录 | 数据目录（安装版指到用户目录） |
| `QQ_BOT_SANDBOX` | `false` | 走沙箱域名（本地联调设 `true`） |
| `MEMO_TRIGGER` | `待办` | 待办触发词 |
| `QQ_STARTUP_MSG` | `我上班了，开摸！` | 启动播报文案（留空不播报） |
| `MEMO_SEARCH_ROOT` | 用户主目录 | 「发文件」搜索根，支持多个（分号 / 换行分隔）；`.ssh` / `.aws` / `.config` 会自动跳过 |
| `MEMO_WORKSPACE_ROOT` | 仓库目录 | 搜索里加权的目录 |
| `MEMO_SETTINGS_FILE` | `settings.json` | 本机设置文件路径 |
| `MEMO_IGNORE` / `MEMO_IGNORE_FILE` | 空 / `ignore.json` | 忽略表种子与规则文件 |
| `MEMO_NO_SEED` | 空 | 设 `1` 可关掉「首次建库播种示例」 |
| `MEMO_NODE` / `MEMO_CHROME` / `MEMO_PLAYWRIGHT_CORE` | 走 PATH | 外部依赖定位，找不到时明确报错并列出已尝试路径 |

> 私有扩展需要的变量（如 `MEMO_FORTUNE_FILE`）由扩展自行读取，加在同一个 `.env` 里即可 —— 它们不属于公开配置。

## 发版

**版本号只有一处真源**：`VERSION` 文件；`version.py` 的常量与它一致（有测试盯着），`packaging/memo-bot.iss` 也直接读那个文件。

1. 改 `VERSION`（与 `version.py`）→ 提交推送；
2. `python packaging/_build_dist.py` → `ISCC.exe packaging\memo-bot.iss`，产物 `packaging\output\memo-bot-setup-v<版本>.exe`；
3. 在 GitHub 建一个 **tag 为 `v<版本>` 的 Release**，把 exe 原样上传成资产。

🔴 第 3 步的 tag 与文件名必须与 `version.py` 里的模板一致（`v{ver}` / `memo-bot-setup-v{ver}.exe`），否则用户点「下载新版」会 404 —— 有护栏盯着这两处。

配置中心「⑩ 版本 → 检查更新」走 jsdelivr 读远端 `VERSION` 比对。想换成自己的下载源，只需设 `MEMO_DOWNLOAD_BASE`，文件名保持一致即可。

## 提交前检查（隐私清单）

仓库里**不写死任何本机路径、不含凭据、不含个人扩展**。以下都已进 `.gitignore`，提交前扫一眼即可：

- `.env` / `.env.bak` —— QQ 凭据
- `memos.db` 及备份 —— 个人记录
- `settings.json` / `ignore.json` / `last_group_msg.json` / `moyu-recent.json` —— 含本机文案与真实群信息
- `moyu-user.json` —— 本机覆盖的那份摸鱼库
- `*.log` / `bot.pid` —— 运行时产物
- `extensions/local/` —— 私有指令

## 社区与反馈

- **Bug / 功能建议**：[GitHub Issues](https://github.com/phononprismlab/memo-bot/issues)。写清重现步骤，再点一下「配置中心 → ⑩ 版本 → 导出诊断包」，把桌面生成的 zip 一起发来 —— 里面只有版本、运行环境、脱敏配置和最近一段日志，**不含**你的待办与密钥。
- **人工帮助**：发邮件到 <prismaticlab@hotmail.com>。
- **QQ 交流群**：**1129867238** —— 保姆级图文教程、安装包与常见问题答疑都在**群文件**里。
- **仓库内另有**（面向普通用户，非开发文档）：[安装指南](安装指南.md) · [功能清单](功能清单.md) · [更新日志](v0.3.2更新日志.md)

## License

[MIT](LICENSE) © 2026 PrismaticLab
