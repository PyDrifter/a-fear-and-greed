# -*- coding: utf-8 -*-
"""
A股个股/ETF 贪恐指数计算工具

6项指标 (等权合成):
  1. 价格动量 - 股价 vs 125日/50日均线偏离度
  2. RSI(14) - 14日相对强弱指标
  3. 成交量变化 - 20日均量 vs 120日均量比值
  4. 历史波动率 - 30日收益率标准差
  5. 相对强弱 vs 基准 - 个股/ETF vs 沪深300 滚动20日收益差
  6. 讨论热度 - 换手率短期/长期比值 (作为新闻热度的代理指标)

数据源: akshare 为主，失败时自动回退到腾讯财经 (免费, 无需 token)
"""
import pickle
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import akshare as ak
import matplotlib
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import requests

matplotlib.use("TkAgg")

# 设置中文字体 (防止图表中文乱码)
plt.rcParams["font.sans-serif"] = ["SimHei", "Microsoft YaHei", "WenQuanYi Micro Hei", "Noto Sans CJK SC", "Arial Unicode MS"]
plt.rcParams["axes.unicode_minus"] = False

# ─────────────────────────── 预配置代码列表 ───────────────────────────
# 常用 A 股个股
STOCK_CODES = [
    "000001",  # 平安银行
    "000002",  # 万科A
    "000333",  # 美的集团
    "000651",  # 格力电器
    "000858",  # 五粮液
    "002415",  # 海康威视
    "002475",  # 立讯精密
    "300059",  # 东方财富
    "300750",  # 宁德时代
    "600036",  # 招商银行
    "600276",  # 恒瑞医药
    "600519",  # 贵州茅台
    "600690",  # 海尔智家
    "600887",  # 伊利股份
    "600900",  # 长江电力
    "601012",  # 隆基绿能
    "601166",  # 兴业银行
    "601318",  # 中国平安
    "601398",  # 工商银行
    "601899",  # 紫金矿业
]

# 常用 ETF
ETF_CODES = [
    "510050",  # 上证50ETF
    "510300",  # 沪深300ETF
    "510500",  # 中证500ETF
    "510880",  # 红利ETF
    "512100",  # 中证1000ETF
    "512880",  # 证券ETF
    "513050",  # 中概互联ETF
    "515790",  # 光伏ETF
    "518880",  # 黄金ETF
    "159915",  # 创业板ETF
]

CSI300_CODE = "000300"  # 沪深300

# ─────────────────────────── 指标权重 (等权) ───────────────────────────
WEIGHTS = {
    "price_momentum": 1 / 6,
    "rsi": 1 / 6,
    "volume_change": 1 / 6,
    "volatility": 1 / 6,
    "relative_strength": 1 / 6,
    "discussion_heat": 1 / 6,
}

# ─────────────────────────── 归一化参数 ───────────────────────────
# 滚动百分位窗口 (交易日)
NORM_WINDOW = 252

# ─────────────────────────── 原始数据本地缓存 ───────────────────────────
RAW_CACHE_DIR = Path(__file__).parent / "cache"
RAW_CACHE_DIR.mkdir(exist_ok=True)

# A股收盘时间: 北京时间 09:30
_MARKET_OPEN_HOUR = 9
_MARKET_OPEN_MINUTE = 30
_BEIJING_TZ = timezone(timedelta(hours=8))


def _market_cache_date() -> str:
    """按 A 股收盘时间决定缓存日期 (北京时间)

    - 交易日开盘前 (< 09:30): 用昨天
    - 交易日开盘后 (>= 09:30): 用今天
    - 周末: 用上一个工作日 (周五)
    """
    now = datetime.now(_BEIJING_TZ)
    weekday = now.weekday()  # 0=Mon ... 6=Sun
    if weekday >= 5:
        # 周末 → 回退到上一个工作日 (周五)
        return (now - timedelta(days=weekday - 4)).strftime("%Y%m%d")
    if now.hour >= _MARKET_OPEN_HOUR and now.minute >= _MARKET_OPEN_MINUTE:
        return now.strftime("%Y%m%d")
    return (now - timedelta(days=1)).strftime("%Y%m%d")


def _raw_cache_path(code: str) -> Path:
    return RAW_CACHE_DIR / f"raw_{code}.pkl"


def _raw_cache_key(code: str) -> str:
    """按市场收盘感知日期 + code 生成缓存校验 key"""
    return f"{code}_{_market_cache_date()}"


def _load_raw_cache(code: str) -> tuple[pd.DataFrame, pd.DataFrame] | None:
    """读取当天有效的原始日线缓存, 无效返回 None"""
    path = _raw_cache_path(code)
    if not path.exists():
        return None
    try:
        with open(path, "rb") as f:
            cached = pickle.load(f)
        if cached.get("key") == _raw_cache_key(code):
            return cached["data"], cached["benchmark"]
    except Exception:
        pass
    return None


def _save_raw_cache(code: str, df: pd.DataFrame, bm: pd.DataFrame) -> None:
    """写入原始日线缓存"""
    try:
        with open(_raw_cache_path(code), "wb") as f:
            pickle.dump({"key": _raw_cache_key(code), "data": df, "benchmark": bm}, f)
    except Exception as e:
        print(f"原始数据缓存写入失败 [{code}]: {e}", file=sys.stderr)

# ─────────────────────────── 腾讯直连备用数据源 ───────────────────────────
TX_KLINE_ENDPOINT = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
TX_HTTP_TIMEOUT = 8          # 秒
TX_MAX_KLINE_BARS = 800      # 腾讯接口单次最多返回的K线数


def _tx_fetch_kline(symbol: str, start_str: str, end_str: str) -> pd.DataFrame:
    """从腾讯财经直连接口获取前复权日K线

    :param symbol: 带交易所前缀的代码, 如 sh600519 / sz000001 / sh000300
    :param start_str: 开始日期 YYYYMMDD
    :param end_str: 结束日期 YYYYMMDD
    """
    start = datetime.strptime(start_str, "%Y%m%d").strftime("%Y-%m-%d")
    end = datetime.strptime(end_str, "%Y%m%d").strftime("%Y-%m-%d")
    # 交易日稀疏于自然日, 预留节假日/停牌余量
    calendar_days = max(1, (datetime.strptime(end_str, "%Y%m%d") - datetime.strptime(start_str, "%Y%m%d")).days + 1)
    lookback = max(30, min(TX_MAX_KLINE_BARS, int(calendar_days * 1.8) + 20))

    resp = requests.get(
        TX_KLINE_ENDPOINT,
        params={"param": f"{symbol},day,{start},{end},{lookback},qfq"},
        headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json,text/plain,*/*"},
        timeout=TX_HTTP_TIMEOUT,
    )
    resp.raise_for_status()
    payload = resp.json()

    data = payload.get("data") if isinstance(payload, dict) else None
    item = data.get(symbol) if isinstance(data, dict) else None
    if not isinstance(item, dict):
        raise ValueError(f"腾讯数据源返回异常 [{symbol}]")
    # 个股/ETF 返回 qfqday, 指数返回 day
    rows = item.get("qfqday") or item.get("day") or []

    records = []
    for row in rows:
        if not isinstance(row, list) or len(row) < 6:
            continue
        records.append(
            {
                "date": str(row[0]),
                "open": row[1],
                "close": row[2],
                "high": row[3],
                "low": row[4],
                "volume": row[5],
            }
        )
    if not records:
        raise ValueError(f"腾讯数据源未返回 [{symbol}] 的K线数据")

    df = pd.DataFrame(records)
    df["date"] = pd.to_datetime(df["date"])
    for col in ("open", "close", "high", "low", "volume"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["volume"] = df["volume"] * 100  # 手 → 股
    df = df[(df["date"] >= start) & (df["date"] <= end)]
    return df.sort_values("date").reset_index(drop=True)

# ─────────────────────────── 主类 ───────────────────────────


class A_FearGreed:
    """A股个股/ETF 贪恐指数计算器"""

    def __init__(self, code: str, name: str = ""):
        self.code = code
        self.name = name or code
        self.is_etf = self._is_etf_code(code)
        self.data: pd.DataFrame | None = None       # 个股/ETF 日线
        self.benchmark: pd.DataFrame | None = None   # 沪深300 日线
        self.result: pd.DataFrame | None = None      # 含所有指标和贪恐指数

    # ── 工具 ──

    @staticmethod
    def _is_etf_code(code: str) -> bool:
        return code.startswith(("15", "16", "51", "56", "58"))

    @staticmethod
    def _is_stock_index(code: str) -> bool:
        """是否是指数代码 (用于判断基准指数)"""
        return code == CSI300_CODE

    # ── 数据获取 ──

    @staticmethod
    def _to_tx_symbol(code: str) -> str:
        """A股/ETF 代码 → 腾讯财经 symbol（带 sh/sz 前缀）"""
        # 上证: 个股 6 开头, ETF 5 开头, B股 9 开头
        return f"sh{code}" if code.startswith(("6", "5", "9")) else f"sz{code}"

    def _fetch_data_primary(self, start_str: str, end_str: str) -> tuple[pd.DataFrame, pd.DataFrame]:
        """使用 akshare 东方财富数据源"""
        if self.is_etf:
            df = ak.fund_etf_hist_em(
                symbol=self.code,
                period="daily",
                start_date=start_str,
                end_date=end_str,
                adjust="qfq",
            )
            df = df.rename(
                columns={
                    "日期": "date",
                    "开盘": "open",
                    "收盘": "close",
                    "最高": "high",
                    "最低": "low",
                    "成交量": "volume",
                    "换手率": "turnover",
                }
            )
        else:
            df = ak.stock_zh_a_hist(
                symbol=self.code,
                period="daily",
                start_date=start_str,
                end_date=end_str,
                adjust="qfq",
            )
            df = df.rename(
                columns={
                    "日期": "date",
                    "开盘": "open",
                    "收盘": "close",
                    "最高": "high",
                    "最低": "low",
                    "成交量": "volume",
                    "换手率": "turnover",
                }
            )
        df = df[["date", "open", "high", "low", "close", "volume", "turnover"]]
        df["date"] = pd.to_datetime(df["date"])
        df = df.sort_values("date").reset_index(drop=True)

        bm = ak.stock_zh_index_daily(symbol="sh000300")
        bm = bm.rename(columns={"date": "date", "close": "bm_close"})
        bm["date"] = pd.to_datetime(bm["date"])
        bm = bm.sort_values("date")[["date", "bm_close"]].reset_index(drop=True)
        return df, bm

    def _fetch_data_tx(self, start_str: str, end_str: str) -> tuple[pd.DataFrame, pd.DataFrame]:
        """akshare 主数据源失败时使用腾讯财经直连接口"""
        tx_symbol = self._to_tx_symbol(self.code)
        df = _tx_fetch_kline(tx_symbol, start_str, end_str)
        # 腾讯接口不提供换手率, 不带 turnover 列 (讨论热度指标自动退化为量比)
        df = df[["date", "open", "high", "low", "close", "volume"]]

        bm = _tx_fetch_kline("sh000300", start_str, end_str)
        bm = bm.rename(columns={"close": "bm_close"})[["date", "bm_close"]]
        return df, bm

    def fetch_data(self) -> None:
        """获取个股/ETF 和 沪深300 历史日线数据 (近1年+), 优先读取本地缓存"""
        cached = _load_raw_cache(self.code)
        if cached is not None:
            self.data, self.benchmark = cached
            return

        end = datetime.now()
        start = end - timedelta(days=800)  # 取足够长数据用于滚动计算
        start_str = start.strftime("%Y%m%d")
        end_str = end.strftime("%Y%m%d")

        try:
            df, bm = self._fetch_data_primary(start_str, end_str)
        except Exception as e:
            print(f"akshare 主数据源获取失败，切换至腾讯数据源 [{self.code}]: {e}", file=sys.stderr)
            df, bm = self._fetch_data_tx(start_str, end_str)

        _save_raw_cache(self.code, df, bm)
        self.data = df
        self.benchmark = bm

    # ── 6项指标计算 (均为时间序列) ──

    def _calc_price_momentum(self) -> pd.Series:
        """指标1: 价格动量 - 综合 50/125 日均线偏离度"""
        close = self.data["close"]
        ma50 = close.rolling(50).mean()
        ma125 = close.rolling(125).mean()
        dev50 = (close - ma50) / ma50
        dev125 = (close - ma125) / ma125
        # 两偏离度取均值
        return (dev50 + dev125) / 2

    def _calc_rsi(self, period: int = 14) -> pd.Series:
        """指标2: RSI(14) - 手动实现 Wilder 平滑 RSI"""
        close = self.data["close"]
        delta = close.diff()
        gain = delta.where(delta > 0, 0.0)
        loss = (-delta).where(delta < 0, 0.0)

        # Wilder 平滑: alpha = 1/period 的 EMA
        avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
        avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()

        rs = avg_gain / avg_loss.replace(0, np.nan)
        rsi = 100 - (100 / (1 + rs))
        return rsi

    def _calc_volume_change(self) -> pd.Series:
        """指标3: 成交量变化 - 20日均量 vs 120日均量"""
        volume = self.data["volume"]
        ma20 = volume.rolling(20).mean()
        ma120 = volume.rolling(120).mean()
        return ma20 / ma120.replace(0, np.nan) - 1  # 偏离度

    def _calc_volatility(self) -> pd.Series:
        """指标4: 历史波动率 - 30日收益率标准差"""
        returns = self.data["close"].pct_change()
        vol = returns.rolling(30).std()
        return vol

    def _calc_relative_strength(self) -> pd.Series:
        """指标5: 相对强弱 vs 沪深300 - 滚动20日收益差"""
        # 合并个股和基准数据
        merged = self.data[["date", "close"]].merge(
            self.benchmark[["date", "bm_close"]], on="date", how="left"
        )
        # 向前填充基准缺失值 (节假日无基准数据)
        merged["bm_close"] = merged["bm_close"].ffill()

        stock_ret = merged["close"].pct_change()
        bm_ret = merged["bm_close"].pct_change()

        # 滚动20日超额收益 (个股 - 基准)
        excess = stock_ret.rolling(20).mean() - bm_ret.rolling(20).mean()
        return excess

    def _calc_discussion_heat(self) -> pd.Series:
        """指标6: 讨论热度 - 换手率 5日均值 / 60日均值 比值

        换手率越高 → 交易越活跃 → 讨论/关注度越高
        作为新闻热度的代理指标。
        """
        if "turnover" not in self.data.columns:
            # 如果没有换手率数据, 用量比替代
            volume = self.data["volume"]
            ratio = volume.rolling(5).mean() / volume.rolling(60).mean().replace(0, np.nan)
            return ratio - 1

        turnover = self.data["turnover"].fillna(0)
        short_ma = turnover.rolling(5).mean()
        long_ma = turnover.rolling(60).mean().replace(0, np.nan)
        return short_ma / long_ma - 1

    # ── 滚动 Min-Max 归一化 (0-100) ──

    @staticmethod
    def _rolling_minmax(s: pd.Series, window: int = NORM_WINDOW) -> pd.Series:
        """对序列做全局 Min-Max 归一化至 0~100 (使用全部已有数据)"""
        min_ = s.expanding().min()
        max_ = s.expanding().max()
        # 防止除零 / 全等序列
        mask = (max_ - min_).abs() < 1e-10
        result = pd.Series(np.full(len(s), 50.0), index=s.index)
        result[~mask] = 100 * (s[~mask] - min_[~mask]) / (max_[~mask] - min_[~mask])
        return result

    # ── 主计算流程 ──

    def compute(self) -> pd.DataFrame:
        """计算所有指标并合成贪恐指数"""
        if self.data is None:
            self.fetch_data()

        df = self.data.copy()

        # 计算 6 项原始指标
        df["raw_price_momentum"] = self._calc_price_momentum()
        df["raw_rsi"] = self._calc_rsi()
        df["raw_volume_change"] = self._calc_volume_change()
        df["raw_volatility"] = self._calc_volatility()
        df["raw_relative_strength"] = self._calc_relative_strength()
        df["raw_discussion_heat"] = self._calc_discussion_heat()

        # RSI 本身在 0-100 范围内, 直接使用
        df["norm_rsi"] = df["raw_rsi"].clip(0, 100)

        # 其余 5 个指标做滚动 min-max 归一化至 0-100
        norm_cols = [
            "raw_price_momentum",
            "raw_volume_change",
            "raw_volatility",
            "raw_relative_strength",
            "raw_discussion_heat",
        ]
        for col in norm_cols:
            df[f"norm_{col.replace('raw_', '')}"] = self._rolling_minmax(df[col])

        # 合成贪恐指数 (加权平均)
        df["fear_greed"] = (
            df["norm_price_momentum"] * WEIGHTS["price_momentum"]
            + df["norm_rsi"] * WEIGHTS["rsi"]
            + df["norm_volume_change"] * WEIGHTS["volume_change"]
            + df["norm_volatility"] * WEIGHTS["volatility"]
            + df["norm_relative_strength"] * WEIGHTS["relative_strength"]
            + df["norm_discussion_heat"] * WEIGHTS["discussion_heat"]
        )
        df["fear_greed"] = df["fear_greed"].clip(0, 100)

        self.result = df
        return df

    # ── 可视化 ──

    def plot(self, save_path: str = "") -> None:
        """绘制双轴图表: 收盘价 + 贪恐指数 (近1年)"""
        if self.result is None:
            self.compute()

        df = self.result.copy()
        # 只取近 1 年数据
        cutoff = df["date"].max() - pd.DateOffset(years=1)
        df = df[df["date"] >= cutoff].copy()

        fig, ax1 = plt.subplots(figsize=(16, 8))
        fig.suptitle(
            f"{self.name} ({self.code})  Fear & Greed Index",
            fontsize=14,
            fontweight="bold",
        )

        # ── 左轴: 收盘价 ──
        color_price = "#1f77b4"
        ax1.set_ylabel("Price", color=color_price, fontsize=12)
        (line1,) = ax1.plot(
            df["date"], df["close"], color=color_price, linewidth=1.5, label="Close Price"
        )
        ax1.tick_params(axis="y", labelcolor=color_price)
        ax1.grid(axis="y", alpha=0.3)

        # ── 右轴: 贪恐指数 ──
        ax2 = ax1.twinx()
        color_fg = "#d62728"
        ax2.set_ylabel("Fear & Greed Index", color=color_fg, fontsize=12)
        ax2.set_ylim(-5, 105)
        (line2,) = ax2.plot(
            df["date"],
            df["fear_greed"],
            color=color_fg,
            linewidth=2,
            alpha=0.85,
            label="Fear & Greed",
        )

        # 填充背景色: 恐惧区域 / 贪婪区域
        ax2.axhspan(0, 25, alpha=0.12, color="red", label="Extreme Fear  (0-25)")
        ax2.axhspan(25, 45, alpha=0.06, color="orange", label="Fear  (25-45)")
        ax2.axhspan(45, 55, alpha=0.04, color="gray", label="Neutral  (45-55)")
        ax2.axhspan(55, 75, alpha=0.06, color="lightgreen", label="Greed  (55-75)")
        ax2.axhspan(75, 100, alpha=0.12, color="green", label="Extreme Greed  (75-100)")

        ax2.tick_params(axis="y", labelcolor=color_fg)

        # ── X 轴日期格式化 ──
        ax1.xaxis.set_major_locator(mdates.MonthLocator(interval=2))
        ax1.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m"))
        plt.setp(ax1.xaxis.get_majorticklabels(), rotation=45, ha="right")

        # ── 图例合并 ──
        lines = [line1, line2]
        labels = [l.get_label() for l in lines]
        # 添加背景色块图例
        from matplotlib.patches import Patch

        legend_elements = [
            line1,
            line2,
            Patch(facecolor="red", alpha=0.12),
            Patch(facecolor="orange", alpha=0.06),
            Patch(facecolor="gray", alpha=0.04),
            Patch(facecolor="lightgreen", alpha=0.06),
            Patch(facecolor="green", alpha=0.12),
        ]
        legend_labels = [
            "Close Price",
            "Fear & Greed",
            "Extreme Fear (0-25)",
            "Fear (25-45)",
            "Neutral (45-55)",
            "Greed (55-75)",
            "Extreme Greed (75-100)",
        ]
        ax1.legend(legend_elements, legend_labels, loc="upper left", fontsize=9)

        fig.tight_layout()

        if save_path:
            plt.savefig(save_path, dpi=150, bbox_inches="tight")
            print(f"图表已保存: {save_path}")
        plt.show()


# ─────────────────────────── 命令行入口 ───────────────────────────

def print_index_table(df: pd.DataFrame) -> None:
    """打印最新一日各指标明细"""
    latest = df.iloc[-1]
    date_str = latest["date"].strftime("%Y-%m-%d")
    print(f"\n{'='*60}")
    print(f"  {latest.get('code', '')} Fear & Greed Index")
    print(f"  日期: {date_str}")
    print(f"  贪恐指数: {latest['fear_greed']:.1f}")
    print(f"{'='*60}")
    print(f"  子指标 (归一化后 0-100):")
    print(f"    价格动量 (MA偏离)  : {latest['norm_price_momentum']:.1f}")
    print(f"    RSI(14)           : {latest['norm_rsi']:.1f}")
    print(f"    成交量变化         : {latest['norm_volume_change']:.1f}")
    print(f"    历史波动率         : {latest['norm_volatility']:.1f}")
    print(f"    相对强弱 vs 沪深300: {latest['norm_relative_strength']:.1f}")
    print(f"    讨论热度(换手率)   : {latest['norm_discussion_heat']:.1f}")
    print(f"{'='*60}\n")


def print_help() -> None:
    print("用法:")
    print("  python cal_a_greed.py <code1> [code2] ...")
    print("")
    print("代码可以是纯数字 (自动识别个股/ETF):")
    print("  个股: 000001, 600519, 300750 ...")
    print("  ETF:  510050, 159915, 512880 ...")
    print("")
    print("或使用预设列表:")
    print("  python cal_a_greed.py --stocks       # 运行全部预设个股")
    print("  python cal_a_greed.py --etfs         # 运行全部预设ETF")
    print("  python cal_a_greed.py --all          # 运行全部预设个股+ETF")
    print("")
    print("示例:")
    print("  python cal_a_greed.py 000001")
    print("  python cal_a_greed.py 600519 510050 159915")


# 常用代码 → 名称映射
NAME_MAP: dict[str, str] = {
    # 个股
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
    # ETF
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


def main():
    args = sys.argv[1:]
    if not args:
        print_help()
        return

    # 解析预设参数
    if args[0] in ("--help", "-h"):
        print_help()
        return

    codes_to_run: list[str] = []
    if args[0] == "--stocks":
        codes_to_run = STOCK_CODES
    elif args[0] == "--etfs":
        codes_to_run = ETF_CODES
    elif args[0] == "--all":
        codes_to_run = STOCK_CODES + ETF_CODES
    else:
        codes_to_run = args

    for code in codes_to_run:
        try:
            name = NAME_MAP.get(code, code)
            print(f"\n▶ 正在计算: {name} ({code}) ...")

            fg = A_FearGreed(code, name=name)
            df = fg.compute()
            print_index_table(df)
            fg.plot()

        except KeyboardInterrupt:
            break
        except Exception as e:
            print(f"✗ 计算失败 [{code}]: {e}", file=sys.stderr)
            import traceback
            traceback.print_exc()
            continue


if __name__ == "__main__":
    main()
