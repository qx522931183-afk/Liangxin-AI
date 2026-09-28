"""
天气感知模块：通过和风天气 API 查询实时天气和天气预报。
支持城市搜索、实时天气、逐天预报、逐小时预报、出行建议。

v2.1 — 优化：重试机制、缓存 TTL、并行请求、统一错误处理
"""

import json
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Optional

import requests

from config import get_qweather_config

logger = logging.getLogger("Weather")


# ══════════════════════════════════════════════════════════
# 错误类型与城市名归一化
# ══════════════════════════════════════════════════════════

class WeatherApiError(Exception):
    """和风天气返回的业务错误，message 是可以直接给用户看的中文解释。"""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind          # invalid_host / invalid_key / not_found / http_xxx
        self.message = message


# 省级行政区名称，用于把「广东广州」拆成「广州」
_PROVINCE_NAMES = (
    "内蒙古", "黑龙江", "广西", "西藏", "宁夏", "新疆", "香港", "澳门", "台湾",
    "北京", "天津", "河北", "山西", "辽宁", "吉林", "上海", "江苏", "浙江",
    "安徽", "福建", "江西", "山东", "河南", "湖北", "湖南", "广东", "海南",
    "重庆", "四川", "贵州", "云南", "陕西", "甘肃", "青海",
)

# 直辖市：本身就是城市，不需要从候选里剔除
_DIRECT_CITIES = ("北京", "上海", "天津", "重庆")

# 行政区划词，剥离后剩下的是"干净的城市名"。
# 长词必须排在短词前面（"维吾尔自治区" 要在 "区" 之前），否则会被拆坏。
_ADMIN_WORDS = (
    "特别行政区", "维吾尔自治区", "回族自治区", "壮族自治区", "自治区",
    "自治州", "自治县", "新区", "城区", "矿区", "地区", "城市",
    "盟", "省", "市", "县", "区",
)


def _strip_admin_affix(name: str) -> str:
    """反复剥离首尾的行政区划词，得到主干名。

    「广东省广州市」→「广州」；「新疆维吾尔自治区乌鲁木齐市」→「乌鲁木齐」。

    注意要**反复**剥：去掉结尾的「市」之后可能露出「省」，只剥一轮会得到
    「省广州」这种没意义的中间结果。
    """
    out = str(name or "").strip()
    for _ in range(8):                       # 迭代上限，防御性写法
        before = out
        for word in _ADMIN_WORDS:
            if len(out) > len(word) and out.endswith(word):
                out = out[: -len(word)].strip()
                break
        for word in _ADMIN_WORDS:
            if len(out) > len(word) and out.startswith(word):
                out = out[len(word):].strip()
                break
        if out == before:
            break
    return out


def _candidate_city_names(city_name: str) -> list[str]:
    """把用户/模型给出的城市写法拆成多个候选查询词，按命中率从高到低返回。

    为什么需要：
      和风天气的城市搜索只认**单一城市名**。「广东广州」「广东省广州市」
      这类把省和市连着写的说法查不到结果，接口直接返回空列表。
      把它拆成「广州」再查，能显著减少"未找到城市"的误报。

    例：
      「广东广州」        → ['广州', '广东广州']
      「广东省广州市」     → ['广州', '广东省广州', '广东省广州市']
      「广州市」          → ['广州', '广州市']
      「新疆维吾尔自治区乌鲁木齐市」→ ['乌鲁木齐', '新疆维吾尔自治区乌鲁木齐', ...]
    """
    raw = str(city_name or "").strip()
    if not raw:
        return []

    result: list[str] = []

    def _add(value: str):
        value = (value or "").strip()
        if value and value not in result:
            result.append(value)

    # ① 先剥省级前缀、再剥行政区后缀：「广东省广州市」→「广州」（命中率最高）
    core = raw
    province_hit = None
    for province in sorted(_PROVINCE_NAMES, key=len, reverse=True):
        if core.startswith(province) and len(core) > len(province):
            province_hit = province
            core = core[len(province):]
            break
    core = _strip_admin_affix(core)
    # 「浙江省杭州市余杭区」剥掉省名后还剩「杭州市余杭」，
    # 取第一个「市」之前的部分才是可查的城市名（→「杭州」）
    if "市" in core:
        head = core.split("市", 1)[0].strip()
        if head:
            core = head
    _add(core)

    # 直辖市本身就是一个城市，「北京市朝阳区」时「北京」也是有效候选
    if province_hit in _DIRECT_CITIES:
        _add(province_hit)

    # ② 只剥后缀：「广州市」→「广州」
    _add(_strip_admin_affix(raw))

    # ③ 原名兜底
    _add(raw)
    return result


def _explain_http_error(status_code: int, payload: dict) -> str:
    """把和风天气的 HTTP 错误翻译成"照着做就能修好"的中文提示。"""
    error = (payload or {}).get("error") or {}
    title = str(error.get("title") or "").strip()
    detail = str(error.get("detail") or "").strip()

    if status_code == 403 and "host" in title.lower():
        return (
            "和风天气 API Host 无效或未授权（403 Invalid Host）。"
            "这不是城市名的问题：请到和风天气控制台 → 设置 → API Host，"
            "复制你账号专属的完整域名（形如 xxxxxx.qweatherapi.com），"
            "填进「API 配置 → 和风天气 → API Host」，注意不要带 https:// 前缀，"
            "也不要继续用 geoapi.qweather.com 这类公共域名（2026 年起陆续停服）。"
        )
    if status_code == 401:
        return "和风天气 API Key 无效或未启用（401）。请检查 Key 是否复制完整、是否已在控制台启用。"
    if status_code == 404:
        return "和风天气接口返回 404，通常是 API Host 或接口路径版本不对（GeoAPI 路径应为 /geo/v2/...）。"
    if status_code == 429:
        return "和风天气接口触发限流（429），请稍后再试或降低查询频率。"
    tail = "：{}{}".format(title, (" " + detail) if detail else "") if (title or detail) else ""
    return "和风天气接口返回 HTTP {}{}".format(status_code, tail)

# ── 和风天气 API 端点 ─────────────────────────────────────
def _api_url(path: str) -> str:
    """从配置读取 API Host，拼接完整的 API URL。"""
    cfg = get_qweather_config()
    host = (cfg.get("api_host") or "").strip()
    if not host:
        host = "devapi.qweather.com"
    return f"https://{host}{path}"


# ── API 请求（带重试） ────────────────────────────────────
def _api_request(url: str, params: dict, max_retries: int = 2) -> dict:
    """带指数退避重试的 API 请求。

    与旧版的区别：**HTTP 4xx 不再重试**。
    403（Host 无效）/ 401（Key 无效）/ 404（路径错）这类是配置问题，
    重试一百次结果都一样，白等 1.5 秒还刷屏。
    """
    last_error = None
    for attempt in range(max_retries + 1):
        try:
            resp = requests.get(url, params=params, timeout=10)
            try:
                payload = resp.json()
            except Exception:
                payload = {}
            if resp.status_code >= 400:
                # 配置类错误直接抛，交给上层翻译成人话
                raise WeatherApiError(
                    "http_{}".format(resp.status_code),
                    _explain_http_error(resp.status_code, payload),
                )
            return payload
        except WeatherApiError:
            raise
        except Exception as e:
            last_error = e
            if attempt < max_retries:
                delay = 0.5 * (2 ** attempt)
                logger.warning("API 请求失败，%.1fs 后重试 (%d/%d): %s",
                               delay, attempt + 1, max_retries, e)
                time.sleep(delay)
    raise last_error


# ── LocationID 缓存（带 TTL） ─────────────────────────────
_location_cache: dict[str, tuple[str, float]] = {}
_CACHE_TTL = 86400  # 24 小时


# ── 天气现象与图标映射 ──────────────────────────────────────
_WEATHER_ICONS = {
    "晴": "☀️", "多云": "⛅", "阴": "☁️",
    "小雨": "🌦", "中雨": "🌧", "大雨": "🌧", "暴雨": "🌊",
    "雷阵雨": "⛈", "雨夹雪": "🌨",
    "小雪": "🌨", "中雪": "❄️", "大雪": "❄️", "暴雪": "❄️",
    "雾": "🌫", "霾": "🌫",
    "浮尘": "💨", "扬沙": "💨", "沙尘暴": "💨",
    "大风": "💨", "强风": "💨",
}


# ══════════════════════════════════════════════════════════
# 公开接口
# ══════════════════════════════════════════════════════════

def get_location_id(city_name: str, api_key: str = None) -> Optional[str]:
    """通过城市名称查询和风天气 LocationID。结果带 TTL 缓存。

    会按候选写法依次尝试：「广东广州」查不到时会自动改用「广州」重查，
    避免把"写法不规范"误报成"城市不存在"。

    配置类错误（Host/Key 无效）会抛 WeatherApiError，而不是静默返回 None ——
    否则上层只能报"未找到城市"，把用户引到错误的方向去排查。
    """
    if not api_key:
        api_key = _get_api_key()
        if not api_key:
            raise WeatherApiError(
                "no_key",
                "还没有配置和风天气 API Key，请在「API 配置 → 和风天气」里填写。",
            )

    raw_key = str(city_name or "").strip()
    if not raw_key:
        return None

    # 缓存命中（按原始写法缓存，避免重复拆解）
    cached = _location_cache.get(raw_key)
    if cached:
        loc_id, ts = cached
        if time.time() - ts < _CACHE_TTL:
            return loc_id
        del _location_cache[raw_key]

    last_payload = None
    for candidate in _candidate_city_names(raw_key):
        try:
            data = _api_request(
                _api_url("/geo/v2/city/lookup"),
                {"location": candidate, "key": api_key},
            )
        except WeatherApiError:
            raise                      # Host / Key 类错误，换名字重试也没用
        except Exception as e:
            logger.error("城市查询异常: %s", e)
            return None

        last_payload = data
        locations = data.get("location") or []
        if data.get("code") == "200" and locations:
            loc = locations[0]
            loc_id = loc["id"]
            _location_cache[raw_key] = (loc_id, time.time())
            if candidate != raw_key:
                logger.info("城市「%s」按「%s」命中：%s / %s",
                            raw_key, candidate, loc.get("name"), loc.get("adm1"))
            return loc_id
        logger.warning("城市查询无结果: %s → code=%s", candidate, data.get("code"))

    logger.warning("城市「%s」的候选写法均未命中，最后返回码 %s",
                   raw_key, (last_payload or {}).get("code"))
    return None


def get_current_weather(location_id: str, api_key: str = None) -> Optional[dict]:
    """获取指定位置的实时天气数据（带重试）。"""
    if not api_key:
        api_key = _get_api_key()
        if not api_key:
            return None

    try:
        data = _api_request(
            _api_url("/v7/weather/now"),
            {"location": location_id, "key": api_key},
        )
        if data.get("code") != "200":
            logger.warning("实时天气查询失败: %s", data.get("code"))
            return None
        return data.get("now")
    except Exception as e:
        logger.error("实时天气查询异常: %s", e)
        return None


def get_forecast_3d(location_id: str, api_key: str = None) -> Optional[list]:
    """获取未来 3 天的逐天预报（带重试）。返回 daily 列表。"""
    if not api_key:
        api_key = _get_api_key()
        if not api_key:
            return None

    try:
        data = _api_request(
            _api_url("/v7/weather/3d"),
            {"location": location_id, "key": api_key},
        )
        if data.get("code") != "200":
            logger.warning("3天预报查询失败: %s", data.get("code"))
            return None
        return data.get("daily", [])
    except Exception as e:
        logger.error("3天预报查询异常: %s", e)
        return None


def get_hourly_24h(location_id: str, api_key: str = None) -> Optional[list]:
    """获取未来 24 小时的逐小时预报（带重试）。返回 hourly 列表。"""
    if not api_key:
        api_key = _get_api_key()
        if not api_key:
            return None

    try:
        data = _api_request(
            _api_url("/v7/weather/24h"),
            {"location": location_id, "key": api_key},
        )
        if data.get("code") != "200":
            logger.warning("24小时预报查询失败: %s", data.get("code"))
            return None
        return data.get("hourly", [])
    except Exception as e:
        logger.error("24小时预报查询异常: %s", e)
        return None


def get_full_weather(city_name: str, api_key: str = None) -> str:
    """一站式获取城市完整天气信息（实时 + 3天预报并行请求），返回格式化文本。"""
    if not api_key:
        api_key = _get_api_key()
        if not api_key:
            return "错误：未配置和风天气 API Key，请在 API 设置中填写。"

    loc_id = get_location_id(city_name, api_key)
    if not loc_id:
        return f"错误：未找到城市「{city_name}」，请检查城市名称是否正确。"

    # 并行获取实时天气 + 3天预报
    now_data = None
    daily_data = None
    with ThreadPoolExecutor(max_workers=2) as executor:
        future_now = executor.submit(get_current_weather, loc_id, api_key)
        future_daily = executor.submit(get_forecast_3d, loc_id, api_key)
        for future in as_completed([future_now, future_daily]):
            try:
                result = future.result()
                if future == future_now:
                    now_data = result
                else:
                    daily_data = result
            except Exception as e:
                logger.error("并行天气查询异常: %s", e)

    return _format_full_weather(city_name, now_data, daily_data)


def get_hourly_weather_text(city_name: str, hours: int = 12, api_key: str = None) -> str:
    """获取逐小时预报文本。"""
    if not api_key:
        api_key = _get_api_key()
        if not api_key:
            return "错误：未配置和风天气 API Key。"

    loc_id = get_location_id(city_name, api_key)
    if not loc_id:
        return f"错误：未找到城市「{city_name}」。"

    hourly_data = get_hourly_24h(loc_id, api_key)
    if not hourly_data:
        return f"无法获取「{city_name}」的逐小时预报。"

    return _format_hourly_weather(city_name, hourly_data[:hours])


def get_weather_advice(now_data: dict = None, daily_data: list = None) -> str:
    """根据天气数据生成出行建议。"""
    tips = []

    if daily_data:
        today = daily_data[0] if len(daily_data) > 0 else None
        tomorrow = daily_data[1] if len(daily_data) > 1 else None

        # 今天建议
        if today:
            text_day = today.get("textDay", "")
            text_night = today.get("textNight", "")
            temp_max = int(today.get("tempMax", 25))
            temp_min = int(today.get("tempMin", 15))
            precip = float(today.get("precip", 0))

            if "雨" in text_day or "雨" in text_night:
                tips.append("🌂 今天有雨，出门记得带伞~")
            if precip > 5:
                tips.append("☔ 今天降雨量较大，注意避开积水路段。")
            if temp_max >= 35:
                tips.append("🥵 今天高温，注意防暑，避免长时间户外活动。")
            elif temp_max >= 30:
                tips.append("🧴 今天较热，出门记得防晒~")
            if temp_min <= 5:
                tips.append("🧣 今天低温，注意保暖，多穿点哦~")
            if temp_max - temp_min >= 12:
                tips.append("🌡 今天昼夜温差大，建议随身带件外套。")

        # 明天建议
        if tomorrow:
            text_day = tomorrow.get("textDay", "")
            if "雨" in text_day:
                tips.append("📅 明天预报有雨，可以提前准备好伞~")
            temp_max = int(tomorrow.get("tempMax", 25))
            if temp_max >= 35:
                tips.append("📅 明天持续高温，注意防暑~")

    # 实时数据 — 体感温差
    if now_data:
        temp = int(now_data.get("temp", 20))
        feels_like = int(now_data.get("feelsLike", 20))
        wind_scale = int(now_data.get("windScale", 0))
        visibility = now_data.get("visibility", "")

        if abs(temp - feels_like) >= 4:
            if feels_like < temp:
                tips.append(f"💨 当前体感比实际温度低{temp - feels_like}°C，风力较大，注意防风~")
            else:
                tips.append(f"🔥 当前体感比实际温度高{feels_like - temp}°C，注意补水~")
        if wind_scale >= 5:
            tips.append("💨 风力较大，注意窗外物品，出行注意安全。")
        if visibility and visibility.isdigit() and int(visibility) < 5:
            tips.append("🌫 当前能见度较低，开车出行请减速慢行。")

    return "\n".join(tips) if tips else ""


def get_user_city_from_memory() -> Optional[str]:
    """从长期记忆中读取用户所在城市。"""
    try:
        from brain.graph_memory import search_facts
        results = search_facts("所在城市是", category="profile")
        if not results:
            results = search_facts("城市是", category="profile")
        if results:
            content = results[0].get("content", "")
            for prefix in ["用户所在城市是", "所在城市是", "城市是"]:
                if prefix in content:
                    city = content.split(prefix, 1)[-1].strip()
                    return city
            return content
    except Exception as e:
        logger.warning("从记忆读取城市失败: %s", e)
    return None


def save_user_city_to_memory(city: str):
    """将用户所在城市保存到长期记忆。"""
    try:
        from brain.graph_memory import add_fact
        add_fact(f"用户所在城市是{city}", category="profile")
    except Exception as e:
        logger.warning("保存城市到记忆失败: %s", e)


# ══════════════════════════════════════════════════════════
# 内部方法
# ══════════════════════════════════════════════════════════

def _get_api_key() -> Optional[str]:
    """从配置读取和风天气 API Key。"""
    cfg = get_qweather_config()
    key = cfg.get("api_key", "").strip()
    return key if key else None


def _get_weather_icon(text: str) -> str:
    """获取天气现象的图标。"""
    for keyword, icon in _WEATHER_ICONS.items():
        if keyword in text:
            return icon
    return "🌤"


def _format_full_weather(city: str, now_data: dict, daily_data: list) -> str:
    """格式化完整天气信息。"""
    lines = [f"📍 {city} · 实时天气"]

    # 实时天气
    if now_data:
        temp = now_data.get("temp", "?")
        text = now_data.get("text", "")
        icon = _get_weather_icon(text)
        feels_like = now_data.get("feelsLike", "?")
        wind_dir = now_data.get("windDir", "")
        wind_scale = now_data.get("windScale", "")
        humidity = now_data.get("humidity", "?")
        lines.append(f"{icon} {text}    {temp}°C（体感 {feels_like}°C）")
        lines.append(f"💨 {wind_dir} {wind_scale}级    💧 湿度 {humidity}%")
    else:
        lines.append("（暂无实时数据）")

    # 3 天预报
    if daily_data:
        lines.append("")
        lines.append("📅 未来 3 天预报")
        for day in daily_data:
            date_str = day.get("fxDate", "")
            try:
                dt = datetime.strptime(date_str, "%Y-%m-%d")
                weekday = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][dt.weekday()]
                label = f"{dt.month}/{dt.day} {weekday}"
            except Exception:
                label = date_str
            temp_max = day.get("tempMax", "?")
            temp_min = day.get("tempMin", "?")
            text_day = day.get("textDay", "")
            text_night = day.get("textNight", "")
            icon_day = _get_weather_icon(text_day)
            icon_night = _get_weather_icon(text_night)
            wind_dir = day.get("windDirDay", "")
            wind_scale = day.get("windScaleDay", "")
            precip = day.get("precip", "0")
            lines.append(
                f"  {label}  {icon_day}{text_day} → {icon_night}{text_night}  "
                f"{temp_min}~{temp_max}°C  {wind_dir}{wind_scale}级  💧{precip}mm"
            )
    else:
        lines.append("（暂无预报数据）")

    # 出行建议
    advice = get_weather_advice(now_data, daily_data)
    if advice:
        lines.append("")
        lines.append("💡 出行建议")
        for tip in advice.split("\n"):
            lines.append(f"  {tip}")

    return "\n".join(lines)


def _format_hourly_weather(city: str, hourly_data: list) -> str:
    """格式化逐小时预报。"""
    lines = [f"📍 {city} · 逐小时预报"]
    for h in hourly_data:
        time_str = h.get("fxTime", "")
        try:
            dt = datetime.fromisoformat(time_str)
            label = f"{dt.hour:02d}:00"
        except Exception:
            label = time_str
        temp = h.get("temp", "?")
        text = h.get("text", "")
        icon = _get_weather_icon(text)
        wind_dir = h.get("windDir", "")
        wind_scale = h.get("windScale", "")
        precip = h.get("precip", "0")
        pop = h.get("pop", "")
        pop_str = f"  🌧 {pop}%" if pop else ""
        lines.append(f"  {label}  {icon}{text}  {temp}°C  {wind_dir}{wind_scale}级{pop_str}")

    return "\n".join(lines)
