"""从 GeoNames 生成中文城市索引（构建期脚本，不是运行时依赖）。

为什么要这么做
--------------
靠第三方地理编码在线解析中文城市名**不可靠**，实测失败率高达 40%：

    name=开罗   -> 开罗/美国 (37.0,-89.2)      ← 美国伊利诺伊州的 Cairo
    name=里斯本  -> 里斯本/美国 (46.4,-97.7)    ← 美国北达科他州的 Lisbon
    name=米兰   -> 米兰/美国                    ← 同上
    name=伊斯坦布尔 / 华沙 / 内罗毕 / 加德满都 -> 无结果

而且 GeoNames 里不少城市**只有繁体中文名**（維也納 / 米蘭 / 利馬 / 基輔 / 開普敦），
所以还需要一步繁简转换。

做法：下载 GeoNames ``cities15000``（3.3 MB，34k 个人口 >1.5 万的城市），
抽出其中的中文别名、转成简体、按「行政级别 + 人口」去重，产出 ``city_index.tsv``。
运行时直接读这张表，零网络、确定性、覆盖完整。

用法::

    uv run python -m mcp_server.data.build_city_index            # 用缓存或下载
    uv run python -m mcp_server.data.build_city_index --offline  # 只用已有缓存

数据来源：GeoNames，CC BY 4.0（https://www.geonames.org/）
"""

from __future__ import annotations

import argparse
import io
import re
import sys
import zipfile
from pathlib import Path
from urllib.request import urlopen

import zhconv

DATA_DIR = Path(__file__).resolve().parent
CACHE_DIR = DATA_DIR / ".cache"
ZIP_PATH = CACHE_DIR / "cities15000.zip"
SOURCE_URL = "https://download.geonames.org/export/dump/cities15000.zip"
INDEX_PATH = DATA_DIR / "city_index.tsv"

# 中文名：2–10 个汉字（不收录含标点/字母的别名，避免把「圣托里尼岛」之类当主名）
CJK_NAME = re.compile(r"^[\u4e00-\u9fff]{2,10}$")

# 行政级别优先级：首都 > 一级行政区首府 > 普通居民点
FEATURE_RANK = {"PPLC": 0, "PPLA": 1, "PPLA2": 2, "PPLA3": 3, "PPLA4": 4, "PPL": 5}

# GeoNames 制表符分隔字段下标
F_NAME, F_ASCII, F_ALTS, F_LAT, F_LON = 1, 2, 3, 4, 5
F_FCODE, F_COUNTRY, F_POP, F_TZ = 7, 8, 14, 17

# 太常见的通用词不该被当成城市名（避免「中心」「新城」之类污染索引）
STOPWORDS = {
    "中心", "新城", "老城", "东区", "西区", "南区", "北区", "中区", "市区",
    "机场", "车站", "港", "海湾", "半岛", "岛", "镇", "村", "县", "区",
}


def download(force: bool = False) -> Path:
    """下载并缓存 GeoNames 数据包。"""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    if ZIP_PATH.exists() and not force:
        print(f"使用缓存 {ZIP_PATH}（{ZIP_PATH.stat().st_size / 1e6:.1f} MB）")
        return ZIP_PATH

    print(f"下载 {SOURCE_URL} …")
    with urlopen(SOURCE_URL, timeout=120) as resp:  # noqa: S310 - 固定官方地址
        data = resp.read()
    ZIP_PATH.write_bytes(data)
    print(f"已缓存到 {ZIP_PATH}（{len(data) / 1e6:.1f} MB）")
    return ZIP_PATH


def _rank(feature_code: str, population: int) -> tuple[int, int]:
    """越小越优。"""
    return FEATURE_RANK.get(feature_code, 9), -population


def build(zip_path: Path) -> list[tuple[str, str, float, float, str, str, int]]:
    """解析数据包，返回去重后的索引行。"""
    best: dict[str, tuple] = {}
    total = 0
    with_zh = 0

    with zipfile.ZipFile(zip_path) as zf, zf.open("cities15000.txt") as raw:
        stream = io.TextIOWrapper(raw, encoding="utf-8")
        for line in stream:
            parts = line.rstrip("\n").split("\t")
            if len(parts) < 19:
                continue
            total += 1

            zh_names = {
                zhconv.convert(alias.strip(), "zh-cn")
                for alias in parts[F_ALTS].split(",")
                if CJK_NAME.match(alias.strip())
            }
            zh_names = {n for n in zh_names if n not in STOPWORDS}
            if not zh_names:
                continue
            with_zh += 1

            population = int(parts[F_POP] or 0)
            feature_code = parts[F_FCODE]
            rank = _rank(feature_code, population)
            row = (
                parts[F_ASCII] or parts[F_NAME],
                float(parts[F_LAT]),
                float(parts[F_LON]),
                parts[F_COUNTRY],
                parts[F_TZ] or "UTC",
                population,
            )
            for name in zh_names:
                current = best.get(name)
                if current is None or rank < current[0]:
                    best[name] = (rank, row)

    print(f"扫描 {total} 个城市，其中 {with_zh} 个带中文别名")
    print(f"去重后得到 {len(best)} 个唯一中文城市名")
    return [(name, *entry[1]) for name, entry in sorted(best.items())]


def write_index(rows: list[tuple]) -> Path:
    lines = ["# name_zh\tname_en\tlat\tlon\tcc\ttz\tpopulation"]
    lines += [
        f"{name}\t{en}\t{lat:.5f}\t{lon:.5f}\t{cc}\t{tz}\t{pop}"
        for name, en, lat, lon, cc, tz, pop in rows
    ]
    INDEX_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"已写入 {INDEX_PATH}（{INDEX_PATH.stat().st_size / 1024:.0f} KB）")
    return INDEX_PATH


def main() -> int:
    parser = argparse.ArgumentParser(description="生成中文城市索引")
    parser.add_argument("--offline", action="store_true", help="只用已有缓存，不下载")
    parser.add_argument("--force-download", action="store_true", help="强制重新下载")
    args = parser.parse_args()

    if args.offline and not ZIP_PATH.exists():
        print(f"缓存不存在：{ZIP_PATH}。请去掉 --offline 先下载一次。", file=sys.stderr)
        return 2

    zip_path = ZIP_PATH if args.offline else download(force=args.force_download)
    rows = build(zip_path)
    write_index(rows)

    # 顺带报告国家代码分布，便于维护 country 中文名映射
    from collections import Counter

    counts = Counter(row[4] for row in rows)
    print(f"\n覆盖 {len(counts)} 个国家/地区，Top 15：")
    for code, n in counts.most_common(15):
        print(f"  {code}: {n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
