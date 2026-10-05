"""控制台输出的编码兜底。

问题
----
Windows 控制台默认是 GBK（cp936），**编码不了的字符会让 `print` 直接抛异常**：

    UnicodeEncodeError: 'gbk' codec can't encode character '\\U0001f680'

这在 CLI 里只是难看的报错，但在**服务入口**里是致命的 ——
`uv run task-planner-web` 会直接退出，用户看到的是一个起不来的服务。

而且它很容易漏测：如果开发时输出被重定向到文件或管道（编码往往是 UTF-8），
一切正常；只有在真实控制台里跑才炸。

做法
----
把 stdout/stderr 的错误处理改成 ``replace``：编码不了的字符降级成 ``?``，
而不是抛异常。中文在 GBK 下本来就能编码，所以只有 emoji 会降级 ——
这比「整个进程崩掉」好得多。

注意：**不要碰 MCP server 的 stdout** —— 那条流是 JSON-RPC 协议通道，
往里写任何非协议内容都会破坏通信。它也不会用到这个函数。
"""

from __future__ import annotations

import sys


def ensure_safe_stdio() -> None:
    """让 stdout/stderr 在编码不了的字符上降级，而不是抛异常。

    幂等，可以重复调用。在任何 print 之前调用即可。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError, OSError):
            # AttributeError：stdout 被替换成了非 TextIOWrapper（如 pytest 捕获）
            # ValueError：流已分离
            # 两种情况都不影响主流程，忽略
            pass
