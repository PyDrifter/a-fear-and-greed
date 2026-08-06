# -*- coding: utf-8 -*-
"""
MA5 偏离度预警脚本

配置来源 (优先级: 真实环境变量 > 项目根目录 .env > config.ini):
  .env 文件 (本地运行推荐):
    WECHAT_WEBHOOK=企业微信群机器人 Webhook 完整 URL (含 key)
    STOCK_LIST={"159552": 4, "588000": 4.5}
    即 {"标的代码": 预警阈值%}

  config.ini (兜底):
    [WECHAT_WEBHOOK]
    URL = ...Webhook 完整地址...

    [MA_WARN]
    STOCK_LIST = {"159552": 4, "588000": 4.5}

对每个标的:
  1. 获取最新 LOOKBACK_DAYS 天的前复权日 K 线 (腾讯财经直连, 免费无需 token)
  2. 计算 5 日收盘均价线 (MA5)
  3. 计算最新收盘价相对 MA5 的偏离度 (百分比)
  4. 若 |偏离度| >= 配置阈值 → 发送企业微信预警消息

标的名称解析 (参考 web_app.py):
  内置 NAME_MAP → cache/name_{code}.pkl → cache/all_etf_names.pkl /
  cache/all_stock_names.pkl → 兜底直接用代码。
"""
import json
import os
import pickle
import sys
from datetime import datetime, timedelta
from pathlib import Path

# 强制 UTF-8 输出, 避免 Windows GBK 控制台打印中文/符号报错
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import requests

# ─────────────────────────── 配置 ───────────────────────────
# 内置常用代码 → 名称映射 (来源: cal_a_greed.py NAME_MAP)
# 仅在本地无 cache 缓存时兜底使用, 缺失的代码直接用代码本身显示
NAME_MAP: dict[str, str] = {
    "000001": "平安银行",
    "000002": "万科A",
    "000333": "美的集团",
    "000651": "格力电器",
    "000858": "五粮液",
    "002415": "海康威视",
    "002475": "立讯精密",
    "300059": "东方财富",
    "300750": "宁德时代",
    "600036": "招商银行",
    "600276": "恒瑞医药",
    "600519": "贵州茅台",
    "600690": "海尔智家",
    "600887": "伊利股份",
    "600900": "长江电力",
    "601012": "隆基绿能",
    "601166": "兴业银行",
    "601318": "中国平安",
    "601398": "工商银行",
    "601899": "紫金矿业",
    "510050": "上证50ETF",
    "510300": "沪深300ETF",
    "510500": "中证500ETF",
    "510880": "红利ETF",
    "512100": "中证1000ETF",
    "512880": "证券ETF",
    "513050": "中概互联ETF",
    "515790": "光伏ETF",
    "518880": "黄金ETF",
    "159915": "创业板ETF",
}

CONFIG_FILE = Path(__file__).parent / "config.ini"
ENV_FILE = Path(__file__).parent / ".env"
CACHE_DIR = Path(__file__).parent / "cache"
TENCENT_KLINE_ENDPOINT = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
HTTP_TIMEOUT = 8
LOOKBACK_DAYS = 30  # 至少需要 5 个交易日算 MA5, 取近 30 天足够


def _load_dotenv() -> None:
    """加载项目根目录下的 .env 文件到环境变量 (不覆盖已有环境变量)

    仅支持简单的 KEY=VALUE 逐行格式, 忽略空行与 # 注释,
    可选地去除两侧引号。无需额外依赖。
    """
    if not ENV_FILE.exists():
        return
    try:
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key, value = key.strip(), value.strip()
            if not key:
                continue
            if value[:1] == value[-1:] and value[:1] in ("'", '"'):
                value = value[1:-1]
            os.environ.setdefault(key, value)
    except OSError as e:
        print(f"⚠ 读取 .env 失败: {e}", file=sys.stderr)


_load_dotenv()


def _read_config() -> tuple[str, dict[str, float]]:
    """读取环境变量 .env 返回 (webhook_url, {code: threshold_percent})

    优先级: 真实环境变量 > .env 文件 > config.ini
    """
    # ── Webhook URL ──
    url = os.environ.get("WECHAT_WEBHOOK", "").strip()
    # ── Webhook URL ──
    url = os.environ.get("WECHAT_WEBHOOK", "").strip()
    if not url:
        import configparser

        cp = configparser.RawConfigParser()
        if cp.read(CONFIG_FILE, encoding="utf-8"):
            url = cp.get("WECHAT_WEBHOOK", "URL", fallback="").strip()
    if not url:
        sys.exit("❌ 未配置 Webhook: 请设置环境变量 WECHAT_WEBHOOK 或 config.ini [WECHAT_WEBHOOK] URL")

    # ── STOCK_LIST ──
    raw = os.environ.get("STOCK_LIST", "").strip()

    try:
        stock_list = json.loads(raw)
    except json.JSONDecodeError as e:
        sys.exit(f"❌ STOCK_LIST 不是合法 JSON: {e}")

    thresholds: dict[str, float] = {}
    for code, val in stock_list.items():
        try:
            thresholds[str(code).strip()] = float(val)
        except (TypeError, ValueError):
            print(f"⚠ 忽略无效阈值: {code}={val}", file=sys.stderr)
    if not thresholds:
        sys.exit("❌ STOCK_LIST 未配置任何有效标的")

    return url, thresholds


# ─────────────────────────── 名称解析 (缓存优先) ───────────────────────────
# 参考 web_app.py: 内存 → name_{code}.pkl → 全量名称表 (all_etf_names/all_stock_names)
_NAME_CACHE: dict[str, str] = {}
_ALL_NAMES: dict[str, str] = {}


def _load_name_pkl(code: str) -> str | None:
    """读取 cache/name_{code}.pkl 单只名称缓存"""
    path = CACHE_DIR / f"name_{code}.pkl"
    if not path.exists():
        return None
    try:
        with open(path, "rb") as f:
            return pickle.load(f).get("name")
    except Exception:
        return None


def _load_all_names_pkl() -> None:
    """合并加载全量名称表缓存 (all_etf_names.pkl + all_stock_names.pkl)"""
    global _ALL_NAMES
    if _ALL_NAMES:
        return
    merged: dict[str, str] = {}
    for name in ("all_etf_names.pkl", "all_stock_names.pkl"):
        path = CACHE_DIR / name
        if not path.exists():
            continue
        try:
            with open(path, "rb") as f:
                cached = pickle.load(f)
            if isinstance(cached, dict) and isinstance(cached.get("names"), dict):
                merged.update(cached["names"])
        except Exception:
            continue
    _ALL_NAMES = merged


def resolve_name(code: str) -> str:
    """解析标的名称: 内置映射 → 单只缓存 → 全量名称表缓存 → 直接用代码"""
    if code in NAME_MAP:
        return NAME_MAP[code]

    if code in _NAME_CACHE:
        return _NAME_CACHE[code]

    name = _load_name_pkl(code)
    if name:
        _NAME_CACHE[code] = name
        return name

    _load_all_names_pkl()
    name = _ALL_NAMES.get(code)
    if name:
        _NAME_CACHE[code] = name
        return name

    return code


# ─────────────────────────── 数据获取 ───────────────────────────
def _to_tencent_symbol(code: str) -> str:
    """A股/ETF 代码 → 腾讯财经 symbol (sh/sz 前缀)"""
    return f"sh{code}" if code.startswith(("5", "6", "9")) else f"sz{code}"


def _fetch_kline(code: str) -> list[dict]:
    """获取近 LOOKBACK_DAYS 天前复权日K线, 返回按日期升序的记录列表"""
    end = datetime.now()
    start = end - timedelta(days=LOOKBACK_DAYS)
    start_str = start.strftime("%Y-%m-%d")
    end_str = end.strftime("%Y-%m-%d")

    symbol = _to_tencent_symbol(code)
    resp = requests.get(
        TENCENT_KLINE_ENDPOINT,
        params={"param": f"{symbol},day,{start_str},{end_str},{LOOKBACK_DAYS},qfq"},
        headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json,text/plain,*/*"},
        timeout=HTTP_TIMEOUT,
    )
    resp.raise_for_status()
    payload = resp.json()

    data = payload.get("data") if isinstance(payload, dict) else None
    item = data.get(symbol) if isinstance(data, dict) else None
    if not isinstance(item, dict):
        raise ValueError(f"腾讯数据源返回异常 [{code}]")

    rows = item.get("qfqday") or item.get("day") or []
    records = []
    for row in rows:
        if isinstance(row, list) and len(row) >= 6:
            records.append({"date": row[0], "close": float(row[2])})
    if not records:
        raise ValueError(f"腾讯数据源未返回 [{code}] 的K线数据")
    return records


def _compute_deviation(records: list[dict]) -> tuple[float, str, float, float]:
    """计算最新收盘价相对 5 日均线的偏离百分比 (单位: %)"""
    closes = [r["close"] for r in records]
    latest_date = records[-1]["date"]
    latest_close = closes[-1]
    ma5 = sum(closes[-5:]) / 5
    deviation = (latest_close - ma5) / ma5 * 100.0
    return deviation, latest_date, latest_close, ma5


# ─────────────────────────── 微信消息 ───────────────────────────
def send_wechat(url: str, text: str) -> None:
    """通过企业微信群机器人 Webhook 发送文本消息"""
    payload = {"msgtype": "text", "text": {"content": text}}
    resp = requests.post(url, json=payload, timeout=HTTP_TIMEOUT)
    resp.raise_for_status()
    ret = resp.json()
    if ret.get("errcode") != 0:
        raise RuntimeError(f"微信发送失败: {ret}")


def main() -> None:
    url, thresholds = _read_config()
    if not thresholds:
        print("❌ 没有配置任何标的, 请检查 STOCK_LIST 配置。")
        return

    over_threshold = []  # 存放 (code, name, deviation)
    results = []

    for code, threshold in thresholds.items():
        name = resolve_name(code)
        print(f"▶ 检查: {name} ({code}), 阈值 {threshold}% ...")
        try:
            records = _fetch_kline(code)
            deviation, date, close, ma5 = _compute_deviation(records)
            print(
                f"   {date} 收盘 {close:.3f} | MA5 {ma5:.3f} | 偏离度 {deviation:+.2f}% "
                f"(阈值 {threshold:+.2f}%)"
            )
            results.append((code, name, deviation, threshold))
            if abs(deviation) >= threshold:
                side = "高于" if deviation > 0 else "低于"
                over_threshold.append(
                    f"{name}({code}): 最新价 {close:.3f} 偏离5日线 {deviation:.2f}%"
                    f"，已触发 {threshold:.2f}% 预警"
                )
        except Exception as e:
            print(f"✗ 获取失败 [{code}]: {e}", file=sys.stderr)

    if not over_threshold:
        print("\n✅ 所有标的均处于安全区间, 无需预警。")
        return

    alert_date = datetime.now().strftime("%Y-%m-%d")
    lines = [f"⚠️ {alert_date} 5日线偏离预警"]
    lines.extend(f" - {msg}" for msg in over_threshold)
    msg = "\n".join(lines)

    print("\n" + msg)
    print("\n正在发送微信预警 ...")
    try:
        send_wechat(url, msg)
        print("✅ 预警已发送。")
    except Exception as e:
        print(f"❌ 微信发送失败: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
