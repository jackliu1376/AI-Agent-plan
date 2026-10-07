"""抓取景点数据，生成 ``mcp_server/data/seed_generated.py``。

为什么要抓而不是手写
--------------------
手写 250 城 × 15 条 = 3700+ 条，票价和开放时间只能凭印象编 ——
而这个库的 ``source`` 字段存在的全部意义就是让人能分辨哪些数据是核实过的。
抓来的数据至少**名称、类型、评分、开放时间是真的**。

两个数据源的能力边界（都实测过）
--------------------------------
- **高德 POI**：中国城市可用，名称/评分/开放时间/类型都真实，
  **但拿不到票价**。海外完全不可用 —— 查「东京浅草寺」会返回佛山的
  「浅草堂」，查「纽约自由女神像」会返回北京世界公园里的复制品。
- **Wikidata SPARQL**：海外可用（**必须走代理**，直连超时），
  能拿名称/类型/坐标，票价和评分基本没有。

所以：中国城市走高德，海外城市走 Wikidata，两边都拿不到的字段
（票价、游玩时长、亲子友好）按**类型规则估算**，并在 ``source`` 里如实标注。

用法
----
::

    python -m scripts.build_attractions --source amap --limit 3   # 试跑 3 城
    python -m scripts.build_attractions --source wikidata --limit 3
    python -m scripts.build_attractions --source all              # 全量（慢）

结果按城市缓存在 ``mcp_server/data/.cache/``，中断后重跑会跳过已抓的城市。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx
import zhconv

from mcp_server.data.cities import lookup_city
from mcp_server.data.city_targets import CHINA_CITIES, OVERSEAS_CITIES
from mcp_server.tools.base import env

CACHE_DIR = Path(__file__).resolve().parents[1] / "mcp_server" / "data" / ".cache"
AMAP_URL = "https://restapi.amap.com/v5/place/text"
WD_ENDPOINT = "https://query.wikidata.org/sparql"

# 每城目标条数。低于 5 会被 tests/test_cities.py 的「每城至少 5 条」断言拦下。
TARGET_PER_CITY = 20
MIN_PER_CITY = 6


# ---------------------------------------------------------------------------
# 类型 → 估算字段
# ---------------------------------------------------------------------------

# 按关键词匹配类型串，给出 (tags, 票价档, 游玩时长, 亲子友好)。
# 顺序有意义：先匹配到的先用，所以具体类型要排在泛类型前面。
TYPE_RULES: list[tuple[tuple[str, ...], tuple[str, float, float, int]]] = [
    (("主题乐园", "游乐园", "游乐场"), ("主题乐园,亲子", 260, 6.0, 1)),
    (("动物园", "水族馆", "海洋馆"), ("动物,亲子", 120, 3.5, 1)),
    (("植物园",), ("自然,植物,亲子", 20, 2.0, 1)),
    (("科技馆", "科学", "天文"), ("科普,亲子,室内", 30, 2.5, 1)),
    (("美术馆", "艺术馆", "画廊"), ("艺术,博物馆", 30, 2.0, 1)),
    (("博物馆", "纪念馆", "展览馆", "陈列馆"), ("博物馆,文化", 30, 2.5, 1)),
    (("世界遗产", "国家级景点", "文物保护"), ("历史,世界遗产", 90, 3.0, 1)),
    (("省级景点",), ("历史,景点", 60, 2.5, 1)),
    (("寺庙", "寺院", "道观", "教堂", "清真寺", "宗教"), ("宗教,历史", 30, 1.5, 0)),
    (("古镇", "古街", "老街", "历史街区"), ("历史,街区", 20, 2.5, 1)),
    (("公园", "广场", "绿地"), ("公园,免费", 0, 1.5, 1)),
    (("步行街", "商业街", "购物"), ("购物,美食", 0, 2.0, 1)),
    (("观景", "塔", "摩天", "瞭望"), ("地标,观景", 80, 1.5, 1)),
    (("温泉", "度假"), ("休闲,温泉", 180, 3.0, 1)),
    (("滑雪", "冰雪"), ("运动,冰雪", 200, 4.0, 1)),
    (("海滩", "海滨", "沙滩"), ("海边,自然", 0, 3.0, 1)),
    (("山", "峡", "瀑布", "溶洞", "湖", "湿地", "森林", "草原", "沙漠"),
     ("自然,风景", 70, 3.5, 0)),
    (("剧院", "音乐厅", "演出"), ("演出,文化", 150, 2.0, 0)),
    (("大学", "校园"), ("校园,文化", 0, 1.5, 1)),
]
DEFAULT_TYPE = ("景点", 40, 2.0, 1)

# 免费信号：命中这些词基本可以判定不要门票
FREE_HINTS = ("免费", "开放式", "全天开放")


def classify(type_text: str, name: str = "") -> tuple[str, float, float, int]:
    """把类型串映射成 (tags, 票价, 时长, 亲子友好)。

    高德的 type 形如 ``风景名胜;风景名胜;世界遗产``，Wikidata 给的是
    中文类型标签（``觀光塔`` / ``美術館``）。两者都当纯文本匹配。
    """
    hay = f"{type_text} {name}"
    for keys, result in TYPE_RULES:
        if any(k in hay for k in keys):
            return result
    return DEFAULT_TYPE


# ---------------------------------------------------------------------------
# 高德：中国城市
# ---------------------------------------------------------------------------

# 明显不是「游客会去」的 POI。高德会返回检票处、停车场、写字楼这类条目。
AMAP_REJECT = re.compile(
    r"检票|售票|停车场|停车楼|卫生间|洗手间|游客中心|游客服务|"
    r"出入口|入口$|出口$|大门$|地铁站|公交站|充电站|"
    r"管理处|管委会|办事处|派出所|营业厅|专卖店|门店|"
    r"第[一二三四五六七八九十\d]+分店|"
    # 下面这些是「按目的地名搜索」时才暴露的：关键词是地名本身时，
    # 高德会把名字里含这个地名的**任意** POI 都返回 ——
    # 实测查「漠河」返回了乐童托管中心、古筝学堂、启航托管中心。
    r"托管|培训|学堂|教室|补习|辅导|教育|幼儿园|幼儿|早教|"
    r"代办|中介|工作室|事务所|有限公司|装饰|建材|汽修|"
    r"画室|琴行|舞|瑜伽|健身|美发|美容|口腔|诊所|药房|"
    # 驾校特别坑：评分 4.1–4.7，比很多真景点还高，评分门槛拦不住。
    # 实测「海东」前 10 条里 5 条是驾校/考场，「花莲」2 条全是驾驶训练班。
    r"驾校|驾考|驾驶人|考场|教练场|"
    r"中学|小学|高中|技校|职校|中专"
)

# 只要这些分类，其余（生活服务 / 公司企业 / 道路附属）一律丢掉
AMAP_KEEP_TYPES = (
    "风景名胜", "博物馆", "展览馆", "美术馆", "科教文化",
    "公园广场", "寺庙道观", "教堂", "纪念馆", "图书馆",
)

# 即使是「科教文化」大类，这些子类也不是景点
AMAP_REJECT_TYPES = (
    "培训机构", "幼儿园", "学校", "书店", "会展", "购物相关",
    "生活服务", "公司企业", "商务住宅", "政府机构", "医疗",
)

# 按目的地名搜索时的评分下限。关键词是地名本身时结果很杂，
# 真正的景点在高德上都有像样的评分，而托管中心/补习班通常 1–3 分。
NAME_SEARCH_MIN_RATING = 3.0

AMAP_TYPES = "110000|140000"  # 风景名胜 | 科教文化服务

# POI 与城市中心的最大距离。高德的 ``region`` 对不认识的县级市会**静默失效**
# 并回落到北京（实测 region=漠河 返回故宫、天安门），只能靠坐标兜住。
# 阈值放宽到 100km：只拦明显的错配（漠河→北京是 1600km），
# 不误杀大城市边缘的景点（北京延庆距市中心约 75km）。
MAX_POI_DISTANCE_KM = 100.0


def fetch_amap_city(city: str, key: str, client: httpx.Client) -> list[dict]:
    """抓一个城市的景点。返回原始条目列表（未去重）。

    先按 ``region`` 关键词搜；**如果坐标校验后剩不下东西，改成按目的地名直接搜**。

    为什么需要两条路：``region`` 要的是**行政区名**，而景区级目的地
    （四姑娘山、青城山、茶卡盐湖）不是行政区，高德不认识 → 静默回落到北京 →
    被坐标校验全部剔除 → 抓成 0 条。

    按名搜（不带 region）能直接命中景区本身，实测拿到的是
    「四姑娘山景区 4.9」「华山风景名胜区 4.9」「茶卡盐湖景区 4.8」，
    都带真实的季节性开放时间。同名异地由坐标校验兜住。
    """
    pois = _amap_text_search(client, key, city)
    if len(normalize_amap(pois, city, quiet=True)) >= MIN_PER_CITY:
        return pois

    extra = _amap_name_search(client, key, city)
    return pois + extra


def _amap_text_search(client: httpx.Client, key: str, city: str) -> list[dict]:
    """按 region + 关键词搜。"""
    out: list[dict] = []
    for page in (1, 2, 3):  # 3 页 75 条候选，去重去子条目后仍够 20
        payload = _amap_page(client, key, city, page)
        if payload is None:
            continue
        pois = payload.get("pois") or []
        if not pois:
            break
        out.extend(pois)
        # 个人账号 QPS 很低。实测 0.22s 的间隔在连续跑 180 城后会开始
        # 返回 CUQPS_HAS_EXCEEDED_THE_LIMIT，一整个城市抓成 0 条。
        time.sleep(0.45)
    return out


def _amap_name_search(client: httpx.Client, key: str, city: str) -> list[dict]:
    """按目的地名直接搜（不带 region）—— 用于 region 失效的景区级目的地。"""
    out: list[dict] = []
    for page in (1, 2):
        for attempt in range(3):
            try:
                resp = client.get(AMAP_URL, params={
                    "key": key, "keywords": city, "types": AMAP_TYPES,
                    "show_fields": "business", "page_size": "25",
                    "page_num": str(page), "output": "JSON",
                })
                payload = resp.json()
            except Exception as exc:  # noqa: BLE001
                print(f"      ! 按名搜第 {page} 页失败: {type(exc).__name__}")
                payload = None
                break

            if payload.get("status") == "1":
                break
            infocode = str(payload.get("infocode", ""))
            if "CUQPS" in infocode or "QPS" in infocode:
                wait = 2.0 * (attempt + 1)
                print(f"      · 按名搜 QPS 超限，等 {wait:.0f}s")
                time.sleep(wait)
                continue
            payload = None
            break

        if not payload:
            continue
        pois = payload.get("pois") or []
        if not pois:
            break
        # 打标记：这批结果质量比 region 搜索差得多，normalize 时要用更严的门槛
        for poi in pois:
            poi["_from_name_search"] = True
        out.extend(pois)
        time.sleep(0.45)

    if out:
        print(f"      · region 失效，按名搜补了 {len(out)} 条候选")
    return out


def _amap_page(
    client: httpx.Client, key: str, city: str, page: int, retries: int = 3
) -> dict | None:
    """取一页结果，QPS 超限时退避重试。

    不做重试的话，超限会让那一页直接落空 —— 实测有 10 个城市因此抓成 0 条，
    而且日志里只是一行 ``! 高德返回错误``，很容易漏过去。
    """
    for attempt in range(retries):
        try:
            resp = client.get(AMAP_URL, params={
                "key": key, "keywords": "景点", "region": city,
                "types": AMAP_TYPES, "city_limit": "true",
                "show_fields": "business", "page_size": "25",
                "page_num": str(page), "output": "JSON",
            })
            payload = resp.json()
        except Exception as exc:  # noqa: BLE001 - 单页失败不该中断整城
            print(f"      ! 第 {page} 页请求失败: {type(exc).__name__}")
            time.sleep(1.0)
            continue

        if payload.get("status") == "1":
            return payload

        info = str(payload.get("info", "?"))
        infocode = str(payload.get("infocode", ""))
        if "DAILY_QUERY_OVER_LIMIT" in infocode or "DAILY" in infocode:
            raise RuntimeError("高德日配额用尽，停止抓取")
        if "CUQPS" in infocode or "QPS" in infocode:
            wait = 2.0 * (attempt + 1)
            print(f"      · QPS 超限，等 {wait:.0f}s 后重试（第 {attempt + 1} 次）")
            time.sleep(wait)
            continue
        print(f"      ! 高德返回错误: {info}（infocode={infocode}）")
        return None

    print(f"      ! 第 {page} 页重试 {retries} 次仍失败")
    return None


def normalize_amap(pois: list[dict], city: str, *, quiet: bool = False) -> list[dict]:
    """过滤 + 去重 + 去子条目，取前 N 条。

    **保留高德返回的顺序**，不按评分重排 —— 那个顺序是「关键词=景点」的
    相关度排序，比评分有区分度（高德只给高分项，一城几十条全是 4.8/4.9，
    按评分排等于按名称长度排）。实测北京前 16 条就是天坛、天安门、故宫、
    国博、鼓楼、什刹海这一串核心景点。
    """
    info = lookup_city(city)
    seen: set[str] = set()
    rows: list[dict] = []
    dropped_far = 0
    dropped_low = 0

    for poi in pois:
        name = (poi.get("name") or "").strip()
        if not name or len(name) > 40:
            continue
        if AMAP_REJECT.search(name):
            continue

        type_text = poi.get("type") or ""
        if type_text and not any(k in type_text for k in AMAP_KEEP_TYPES):
            continue
        # 「科教文化」是个大类，培训机构/幼儿园/书店都挂在下面
        if any(k in type_text for k in AMAP_REJECT_TYPES):
            continue

        # 坐标校验：region 参数对高德不认识的县级市会**静默失效并回落到北京**。
        # 实测 region=漠河 返回的是故宫、天安门、雍和宫 —— 不校验的话
        # 漠河会挂上 20 条北京的景点，而且看起来毫无异常。
        if info is not None:
            dist = _poi_distance_km(poi.get("location") or "", info.lat, info.lon)
            if dist is not None and dist > MAX_POI_DISTANCE_KM:
                dropped_far += 1
                continue

        if name in seen:  # 分页之间会有重叠
            continue
        seen.add(name)

        biz = poi.get("business") or {}
        try:
            rating = float(biz.get("rating") or 0)
        except (TypeError, ValueError):
            rating = 0.0

        # 按名搜索的结果很杂（关键词是地名本身），再加一道评分门槛。
        # 实测「漠河」返回了 10 家托管中心/培训班，评分 1.7–3.9。
        if poi.get("_from_name_search") and rating < NAME_SEARCH_MIN_RATING:
            dropped_low += 1
            continue

        open_hours = str(biz.get("opentime_week") or biz.get("opentime_today") or "").strip()
        rows.append({
            "name": name, "type": type_text, "rating": rating,
            "open_hours": open_hours[:120],
            "address": (poi.get("address") or "")[:60],
        })

    if not quiet:
        if dropped_far:
            print(f"      · 坐标校验剔除 {dropped_far} 条（不属于 {city}）")
        if dropped_low:
            print(f"      · 评分过低剔除 {dropped_low} 条")

    rows = _drop_subentries(rows)
    # 无评分的排到最后，其余保持原序（stable sort）
    rows.sort(key=lambda r: 0 if r["rating"] > 0 else 1)
    return rows[:TARGET_PER_CITY]


def _poi_distance_km(location: str, lat: float, lon: float) -> float | None:
    """高德的 ``location`` 是 ``"经度,纬度"`` 字符串。解析不了就返回 None。"""
    if not location or "," not in location:
        return None
    lon_s, _, lat_s = location.partition(",")
    try:
        return _haversine_km(lat, lon, float(lat_s), float(lon_s))
    except ValueError:
        return None


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    from math import asin, cos, radians, sin, sqrt

    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    return 2 * 6371.0 * asin(sqrt(a))


def _drop_subentries(rows: list[dict]) -> list[dict]:
    """丢掉「父条目的组成部分」。

    高德会把一个景区拆成多条：``天坛公园`` / ``天坛公园-祈年殿``、
    ``外滩`` / ``外滩观景台``。父条目在列表里时子条目没有信息增量，
    留着只会挤占名额、让计划里出现两个几乎一样的点。
    """
    names = [r["name"] for r in rows]
    out: list[dict] = []
    for r in rows:
        name = r["name"]
        is_sub = False
        for other in names:
            if other == name or len(other) >= len(name):
                continue
            # 短名 + 分隔符 + 后缀（天坛公园-祈年殿）
            if name.startswith(other) and name[len(other)] in "-—·（( ":
                is_sub = True
                break
            # 短名 + 景点类后缀（外滩 → 外滩观景台 / 外滩-观景平台）
            if name.startswith(other) and len(name) - len(other) <= 5 and any(
                s in name[len(other):] for s in ("观景", "打卡", "平台", "入口")
            ):
                is_sub = True
                break
        if not is_sub:
            out.append(r)
    return out


# ---------------------------------------------------------------------------
# Wikidata：海外城市
# ---------------------------------------------------------------------------

WD_TYPES = [
    "wd:Q570116",   # tourist attraction
    "wd:Q33506",    # museum
    "wd:Q23413",    # castle
    "wd:Q16970",    # church building
    "wd:Q4989906",  # monument
    "wd:Q839954",   # archaeological site
    "wd:Q153562",   # historic building
    "wd:Q16560",    # palace
    "wd:Q44613",    # monastery
    "wd:Q22698",    # park
    "wd:Q174782",   # square
    "wd:Q12518",    # tower
    "wd:Q12280",    # bridge
    "wd:Q207694",   # art museum
    "wd:Q32815",    # mosque
    "wd:Q131647",   # botanical garden
    "wd:Q35127",    # theatre building
    "wd:Q1440300",  # observation deck
    "wd:Q15848826",  # historic site
]

# 这些类型虽然有 sitelinks，但游客通常不会专程去 —— 会挤掉真正的景点
WD_REJECT_TYPES = ("stadium", "arena", "sports", "station", "airport",
                   "headquarters", "office", "company", "university",
                   "school", "hospital", "cemetery", "hotel", "bank")

# **分批查询**。把 19 个类型一次性丢进去会让查询过重：实测曼谷那次
# 只返回了 17 条、连大皇宫（sitelinks=48）都没出来 —— 查询引擎在算不完时
# 会返回部分结果而不报错，这种静默截断最难发现。
WD_BATCHES: list[list[str]] = [
    ["wd:Q570116", "wd:Q33506", "wd:Q207694", "wd:Q16560"],
    ["wd:Q16970", "wd:Q2977", "wd:Q32815", "wd:Q23413", "wd:Q153562", "wd:Q15848826"],
    ["wd:Q12518", "wd:Q11303", "wd:Q860861", "wd:Q12280", "wd:Q174782",
     "wd:Q19844914"],
    ["wd:Q22698", "wd:Q839954", "wd:Q4989906", "wd:Q1440300", "wd:Q162157",
     "wd:Q1107656", "wd:Q330284"],
]

# 单一类型在每城的上限。Wikidata 对伊斯兰国家的清真寺记录极全 ——
# 不加限制时吉隆坡 20 条里 12 条是清真寺，把博物馆、塔、街区全挤掉了。
# 清真寺确实是当地必去（国家清真寺、苏丹回教堂），但 60% 的占比不合理。
MAX_PER_TYPE = 6

# 摩天大楼单独设高阈值：双峰塔（89）、吉隆坡塔（43）是地标，
# 但东京那批 17~21 的写字楼不是 —— 它们会把真正的景点挤出前 20。
SKYSCRAPER_KEYS = ("摩天大楼", "摩天大樓", "skyscraper", "高层建筑", "高層建築")
SKYSCRAPER_MIN_SITELINKS = 38

WD_QUERY = """
SELECT ?item ?itemLabel ?typeLabel ?sl WHERE {{
  SERVICE wikibase:around {{
    ?item wdt:P625 ?c .
    bd:serviceParam wikibase:center "Point({lon} {lat})"^^geo:wktLiteral .
    bd:serviceParam wikibase:radius "{radius}" .
  }}
  ?item wdt:P31 ?type .
  VALUES ?type {{ {types} }}
  ?item wikibase:sitelinks ?sl .
  FILTER(?sl >= 5)
  SERVICE wikibase:label {{
    bd:serviceParam wikibase:language "zh-cn,zh-hans,zh,en".
  }}
}}
ORDER BY DESC(?sl)
LIMIT 45
"""


def fetch_wikidata_city(city: str, client: httpx.Client, radius: int) -> list[dict]:
    """按坐标半径查一个海外城市的景点。

    分四批查询后合并 —— 单批一次 19 个类型会被静默截断（见 ``WD_BATCHES``）。
    """
    info = lookup_city(city)
    if info is None:
        return []

    best: dict[str, dict] = {}
    for batch in WD_BATCHES:
        query = WD_QUERY.format(
            lon=info.lon, lat=info.lat, radius=radius, types=" ".join(batch)
        )
        try:
            resp = client.post(WD_ENDPOINT, data={"query": query},
                               headers={"Accept": "application/sparql-results+json"})
        except Exception as exc:  # noqa: BLE001 - 单批失败不该中断整城
            print(f"      ! SPARQL 请求失败: {type(exc).__name__}")
            continue

        if resp.status_code != 200:
            print(f"      ! SPARQL HTTP {resp.status_code}")
            continue

        for b in resp.json().get("results", {}).get("bindings", []):
            name = (b.get("itemLabel", {}).get("value") or "").strip()
            qid = (b.get("item", {}).get("value") or "").rsplit("/", 1)[-1]
            # 没有标签的条目返回的是 QID，直接丢掉
            if not name or name == qid or len(name) > 40 or len(name) < 2:
                continue

            type_label = (b.get("typeLabel", {}).get("value") or "").strip()
            if any(k in type_label.lower() for k in WD_REJECT_TYPES):
                continue

            try:
                sl = int(b.get("sl", {}).get("value") or 0)
            except ValueError:
                sl = 0

            # 摩天大楼里只有真正的城市地标才留（双峰塔、吉隆坡塔），
            # 写字楼一律丢掉
            if any(k in type_label for k in SKYSCRAPER_KEYS) and sl < SKYSCRAPER_MIN_SITELINKS:
                continue

            # 同一个 QID 会因为多个 P31 出现多次，保留 sitelinks 最高的那次
            prev = best.get(qid)
            if prev is None or sl > prev["sitelinks"]:
                best[qid] = {
                    "name": zhconv.convert(name, "zh-cn"),
                    "type": zhconv.convert(type_label, "zh-cn"),
                    "coord": (b.get("coord", {}).get("value") or "").strip(),
                    "admin": (b.get("adminLabel", {}).get("value") or "").strip(),
                    "sitelinks": sl,
                }
        time.sleep(0.4)

    rows = sorted(best.values(), key=lambda r: -r["sitelinks"])

    # 行政区过滤：剔除明显属于邻近城市的条目（东京查询会带出横滨的景点）。
    # 拿不到 admin 的保留 —— 宁可多留也不误杀。
    filtered = [
        r for r in rows
        if not r["admin"] or city in r["admin"] or _admin_related(city, r["admin"])
    ]
    # 全被过滤掉说明 admin 字段不可靠，退回未过滤的
    if len(filtered) < MIN_PER_CITY:
        filtered = rows

    # 类型多样性上限（见 MAX_PER_TYPE）。按**归并后的类型组**计数 ——
    # Wikidata 的标签不统一（同一个 Q32815 在曼谷叫「清真寺」、
    # 在吉隆坡叫「回教堂」），按原始标签计数会漏掉一半。
    picked: list[dict] = []
    overflow: list[dict] = []
    group_count: dict[str, int] = {}
    for r in filtered:
        group = _type_group(r["type"])
        if group_count.get(group, 0) < MAX_PER_TYPE:
            group_count[group] = group_count.get(group, 0) + 1
            picked.append(r)
        else:
            overflow.append(r)

    # 小城市本来就凑不满，这时用溢出的补上 —— 多样性是锦上添花，
    # 条数不够会直接触发「每城至少 5 条」的断言。
    if len(picked) < MIN_PER_CITY:
        picked.extend(overflow[: MIN_PER_CITY - len(picked)])

    return picked[:TARGET_PER_CITY]


# 类型标签归并：把同义标签折到一组，供多样性上限计数
_TYPE_GROUP_SYNONYMS = (
    ("清真寺", "回教堂", "mosque", "masjid"),
    ("教堂", "主教座堂", "church", "cathedral", "chapel"),
    ("博物馆", "美術館", "美术馆", "museum", "gallery"),
    ("公园", "公園", "park", "garden", "植物園", "植物园"),
    ("宫殿", "王宫", "palace", "castle", "城堡"),
    ("塔", "tower", "觀光塔", "观景塔", "摩天", "skyscraper"),
    ("广场", "square", "plaza"),
    ("桥", "bridge"),
    ("纪念碑", "紀念", "monument", "memorial"),
)


def _type_group(type_label: str) -> str:
    """把类型标签归到一组，用于计数。归不到的用标签本身。"""
    low = type_label.lower()
    for group in _TYPE_GROUP_SYNONYMS:
        if any(k.lower() in low for k in group):
            return group[0]
    return type_label


def _admin_related(city: str, admin: str) -> bool:
    """行政区是否与目标城市相关。

    东京的景点 P131 可能挂到「台东区」「新宿区」这类下级区，
    所以不能只做字符串包含 —— 那样会把整个城市的景点全滤掉。
    这里只在 admin 明确是**另一个城市**时才判为不相关。
    """
    # 城市名的前两个字相同通常就是同一个城市体系（如 东京 / 东京都）
    if city[:2] and city[:2] in admin:
        return True
    return False


# ---------------------------------------------------------------------------
# 缓存
# ---------------------------------------------------------------------------


def cache_path(source: str) -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return CACHE_DIR / f"fetch_{source}.json"


def load_cache(source: str) -> dict:
    path = cache_path(source)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def save_cache(source: str, data: dict) -> None:
    cache_path(source).write_text(
        json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def run_amap(cities: list[str], limit: int | None) -> dict:
    key = env("AMAP_API_KEY")
    if not key:
        print("缺少 AMAP_API_KEY，跳过")
        return {}

    cache = load_cache("amap")
    todo = [c for c in cities if c not in cache]
    if limit:
        todo = todo[:limit]
    print(f"高德：{len(cities)} 城，已缓存 {len(cache)}，本次抓 {len(todo)}")

    with httpx.Client(timeout=20, trust_env=True) as client:
        for i, city in enumerate(todo, 1):
            try:
                pois = fetch_amap_city(city, key, client)
                rows = normalize_amap(pois, city)
                cache[city] = rows
                print(f"  [{i}/{len(todo)}] {city}: {len(rows)} 条")
            except RuntimeError as exc:
                print(f"  [{i}/{len(todo)}] {city}: {exc} —— 提前结束")
                save_cache("amap", cache)
                raise SystemExit(1)
            except Exception as exc:  # noqa: BLE001
                print(f"  [{i}/{len(todo)}] {city}: 失败 {type(exc).__name__}")
            if i % 10 == 0:
                save_cache("amap", cache)

    save_cache("amap", cache)
    return cache


# 大都市半径放大（覆盖市区全域），小城收紧避免带出邻市
BIG_CITIES = {"东京", "巴黎", "伦敦", "纽约", "首尔", "曼谷", "新加坡",
              "伊斯坦布尔", "莫斯科", "洛杉矶", "罗马", "柏林", "马德里",
              "巴塞罗那", "米兰", "迪拜", "新德里", "开罗"}


def run_wikidata(cities: list[str], limit: int | None) -> dict:
    cache = load_cache("wikidata")
    todo = [c for c in cities if c not in cache]
    if limit:
        todo = todo[:limit]
    print(f"Wikidata：{len(cities)} 城，已缓存 {len(cache)}，本次抓 {len(todo)}")

    with httpx.Client(timeout=90, trust_env=True, headers={
        "User-Agent": "Cairn/1.0 (offline seed builder; educational demo)",
    }) as client:
        for i, city in enumerate(todo, 1):
            radius = 20 if city in BIG_CITIES else 14
            rows = fetch_wikidata_city(city, client, radius)
            cache[city] = rows
            print(f"  [{i}/{len(todo)}] {city}: {len(rows)} 条（半径 {radius}km）")
            if i % 5 == 0:
                save_cache("wikidata", cache)
            time.sleep(1.2)  # SPARQL 端点对频率敏感

    save_cache("wikidata", cache)
    return cache


# ---------------------------------------------------------------------------
# 生成 seed_generated.py
# ---------------------------------------------------------------------------

CURRENCY_BY_COUNTRY = {
    "美国": "USD", "日本": "JPY", "英国": "GBP", "法国": "EUR", "德国": "EUR",
    "意大利": "EUR", "西班牙": "EUR", "荷兰": "EUR", "比利时": "EUR",
    "奥地利": "EUR", "葡萄牙": "EUR", "希腊": "EUR", "爱尔兰": "EUR",
    "芬兰": "EUR", "斯洛伐克": "EUR", "捷克": "CZK", "匈牙利": "HUF",
    "波兰": "PLN", "丹麦": "DKK", "瑞典": "SEK", "挪威": "NOK",
    "瑞士": "CHF", "俄罗斯": "RUB", "土耳其": "TRY", "阿联酋": "AED",
    "泰国": "THB", "新加坡": "SGD", "马来西亚": "MYR", "韩国": "KRW",
    "越南": "VND", "印度": "INR", "澳大利亚": "AUD", "新西兰": "NZD",
    "加拿大": "CAD", "埃及": "EGP", "中国": "CNY", "中国台湾": "TWD",
}

# 城市级货币覆盖。港澳的 country 都是「中国」，按国家映射会错成 CNY，
# 而它们实际用 HKD / MOP —— 手写那批就是这么标的，不能两套数据不一致。
CITY_CURRENCY = {"香港": "HKD", "澳门": "MOP"}


def currency_for(city: str, country: str) -> str:
    return CITY_CURRENCY.get(city) or CURRENCY_BY_COUNTRY.get(country, "CNY")

# 海外城市的门票价格基准。抓取拿不到票价，只能按类型给一个量级 ——
# 所以 source 标 wikidata，明确告诉模型「票价是估算的」。
OVERSEAS_PRICE_SCALE = {
    "USD": 1.0, "EUR": 1.0, "GBP": 1.0, "CHF": 1.0, "AUD": 1.2, "CAD": 1.0,
    "SGD": 1.0, "NZD": 1.1,
    "JPY": 100.0, "KRW": 9000.0, "THB": 200.0, "MYR": 3.0, "VND": 15000.0,
    "INR": 60.0, "CZK": 15.0, "HUF": 250.0, "PLN": 2.5, "DKK": 4.5,
    "SEK": 7.0, "NOK": 7.0, "RUB": 60.0, "TRY": 20.0, "AED": 2.5,
    "EGP": 30.0, "CNY": 1.0, "TWD": 20.0,
}


def round_price(price: float, currency: str) -> float:
    """把价格修到当地货币的合理粒度（日元不会有小数，欧元取整到 5）。"""
    if price <= 0:
        return 0.0
    if currency in ("JPY", "KRW", "VND", "INR", "HUF", "EGP"):
        return float(int(round(price / 100.0)) * 100)
    if price < 20:
        return round(price)
    return float(int(round(price / 5.0)) * 5)


def build_rows(amap: dict, wikidata: dict) -> list[tuple]:
    """合并两个来源，产出 ``_row()`` 的位置参数元组。"""
    rows: list[tuple] = []

    for city, items in amap.items():
        info = lookup_city(city)
        country = info.country if info else "中国"
        currency = currency_for(city, country)
        for it in items:
            tags, price, hours, kid = classify(it["type"], it["name"])
            note = "高德实抓：名称/评分/开放时间真实，票价为估算" if price else "高德实抓"
            rows.append((
                city, it["name"], tags, price, hours,
                round(it["rating"], 1) if it["rating"] else 4.0, kid,
                it["open_hours"] or "以官方公告为准", note, currency,
                "", "amap",
            ))

    for city, items in wikidata.items():
        info = lookup_city(city)
        country = info.country if info else ""
        currency = currency_for(city, country)
        scale = OVERSEAS_PRICE_SCALE.get(currency, 1.0)
        for it in items:
            tags, price, hours, kid = classify(it["type"], it["name"])
            price = round_price(price * scale, currency)
            # Wikidata 的知名度可以用 sitelinks 粗估评分：链接越多越有名
            rating = min(4.8, 3.6 + it["sitelinks"] * 0.02)
            rows.append((
                city, it["name"], tags, price, hours,
                round(rating, 1), kid,
                "以官方公告为准", "Wikidata 实抓：名称/类型真实，票价与评分为估算",
                currency, "", "wikidata",
            ))

    return rows


def write_generated(rows: list[tuple]) -> Path:
    """写 ``seed_generated.py``。文件是机器生成的，不追求可读性。"""
    target = Path(__file__).resolve().parents[1] / "mcp_server" / "data" / "seed_generated.py"
    lines = [
        '"""抓取生成的景点数据 —— **请勿手工编辑**。',
        "",
        "重新生成：``python -m scripts.build_attractions --source all``",
        "",
        "字段可信度见 ``seed.py`` 里 ``source`` 字段的说明。",
        '"""',
        "",
        "from __future__ import annotations",
        "",
        "# (city, name, tags, price, hours, rating, kid, open_hours, note,",
        "#  currency, verified_at, source)",
        "RAW_ROWS: list[tuple] = [",
    ]
    for r in rows:
        lines.append("    " + repr(r) + ",")
    lines += [
        "]",
        "",
        "",
        "def build_rows(row_factory):",
        '    """用 ``seed._row`` 把原始行补成完整行（country 由城市表实时查）。"""',
        "    return [row_factory(*r) for r in RAW_ROWS]",
        "",
    ]
    target.write_text("\n".join(lines), encoding="utf-8")
    return target


def main() -> None:
    ap = argparse.ArgumentParser(description="抓取景点数据")
    ap.add_argument("--source", choices=["amap", "wikidata", "all"], default="all")
    ap.add_argument("--limit", type=int, default=None, help="每源最多抓几个城市（试跑用）")
    args = ap.parse_args()

    amap: dict = {}
    wikidata: dict = {}

    if args.source in ("amap", "all"):
        amap = run_amap(CHINA_CITIES, args.limit)
    else:
        amap = load_cache("amap")

    if args.source in ("wikidata", "all"):
        wikidata = run_wikidata(OVERSEAS_CITIES, args.limit)
    else:
        wikidata = load_cache("wikidata")

    # 高德覆盖不足的中国城市回落到 Wikidata。
    # 主要是台湾（高德是大陆地图，对台澎金马几乎无覆盖，region 会静默
    # 回落到北京）和几个县级市（漠河、青海湖、海东）。
    if args.source in ("amap", "all", "wikidata"):
        thin = [c for c in CHINA_CITIES if len(amap.get(c) or []) < MIN_PER_CITY]
        thin = [c for c in thin if c not in wikidata or len(wikidata.get(c) or []) < MIN_PER_CITY]
        if thin:
            print(f"\n高德覆盖不足的 {len(thin)} 城，回落到 Wikidata：{thin}")
            wikidata.update(run_wikidata(thin, None))

    rows = build_rows(amap, wikidata)
    path = write_generated(rows)
    print(f"\n写出 {len(rows)} 行 → {path}")
    print(f"  高德 {sum(len(v) for v in amap.values())} 条 / {len(amap)} 城")
    print(f"  Wikidata {sum(len(v) for v in wikidata.values())} 条 / {len(wikidata)} 城")


if __name__ == "__main__":
    main()
