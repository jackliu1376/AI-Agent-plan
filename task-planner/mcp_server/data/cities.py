"""城市坐标表 —— 天气与交通两个工具共用的唯一城市事实来源。

为什么必须有这张表
------------------
Open-Meteo 的地理编码接口对**中文城市名**基本不可用，而且会静默返回错误结果：

    name=纽约    language=zh  -> 无结果
    name=东京    language=zh  -> 命中「江苏·东京」(32.21,119.29)   ← 中国江苏的一个镇
    name=New York language=zh -> 命中「内布拉斯加州·约克」          ← 美国内布拉斯加的 York

第二种情况最危险：不报错，直接把江苏的天气当成东京天气返回，
正好违反「事实数据不得臆造」这条硬约束。

因此策略是：**本地表优先，命中即用；未命中才回落到 Open-Meteo 地理编码**，
并要求回落时返回完整的匹配元信息（国家 / 一级行政区 / 人口），供模型复核。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class City:
    """一个城市的确定坐标与归属。"""

    name_zh: str
    name_en: str
    lat: float
    lon: float
    country: str
    timezone: str
    aliases: tuple[str, ...] = ()


# 中国城市
_CN: list[City] = [
    City("北京", "Beijing", 39.9042, 116.4074, "中国", "Asia/Shanghai"),
    City("上海", "Shanghai", 31.2304, 121.4737, "中国", "Asia/Shanghai"),
    City("广州", "Guangzhou", 23.1291, 113.2644, "中国", "Asia/Shanghai"),
    City("深圳", "Shenzhen", 22.5431, 114.0579, "中国", "Asia/Shanghai"),
    City("成都", "Chengdu", 30.5728, 104.0668, "中国", "Asia/Shanghai"),
    City("重庆", "Chongqing", 29.5630, 106.5516, "中国", "Asia/Shanghai"),
    City("杭州", "Hangzhou", 30.2741, 120.1551, "中国", "Asia/Shanghai"),
    City("南京", "Nanjing", 32.0603, 118.7969, "中国", "Asia/Shanghai"),
    City("西安", "Xi'an", 34.3416, 108.9398, "中国", "Asia/Shanghai", ("西安", "Xian")),
    City("武汉", "Wuhan", 30.5928, 114.3055, "中国", "Asia/Shanghai"),
    City("长沙", "Changsha", 28.2282, 112.9388, "中国", "Asia/Shanghai"),
    City("青岛", "Qingdao", 36.0671, 120.3826, "中国", "Asia/Shanghai"),
    City("厦门", "Xiamen", 24.4798, 118.0894, "中国", "Asia/Shanghai"),
    City("三亚", "Sanya", 18.2528, 109.5119, "中国", "Asia/Shanghai"),
    City("海口", "Haikou", 20.0444, 110.1999, "中国", "Asia/Shanghai"),
    City("昆明", "Kunming", 25.0389, 102.7183, "中国", "Asia/Shanghai"),
    City("哈尔滨", "Harbin", 45.8038, 126.5349, "中国", "Asia/Shanghai"),
    City("沈阳", "Shenyang", 41.8057, 123.4315, "中国", "Asia/Shanghai"),
    City("郑州", "Zhengzhou", 34.7466, 113.6254, "中国", "Asia/Shanghai"),
    City("兰州", "Lanzhou", 36.0611, 103.8343, "中国", "Asia/Shanghai"),
    City("拉萨", "Lhasa", 29.6520, 91.1721, "中国", "Asia/Shanghai"),
    City("乌鲁木齐", "Urumqi", 43.8256, 87.6168, "中国", "Asia/Shanghai"),
    City("桂林", "Guilin", 25.2736, 110.2900, "中国", "Asia/Shanghai"),
    City("丽江", "Lijiang", 26.8721, 100.2299, "中国", "Asia/Shanghai"),
    City("香港", "Hong Kong", 22.3193, 114.1694, "中国", "Asia/Hong_Kong", ("香港", "HongKong")),
]

# 国际城市
_INTL: list[City] = [
    City("东京", "Tokyo", 35.6762, 139.6503, "日本", "Asia/Tokyo"),
    City("大阪", "Osaka", 34.6937, 135.5023, "日本", "Asia/Tokyo"),
    City("京都", "Kyoto", 35.0116, 135.7681, "日本", "Asia/Tokyo"),
    City("札幌", "Sapporo", 43.0618, 141.3545, "日本", "Asia/Tokyo"),
    City("首尔", "Seoul", 37.5665, 126.9780, "韩国", "Asia/Seoul"),
    City("新加坡", "Singapore", 1.3521, 103.8198, "新加坡", "Asia/Singapore"),
    City("曼谷", "Bangkok", 13.7563, 100.5018, "泰国", "Asia/Bangkok"),
    City("普吉", "Phuket", 7.8804, 98.3923, "泰国", "Asia/Bangkok"),
    City("吉隆坡", "Kuala Lumpur", 3.1390, 101.6869, "马来西亚", "Asia/Kuala_Lumpur"),
    City("巴厘岛", "Bali", -8.4095, 115.1889, "印度尼西亚", "Asia/Makassar", ("登巴萨", "Denpasar")),
    City("河内", "Hanoi", 21.0278, 105.8342, "越南", "Asia/Bangkok"),
    City("胡志明市", "Ho Chi Minh City", 10.8231, 106.6297, "越南", "Asia/Bangkok", ("西贡", "Saigon")),
    City("迪拜", "Dubai", 25.2048, 55.2708, "阿联酋", "Asia/Dubai"),
    City("巴黎", "Paris", 48.8566, 2.3522, "法国", "Europe/Paris"),
    City("伦敦", "London", 51.5074, -0.1278, "英国", "Europe/London"),
    City("罗马", "Rome", 41.9028, 12.4964, "意大利", "Europe/Rome"),
    City("巴塞罗那", "Barcelona", 41.3874, 2.1686, "西班牙", "Europe/Madrid"),
    City("阿姆斯特丹", "Amsterdam", 52.3676, 4.9041, "荷兰", "Europe/Amsterdam"),
    City("柏林", "Berlin", 52.5200, 13.4050, "德国", "Europe/Berlin"),
    City("苏黎世", "Zurich", 47.3769, 8.5417, "瑞士", "Europe/Zurich"),
    City("莫斯科", "Moscow", 55.7558, 37.6173, "俄罗斯", "Europe/Moscow"),
    City("纽约", "New York", 40.7128, -74.0060, "美国", "America/New_York", ("纽约市", "NYC", "New York City")),
    City("洛杉矶", "Los Angeles", 34.0522, -118.2437, "美国", "America/Los_Angeles", ("LA",)),
    City("旧金山", "San Francisco", 37.7749, -122.4194, "美国", "America/Los_Angeles", ("三藩市", "SF")),
    City("拉斯维加斯", "Las Vegas", 36.1699, -115.1398, "美国", "America/Los_Angeles"),
    City("温哥华", "Vancouver", 49.2827, -123.1207, "加拿大", "America/Vancouver"),
    City("多伦多", "Toronto", 43.6532, -79.3832, "加拿大", "America/Toronto"),
    City("悉尼", "Sydney", -33.8688, 151.2093, "澳大利亚", "Australia/Sydney"),
    City("墨尔本", "Melbourne", -37.8136, 144.9631, "澳大利亚", "Australia/Melbourne"),
    City("奥克兰", "Auckland", -36.8485, 174.7633, "新西兰", "Pacific/Auckland"),
]

CITIES: list[City] = [*_CN, *_INTL]

# 查询索引：中文名 / 英文名 / 别名（统一小写、去空格） -> City
_INDEX: dict[str, City] = {}
for _city in CITIES:
    for _key in (_city.name_zh, _city.name_en, *_city.aliases):
        _INDEX[_key.strip().lower()] = _city
    # 英文名去掉空格与连字符的写法也要能命中（"newyork" / "hongkong"）
    _INDEX[_city.name_en.replace(" ", "").replace("-", "").lower()] = _city


def lookup_city(query: str) -> City | None:
    """按中文名 / 英文名 / 别名查城市。未命中返回 None。"""
    if not query:
        return None
    return _INDEX.get(query.strip().lower())


def supported_city_names() -> list[str]:
    """返回全部支持的城市中文名，用于错误提示。"""
    return sorted(city.name_zh for city in CITIES)


def cities_by_country() -> dict[str, list[str]]:
    """按国家/地区分组，便于在错误信息里给出可读的覆盖范围。"""
    grouped: dict[str, list[str]] = {}
    for city in CITIES:
        grouped.setdefault(city.country, []).append(city.name_zh)
    return {country: sorted(names) for country, names in grouped.items()}
