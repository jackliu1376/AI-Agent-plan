"""工具 T7：save_itinerary —— 计划落盘（唯一的写操作）。

沙箱约束：只能写入 ``OUTPUT_DIR`` 之内，路径越界直接拒绝。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, Field

from common.envelope import ERR_FILE_ERROR, ERR_FORBIDDEN_PATH, ToolResult
from mcp_server.tools.base import PROJECT_ROOT, env, register


class SaveParams(BaseModel):
    path: str = Field(description="相对 OUTPUT_DIR 的文件名，如 chengdu-2d.md")
    content: str = Field(min_length=1, description="要写入的文本内容（Markdown / JSON）")


def output_root() -> Path:
    """落盘沙箱根目录。"""
    root = Path(env("OUTPUT_DIR", "outputs"))
    if not root.is_absolute():
        root = PROJECT_ROOT / root
    return root.resolve()


@register(
    name="save_itinerary",
    description=(
        "把生成的计划写入文件保存。只能写入项目的 outputs/ 目录，"
        "传入文件名（可含子目录），不接受绝对路径。"
    ),
    params_model=SaveParams,
    idempotent=False,  # 写操作，重复调用会覆盖，不视为幂等
    tags=["本地文件", "写操作"],
)
def save_itinerary(params: SaveParams) -> ToolResult:
    root = output_root()
    target = (root / params.path).resolve()

    # 沙箱校验：目标必须位于 OUTPUT_DIR 之内
    if target != root and root not in target.parents:
        return ToolResult.failure(
            ERR_FORBIDDEN_PATH,
            f"路径越界，只能写入 {root} 之内，收到: {params.path}",
        )

    if target.is_dir():
        return ToolResult.failure(ERR_FILE_ERROR, f"目标是一个目录，无法写入: {target}")

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        existed = target.exists()
        target.write_text(params.content, encoding="utf-8")
    except OSError as exc:
        return ToolResult.failure(ERR_FILE_ERROR, f"写入失败: {exc}")

    return ToolResult.success(
        {
            "path": str(target),
            "relative_path": str(target.relative_to(root)),
            "bytes": target.stat().st_size,
            "overwritten": existed,
            "saved_at": datetime.now().isoformat(timespec="seconds"),
        },
        source="local:filesystem",
    )
