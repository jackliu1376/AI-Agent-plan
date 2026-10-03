"""桌面版：用原生窗口承载 Web 界面。

与 ``web/`` 的关系：桌面版只是 ``web.app`` 的一层薄封装 ——
启动本地服务 + 开一个窗口指向它，不复制任何业务逻辑。

::

    uv run task-planner-desktop        # 或双击 start-desktop.bat
"""
