"""跨模型跑评测：一行命令切换 LLM，并对比结果。

为什么需要这层封装
------------------
`task-planner-eval` 本身已经能跑评测，但它读的是 `DEEPSEEK_*` 三个环境变量。
换模型要手写一长串前缀：

    DEEPSEEK_BASE_URL=... DEEPSEEK_MODEL=... DEEPSEEK_API_KEY=... \\
        uv run task-planner-eval --label ...

**而且很容易漏改**——比如只换了 `DEEPSEEK_MODEL` 忘了换 `BASE_URL`，
结果拿着旧服务商跑了一遍，还以为是新模型的成绩。

这里把常见模型做成清单，一条命令搞定；并且**开跑前会把实际生效的
base_url / model 打出来**，杜绝「测错模型」。

用法
----
    python scripts/eval_model.py --list                    # 看清单与 Key 状态
    python scripts/eval_model.py mimo                      # 跑 MiMo 全量
    python scripts/eval_model.py glm --category 注入抵抗    # 只跑某一类
    python scripts/eval_model.py mimo --repeat 3           # 每条跑 3 次（看稳定性）
    python scripts/eval_model.py mimo --key sk-xxx         # 临时传 Key
    python scripts/eval_model.py --compare mimo glm        # 对比两次结果

给新模型加一条：在 `MODELS` 里加一行 ModelSpec 即可。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


@dataclass(frozen=True)
class ModelSpec:
    """一个可评测的模型。`key` 是命令行里用的短名，`model` 是传给 API 的名字。"""

    key: str
    model: str
    base_url: str
    key_env: str
    vendor: str
    note: str = ""
    # 关掉思考模式要透传的私有参数（None = 该模型没有思考模式）。
    # 评测默认关掉：MiMo 开思考时实测一条样本 185 秒，跑完 24 条要 74 分钟，
    # 而且和 DeepSeek 这种非推理模型不在同一口径上。要测思考版加 --thinking。
    no_think: dict[str, Any] | None = None


# 注意：这些端点会变，接入前对一下各家官方文档。
# 本项目要求的是 **OpenAI 兼容 + function calling**，两家都满足。
MODELS: list[ModelSpec] = [
    ModelSpec(
        key="deepseek",
        model="deepseek-chat",
        base_url="https://api.deepseek.com",
        key_env="DEEPSEEK_API_KEY",
        vendor="DeepSeek",
    ),
    ModelSpec(
        key="mimo",
        model="mimo-v2.6-pro",
        base_url="https://api.xiaomimimo.com/v1",
        key_env="MIMO_API_KEY",
        vendor="小米",
        note="默认开思考（评测时已自动关闭，用 --thinking 可开）",
        no_think={"thinking": {"type": "disabled"}},
    ),
    ModelSpec(
        key="glm",
        model="glm-5.3",
        base_url="https://open.bigmodel.cn/api/paas/v4",
        key_env="ZAI_API_KEY",
        vendor="智谱",
        note="temperature 上限 1.0（本项目默认 0.2，安全）",
    ),
]

BY_KEY = {m.key: m for m in MODELS}


def load_env_file() -> None:
    """把 `.env` 读进 os.environ。

    `agent.config` 在 import 时执行 `load_dotenv`，所以这里 import 一下就够 ——
    不这么做的话 `--list` 会误报「未配置 Key」，明明 `.env` 里写着。
    """
    import agent.config  # noqa: F401  （副作用：load_dotenv）


def _is_placeholder(value: str) -> bool:
    """`sk-xxxx` 这类占位符不算配好。"""
    return value.startswith("sk-xxxx") or value.startswith("你的")


def resolve_key(spec: ModelSpec, explicit: str | None) -> tuple[str, str]:
    """按优先级找 Key：命令行 > 该模型的专用环境变量。返回 `(key, 来源)`。

    **故意不兜底到 `DEEPSEEK_API_KEY`。** 一开始写了这个兜底，
    结果 `--list` 给 MiMo 和 GLM 都显示「✅ Key 来自 DEEPSEEK_API_KEY」——
    可不同服务商的 Key 根本不通用，用户会以为配好了，跑起来却 401，
    而且报错来自上游、很难联想到是 Key 张冠李戴。
    """
    if explicit:
        return explicit, "命令行 --key"

    value = os.getenv(spec.key_env, "").strip()
    if value and not _is_placeholder(value):
        return value, f"环境变量 {spec.key_env}"

    return "", ""


def cmd_list() -> int:
    print("可评测的模型：\n")
    width = max(len(m.key) for m in MODELS) + 2
    for spec in MODELS:
        key, source = resolve_key(spec, None)
        mark = "✅" if key else "⬜"
        print(f"  {mark} {spec.key.ljust(width)} {spec.vendor}  ·  {spec.model}")
        print(f"     {' ' * width} {spec.base_url}")
        if spec.note:
            print(f"     {' ' * width} ⚠️  {spec.note}")
        if key:
            print(f"     {' ' * width} Key 来自 {source}")
        else:
            print(f"     {' ' * width} 未配置 Key —— 设 {spec.key_env} 或用 --key 传")
        print()

    print("用法：python scripts/eval_model.py <短名> [选项]")
    print("      python scripts/eval_model.py --list 看这份清单")
    return 0


def cmd_compare(labels: list[str]) -> int:
    """转发给 run_report。不给标签时列出历史，方便挑。"""
    from agent.run_report import main as report_main

    if len(labels) >= 2:
        return report_main(["--compare", labels[0], labels[1]])

    print("历史评测结果（用 `--compare A B` 对比两轮）：\n")
    code = report_main(["--group-by", "label"])
    if code != 0:
        return code
    print("\n例：python scripts/eval_model.py --compare deepseek-chat mimo-v2.6-pro")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="eval_model",
        description="跨模型跑评测（模型清单 + 自动设环境变量）。",
    )
    parser.add_argument("model", nargs="?", help="模型短名，见 --list")
    parser.add_argument("--list", action="store_true", help="列出可用模型与 Key 状态")
    parser.add_argument("--key", default=None, help="临时传 API Key（不写进 .env）")
    parser.add_argument("--compare", nargs="*", default=None,
                        help="对比历史结果：给两个标签，或留空列出历史")
    parser.add_argument("--category", default="", help="只跑指定类别")
    parser.add_argument("--only", default="", help="只跑指定样本 id")
    parser.add_argument("--repeat", type=int, default=1, help="每条样本重复次数")
    parser.add_argument("--limit", type=int, default=0, help="最多跑 N 条")
    parser.add_argument("--label", default=None, help="覆盖评测标签（默认用模型名）")
    parser.add_argument("--temperature", type=float, default=None, help="覆盖采样温度")
    parser.add_argument("--dry-run", action="store_true", help="只校验样本，不调用 API")
    parser.add_argument("--thinking", action="store_true",
                        help="保留模型默认的思考模式（默认会关掉以对齐非推理模型的口径）")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    load_env_file()

    if args.list:
        return cmd_list()
    if args.compare is not None:
        return cmd_compare(args.compare)
    if not args.model:
        build_parser().print_help()
        return 2

    spec = BY_KEY.get(args.model)
    if spec is None:
        print(f"❌ 未知模型「{args.model}」。可用：{', '.join(BY_KEY)}", file=sys.stderr)
        print("   加新模型：编辑 scripts/eval_model.py 的 MODELS。", file=sys.stderr)
        return 2

    # dry-run 只校验样本文件、不碰 API，所以不该被 Key 挡住 ——
    # 想先看看样本长什么样是很自然的动作。
    if args.dry_run:
        key, source = "(dry-run 不需要)", "—"
    else:
        key, source = resolve_key(spec, args.key)
        if not key:
            print(f"❌ 没有 {spec.vendor} 的 API Key。", file=sys.stderr)
            print(f"   方式一：export {spec.key_env}=sk-xxx", file=sys.stderr)
            print("   方式二：python scripts/eval_model.py "
                  f"{spec.key} --key sk-xxx", file=sys.stderr)
            return 2

    # 关键：先把环境变量改成目标模型，**再** import eval_runner。
    # agent.config 在 import 时执行 load_dotenv（不覆盖已有变量），
    # 所以这里设的值会赢过 .env。
    os.environ["DEEPSEEK_API_KEY"] = key
    os.environ["DEEPSEEK_BASE_URL"] = spec.base_url
    os.environ["DEEPSEEK_MODEL"] = spec.model

    # 私有参数透传（见 agent/config.py 的 Settings.extra_body）
    os.environ.pop("LLM_EXTRA_BODY", None)
    if spec.no_think is not None and not args.thinking:
        os.environ["LLM_EXTRA_BODY"] = json.dumps(spec.no_think)

    label = args.label if args.label is not None else spec.model

    print("=" * 68)
    print(f"  模型      {spec.model}（{spec.vendor}）")
    print(f"  端点      {spec.base_url}")
    shown = key if args.dry_run else f"{key[:6]}…{key[-4:]}"
    print(f"  Key       {source} · {shown}")
    print(f"  标签      {label}")
    if spec.no_think is not None:
        print(f"  思考      {'开启（模型默认）' if args.thinking else '已关闭（--thinking 可开）'}")
    print("=" * 68)
    print()

    from agent.eval_runner import main as eval_main

    forwarded: list[str] = ["--label", label]
    if args.category:
        forwarded += ["--category", args.category]
    if args.only:
        forwarded += ["--only", args.only]
    if args.repeat != 1:
        forwarded += ["--repeat", str(args.repeat)]
    if args.limit:
        forwarded += ["--limit", str(args.limit)]
    if args.temperature is not None:
        forwarded += ["--temperature", str(args.temperature)]
    if args.dry_run:
        forwarded += ["--dry-run"]

    code = eval_main(forwarded)

    if code == 0 and not args.dry_run:
        print()
        print("下一步：换一个模型再跑一遍，然后对比——")
        print(f"  python scripts/eval_model.py --compare {label} <另一个模型名>")
        print()
        print("重点看两个数字：通过率，和平均修复轮次（repairs 越高说明")
        print("模型越难一次产出合规计划，实际使用会更慢更贵）。")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
