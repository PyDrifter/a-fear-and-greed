# -*- coding: utf-8 -*-
"""
A股贪恐指数 Web 服务
"""
import pickle
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import akshare as ak
import pandas as pd
from flask import Flask, jsonify, render_template, request

# 从 calc 模块导入核心计算类
from cal_a_greed import A_FearGreed, NAME_MAP, STOCK_CODES, ETF_CODES, CSI300_CODE

app = Flask(__name__)

# ── 缓存配置 ──
CACHE_DIR = Path(__file__).parent / "cache"
CACHE_DIR.mkdir(exist_ok=True)
CACHE_TTL_HOURS = 2  # 缓存有效期 2 小时

# A股收盘时间: 北京时间 15:00
_MARKET_CLOSE_HOUR = 15
_BEIJING_TZ = timezone(timedelta(hours=8))


def _market_cache_date() -> str:
    """按 A 股收盘时间决定缓存日期 (北京时间)

    - 交易日收盘前 (< 15:00): 用昨天
    - 交易日收盘后 (>= 15:00): 用今天
    - 周末: 用上一个工作日 (周五)
    """
    now = datetime.now(_BEIJING_TZ)
    weekday = now.weekday()
    if weekday >= 5:
        return (now - timedelta(days=weekday - 4)).strftime("%Y%m%d")
    if now.hour >= _MARKET_CLOSE_HOUR:
        return now.strftime("%Y%m%d")
    return (now - timedelta(days=1)).strftime("%Y%m%d")


def _cache_path(code: str) -> Path:
    return CACHE_DIR / f"{code}.pkl"


def _cache_key(code: str) -> str:
    """按市场收盘感知日期 + code 生成缓存校验 key"""
    return f"{code}_{_market_cache_date()}"


def _load_cache(code: str) -> pd.DataFrame | None:
    path = _cache_path(code)
    if not path.exists():
        return None
    try:
        with open(path, "rb") as f:
            cached = pickle.load(f)
        # 检查是否当天有效
        if cached.get("key") == _cache_key(code):
            return cached["data"]
    except Exception:
        pass
    return None


def _save_cache(code: str, df: pd.DataFrame) -> None:
    path = _cache_path(code)
    try:
        with open(path, "wb") as f:
            pickle.dump({"key": _cache_key(code), "data": df}, f)
    except Exception as e:
        print(f"缓存写入失败 [{code}]: {e}", file=sys.stderr)


# ── 预配置代码列表 (用于前端快速选择) ──
STOCK_LIST = [{"code": c, "name": NAME_MAP.get(c, c)} for c in STOCK_CODES]
ETF_LIST = [{"code": c, "name": NAME_MAP.get(c, c)} for c in ETF_CODES]


# ── API: 计算贪恐指数 ──


@app.route("/api/fear_greed")
def api_fear_greed():
    code = request.args.get("code", "").strip()
    if not code:
        return jsonify({"error": "请输入股票/ETF 代码"}), 400
    if not code.isdigit():
        return jsonify({"error": "代码格式错误，请输入纯数字代码"}), 400

    # 尝试读缓存
    df = _load_cache(code)
    if df is not None:
        return _build_response(code, df)

    # 实时计算
    try:
        name = NAME_MAP.get(code, code)
        fg = A_FearGreed(code, name=name)
        df = fg.compute()
        if df is None or df.empty:
            return jsonify({"error": f"未获取到 [{code}] 的数据，请检查代码是否正确"}), 404
        _save_cache(code, df)
        return _build_response(code, df)
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": f"计算失败: {str(e)}"}), 500


def _build_response(code: str, df: pd.DataFrame):
    """将 DataFrame 转为前端所需的 JSON"""
    # 只取近 1 年数据
    cutoff = df["date"].max() - pd.DateOffset(years=1)
    chart_df = df[df["date"] >= cutoff].copy()

    # 转为毫秒时间戳 (ECharts 需要)
    dates = [int(d.timestamp() * 1000) for d in chart_df["date"]]
    closes = [round(float(v), 2) for v in chart_df["close"]]
    fear_greed = [
        round(float(v), 1) if pd.notna(v) else None for v in chart_df["fear_greed"]
    ]

    # 最新值
    latest = df.iloc[-1]
    today_str = str(latest["date"].date())

    # 子指标
    indicators = {}
    for col, label in [
        ("norm_price_momentum", "价格动量"),
        ("norm_rsi", "RSI(14)"),
        ("norm_volume_change", "成交量变化"),
        ("norm_volatility", "历史波动率"),
        ("norm_relative_strength", "相对强弱"),
        ("norm_discussion_heat", "讨论热度"),
    ]:
        val = latest.get(col)
        indicators[label] = round(float(val), 1) if pd.notna(val) else None

    return jsonify(
        {
            "code": code,
            "name": NAME_MAP.get(code, code),
            "date": today_str,
            "latest_fear_greed": round(float(latest["fear_greed"]), 1)
            if pd.notna(latest["fear_greed"])
            else None,
            "latest_close": round(float(latest["close"]), 2),
            "indicators": indicators,
            "series": {
                "date": dates,
                "close": closes,
                "fear_greed": fear_greed,
            },
        }
    )


# ── 首页 ──


@app.route("/")
def index():
    return render_template("index.html", stocks=STOCK_LIST, etfs=ETF_LIST)


# ── API: 真实股票/ETF 名称查询 ──


# 内存中的 A 股个股名称字典 (进程生命周期内保持, 通过 daily pickle 持久化)
_stock_name_cache: dict[str, str] = {}
_stock_name_cache_date: str = ""

# 内存中的全量 ETF 名称字典 (fund_etf_spot_em 返回, daily pickle 持久化)
_etf_name_cache: dict[str, str] = {}
_etf_name_cache_date: str = ""


def _name_cache_path(code: str) -> Path:
    return CACHE_DIR / f"name_{code}.pkl"


def _name_cache_key(code: str) -> str:
    return f"{code}_{_market_cache_date()}"


def _all_stock_names_path() -> Path:
    """stock_info_a_code_name() 的全量缓存文件 (进程重启后可复用)"""
    return CACHE_DIR / "all_stock_names.pkl"


def _all_etf_names_path() -> Path:
    """fund_etf_spot_em() 的全量 ETF 缓存文件 (进程重启后可复用)"""
    return CACHE_DIR / "all_etf_names.pkl"


def _load_all_stock_names_cache() -> dict[str, str] | None:
    """读取当日有效的全量个股名称缓存"""
    path = _all_stock_names_path()
    if not path.exists():
        return None
    try:
        with open(path, "rb") as f:
            cached = pickle.load(f)
        if cached.get("key") == f"names_{_market_cache_date()}":
            return cached["names"]
    except Exception:
        pass
    return None


def _save_all_stock_names_cache(names: dict[str, str]) -> None:
    try:
        with open(_all_stock_names_path(), "wb") as f:
            pickle.dump({"key": f"names_{_market_cache_date()}", "names": names}, f)
    except Exception:
        pass


def _load_all_etf_names_cache() -> dict[str, str] | None:
    """读取当日有效的全量 ETF 名称缓存"""
    path = _all_etf_names_path()
    if not path.exists():
        return None
    try:
        with open(path, "rb") as f:
            cached = pickle.load(f)
        if cached.get("key") == f"etf_names_{_market_cache_date()}":
            return cached["names"]
    except Exception:
        pass
    return None


def _save_all_etf_names_cache(names: dict[str, str]) -> None:
    try:
        with open(_all_etf_names_path(), "wb") as f:
            pickle.dump({"key": f"etf_names_{_market_cache_date()}", "names": names}, f)
    except Exception:
        pass


def _ensure_etf_names_loaded() -> dict[str, str]:
    """加载 ETF 名称表: 内存 → daily 文件 → akshare (fund_etf_spot_em)"""
    global _etf_name_cache, _etf_name_cache_date
    today = _market_cache_date()
    if _etf_name_cache and _etf_name_cache_date == today:
        return _etf_name_cache

    cached = _load_all_etf_names_cache()
    if cached is not None:
        _etf_name_cache = cached
        _etf_name_cache_date = today
        return _etf_name_cache

    try:
        df = ak.fund_etf_spot_em()
        code_col = "代码" if "代码" in df.columns else df.columns[0]
        name_col = "名称" if "名称" in df.columns else None
        if name_col is None:
            return {}
        names = {
            str(r[code_col]).zfill(6): str(r[name_col])
            for _, r in df.iterrows()
            if pd.notna(r[name_col])
        }
        _save_all_etf_names_cache(names)
        _etf_name_cache = names
        _etf_name_cache_date = today
        print(f"加载 ETF 名称表: {len(names)} 条")
        return _etf_name_cache
    except Exception as e:
        print(f"加载 fund_etf_spot_em 失败: {e}", file=sys.stderr)
        return {}


def _ensure_stock_names_loaded() -> dict[str, str]:
    """加载 A 股个股名称表: 内存 → daily 文件 → akshare (stock_info_a_code_name)"""
    global _stock_name_cache, _stock_name_cache_date
    today = _market_cache_date()
    if _stock_name_cache and _stock_name_cache_date == today:
        return _stock_name_cache

    # 先尝试 daily 文件缓存 (进程重启后复用, 不重复请求 akshare)
    cached = _load_all_stock_names_cache()
    if cached is not None:
        _stock_name_cache = cached
        _stock_name_cache_date = today
        return _stock_name_cache

    # 实时拉取 akshare 全量名称表
    try:
        df = ak.stock_info_a_code_name()
        code_col = next((c for c in df.columns if c.lower() in ("code", "代码", "股票代码")), df.columns[0])
        name_col = next((c for c in df.columns if c.lower() in ("name", "名称", "股票简称", "股票名称")), df.columns[1])
        names = {str(r[code_col]).zfill(6): str(r[name_col]) for _, r in df.iterrows()}
        _save_all_stock_names_cache(names)
        _stock_name_cache = names
        _stock_name_cache_date = today
        print(f"加载 A 股名称表: {len(names)} 条")
        return _stock_name_cache
    except Exception as e:
        print(f"加载 stock_info_a_code_name 失败: {e}", file=sys.stderr)
        return {}


def _load_name_cache(code: str) -> str | None:
    path = _name_cache_path(code)
    if not path.exists():
        return None
    try:
        with open(path, "rb") as f:
            cached = pickle.load(f)
        if cached.get("key") == _name_cache_key(code):
            return cached["name"]
    except Exception:
        pass
    return None


def _save_name_cache(code: str, name: str) -> None:
    try:
        with open(_name_cache_path(code), "wb") as f:
            pickle.dump({"key": _name_cache_key(code), "name": name}, f)
    except Exception:
        pass


def _resolve_etf_name(code: str) -> str | None:
    """ETF 名称: fund_etf_spot_em 全量实时列表 (带内存 + daily 文件缓存)"""
    names = _ensure_etf_names_loaded()
    return names.get(code)


def _resolve_real_name(code: str) -> str | None:
    """从 akshare 查询代码的真实名称, 查不到返回 None

    查询优先级:
      1. stock_info_a_code_name() 全量 A 股名称表 (带文件缓存, 仅首次拉取)
      2. 若未命中, 且是 ETF 前缀 → fund_etf_spot_em
      3. 最后 fallback: stock_individual_info_em (东财单只查询, 网络不稳定)
    """
    if not code or code == CSI300_CODE:
        return None

    # ── 优先: 全量 A 股名称表 (内存 + daily 文件缓存) ──
    names = _ensure_stock_names_loaded()
    name = names.get(code)
    if name:
        return name

    # ── ETF 前缀 → ETF 实时列表 ──
    if code.startswith(("15", "16", "51", "56", "58")):
        name = _resolve_etf_name(code)
        if name:
            return name

    # ── 最后 fallback: 单只个股接口 (网络不稳, 偶尔会失败) ──
    try:
        df = ak.stock_individual_info_em(symbol=code)
        if df is None or df.empty or df.shape[1] < 2:
            return None
        kv = dict(zip(df.iloc[:, 0], df.iloc[:, 1]))
        name = kv.get("股票简称") or kv.get("股票名称")
        return str(name) if name else None
    except Exception as e:
        print(f"查询名称失败 [{code}]: {e}", file=sys.stderr)
        return None


@app.route("/api/stock_name")
def api_stock_name():
    code = request.args.get("code", "").strip()
    if not code or not code.isdigit() or len(code) != 6:
        return jsonify({"name": None})

    # 预置列表优先
    if code in NAME_MAP:
        return jsonify({"name": NAME_MAP[code]})

    # 文件缓存命中
    cached = _load_name_cache(code)
    if cached:
        return jsonify({"name": cached})

    # 实时查询
    name = _resolve_real_name(code)
    if name:
        _save_name_cache(code, name)
    return jsonify({"name": name})


# ── 清缓存 ──


@app.route("/api/clear_cache")
def api_clear_cache():
    code = request.args.get("code", "")
    if code:
        path = _cache_path(code)
        if path.exists():
            path.unlink()
        return jsonify({"ok": True, "msg": f"已清除 {code} 的缓存"})
    # 清全部
    for p in CACHE_DIR.glob("*.pkl"):
        p.unlink()
    return jsonify({"ok": True, "msg": "已清除所有缓存"})


if __name__ == "__main__":
    print("=" * 50)
    print("  A 股贪恐指数 Web 服务")
    print(f"  访问地址: http://127.0.0.1:5000")
    print("=" * 50)
    app.run(host="127.0.0.1", port=5000, debug=True)
