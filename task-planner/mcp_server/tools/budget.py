"""工具 T4：parse_budget_csv —— 解析预算 CSV。

期望列：``category,item,amount``（表头大小写不敏感，容忍中文列名）。
自动尝试 utf-8-sig / gbk 编码，兼容 Excel 导出的中文 CSV。
"""

from __future__ import annotations

import csv
import io
from pathlib import Path

from pydantic import BaseModel, Field

from common.envelope import ERR_BAD_ARGS, ERR_FILE_ERROR, ERR_FORBIDDEN_PATH, ToolResult
from mcp_server.tools.base import PROJECT_ROOT, register

CATEGORY_ALIASES = {"category", "类别", "分类", "科目"}
ITEM_ALIASES = {"item", "项目", "名称", "明细"}
AMOUNT_ALIASES = {"amount", "金额", "费用", "预算"}


class BudgetParams(BaseModel):
    path: str = Field(description="CSV 文件路径（相对于项目根目录或绝对路径）")
    total_budget: float | None = Field(
        default=None, ge=0, description="总预算上限（元），用于超支预警"
    )


def _resolve_sandboxed(raw: str) -> Path:
    """把路径限制在项目根目录内，防止读取任意系统文件。"""
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = PROJECT_ROOT / candidate
    resolved = candidate.resolve()
    root = PROJECT_ROOT.resolve()
    if root not in resolved.parents and resolved != root:
        raise PermissionError(f"路径越界，仅允许访问项目目录内的文件: {raw}")
    return resolved


def _read_text(path: Path) -> str:
    for encoding in ("utf-8-sig", "utf-8", "gbk"):
        try:
            return path.read_text(encoding=encoding)
        except UnicodeDecodeError:
            continue
    raise UnicodeDecodeError("utf-8", b"", 0, 1, "无法用 utf-8/gbk 解码该 CSV")


def _pick(header: list[str], aliases: set[str]) -> str | None:
    for col in header:
        if col.strip().lower() in aliases:
            return col
    return None


@register(
    name="parse_budget_csv",
    description=(
        "解析预算/开销 CSV 文件，按类别汇总金额并给出超支预警。"
        "CSV 需包含 类别/项目/金额 三列（中英文列名均可）。"
    ),
    params_model=BudgetParams,
    idempotent=True,
    tags=["本地文件", "CSV"],
)
def parse_budget_csv(params: BudgetParams) -> ToolResult:
    try:
        path = _resolve_sandboxed(params.path)
    except PermissionError as exc:
        return ToolResult.failure(ERR_FORBIDDEN_PATH, str(exc))

    if not path.exists() or not path.is_file():
        return ToolResult.failure(ERR_FILE_ERROR, f"文件不存在: {path}")

    try:
        text = _read_text(path)
    except UnicodeDecodeError as exc:
        return ToolResult.failure(ERR_FILE_ERROR, str(exc))

    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        return ToolResult.failure(ERR_BAD_ARGS, "CSV 没有表头")

    header = list(reader.fieldnames)
    col_cat = _pick(header, CATEGORY_ALIASES)
    col_item = _pick(header, ITEM_ALIASES)
    col_amt = _pick(header, AMOUNT_ALIASES)

    if not col_amt:
        return ToolResult.failure(
            ERR_BAD_ARGS,
            f"CSV 缺少金额列，现有表头: {header}（需要 category/item/amount 或 类别/项目/金额）",
        )

    by_category: dict[str, float] = {}
    line_items: list[dict] = []
    skipped: list[str] = []

    for idx, row in enumerate(reader, start=2):
        raw_amount = (row.get(col_amt) or "").strip().replace(",", "").replace("¥", "")
        if not raw_amount:
            continue
        try:
            amount = float(raw_amount)
        except ValueError:
            skipped.append(f"第 {idx} 行金额无法解析: {raw_amount!r}")
            continue

        category = (row.get(col_cat) or "未分类").strip() if col_cat else "未分类"
        item = (row.get(col_item) or "").strip() if col_item else ""
        by_category[category] = round(by_category.get(category, 0.0) + amount, 2)
        line_items.append({"category": category, "item": item, "amount": amount})

    total = round(sum(by_category.values()), 2)

    warnings: list[str] = []
    if params.total_budget is not None:
        if total > params.total_budget:
            warnings.append(
                f"⚠️ 超支 ¥{round(total - params.total_budget, 2)}"
                f"（预算 ¥{params.total_budget}，实际 ¥{total}）"
            )
        else:
            warnings.append(f"✅ 未超支，剩余 ¥{round(params.total_budget - total, 2)}")
    warnings.extend(skipped)

    return ToolResult.success(
        {
            "path": str(path),
            "line_items": line_items,
            "by_category": by_category,
            "total": total,
            "total_budget": params.total_budget,
            "warnings": warnings,
        },
        source=f"local:{path.name}",
    )
