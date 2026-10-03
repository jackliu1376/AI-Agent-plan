"""Web 服务启动入口。

::

    uv run task-planner-web                    # 默认 127.0.0.1:8000
    WEB_PORT=9000 uv run task-planner-web      # 换端口
    WEB_RELOAD=1 uv run task-planner-web       # 开发模式（改代码自动重载）

开发前端时通常跑两个进程：
- ``uv run task-planner-web``   → 后端 8000
- ``npm run dev``（frontend/）  → 前端 5173，Vite 把 /api 代理到 8000
"""

from __future__ import annotations

import os


def main() -> None:
    import uvicorn

    host = os.getenv("WEB_HOST", "127.0.0.1")
    port = int(os.getenv("WEB_PORT", "8000"))
    reload = os.getenv("WEB_RELOAD") == "1"

    print(f"🚀 任务规划助手 API → http://{host}:{port}")
    print(f"   接口文档         → http://{host}:{port}/docs")
    uvicorn.run("web.app:app", host=host, port=port, reload=reload)


if __name__ == "__main__":
    main()
