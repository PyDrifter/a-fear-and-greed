import os
import sys
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from tiingo import TiingoClient
from fredapi import Fred
from serverchan_sdk import sc_send

# ================= 配置与初始化 =================
# 请在环境变量中设置 TIINGO_API_KEY 和 FRED_API_KEY
TIINGO_API_KEY = os.environ.get("TIINGO_API_KEY", "").strip()
FRED_API_KEY = os.environ.get("FRED_API_KEY", "").strip()
SERVER_CHAN_KEY = os.environ.get("SERVER_CHAN_KEY", "").strip()
if not TIINGO_API_KEY or not FRED_API_KEY:
    raise ValueError("请设置环境变量 TIINGO_API_KEY 和 FRED_API_KEY")

HTTP_TIMEOUT = 15

# 初始化 Tiingo 客户端
config = {
    'session': True,
    'api_key': TIINGO_API_KEY
}
tiingo_client = TiingoClient(config)
fred = Fred(api_key=FRED_API_KEY)


# ================= 数据获取模块 =================

def get_market_data(ticker: str, days: int = 400) -> pd.DataFrame:
    """
    使用 Tiingo 获取股票/ETF 的日线数据
    """
    end_date = datetime.now()
    start_date = end_date - timedelta(days=days)

    df = tiingo_client.get_dataframe(
        ticker,
        startDate=start_date.strftime('%Y-%m-%d'),
        endDate=end_date.strftime('%Y-%m-%d'),
        fmt='json'
    )
    return df


def get_high_yield_spread(days=400) -> pd.Series:
    """
    获取高收益债利差 (基点)。
    优先使用 FRED 数据 (BAMLH0A0HYMYSYLD - 美银美林高收益债利差)。
    如果没有 FRED KEY，则返回模拟数据供测试。
    """
    end_date = datetime.now()
    start_date = end_date - timedelta(days=days)
    # 使用 FRED API 获取真实数据
    try:
        # 简化请求，实际需处理 FRED 的分页和日期格式
        # 这里假设获取到了最近30天的利差数据 (百分比)
        series_pct = fred.get_series('BAMLH0A0HYM2', observation_start=start_date, observation_end=end_date)
        if series_pct.empty:
            print("警告: FRED 返回了空数据")
            return pd.Series()

            # 去除可能存在的空值
        series_pct = series_pct.dropna()
        series_bps = (series_pct * 100)
        # 转换为基点 (1% = 100 bps)
        return series_bps
    except Exception as e:
        print(f"获取 FRED 数据失败: {e}")
        return pd.Series()


# ================= 信号判断模块 =================

def check_signal_1_yield_spread(spread_series: pd.Series) -> dict:
    """
    指标1：高收益债利差是否从340个基点明确回落并企稳
    """
    if spread_series.empty or len(spread_series) < 10:
        return {"signal": False, "detail": "利差数据不足"}

    latest_spread = spread_series.iloc[-1]
    max_spread_recent = spread_series.max()

    # 条件1: 近期必须出现过 > 340 的高利差
    has_spike = max_spread_recent > 340

    # 条件2: 最新利差需从高点回落 (例如回落至少 15 个基点)
    has_declined = (max_spread_recent - latest_spread) > 15

    # 条件3: 企稳 (近5日利差波动率极小，标准差 < 5 基点)
    recent_5d = spread_series.tail(5)
    is_stabilized = recent_5d.std() < 5.0

    signal = has_spike and has_declined and is_stabilized

    return {
        "signal": signal,
        "detail": f"近期最高利差: {max_spread_recent:.1f}bps, 当前利差: {latest_spread:.1f}bps, 近5日波动: {recent_5d.std():.1f}bps"
    }


def check_signal_2_down_volume(market_ticker: str = 'SPY', threshold: float = 0.8, consecutive_days: int = 3) -> dict:
    """
    指标2：是否出现连续的、下跌成交量占比超过80%的交易日
    """
    df = get_market_data(market_ticker)
    if df.empty: return {"signal": False, "detail": "行情数据获取失败"}

    # 判断涨跌：收盘价 < 开盘价 视为下跌
    df['is_down'] = df['close'] < df['open']

    # 统计下跌成交量占比 (这里简化处理：下跌日成交量为总成交量)
    # 实际大盘占比需计算全市场上涨/下跌股票总成交量，此处用 SPY 近似替代
    df['down_vol_ratio'] = np.where(df['is_down'], 1.0, 0.0)

    # 寻找连续 N 天占比 > threshold 的序列
    current_streak = 0
    max_streak = 0
    for ratio in df['down_vol_ratio'].iloc[-20:]:  # 检查近20个交易日
        if ratio > threshold:
            current_streak += 1
            max_streak = max(max_streak, current_streak)
        else:
            current_streak = 0

    signal = max_streak >= consecutive_days
    return {
        "signal": signal,
        "detail": f"近20个交易日内，下跌高量占比(>{threshold * 100}%)最长连续天数: {max_streak}天 (要求: >{consecutive_days}天)"
    }


def check_signal_3_defensive_convergence() -> dict:
    """
    指标3：消费必需品与医疗板块相对200日均线的偏离度是否开始收敛
    """
    # 获取 XLP (Consumer Staples) 和 XLV (Health Care) 数据
    xlp_df = get_market_data('XLP', days=500)
    xlv_df = get_market_data('XLV', days=500)

    if xlp_df.empty or xlv_df.empty:
        return {"signal": False, "detail": "板块数据获取失败"}

    # 计算 200 日均线
    xlp_df['ma200'] = xlp_df['close'].rolling(window=200).mean()
    xlv_df['ma200'] = xlv_df['close'].rolling(window=200).mean()

    # 计算偏离度 (%)
    xlp_df['deviation'] = (xlp_df['close'] / xlp_df['ma200'] - 1) * 100
    xlv_df['deviation'] = (xlv_df['close'] / xlv_df['ma200'] - 1) * 100

    # 取近 10 个交易日数据判断收敛趋势
    recent_xlp_dev = xlp_df['deviation'].tail(10).dropna()
    recent_xlv_dev = xlv_df['deviation'].tail(10).dropna()

    if len(recent_xlp_dev) < 5 or len(recent_xlv_dev) < 5:
        return {"signal": False, "detail": "200日均线数据不足"}

    # 判断逻辑：前期偏离度为正（或高位抗跌），近期偏离度数值开始下降（向 0 或负值收敛）
    # 使用简单线性回归斜率判断方向，或者直接比较首尾
    xlp_converging = recent_xlp_dev.iloc[-1] < recent_xlp_dev.iloc[0]
    xlv_converging = recent_xlv_dev.iloc[-1] < recent_xlv_dev.iloc[0]

    signal = xlp_converging and xlv_converging

    return {
        "signal": signal,
        "detail": f"XLP偏离度: {recent_xlp_dev.iloc[0]:.2f}% -> {recent_xlp_dev.iloc[-1]:.2f}% | XLV偏离度: {recent_xlv_dev.iloc[0]:.2f}% -> {recent_xlv_dev.iloc[-1]:.2f}%"
    }


# ================= 主控逻辑 =================

def send_wechat(key: str, text: str, count: int) -> None:
    """通过企业微信群机器人 Webhook 发送文本消息（参照 ma_warn.py）。"""
    result = sc_send(
        key,
        f"{count} 项预警已触发",
        text,
        {"tags": "警告"}
    )
    if result.get("code") != 0:
        raise RuntimeError(f"微信发送失败: {result}")


def judge_market_bottom():
    """
    按优先级综合判断大盘止跌信号
    """
    print("开始执行大盘止跌信号判断...\n")

    results = []

    # 优先级 1: 高收益债利差
    print("正在检查指标1: 高收益债利差...")
    spread_data = get_high_yield_spread()
    res1 = check_signal_1_yield_spread(spread_data)
    results.append(("指标1(高收益债利差)", res1))
    print(f"结果: {res1['detail']}\n")

    # 优先级 2: 下跌成交量占比
    print("正在检查指标2: 下跌成交量占比...")
    res2 = check_signal_2_down_volume(market_ticker='SPY')
    results.append(("指标2(下跌成交量)", res2))
    print(f"结果: {res2['detail']}\n")

    # 优先级 3: 防御板块偏离度收敛
    print("正在检查指标3: 防御板块偏离度...")
    res3 = check_signal_3_defensive_convergence()
    results.append(("指标3(板块偏离度)", res3))
    print(f"结果: {res3['detail']}\n")

    # 汇总判断
    print("====== 综合评估 ======")
    triggered_count = sum(1 for _, r in results if r['signal'])

    if triggered_count == 3:
        verdict = "🟢 强烈止跌信号：三大指标全部触发，大盘可能已见底或即将止跌反弹。"
    elif triggered_count == 2:
        # 判断是否包含高优先级指标
        if results[0][1]['signal']:
            verdict = "🟡 中度止跌信号：核心指标(利差)已触发，伴随一个次级指标，建议密切关注。"
        else:
            verdict = "🟡 弱止跌信号：两个次级指标触发，但核心利差未改善，可能仅为超跌反弹。"
    elif triggered_count == 1:
        if results[0][1]['signal']:
            verdict = "🟡 观望信号：仅核心指标(利差)触发，市场流动性可能开始恢复，但情绪和趋势尚未反转。"
        else:
            verdict = "🔴 弱信号：仅一个次级指标触发，下跌趋势可能仍在继续。"
    else:
        verdict = "🔴 无止跌信号：所有指标均未触发，大盘仍处于下跌趋势中。"

    print(verdict)

    # 汇总成日报并推送到企业微信（每天推送结果）
    today = datetime.now().strftime("%Y-%m-%d")
    lines = [f"📊 {today} 美股大盘止跌信号日报"]
    for name, r in results:
        mark = "✅" if r["signal"] else "❌"
        lines.append(f"{mark} {name}\n   {r['detail']}")
    lines.append(verdict)
    msg = "\n".join(lines)
    print("\n" + msg)

    if not SERVER_CHAN_KEY:
        print("\n提示: 未配置 SERVER_CHAN_KEY，仅输出到日志。")
        return
    try:
        if triggered_count > 0:
            send_wechat(SERVER_CHAN_KEY, msg, triggered_count)
            print("✅ 微信推送完成。")
    except Exception as e:
        print(f"❌ 微信发送失败: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    # 使用前请设置环境变量:
    # export TIINGO_API_KEY="你的tiingo_key"
    # export FRED_API_KEY="你的fred_key"
    judge_market_bottom()
