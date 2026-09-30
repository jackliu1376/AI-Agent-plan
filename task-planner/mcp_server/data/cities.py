"""城市解析 —— 三层策略，本地优先，网络兜底。

为什么不能只靠在线地理编码
--------------------------
Open-Meteo 的地理编码对中文城市名**不可靠**，实测未收录城市的失败率约 40%：

    name=开罗    -> 开罗/美国 (37.0,-89.2)     ← 美国伊利诺伊州的 Cairo
    name=里斯本   -> 里斯本/美国 (46.4,-97.7)   ← 美国北达科他州的 Lisbon
    name=米兰    -> 米兰/美国                  ← 同上
    name=伊斯坦布尔 / 华沙 / 内罗毕 / 加德满都 -> 无结果

而且 GeoNames 里不少城市**只有繁体中文名**（維也納 / 米蘭 / 利馬 / 基輔 / 開普敦）。

三层解析
--------
1. **策展表**（本文件，人工维护）—— 常用目的地 + GeoNames 收录不全的**非城市目的地**
   （圣托里尼、长滩岛、少女峰、马丘比丘……）与译名差异（科伦坡 vs GeoNames 的「可倫坡」）。
   元数据最全：中文国家名、时区、别名。
2. **生成索引**（``city_index.tsv``，由 ``build_city_index.py`` 从 GeoNames 生成）——
   14,000+ 个中文城市名，覆盖 229 个国家/地区。零网络、确定性。
3. **Open-Meteo 地理编码** —— 最后兜底，返回低置信度标记供模型复核。

查询时会把繁体输入转成简体，因此 ``維也納`` 与 ``维也纳`` 都能命中。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import zhconv

from mcp_server.data.countries import country_zh

DATA_DIR = Path(__file__).resolve().parent
INDEX_PATH = DATA_DIR / "city_index.tsv"


@dataclass(frozen=True)
class City:
    """一个地点的确定坐标与归属。"""

    name_zh: str
    name_en: str
    lat: float
    lon: float
    country: str
    timezone: str
    source: str = "curated-table"  # curated-table | city-index
    aliases: tuple[str, ...] = field(default=())


# ---------------------------------------------------------------------------
# 第一层：策展表（人工维护）
# ---------------------------------------------------------------------------

# 中国城市
_CN: list[City] = [
    City("北京", "Beijing", 39.9042, 116.4074, "中国", "Asia/Shanghai"),
    City("上海", "Shanghai", 31.2304, 121.4737, "中国", "Asia/Shanghai"),
    City("天津", "Tianjin", 39.3434, 117.3616, "中国", "Asia/Shanghai"),
    City("重庆", "Chongqing", 29.5630, 106.5516, "中国", "Asia/Shanghai"),
    City("广州", "Guangzhou", 23.1291, 113.2644, "中国", "Asia/Shanghai"),
    City("深圳", "Shenzhen", 22.5431, 114.0579, "中国", "Asia/Shanghai"),
    City("成都", "Chengdu", 30.5728, 104.0668, "中国", "Asia/Shanghai"),
    City("杭州", "Hangzhou", 30.2741, 120.1551, "中国", "Asia/Shanghai"),
    City("南京", "Nanjing", 32.0603, 118.7969, "中国", "Asia/Shanghai"),
    City("西安", "Xi'an", 34.3416, 108.9398, "中国", "Asia/Shanghai", aliases=("Xian",)),
    City("武汉", "Wuhan", 30.5928, 114.3055, "中国", "Asia/Shanghai"),
    City("长沙", "Changsha", 28.2282, 112.9388, "中国", "Asia/Shanghai"),
    City("郑州", "Zhengzhou", 34.7466, 113.6254, "中国", "Asia/Shanghai"),
    City("济南", "Jinan", 36.6512, 117.1201, "中国", "Asia/Shanghai"),
    City("青岛", "Qingdao", 36.0671, 120.3826, "中国", "Asia/Shanghai"),
    City("沈阳", "Shenyang", 41.8057, 123.4315, "中国", "Asia/Shanghai"),
    City("长春", "Changchun", 43.8171, 125.3235, "中国", "Asia/Shanghai"),
    City("哈尔滨", "Harbin", 45.8038, 126.5349, "中国", "Asia/Shanghai"),
    City("石家庄", "Shijiazhuang", 38.0428, 114.5149, "中国", "Asia/Shanghai"),
    City("太原", "Taiyuan", 37.8706, 112.5489, "中国", "Asia/Shanghai"),
    City("合肥", "Hefei", 31.8206, 117.2272, "中国", "Asia/Shanghai"),
    City("福州", "Fuzhou", 26.0745, 119.2965, "中国", "Asia/Shanghai"),
    City("南昌", "Nanchang", 28.6820, 115.8579, "中国", "Asia/Shanghai"),
    City("南宁", "Nanning", 22.8170, 108.3665, "中国", "Asia/Shanghai"),
    City("海口", "Haikou", 20.0444, 110.1999, "中国", "Asia/Shanghai"),
    City("三亚", "Sanya", 18.2528, 109.5119, "中国", "Asia/Shanghai"),
    City("贵阳", "Guiyang", 26.6470, 106.6302, "中国", "Asia/Shanghai"),
    City("昆明", "Kunming", 25.0389, 102.7183, "中国", "Asia/Shanghai"),
    City("拉萨", "Lhasa", 29.6520, 91.1721, "中国", "Asia/Shanghai"),
    City("兰州", "Lanzhou", 36.0611, 103.8343, "中国", "Asia/Shanghai"),
    City("西宁", "Xining", 36.6171, 101.7782, "中国", "Asia/Shanghai"),
    City("银川", "Yinchuan", 38.4872, 106.2309, "中国", "Asia/Shanghai"),
    City("乌鲁木齐", "Urumqi", 43.8256, 87.6168, "中国", "Asia/Shanghai"),
    City("呼和浩特", "Hohhot", 40.8414, 111.7519, "中国", "Asia/Shanghai", aliases=("呼市",)),
    City("厦门", "Xiamen", 24.4798, 118.0894, "中国", "Asia/Shanghai"),
    City("苏州", "Suzhou", 31.2989, 120.5853, "中国", "Asia/Shanghai"),
    City("桂林", "Guilin", 25.2736, 110.2900, "中国", "Asia/Shanghai"),
    City("丽江", "Lijiang", 26.8721, 100.2299, "中国", "Asia/Shanghai"),
    City("张家界", "Zhangjiajie", 29.1274, 110.4794, "中国", "Asia/Shanghai"),
    City("九寨沟", "Jiuzhaigou", 33.2600, 103.9180, "中国", "Asia/Shanghai"),
    City("敦煌", "Dunhuang", 40.1421, 94.6618, "中国", "Asia/Shanghai"),
    City("喀什", "Kashgar", 39.4704, 75.9898, "中国", "Asia/Shanghai"),
    City("大理", "Dali", 25.6065, 100.2676, "中国", "Asia/Shanghai"),
    City("香格里拉", "Shangri-La", 27.8253, 99.7065, "中国", "Asia/Shanghai"),
    City("西双版纳", "Xishuangbanna", 22.0017, 100.7971, "中国", "Asia/Shanghai", aliases=("景洪",)),
    City("香港", "Hong Kong", 22.3193, 114.1694, "中国", "Asia/Hong_Kong", aliases=("HongKong",)),
    City("澳门", "Macau", 22.1987, 113.5439, "中国", "Asia/Macau", aliases=("Macao",)),
    City("台北", "Taipei", 25.0330, 121.5654, "中国台湾", "Asia/Taipei"),
    City("高雄", "Kaohsiung", 22.6273, 120.3014, "中国台湾", "Asia/Taipei"),
]

# 国际城市（常用 + 译名与 GeoNames 不一致的）
_INTL: list[City] = [
    City("东京", "Tokyo", 35.6762, 139.6503, "日本", "Asia/Tokyo"),
    City("大阪", "Osaka", 34.6937, 135.5023, "日本", "Asia/Tokyo"),
    City("京都", "Kyoto", 35.0116, 135.7681, "日本", "Asia/Tokyo"),
    City("札幌", "Sapporo", 43.0618, 141.3545, "日本", "Asia/Tokyo"),
    City("冲绳", "Okinawa", 26.3344, 127.8056, "日本", "Asia/Tokyo", aliases=("那霸", "Naha")),
    City("首尔", "Seoul", 37.5665, 126.9780, "韩国", "Asia/Seoul"),
    City("济州岛", "Jeju", 33.4996, 126.5312, "韩国", "Asia/Seoul", aliases=("济州",)),
    City("新加坡", "Singapore", 1.3521, 103.8198, "新加坡", "Asia/Singapore"),
    City("曼谷", "Bangkok", 13.7563, 100.5018, "泰国", "Asia/Bangkok"),
    City("普吉", "Phuket", 7.8804, 98.3923, "泰国", "Asia/Bangkok", aliases=("普吉岛",)),
    City("苏梅岛", "Koh Samui", 9.5120, 100.0136, "泰国", "Asia/Bangkok", aliases=("苏梅",)),
    City("清迈", "Chiang Mai", 18.7883, 98.9853, "泰国", "Asia/Bangkok"),
    City("吉隆坡", "Kuala Lumpur", 3.1390, 101.6869, "马来西亚", "Asia/Kuala_Lumpur"),
    City("亚庇", "Kota Kinabalu", 5.9804, 116.0735, "马来西亚", "Asia/Kuala_Lumpur", aliases=("沙巴",)),
    City("槟城", "Penang", 5.4141, 100.3288, "马来西亚", "Asia/Kuala_Lumpur",
         aliases=("乔治市", "George Town")),
    City("巴厘岛", "Bali", -8.4095, 115.1889, "印度尼西亚", "Asia/Makassar", aliases=("登巴萨", "Denpasar")),
    City("龙目岛", "Lombok", -8.6500, 116.3240, "印度尼西亚", "Asia/Makassar"),
    City("美娜多", "Manado", 1.4748, 124.8421, "印度尼西亚", "Asia/Makassar"),
    City("雅加达", "Jakarta", -6.2088, 106.8456, "印度尼西亚", "Asia/Jakarta"),
    City("河内", "Hanoi", 21.0278, 105.8342, "越南", "Asia/Bangkok"),
    City("胡志明市", "Ho Chi Minh City", 10.8231, 106.6297, "越南", "Asia/Bangkok", aliases=("西贡", "Saigon")),
    City("岘港", "Da Nang", 16.0544, 108.2022, "越南", "Asia/Bangkok"),
    City("芽庄", "Nha Trang", 12.2388, 109.1967, "越南", "Asia/Bangkok"),
    City("富国岛", "Phu Quoc", 10.2270, 103.9640, "越南", "Asia/Bangkok"),
    City("大叻", "Da Lat", 11.9404, 108.4583, "越南", "Asia/Bangkok"),
    City("素可泰", "Sukhothai", 17.0078, 99.8230, "泰国", "Asia/Bangkok"),
    City("奥南", "Ao Nang", 8.0333, 98.8333, "泰国", "Asia/Bangkok", aliases=("甲米海滩",)),
    City("乌布", "Ubud", -8.5069, 115.2625, "印度尼西亚", "Asia/Makassar"),
    City("努沙杜瓦", "Nusa Dua", -8.7964, 115.2283, "印度尼西亚", "Asia/Makassar"),
    City("蒲甘", "Bagan", 21.1717, 94.8585, "缅甸", "Asia/Yangon"),
    City("瓦拉纳西", "Varanasi", 25.3176, 82.9739, "印度", "Asia/Kolkata",
         aliases=("瓦腊纳西", "贝拿勒斯")),
    City("果阿", "Goa", 15.4909, 73.8278, "印度", "Asia/Kolkata", aliases=("帕纳吉", "Panaji")),
    City("科威特城", "Kuwait City", 29.3759, 47.9774, "科威特", "Asia/Kuwait",
         aliases=("科威特",)),
    City("马尼拉", "Manila", 14.5995, 120.9842, "菲律宾", "Asia/Manila"),
    City("长滩岛", "Boracay", 11.9674, 121.9248, "菲律宾", "Asia/Manila"),
    City("薄荷岛", "Bohol", 9.8500, 124.1435, "菲律宾", "Asia/Manila"),
    City("巴拉望", "Palawan", 9.8349, 118.7384, "菲律宾", "Asia/Manila"),
    City("达沃", "Davao", 7.1907, 125.4553, "菲律宾", "Asia/Manila"),
    City("伊洛伊洛", "Iloilo", 10.7202, 122.5621, "菲律宾", "Asia/Manila"),
    City("金边", "Phnom Penh", 11.5564, 104.9282, "柬埔寨", "Asia/Phnom_Penh"),
    City("暹粒", "Siem Reap", 13.3671, 103.8448, "柬埔寨", "Asia/Phnom_Penh", aliases=("吴哥",)),
    City("加德满都", "Kathmandu", 27.7172, 85.3240, "尼泊尔", "Asia/Kathmandu"),
    City("科伦坡", "Colombo", 6.9271, 79.8612, "斯里兰卡", "Asia/Colombo",
         aliases=("可伦坡", "可倫坡")),
    City("康提", "Kandy", 7.2906, 80.6337, "斯里兰卡", "Asia/Colombo"),
    City("加勒", "Galle", 6.0535, 80.2210, "斯里兰卡", "Asia/Colombo"),
    City("马累", "Male", 4.1755, 73.5093, "马尔代夫", "Indian/Maldives", aliases=("马尔代夫",)),
    City("孟买", "Mumbai", 19.0760, 72.8777, "印度", "Asia/Kolkata"),
    City("新德里", "New Delhi", 28.6139, 77.2090, "印度", "Asia/Kolkata", aliases=("德里", "Delhi")),
    City("迪拜", "Dubai", 25.2048, 55.2708, "阿联酋", "Asia/Dubai"),
    City("阿布扎比", "Abu Dhabi", 24.4539, 54.3773, "阿联酋", "Asia/Dubai"),
    City("多哈", "Doha", 25.2854, 51.5310, "卡塔尔", "Asia/Qatar"),
    City("伊斯坦布尔", "Istanbul", 41.0082, 28.9784, "土耳其", "Europe/Istanbul"),
    City("卡帕多奇亚", "Cappadocia", 38.6431, 34.8289, "土耳其", "Europe/Istanbul",
         aliases=("格雷梅", "Goreme")),
    City("棉花堡", "Pamukkale", 37.9204, 29.1206, "土耳其", "Europe/Istanbul"),
    City("特拉维夫", "Tel Aviv", 32.0853, 34.7818, "以色列", "Asia/Jerusalem"),
    City("巴黎", "Paris", 48.8566, 2.3522, "法国", "Europe/Paris"),
    City("伦敦", "London", 51.5074, -0.1278, "英国", "Europe/London"),
    City("爱丁堡", "Edinburgh", 55.9533, -3.1883, "英国", "Europe/London"),
    City("罗马", "Rome", 41.9028, 12.4964, "意大利", "Europe/Rome"),
    City("米兰", "Milan", 45.4642, 9.1900, "意大利", "Europe/Rome"),
    City("威尼斯", "Venice", 45.4408, 12.3155, "意大利", "Europe/Rome"),
    City("佛罗伦萨", "Florence", 43.7696, 11.2558, "意大利", "Europe/Rome"),
    City("五渔村", "Cinque Terre", 44.1461, 9.6439, "意大利", "Europe/Rome"),
    City("巴塞罗那", "Barcelona", 41.3874, 2.1686, "西班牙", "Europe/Madrid"),
    City("马德里", "Madrid", 40.4168, -3.7038, "西班牙", "Europe/Madrid"),
    City("里斯本", "Lisbon", 38.7223, -9.1393, "葡萄牙", "Europe/Lisbon"),
    City("阿姆斯特丹", "Amsterdam", 52.3676, 4.9041, "荷兰", "Europe/Amsterdam"),
    City("布鲁塞尔", "Brussels", 50.8503, 4.3517, "比利时", "Europe/Brussels"),
    City("柏林", "Berlin", 52.5200, 13.4050, "德国", "Europe/Berlin"),
    City("慕尼黑", "Munich", 48.1351, 11.5820, "德国", "Europe/Berlin"),
    City("法兰克福", "Frankfurt", 50.1109, 8.6821, "德国", "Europe/Berlin"),
    City("苏黎世", "Zurich", 47.3769, 8.5417, "瑞士", "Europe/Zurich"),
    City("日内瓦", "Geneva", 46.2044, 6.1432, "瑞士", "Europe/Zurich"),
    City("因特拉肯", "Interlaken", 46.6863, 7.8632, "瑞士", "Europe/Zurich"),
    City("少女峰", "Jungfrau", 46.5367, 7.9625, "瑞士", "Europe/Zurich"),
    City("维也纳", "Vienna", 48.2082, 16.3738, "奥地利", "Europe/Vienna"),
    City("哈尔施塔特", "Hallstatt", 47.5622, 13.6493, "奥地利", "Europe/Vienna"),
    City("布拉格", "Prague", 50.0755, 14.4378, "捷克", "Europe/Prague"),
    City("布达佩斯", "Budapest", 47.4979, 19.0402, "匈牙利", "Europe/Budapest"),
    City("华沙", "Warsaw", 52.2297, 21.0122, "波兰", "Europe/Warsaw"),
    City("哥本哈根", "Copenhagen", 55.6761, 12.5683, "丹麦", "Europe/Copenhagen"),
    City("斯德哥尔摩", "Stockholm", 59.3293, 18.0686, "瑞典", "Europe/Stockholm"),
    City("奥斯陆", "Oslo", 59.9139, 10.7522, "挪威", "Europe/Oslo"),
    City("赫尔辛基", "Helsinki", 60.1699, 24.9384, "芬兰", "Europe/Helsinki"),
    City("雷克雅未克", "Reykjavik", 64.1466, -21.9426, "冰岛", "Atlantic/Reykjavik"),
    City("都柏林", "Dublin", 53.3498, -6.2603, "爱尔兰", "Europe/Dublin"),
    City("雅典", "Athens", 37.9838, 23.7275, "希腊", "Europe/Athens"),
    City("圣托里尼", "Santorini", 36.3932, 25.4615, "希腊", "Europe/Athens", aliases=("锡拉", "Thira")),
    City("米科诺斯", "Mykonos", 37.4467, 25.3289, "希腊", "Europe/Athens"),
    City("莫斯科", "Moscow", 55.7558, 37.6173, "俄罗斯", "Europe/Moscow"),
    City("圣彼得堡", "Saint Petersburg", 59.9311, 30.3609, "俄罗斯", "Europe/Moscow"),
    City("纽约", "New York", 40.7128, -74.0060, "美国", "America/New_York",
         aliases=("纽约市", "NYC", "New York City")),
    City("洛杉矶", "Los Angeles", 34.0522, -118.2437, "美国", "America/Los_Angeles", aliases=("LA",)),
    City("旧金山", "San Francisco", 37.7749, -122.4194, "美国", "America/Los_Angeles",
         aliases=("三藩市", "SF")),
    City("拉斯维加斯", "Las Vegas", 36.1699, -115.1398, "美国", "America/Los_Angeles"),
    City("夏威夷", "Honolulu", 21.3069, -157.8583, "美国", "Pacific/Honolulu",
         aliases=("檀香山", "Hawaii")),
    City("关岛", "Guam", 13.4443, 144.7937, "美国", "Pacific/Guam"),
    City("塞班", "Saipan", 15.1778, 145.7509, "美国", "Pacific/Saipan"),
    City("黄石国家公园", "Yellowstone", 44.4280, -110.5885, "美国", "America/Denver",
         aliases=("黄石",)),
    City("大峡谷", "Grand Canyon", 36.1069, -112.1129, "美国", "America/Phoenix"),
    City("优胜美地", "Yosemite", 37.8651, -119.5383, "美国", "America/Los_Angeles"),
    City("坎昆", "Cancun", 21.1619, -86.8515, "墨西哥", "America/Cancun"),
    City("墨西哥城", "Mexico City", 19.4326, -99.1332, "墨西哥", "America/Mexico_City"),
    City("温哥华", "Vancouver", 49.2827, -123.1207, "加拿大", "America/Vancouver"),
    City("多伦多", "Toronto", 43.6532, -79.3832, "加拿大", "America/Toronto"),
    City("班夫", "Banff", 51.1784, -115.5708, "加拿大", "America/Edmonton"),
    City("尼亚加拉瀑布", "Niagara Falls", 43.0962, -79.0377, "加拿大", "America/Toronto"),
    City("里约热内卢", "Rio de Janeiro", -22.9068, -43.1729, "巴西", "America/Sao_Paulo"),
    City("圣保罗", "Sao Paulo", -23.5505, -46.6333, "巴西", "America/Sao_Paulo"),
    City("布宜诺斯艾利斯", "Buenos Aires", -34.6037, -58.3816, "阿根廷",
         "America/Argentina/Buenos_Aires"),
    City("利马", "Lima", -12.0464, -77.0428, "秘鲁", "America/Lima"),
    City("库斯科", "Cusco", -13.5319, -71.9675, "秘鲁", "America/Lima"),
    City("马丘比丘", "Machu Picchu", -13.1631, -72.5450, "秘鲁", "America/Lima"),
    City("圣地亚哥", "Santiago", -33.4489, -70.6693, "智利", "America/Santiago"),
    City("复活节岛", "Easter Island", -27.1127, -109.3497, "智利", "Pacific/Easter"),
    City("乌尤尼", "Uyuni", -20.4624, -66.8250, "玻利维亚", "America/La_Paz"),
    City("开罗", "Cairo", 30.0444, 31.2357, "埃及", "Africa/Cairo"),
    City("卢克索", "Luxor", 25.6872, 32.6396, "埃及", "Africa/Cairo"),
    City("阿斯旺", "Aswan", 24.0889, 32.8998, "埃及", "Africa/Cairo"),
    City("开普敦", "Cape Town", -33.9249, 18.4241, "南非", "Africa/Johannesburg"),
    City("约翰内斯堡", "Johannesburg", -26.2041, 28.0473, "南非", "Africa/Johannesburg"),
    City("内罗毕", "Nairobi", -1.2921, 36.8219, "肯尼亚", "Africa/Nairobi"),
    City("卡萨布兰卡", "Casablanca", 33.5731, -7.5898, "摩洛哥", "Africa/Casablanca"),
    City("马拉喀什", "Marrakech", 31.6295, -7.9811, "摩洛哥", "Africa/Casablanca"),
    City("桑给巴尔", "Zanzibar", -6.1659, 39.2026, "坦桑尼亚", "Africa/Dar_es_Salaam"),
    City("塞伦盖蒂", "Serengeti", -2.3333, 34.8333, "坦桑尼亚", "Africa/Dar_es_Salaam"),
    City("维多利亚瀑布", "Victoria Falls", -17.9243, 25.8572, "津巴布韦", "Africa/Harare"),
    City("佩特拉", "Petra", 30.3285, 35.4444, "约旦", "Asia/Amman"),
    City("死海", "Dead Sea", 31.5590, 35.4732, "约旦", "Asia/Amman"),
    City("悉尼", "Sydney", -33.8688, 151.2093, "澳大利亚", "Australia/Sydney"),
    City("墨尔本", "Melbourne", -37.8136, 144.9631, "澳大利亚", "Australia/Melbourne"),
    City("布里斯班", "Brisbane", -27.4698, 153.0251, "澳大利亚", "Australia/Brisbane"),
    City("奥克兰", "Auckland", -36.8485, 174.7633, "新西兰", "Pacific/Auckland"),
    City("皇后镇", "Queenstown", -45.0312, 168.6626, "新西兰", "Pacific/Auckland"),
    City("大溪地", "Tahiti", -17.6509, -149.4260, "法属波利尼西亚", "Pacific/Tahiti",
         aliases=("帕皮提", "Papeete")),
]

CURATED: list[City] = [*_CN, *_INTL]

# ---------------------------------------------------------------------------
# 查询索引
# ---------------------------------------------------------------------------

_CURATED_INDEX: dict[str, City] = {}
for _city in CURATED:
    for _key in (_city.name_zh, _city.name_en, *_city.aliases):
        _CURATED_INDEX[_key.strip().lower()] = _city
        _CURATED_INDEX[_key.strip().lower().replace(" ", "").replace("-", "")] = _city

_INDEX_CACHE: dict[str, City] | None = None
_INDEX_ZH_COUNT = 0

# 行政区划后缀：用户通常省略，而 GeoNames 的中文别名常常带着。
# 例：索引里是「达沃市」，用户输入「达沃」；索引里是「科威特市」，用户输入「科威特城」。
CITY_SUFFIXES = ("市", "县", "区", "镇", "城", "岛", "港", "湾", "州", "省", "邦", "都")


def _base_names(name: str) -> list[str]:
    """返回去掉行政区后缀的候选名。结果短于 2 字则丢弃。

    短于 2 字的保护很重要：否则「盐城」会被剥成「盐」、「聊城」剥成「聊」，
    制造出无意义的键。
    """
    bases = []
    for suffix in CITY_SUFFIXES:
        if name.endswith(suffix) and len(name) - len(suffix) >= 2:
            bases.append(name[: -len(suffix)])
    return bases


def _load_index() -> dict[str, City]:
    """懒加载 GeoNames 生成的城市索引（约 1.4 万个中文名 + 英文名）。"""
    global _INDEX_CACHE, _INDEX_ZH_COUNT
    if _INDEX_CACHE is not None:
        return _INDEX_CACHE

    index: dict[str, City] = {}
    zh_count = 0
    if INDEX_PATH.exists():
        with INDEX_PATH.open(encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("#"):
                    continue
                parts = line.rstrip("\n").split("\t")
                if len(parts) < 6:
                    continue
                name_zh, name_en, lat, lon, cc, tz = parts[:6]
                city = City(
                    name_zh=name_zh,
                    name_en=name_en,
                    lat=float(lat),
                    lon=float(lon),
                    country=country_zh(cc),
                    timezone=tz,
                    source="city-index",
                )
                zh_count += 1
                # 精确名优先；后缀变体只在没有精确键时补位
                index.setdefault(name_zh, city)
                for base in _base_names(name_zh):
                    index.setdefault(base, city)
                index.setdefault(name_en.lower(), city)

    _INDEX_CACHE = index
    _INDEX_ZH_COUNT = zh_count
    return index


def _norm(text: str) -> str:
    return text.strip().lower()


def lookup_city(query: str) -> City | None:
    """按中文名 / 英文名 / 别名查城市。未命中返回 None。

    查找顺序（全部为确定性查表，不涉及网络，也不会返回"看起来像"的错误城市）：

    1. 策展表精确匹配
    2. 策展表去掉行政区后缀（``上海市`` → 策展表里的 ``上海``，展示名更规范）
    3. 生成索引精确匹配
    4. 生成索引去掉行政区后缀（``科威特城`` → 索引里的 ``科威特市``）
    5. 繁体转简体后重跑 1–4（``維也納`` → ``维也纳``）

    第 2 步排在第 3 步之前是有意的：策展表里的名字经过人工校对，
    比 GeoNames 的「上海市」更适合展示给用户。
    """
    if not query or not query.strip():
        return None

    raw = query.strip()

    def _try(text: str) -> City | None:
        key = _norm(text)
        flat = key.replace(" ", "").replace("-", "")

        hit = _CURATED_INDEX.get(key) or _CURATED_INDEX.get(flat)
        if hit is not None:
            return hit

        for base in _base_names(text):
            hit = _CURATED_INDEX.get(base.lower())
            if hit is not None:
                return hit

        index = _load_index()
        hit = index.get(key) or index.get(flat)
        if hit is not None:
            return hit

        for base in _base_names(text):
            hit = index.get(base.lower())
            if hit is not None:
                return hit

        return None

    hit = _try(raw)
    if hit is not None:
        return hit

    simplified = zhconv.convert(raw, "zh-cn")
    if simplified != raw:
        return _try(simplified)

    return None


def index_size() -> int:
    """生成索引收录的中文城市名数量。"""
    _load_index()
    return _INDEX_ZH_COUNT


def index_path() -> Path:
    return INDEX_PATH


def supported_city_names() -> list[str]:
    """策展表覆盖的城市中文名（用于错误提示里的示例）。"""
    return sorted(city.name_zh for city in CURATED)


def cities_by_country() -> dict[str, list[str]]:
    """按国家/地区分组（仅策展表，用于可读的覆盖范围提示）。"""
    grouped: dict[str, list[str]] = {}
    for city in CURATED:
        grouped.setdefault(city.country, []).append(city.name_zh)
    return {country: sorted(names) for country, names in grouped.items()}
