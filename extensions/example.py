"""扩展示例（模板）—— 复制到 `extensions/local/` 改成你自己的指令。

⚠️ 本文件是**公开仓库里的模板**，`register()` 是空的，**不会注册任何指令**。
   你自己的自定义指令请放 `extensions/local/`（该目录已 gitignore，不会进公开仓库）。

────────────────────────────────────────────────────────────────
一个扩展模块只需要暴露一个 `register(reg)` 函数，在里面调用 `reg.add(...)`：

    def register(reg):
        def handle(text, mentioned):
            return "👋 你好，这是一条自定义回复"
        reg.add(["打招呼", "hi"], handle, help="打招呼 —— 回你一句")

────────────────────────────────────────────────────────────────
handler 的契约：

    def handle(text, mentioned) -> str | list[str] | None

      text      —— 已剥掉 @ 标记的指令全文（例如 "我的指令 参数"）
      mentioned —— 是否在 @机器人 场景（非 @ 时群里普通聊天也会走到这）
      返回 None —— 不处理，继续走后面的逻辑
      返回 str  —— 回一条消息
      返回 list —— 依次回多条（msg_seq 自动递增）

    handler 是**同步函数**；框架用线程执行它，
    所以里面可以直接做阻塞操作（读硬件 / 调外部命令），不会卡住心跳。

────────────────────────────────────────────────────────────────
常用能力（通过 `reg.api`）：

    reg.api.search_files("关键词", limit=5)   # 按文件名/路径模糊搜配置的根目录
    reg.api.SEARCH_ROOT                        # 当前搜索根（默认用户主目录）

────────────────────────────────────────────────────────────────
想让指令只在 @机器人 时生效？在 handler 里判断 mentioned：

    def handle(text, mentioned):
        if not mentioned:
            return None          # 群里普通聊天不接话
        return "只在 @ 时回复"
"""


def register(reg):
    # 取消下面注释、改成你自己的指令即可：
    #
    # def handle(text, mentioned):
    #     return "👋 自定义回复"
    #
    # reg.add(["我的指令", "别名"], handle, help="我的指令 —— 说明")
    pass
