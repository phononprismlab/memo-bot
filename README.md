# memo-bot · 群备忘 + 发文件机器人

本地运行的 QQ 群机器人，走 QQ 官方机器人（**WebSocket 收 / HTTP 发**），只需出网，**无需公网 IP / 公网回调**。

## 能力
1. **备忘**：群消息以触发词（默认「备忘」）开头 → 记为 `memo`，落本地 SQLite，并回执。
2. **灵感**：群里**非 @** 的普通发言 → 自动收作 `idea`（**静默记录，不打扰群**）。
3. **发**：把本地文件经机器人**主动推送**到指定群；文本类文件**渲染成图片**发送。
4. **启动播报**：每次启动在群里发一句「我上班了，开摸！」（文案见 `.env` 的 `QQ_STARTUP_MSG`）。
5. **摸鱼**：群内发「摸鱼」→ 从内置库里随机抽一条放松动作 / 摸鱼点子，帮忙从工位上分神 30 秒。
6. **群指令**：

   | 指令 | 作用 | 是否需 @ |
   |---|---|---|
   | `备忘 <内容>` | 记一条备忘 | 否 |
   | `查备忘` | 看最近 10 条备忘 | 否 |
   | `查灵感` | 看**还没处理**的灵感（含待处理/已处理/已决议/已丢弃计数） | 否 |
   | `发文件 <关键词>` | 搜**配置的根目录**（默认你的用户主目录，可用 `MEMO_SEARCH_ROOT` 改）的文件、渲染成图片发送；命中多个列出来让你回复序号选 | 否 |
   | `摸鱼`（可加条数 / 分类） | 随机来一条**放松动作 / 摸鱼点子**，歇一下 | 否 |
   | `帮助` | 显示指令菜单 | 否 |

   - **@机器人**时可简写（`发 设计说明`、`发送 xxx`）。
   - **不 @** 时只认明确写法（`发文件` / `找文件` / `发送文件`），避免「发工资了吗」这类误触。
   - 「发文件」支持模糊匹配（如「设计说明」能命中「设计风格参考说明-2026-10-08.md」），回复**序号**或**文件名片段**即可选定。
   - 以上是**内置指令**。想加自己的指令（天气、运势、系统状态…）→ 见 [可扩展性](#可扩展性加你自己的指令)：
     自定义指令放 `extensions/local/`，**不会进公开仓库**。

## 群内需开启的两个开关（QQ 群设置 → 机器人）
1. **机器人可获取的群聊消息范围 = 获取群内全部消息** → 机器人能收到**非 @** 的群消息（`GROUP_MESSAGE_CREATE`）。
2. **机器人主动在群聊内发言 = 开** → 机器人可**主动推送**（不再受 5 分钟被动窗口限制）。

> 实测（2026-10-08）：两个开关打开后，主动推送文本与文件均成功；未开启前主动推送报 `40034105 主动消息失败, 无权限`。

## 文件
- `bot.py` — 收端（WebSocket）：识别 + 落库 + 回执 + 群指令；含 `--self-test` / `--list` / `--log <文件>`。
  - 运行时用 `install_group_message_parser()` 给 botpy 补上 `GROUP_MESSAGE_CREATE` 解析器（官方 1.2.1 未注册该事件，会被网关丢弃）。
  - `--log <文件>` 把 stdout/stderr 与 logging 重定向到文件，供 `pythonw.exe` 无控制台常驻运行。
- `send_file.py` — 发文件（纯 HTTP）：**零第三方依赖**，含 `--self-test`。默认主动推送，失败自动降级被动回复。
- `md2img.py` — Markdown/纯文本 → PNG（Python `markdown` 转 HTML，调 `md2png.mjs` 截图）。含 CLI：`python md2img.py <文件> [输出目录]`。
- `md2png.mjs` — Node + `playwright-core` 用系统 Chrome 渲染截图（可纵向切片）。
- `ideas.py` — **灵感池入口**：导出 / 列出 / 标记灵感处理结果（供「处理灵感的专用会话」用）。
- `ideas-bubbles.py` — 把灵感库渲染成**单文件 HTML** 的「思维泡泡」轮播页（`灵感泡泡.html`）。
- `moyu.py` — 摸鱼库抽签（指令解析 + 防重复），供「摸鱼」指令调用。含 CLI。
- `moyu-data.json` — 摸鱼库内容（65 条），**改这个文件即可增删内容，不用动代码**。
- `store.py` — SQLite 存储层（单文件 `memos.db`，含 `kind` = memo/idea、`status` = open/done/decided/dropped）。
- `extensions/` — **扩展机制**：`__init__.py` 加载器 + `example.py` 模板（不注册任何指令）。
- `extensions/local/` — **私有扩展目录（已 gitignore，不进公开仓库）**，放你自己的自定义指令。
- `test/run-tests.py` — 回归测试（纯 stdlib，122 项）。
- `requirements.txt` — Python 依赖：`qq-botpy`（官方 SDK，导入名 `botpy`）/ `python-dotenv` / `markdown` / `pillow`。
- `package.json` — Node 侧依赖：`playwright-core`（仅渲染成图片时用到）。
- `.env.example` — 凭据与配置模板。

## 发送格式：文本类文件渲染成图片
**手机端文件卡片常打不开**（尤其 `.md` 这类无默认应用的类型），而把内容拆成多条文本消息又太碎。因此：

| 文件类型 | 发送方式 |
|---|---|
| 文本类（`.md/.txt/.json/.py/.js/.csv/.log/.html/.yml` 等）且 ≤150KB | **渲染成图片发送**（Markdown 转 HTML → Chrome 截图 → `file_type=1`），手机端原生可看、可缩放、保留排版 |
| 图片 / 视频 / 语音 | 对应富媒体（手机原生可看） |
| 其他二进制 / 超大文本 | 文件卡片（`file_type=4`） |

- 渲染链路：`md2img.py`（Python `markdown` 库 → HTML）→ `md2png.mjs`（Node + `playwright-core` 用系统 Chrome 截图）。
- 默认**单张长图**（宽 760 CSS px、dpr=2）；整篇超过 14000 CSS px 才自动分页。
- 🔴 **正文里的相对路径图片**靠注入 `<base href="{源文件目录}/">` 加载（HTML 写在临时目录，否则图片全部不显示）；正文图片限高 600px。
- CLI：`--as-text` 强制文本直发、`--as-file` 强制文件卡片。
- 🔴 被动回复的 `msg_seq` 在 bot（1~3）与 CLI（从 100 起）之间**必须错开**，否则同一 `msg_id` 会撞 `40054005 消息被去重`。

## 备忘 vs 灵感
| | 触发 | 落库 `kind` | 是否回执 |
|---|---|---|---|
| 备忘 | `@bot 备忘 xxx`，或任意位置以「备忘」开头 | `memo` | 回「已记到秘书会话 ✅」 |
| 灵感 | 群里**非 @** 的普通发言 | `idea` | **不回执**（静默） |

灵感的处理状态（`status`）：

| 状态 | 含义 | 导出标记 |
|---|---|---|
| `open` | 待处理 | `[ ]` |
| `done` | 已处理（产生了行动并完成） | `[x]` |
| `decided` | 已决议（有结论，但不产生行动） | `[~]` |
| `dropped` | 已丢弃（不成立 / 无效） | `[-]` |

`查备忘` / `查灵感` 分别只看对应类别（`查灵感` 只列 `open`）；`python bot.py --list` 打印全部并标注类别。

## 灵感池：给「专门处理灵感的会话」用的入口
灵感是碎片，需要在电脑上集中处理（评估 → 做/不做 → 留结论）。`ideas.py` 提供这个闭环：

```bash
python memo-bot/ideas.py --export              # 导出「待处理」灵感 → 工作区 灵感池.md（逐条编号）
python memo-bot/ideas.py --list                # 终端列出待处理（--all 看全部）
python memo-bot/ideas.py --done 1,3 --note "已做成 X"   # 标记处理完（有行动）
python memo-bot/ideas.py --decide 4 --note "结论：不做 X"  # 标记已决议（有结论、无行动）
python memo-bot/ideas.py --drop 2 --note "与现有方案重复"  # 标记丢弃
python memo-bot/ideas.py --stats               # 只看计数
```

- **序号**（`1,2,3…`）相对**当前待处理列表**，每次 `--export` 后固定；也支持传 id 前 8 位。
- 处理过的灵感从「待处理」消失、进 `灵感池.md` 的归档区，**不会被重复捞**。
- 存储字段：`status`（open/done/decided/dropped）+ `note`（结论）+ `processed_at`（处理时间）。

> 新会话开场直接说「处理灵感」即可 —— 项目记忆里已登记这条入口。

## 摸鱼库：群内发「摸鱼」随机歇一下
内容全部是**内置静态库**（`moyu-data.json`，65 条），不调 AI、不联网、零延迟。

| 指令 | 结果 |
|---|---|
| `摸鱼` / `放松` / `歇会儿` | 抽 1 条（**默认池 = 放松 + 摸鱼，不含冷知识**） |
| `摸鱼 3` / `摸鱼 3 条` | 一次抽 3 条（上限 5 条） |
| `摸鱼 冷知识` | 只抽冷知识 |
| `摸鱼 3 冷知识` | 条数与分类可组合 |
| `摸鱼 全部` | 三类混抽 |

### 🔴 指令识别是「宽容」的，不是精确匹配
群里没人会规规矩矩打「摸鱼」两个字。识别走 **归一化 → 前缀匹配 → 参数解析** 三步：

| 你打的 | 归一化后 | 结果 |
|---|---|---|
| `摸鱼！` `摸鱼~` `摸鱼。` | `摸鱼` | ✅ |
| `摸摸鱼` | `摸鱼`（重复字压缩） | ✅ |
| `想摸鱼` `来点摸鱼` `该摸鱼了` | `摸鱼`（剥前置语气词） | ✅ |
| `摸鱼吗` `摸鱼吧` `摸鱼一下` | `摸鱼`（剥后置语气词） | ✅ |
| `摸个鱼` | 同义词表 | ✅ |
| `摸鱼 3 条` | `摸鱼3条` | ✅ 抽 3 条 |
| `今天不想摸鱼了` | — | ❌ 不是指令（句中，不触发） |

- **@ 机器人**时若参数不认识（如 `摸鱼 来点啥`），回一句参数提示；
  **非 @** 时不打扰群聊，按普通发言处理。
- **防重复**：最近抽过的 20 条记在 `moyu-recent.json`，抽的时候排除；某个池快抽完一轮就重置。
- 加内容：直接编辑 `moyu-data.json` 的 `items` 数组，`id` 不重复即可（`relax-21`、`slack-26`…），**不用改代码**。
- CLI：`python moyu.py` / `python moyu.py 3` / `python moyu.py --cat trivia` / `python moyu.py --parse "摸鱼！"` / `python moyu.py --stats`。

## 灵感泡泡：把灵感库放成一个轮播页面
`ideas-bubbles.py` 读 `memos.db`，生成**单文件 HTML**（数据内嵌，`file://` 直接打开，无需服务器）。

```bash
python memo-bot/ideas-bubbles.py              # → 工作区根目录 灵感泡泡.html
python memo-bot/ideas-bubbles.py --open-only  # 只放待处理的
python memo-bot/ideas-bubbles.py --include-memo
python memo-bot/ideas-bubbles.py --serve      # 起本地服务，页面里点一下**直接写库**
python memo-bot/ideas-bubbles.py --apply 灵感操作.json   # 应用离线模式导出的操作
```

- 气泡从底部「思维源」升起、横向漂移、顶部淡出，循环不停；一轮内不重复，放完自动重洗。
- **全文展示**：每条灵感的正文完整渲染在气泡里，永不截断（长文本自动缩小字号、必要时横跨整个舞台）。
- **页内处理**：点击气泡 → 浮层显示全文 + 结论输入 + 四个按钮（已处理 / 已决议 / 丢弃 / 恢复待处理）。
  - `--serve` 直连模式：点按钮**直接写 `memos.db`**（适合本机用）。
  - 双击 HTML（离线）：操作暂存浏览器 localStorage，顶部显示「待提交 N 条」，点它导出 `灵感操作.json`，再跑 `--apply` 落地（适合拿到别人的 HTML 文件时）。
- 悬停暂停单个气泡；**空格**暂停/继续生成；**Esc** 关浮层。
- 防重叠布局：所有气泡同一下降速度 + 补位卡最小间隔 ⇒ 竖直永久等距；长文本按**估算高度**（非固定字数）决定是否横跨舞台。桌面 3 泳道 / 窄屏 2 泳道，边界泳道只向内漂。
- 数据是生成时**快照**：灵感库更新后重新跑一次脚本即可。

## 回归测试
```bash
python test/run-tests.py
```
纯 stdlib（不依赖 pytest），**122 项**，覆盖：`moyu.parse` 三态解析、`moyu.draw` 抽签/防重复/分类池、
`store` 状态机与老库迁移、`bot` 文本清洗/搜索打分/动态帮助/日志轮转/PID 判活、
`send_file` 类型识别、`md2img` 依赖定位、`ideas-bubbles` 页面生成 + `--serve` API + `--apply` + 防重叠源码护栏、
`extensions` 注册表 / 加载容错 / `example.py` 模板不注册指令。
所有落盘用例都改写到临时目录，**不碰真实数据**。

## 配置项一览（全部走环境变量，见 `.env.example`）

所有敏感值与本机专属路径都通过环境变量注入，**代码里不写死任何本机路径**。以下变量都可选，不填走安全默认值。

### 必填（QQ 机器人凭据）
| 变量 | 说明 |
|---|---|
| `QQ_BOT_APPID` | QQ 机器人 AppID（https://q.qq.com 创建获取） |
| `QQ_BOT_SECRET` | AppSecret（**机器人密钥**，不是 Token）；兼容旧名 `QQ_BOT_TOKEN` |
| `QQ_GROUP_OPENID` | 默认发文件目标群 openid（不填则 `--group` 指定） |

### 可选（行为 / 路径）
| 变量 | 默认值 | 说明 |
|---|---|---|
| `QQ_BOT_SANDBOX` | `false` | 是否走沙箱域名（本地联调设 `true`） |
| `MEMO_TRIGGER` | `备忘` | 备忘触发词 |
| `QQ_STARTUP_MSG` | `我上班了，开摸！` | 启动播报文案（留空不播报） |
| `MEMO_SEARCH_ROOT` | 你的用户主目录 `~` | 「发文件」搜索根；**不要指向含凭据的目录**（`.ssh`/`.aws`/`.config` 等已被强制排除） |
| `MEMO_WORKSPACE_ROOT` | memo-bot 目录 | 搜索里加权的目录（一般无需改） |

> 私有扩展（`extensions/local/`）需要的变量（如 `MEMO_FORTUNE_FILE`、`FA_ENV_ID`）由扩展自行读取，
> 加在同一个 `.env` 里即可 —— 它们**不属于公开配置**。

### 可选（外部依赖定位，默认都走 PATH）
| 变量 | 用途 |
|---|---|
| `MEMO_NODE` | `node` 可执行文件（渲染成图片用到） |
| `MEMO_CHROME` | 浏览器可执行文件（找不到时自动试 Chrome/Edge 常见安装位置） |
| `MEMO_PLAYWRIGHT_CORE` | `playwright-core` 包目录 |

找不到时**明确报错并列出已尝试的路径**，不再静默失败。

## 可扩展性：加你自己的指令

内置指令之外，你可以**不改任何公开代码**就加自己的指令 —— 放在 `extensions/local/` 即可。
该目录已在 `.gitignore` 里，**不会进公开仓库**（本地 bot = 公开代码 + 你的私有扩展）。

### 三步加一条指令
1. 在 `extensions/local/` 新建 `myext.py`；
2. 写一个 `register(reg)`；
3. 重启 bot，`帮助` 菜单会自动带上你写的 `help` 行。

```python
# extensions/local/myext.py
def register(reg):
    def handle(text, mentioned):
        return "👋 你好，这是一条自定义回复"
    reg.add(["打招呼", "hi"], handle, help="打招呼 —— 回你一句")
```

### handler 契约
```python
def handle(text, mentioned) -> str | list[str] | None
```

| 参数 / 返回 | 含义 |
|---|---|
| `text` | 已剥掉 @ 标记的指令全文（如 `"我的指令 参数"`） |
| `mentioned` | 是否在 @机器人 场景（非 @ 时群里普通聊天也会走到这） |
| 返回 `None` | 不处理，继续走后面的逻辑 |
| 返回 `str` | 回一条消息 |
| 返回 `list[str]` | 依次回多条（`msg_seq` 自动递增） |

- handler 是**同步函数**，框架用线程执行 → 里面可以直接做阻塞操作（读硬件 / 调外部命令），**不会卡住机器人心跳**。
- 想在非 @ 场景不接话？在 handler 里 `if not mentioned: return None`。

### 常用能力
| 通过 `reg` | 说明 |
|---|---|
| `reg.add(names, handler, help="")` | 注册指令（`names` 为 str 或 list[str]，**精确匹配**） |
| `reg.api.search_files(q, limit=5)` | 按文件名/路径模糊搜配置的根目录 |
| `reg.api.SEARCH_ROOT` | 当前搜索根 |

### 加载与容错
- `extensions.load()` 依次加载 `extensions/*.py` 与 `extensions/local/*.py`。
- 某个扩展导入失败 / `register()` 抛异常 → **只打印 warning，不影响 bot 启动**，其余扩展照常生效。
- 精确匹配时**后注册者优先**，所以私有扩展可以覆盖公开扩展的同名指令。

完整模板见 `extensions/example.py`；本机私有扩展清单见 `extensions/local/README.md`。

## 本地验证（无需 QQ 凭据）
```bash
python bot.py --self-test       # 模拟群消息，验证识别+落库
python bot.py --list            # 打印已记录的备忘（本地读取用）
python send_file.py --self-test # 验证文件类型识别/编码/凭据
python test/run-tests.py        # 全量回归测试（122 项）
python moyu.py 3                # 摸鱼库抽 3 条
python moyu.py --stats          # 摸鱼库计数
python ideas-bubbles.py         # 生成灵感泡泡页面
```

## 真实部署（在你的本机，电脑需开机）
1. 安装依赖：`pip install -r requirements.txt`
   - 注意包名是 **`qq-botpy`**（`pip install botpy` 会失败）；导入名是 `botpy`。
2. `cp .env.example .env`，填入 QQ 机器人 **AppID / AppSecret**（从 https://q.qq.com 创建机器人获取）。
   - botpy 登录用 **AppSecret（机器人密钥）**，不是 Token。
3. 把机器人加入你的群，并在**群设置**里打开上面那两个开关。
4. `python bot.py` 启动常驻。**启动后每条备忘会打印 `group_openid=...`**，把它填进 `.env` 的 `QQ_GROUP_OPENID`（发文件要用）。
5. 发文件（默认主动推送，无需先 @）：
   `python send_file.py --file <路径> --caption "说明"`

### 开机自启（不绑定任何会话）
用 Windows 计划任务托管，登录即启动，且**不显示控制台窗口**（`pythonw.exe` + `--log` 写文件）：

```powershell
# pythonw 路径：优先取 PATH 上的；否则改成你本机的 pythonw.exe 绝对路径
$py  = if (Get-Command pythonw -ErrorAction SilentlyContinue) { (Get-Command pythonw).Source } else { '<你的 pythonw.exe 路径>' }
$dir = '<仓库>\memo-bot'
$act = New-ScheduledTaskAction -Execute $py -WorkingDirectory $dir `
       -Argument ('-u "' + $dir + '\bot.py" --log "' + $dir + '\bot.out.log"')
$trg = New-ScheduledTaskTrigger -AtLogOn
$set = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
       -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero) `
       -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName 'MoyuBot' -Action $act -Trigger $trg -Settings $set -Force
```

- 任务名 `MoyuBot`；查看状态：`Get-ScheduledTask -TaskName MoyuBot`；立刻跑：`Start-ScheduledTask -TaskName MoyuBot`。
- 日志：`memo-bot/bot.out.log`。
- 🔴 **同一时刻只能跑一个实例**（两个进程会各建一条 WS 连接、事件被抢）。改代码后要 `Stop-ScheduledTask` 再 `Start-ScheduledTask`。

## 与外部助手集成（可选）
bot 是纯本地进程，任何能在本机执行命令的程序都可以驱动它：
- **读备忘 / 灵感**：`python bot.py --list` 或读 `memos.db`。
- **处理灵感**：`python ideas.py --export` → 读导出的 md → 处理完 `--done/--decide/--drop` 回写。
- **发文件**：`python send_file.py --file <路径> --caption "说明"`。

「让助手把文件发到群」这种需求，本质是**助手在本机调用 `send_file.py`**，而不是 bot 反向驱动助手——
因为 bot 只暴露 QQ 收发接口，不对外暴露本机控制能力。

## 定时巡检（可选）
可另起一个定时任务周期性跑 `ideas.py --export` 刷新灵感池、跟进备忘库，输出周报。
纯本地、不调 AI，可按自己的节奏接。

## 两个入口为什么能并存
`bot.py`（收）走 WebSocket 网关常驻；`send_file.py`（发）走**纯 HTTP**（直接调 REST API，不建网关连接）。互不抢连接，可同时运行。

## 限制与注意
- **主动推送**：需群内开启「机器人主动在群聊内发言」。未开启时会自动降级为被动回复（需 4 分钟内群里 @ 过 bot，`msg_id` 缓存在 `last_group_msg.json`）。`--passive` 可强制走被动回复。
- **全量群消息**：需群内开启「消息范围 = 全部消息」，且依赖 `install_group_message_parser()` 补的解析器。
- 本地文件上传走官方 `/v2/groups/{openid}/files` 的 `file_data`(base64) 参数；200MB 上限。
- 生产存储可把 `store.py` 换成任意数据库后端（接口不变）。

## 发布到 Git（隐私清单）
本项目**不写死任何本机路径、不含任何凭据、不含个人自定义指令**。提交前确认以下都已进 `.gitignore`（已配好）：

- `.env` —— 你的 QQ 凭据（**绝不进仓库**）
- `memos.db` / `*.db` —— 个人备忘 / 灵感库
- `last_group_msg.json` / `moyu-recent.json` —— 含真实群 openid / 使用状态
- `*.log` / `bot.pid` / `灵感泡泡.html` —— 运行时产物
- `extensions/local/` —— **你的私有指令**（查运势 / 自检 / 资源点等），不进公开仓库

```bash
cd memo-bot
git init
git add .
git commit -m "feat: 群备忘 + 发文件机器人（路径/凭据全部可配置）"
git remote add origin <你的仓库地址>
git push -u origin main
```

克隆下来后别人只需：`pip install -r requirements.txt` → `cp .env.example .env` 填凭据 → `python bot.py`。
带渲染成图片功能时：`npm install`（装 playwright-core）→ 系统装好 Chrome/Edge。

