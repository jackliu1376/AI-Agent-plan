"""桌面版入口：用原生窗口承载现有的 Web 界面。

为什么这么薄
------------
因为架构已经天然适合：
- 前端是**静态产物**（运行时不需要 Node）
- 后端**单进程**同时提供 API 与静态文件
- Web 层用的是 ``LocalToolRunner``，**不依赖 MCP 子进程**

所以桌面版 = 启动一个本地服务 + 开一个窗口指向它，不需要复制任何业务逻辑。

三个实现要点
------------
1. **端口从已绑定的 socket 读回**，而不是"先探测空闲端口再绑定" ——
   后者两步之间存在竞态窗口，可能被别的进程抢走。
2. **服务跑在后台线程** —— pywebview 的窗口循环必须占用主线程。
3. **关窗即退出** —— ``webview.start()`` 返回后通知 uvicorn 停止，
   否则会留下僵尸进程占着端口。
"""

from __future__ import annotations

import threading
import time

import uvicorn

from agent.config import Settings

HOST = "127.0.0.1"
WINDOW_TITLE = "任务规划助手"
STARTUP_TIMEOUT_SECONDS = 20.0


def _read_bound_port(server: uvicorn.Server) -> int:
    """从 uvicorn 已绑定的 socket 读回真实端口。"""
    for bound in getattr(server, "servers", []) or []:
        for sock in getattr(bound, "sockets", []) or []:
            return sock.getsockname()[1]
    raise RuntimeError("无法读取后端服务绑定的端口")


def _start_server(app) -> tuple[uvicorn.Server, int]:  # noqa: ANN001
    """在后台线程启动服务，返回 (server, port)。"""
    config = uvicorn.Config(
        app,
        host=HOST,
        port=0,  # 交给系统分配，避免端口冲突
        log_level="warning",
        access_log=False,
    )
    server = uvicorn.Server(config)
    threading.Thread(target=server.run, daemon=True, name="uvicorn").start()

    deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError(f"后端服务启动超时（{STARTUP_TIMEOUT_SECONDS:.0f} 秒）")
        time.sleep(0.05)

    return server, _read_bound_port(server)


def main() -> int:
    import webview

    from web.app import STATIC_DIR, create_app

    if not (STATIC_DIR / "index.html").is_file():
        print(
            "⚠️  未找到前端构建产物（web/static/index.html）。\n"
            "   请先执行：cd frontend && npm install && npm run build",
        )
        return 2

    settings = Settings.load()
    if not settings.has_credentials:
        print(
            "⚠️  未配置 DEEPSEEK_API_KEY —— 界面能打开，但提交任务会报错。\n"
            "   请在 task-planner/.env 中填入真实 Key。",
        )

    server, port = _start_server(create_app(settings=settings))
    url = f"http://{HOST}:{port}"
    print(f"🚀 后端已就绪 → {url}")

    webview.create_window(
        WINDOW_TITLE,
        url,
        width=1180,
        height=880,
        min_size=(880, 620),
        text_select=True,
    )

    try:
        webview.start()
    finally:
        # 窗口关闭后必须显式停掉 uvicorn，否则进程不退出
        server.should_exit = True
        print("👋 已退出")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
