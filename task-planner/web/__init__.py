"""Web 层：FastAPI + SSE，把编排循环暴露为 HTTP 服务。

本文件刻意不导入任何子模块 —— ``web.app`` 在导入时会构建默认应用实例
（读环境变量），而只跑 CLI 或只想用 ``web.session`` 时不该触发这个副作用。

::

    from web.app import create_app        # 需要时再显式导入
    from web.session import SessionStore
"""
