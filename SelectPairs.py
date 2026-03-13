"""
SelectPairs.py — 从 someopark 数据库的 pairs_day_select 集合中
筛选 15 对 MRPT 配对 + 15 对 MTFS 配对。

逻辑:
  MRPT (均值回归): 找跨天稳定出现的协整配对，优先 coint ∩ pca 交集
  MTFS (动量分化): 找 pca 中出现但协整性不稳定的配对（有因子关联但价差在发散）

用法:
  python SelectPairs.py              # 分析最近30天，输出推荐
  python SelectPairs.py --days 60    # 分析最近60天
  python SelectPairs.py --save       # 分析并覆写 pair_universe_*.json
"""

import argparse
import json
import os
import sys
import time
from collections import defaultdict, Counter

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

from db.connection import get_main_db


# ---------------------------------------------------------------------------
# 1. 读取数据库
# ---------------------------------------------------------------------------

def fetch_pair_records(days: int = 30):
    """从 pairs_day_select 集合读取最近 N 天的记录。"""
    db = get_main_db()
    col = db["pairs_day_select"]
    cursor = col.find({}, {"day": 1, "coint_pairs": 1, "similar_pairs": 1, "pca_pairs": 1}) \
               .sort("day", -1) \
               .limit(days)
    records = list(cursor)
    print(f"读取到 {len(records)} 天的配对记录")
    return records


# ---------------------------------------------------------------------------
# 近期动量：从 stock_data 查收盘价，计算 return 决定 s1/s2 方向
# ---------------------------------------------------------------------------

def fetch_returns(tickers: list, lookback_days: int = 30) -> dict:
    """从 stock_data 集合获取各 ticker 近 lookback_days 天的累计收益率。
    返回 {ticker: return_float}，缺数据的 ticker 不在字典里。
    stock_data 字段: symbol, c(收盘价), t(毫秒时间戳)
    """
    db = get_main_db()
    col = db["stock_data"]
    since_ms = int((time.time() - lookback_days * 86400) * 1000)

    returns = {}
    for ticker in tickers:
        docs = list(
            col.find(
                {"symbol": ticker, "t": {"$gte": since_ms}},
                {"c": 1, "t": 1, "_id": 0}
            ).sort("t", 1)
        )
        if len(docs) < 2:
            continue
        c_start = float(docs[0]["c"])
        c_end   = float(docs[-1]["c"])
        if c_start > 0:
            returns[ticker] = (c_end - c_start) / c_start
    return returns


def orient_pair(a: str, b: str, returns: dict):
    """根据近期 return 决定 (s1, s2)：s1 是动量赢家（涨更多），s2 是输家。
    如果某一方没有 return 数据，则保持字母序并标记 uncertain=True。
    返回 (s1, s2, ret_s1, ret_s2, uncertain)
    """
    ra = returns.get(a)
    rb = returns.get(b)
    if ra is None or rb is None:
        return a, b, ra, rb, True
    if ra >= rb:
        return a, b, ra, rb, False
    else:
        return b, a, rb, ra, False


def normalize_pair(pair) -> tuple:
    """将各种格式的配对统一成 (A, B) 其中 A < B（字母序）。
    支持格式: ["AAPL","META"] / ("AAPL","META") / {"s1":"AAPL","s2":"META"}
    """
    if isinstance(pair, dict):
        a = pair.get("s1") or pair.get("stock1") or pair.get("0", "")
        b = pair.get("s2") or pair.get("stock2") or pair.get("1", "")
    elif isinstance(pair, (list, tuple)) and len(pair) >= 2:
        a, b = str(pair[0]), str(pair[1])
    else:
        return None
    if not a or not b:
        return None
    return tuple(sorted([a, b]))


# ---------------------------------------------------------------------------
# 2. 统计每对配对的跨天出现频率
# ---------------------------------------------------------------------------

def build_frequency_stats(records):
    """返回三个 dict: coint_freq, similar_freq, pca_freq
    每个 dict: { (A, B): count_of_days }
    以及 total_days 数。
    """
    coint_freq = Counter()
    similar_freq = Counter()
    pca_freq = Counter()
    total_days = len(records)

    for rec in records:
        for pair in (rec.get("coint_pairs") or []):
            key = normalize_pair(pair)
            if key:
                coint_freq[key] += 1

        for pair in (rec.get("similar_pairs") or []):
            key = normalize_pair(pair)
            if key:
                similar_freq[key] += 1

        for pair in (rec.get("pca_pairs") or []):
            key = normalize_pair(pair)
            if key:
                pca_freq[key] += 1

    print(f"总天数: {total_days}")
    print(f"协整配对去重: {len(coint_freq)} 对")
    print(f"相似配对去重: {len(similar_freq)} 对")
    print(f"PCA配对去重:  {len(pca_freq)} 对")
    return coint_freq, similar_freq, pca_freq, total_days


# ---------------------------------------------------------------------------
# 3. MRPT 筛选: 稳定均值回归者
# ---------------------------------------------------------------------------

def select_mrpt(coint_freq, similar_freq, pca_freq, total_days, n=15):
    """
    MRPT 需要价差稳定回归均值的配对。

    评分公式:
      score = coint_rate * 1.0       (协整出现率，最重要)
            + pca_rate   * 0.5       (PCA验证，Hurst<0.5+半衰期<21天)
            + similar_bonus * 0.3    (特征向量也相似，基本面支撑)

    筛选条件:
      - coint_rate >= 20%  (滚动协整数据轮换快，20%已代表较强稳定性)
      - 行业分散化 (同一 ticker 最多出现在 3 对中)
    """
    candidates = []

    for pair, coint_count in coint_freq.items():
        coint_rate = coint_count / total_days
        if coint_rate < 0.20:
            continue

        pca_count = pca_freq.get(pair, 0)
        pca_rate = pca_count / total_days

        similar_bonus = 1.0 if pair in similar_freq else 0.0

        score = coint_rate * 1.0 + pca_rate * 0.5 + similar_bonus * 0.3

        candidates.append({
            "pair": pair,
            "score": round(score, 4),
            "coint_rate": round(coint_rate, 2),
            "pca_rate": round(pca_rate, 2),
            "in_similar": pair in similar_freq,
            "coint_days": coint_count,
            "pca_days": pca_count,
        })

    # 按 score 降序
    candidates.sort(key=lambda x: -x["score"])

    # 行业分散化: 限制每个 ticker 最多出现 3 次
    selected = []
    ticker_count = Counter()

    for c in candidates:
        a, b = c["pair"]
        if ticker_count[a] >= 3 or ticker_count[b] >= 3:
            continue
        selected.append(c)
        ticker_count[a] += 1
        ticker_count[b] += 1
        if len(selected) >= n:
            break

    return selected


# ---------------------------------------------------------------------------
# 4. MTFS 筛选: 动量分化者
# ---------------------------------------------------------------------------

def select_mtfs(coint_freq, similar_freq, pca_freq, total_days, n=15):
    """
    MTFS 需要两只股票动量方向相反的配对。
    核心矛盾: someopark 的筛选器都是找 "相似/协整" 的配对,
              而 MTFS 需要 "有因子关联但协整性弱" 的配对。

    策略: 从 PCA 配对中找 "边缘对"
      - 有 PCA 因子关联 (同聚类), 但协整性不稳定
      - 这意味着价差在发散↔收敛之间切换 = 动量交易机会

    评分公式:
      score = pca_rate * (1.0 - coint_rate)
      - pca_rate 高: 有因子关联，不是随机噪声
      - coint_rate 低: 价差不回归，有趋势性

    筛选条件:
      - pca_rate >= 20%   (至少有因子关联)
      - coint_rate <= 40% (不能太协整，否则适合 MRPT)
      - 不能和 MRPT 已选配对重叠
    """
    candidates = []

    for pair, pca_count in pca_freq.items():
        pca_rate = pca_count / total_days
        if pca_rate < 0.20:
            continue

        coint_count = coint_freq.get(pair, 0)
        coint_rate = coint_count / total_days
        if coint_rate > 0.40:
            continue

        # 核心分数: pca_rate^2 拉开高频出现对的差距
        score = (pca_rate ** 2) * (1.0 - coint_rate)

        # similar 加分：特征向量也相似 = 基本面支撑的动量对，打破pca同分平局
        similar_count = similar_freq.get(pair, 0)
        similar_rate = similar_count / total_days
        score += similar_rate * 0.5

        # 偶有协整轻微惩罚
        if coint_count > 0:
            score *= 0.9

        candidates.append({
            "pair": pair,
            "score": round(score, 4),
            "pca_rate": round(pca_rate, 2),
            "coint_rate": round(coint_rate, 2),
            "similar_rate": round(similar_rate, 2),
            "pca_days": pca_count,
            "coint_days": coint_count,
            "similar_days": similar_count,
        })

    candidates.sort(key=lambda x: -x["score"])

    # 行业分散化
    selected = []
    ticker_count = Counter()

    for c in candidates:
        a, b = c["pair"]
        if ticker_count[a] >= 3 or ticker_count[b] >= 3:
            continue
        selected.append(c)
        ticker_count[a] += 1
        ticker_count[b] += 1
        if len(selected) >= n:
            break

    return selected


# ---------------------------------------------------------------------------
# 5. 去重: 确保 MRPT 和 MTFS 无重叠
# ---------------------------------------------------------------------------

def remove_overlap(mrpt_selected, mtfs_selected):
    """如果有重叠配对，从 MTFS 中移除（MRPT 优先）。"""
    mrpt_pairs = {c["pair"] for c in mrpt_selected}
    cleaned = [c for c in mtfs_selected if c["pair"] not in mrpt_pairs]
    removed = len(mtfs_selected) - len(cleaned)
    if removed:
        print(f"从 MTFS 中移除 {removed} 对与 MRPT 重叠的配对")
    return cleaned


# ---------------------------------------------------------------------------
# 6. 输出 & 保存
# ---------------------------------------------------------------------------

SECTOR_MAP = {
    # 常见行业分类 (简化版，基于 GICS)
    "tech": {"AAPL", "META", "MSFT", "GOOGL", "GOOG", "AMZN", "NVDA", "AMD",
             "INTC", "CRM", "ORCL", "ADBE", "CSCO", "AVGO", "TXN", "QCOM",
             "MCHP", "ANET", "NOW", "SNPS", "CDNS", "KLAC", "LRCX", "MRVL",
             "MU", "NXPI", "ON", "DASH", "CART", "UBER", "LYFT", "ABNB",
             "BKNG", "PANW", "CRWD", "ZS", "FTNT", "NET", "DDOG", "SNOW",
             "PLTR", "COIN", "SQ", "PYPL", "INTU", "NFLX", "SPOT", "RBLX",
             "SHOP", "TEAM", "TWLO", "TTD", "PINS", "SNAP", "U", "SE",
             "MELI", "WDAY", "VEEV", "HUBS", "BILL", "MDB", "CFLT", "ESTC",
             "DKNG", "ROKU", "ZM", "DOCU", "OKTA", "FIVN", "APPN", "PATH",
             "D"},
    "finance": {"GS", "MS", "JPM", "BAC", "WFC", "C", "BLK", "BX", "KKR",
                "APO", "ARES", "CG", "OWL", "BN", "ALLY", "SCHW", "IBKR",
                "CME", "ICE", "TW", "NDAQ", "CBOE", "MCO", "SPGI", "MSCI",
                "FIS", "FISV", "GPN", "AXP", "V", "MA", "COF", "SYF", "DFS",
                "ACGL", "PGR", "ALL", "TRV", "AIG", "MET", "PRU", "AFL",
                "AMG", "BEN", "TROW", "IVZ", "EV", "UBS", "DB", "HSBC",
                "BCS", "RY", "TD", "BMO", "CM"},
    "energy": {"XOM", "CVX", "COP", "SLB", "EOG", "MPC", "PSX", "VLO",
               "PXD", "HES", "OXY", "DVN", "HAL", "BKR", "FANG", "APA",
               "MRO", "CTRA", "AR", "EQT", "USO", "XLE", "OIH", "AMLP",
               "WMB", "OKE", "KMI", "ET", "ENB", "TRP", "LNG", "CL",
               "ESS", "EXPD"},
    "food": {"MCD", "YUM", "SBUX", "CMG", "DPZ", "QSR", "WEN", "JACK",
             "SHAK", "DG", "DLTR", "COST", "WMT", "TGT", "KR", "SYY",
             "US", "ADM", "BG", "MOS", "CF", "NTR", "FMC", "IFF",
             "KO", "PEP", "MNST", "KDP", "STZ", "SAM", "DEO", "BUD",
             "HSY", "MDLZ", "GIS", "K", "SJM", "CAG", "CPB", "HRL",
             "TSN", "PPC", "CALM", "SAFM"},
    "health": {"UNH", "JNJ", "PFE", "ABBV", "MRK", "LLY", "TMO", "ABT",
               "DHR", "AMGN", "GILD", "BMY", "ISRG", "SYK", "BDX", "MDT",
               "BSX", "EW", "ZBH", "HCA", "UHS", "THC", "CNC", "MOH",
               "HUM", "CI", "ELV", "CVS", "WBA", "MCK", "CAH", "ABC",
               "A", "IQV", "CRL", "ALGN", "HOLX", "IDXX", "DXCM", "VEEV",
               "TDOC", "DOCS"},
    "industrial": {"CAT", "DE", "HON", "MMM", "GE", "BA", "LMT", "RTX",
                   "NOC", "GD", "TDG", "HWM", "TXT", "LHX", "HII",
                   "UNP", "CSX", "NSC", "FDX", "UPS", "XPO", "JBHT",
                   "DAL", "UAL", "LUV", "AAL", "ALK", "SAVE", "HA",
                   "WM", "RSG", "WCN", "CLH", "EMR", "ROK", "AME",
                   "ETN", "PH", "ITW", "SWK", "IR", "CARR", "TT",
                   "JCI", "LII", "GNRC", "PWR", "FAST", "GWW", "WSO"},
    "realestate": {"AMT", "PLD", "CCI", "EQIX", "SPG", "O", "VICI",
                   "PSA", "EXR", "DLR", "AVB", "EQR", "MAA", "UDR",
                   "CPT", "ARE", "BXP", "SLG", "VNO", "KIM", "REG",
                   "FRT", "NNN", "WPC", "ADC", "STAG"},
    "materials": {"LIN", "APD", "SHW", "ECL", "DD", "DOW", "LYB",
                  "PPG", "NEM", "FCX", "SCCO", "AA", "NUE", "STLD",
                  "CLF", "X", "RS", "VMC", "MLM", "CX", "EXP", "SUM",
                  "IP", "PKG", "SEE", "BLL", "AVY", "SON", "GPK"},
    "utilities": {"NEE", "DUK", "SO", "AEP", "SRE", "ED", "EXC",
                  "XEL", "WEC", "ES", "AWK", "ATO", "NI", "CMS",
                  "DTE", "FE", "PEG", "PPL", "ETR", "AES"},
    "telecom": {"T", "VZ", "TMUS", "CHTR", "CMCSA", "LUMN", "FYBR",
                "ATUS", "SATS", "DISH"},
}


def guess_sector(ticker: str) -> str:
    """根据内置映射猜测 ticker 所属行业。"""
    for sector, tickers in SECTOR_MAP.items():
        if ticker in tickers:
            return sector
    return "other"


def guess_pair_sector(a: str, b: str) -> str:
    """取两只股票的行业（优先共同行业）。"""
    sa = guess_sector(a)
    sb = guess_sector(b)
    if sa == sb:
        return sa
    # 不同行业时，取非 other 的那个；都非 other 取第一个
    if sa == "other":
        return sb
    return sa


def format_mrpt_json(selected):
    """生成 pair_universe_mrpt.json 格式。"""
    result = []
    for c in selected:
        a, b = c["pair"]
        sector = guess_pair_sector(a, b)
        result.append({
            "s1": a,
            "s2": b,
            "sector": sector,
            "z_col": f"Z_{sector}",
        })
    return result


def format_mtfs_json(selected, returns: dict):
    """生成 pair_universe_mtfs.json 格式。
    s1 = 动量赢家（近期涨幅更大），s2 = 动量输家。
    """
    result = []
    for c in selected:
        a, b = c["pair"]
        sector = guess_pair_sector(a, b)
        s1, s2, _, _, uncertain = orient_pair(a, b, returns)
        entry = {
            "s1": s1,
            "s2": s2,
            "sector": sector,
            "spread_col": f"Momentum_Spread_{sector}",
        }
        if uncertain:
            entry["direction_uncertain"] = True
        result.append(entry)
    return result


def print_table(title, selected, strategy_type="mrpt", returns: dict = None):
    """打印候选配对表格。"""
    print(f"\n{'='*70}")
    print(f"  {title}")
    print(f"{'='*70}")

    if strategy_type == "mrpt":
        header = f"{'排名':>4}  {'配对':<16} {'行业':<12} {'得分':>6} {'协整率':>6} {'PCA率':>6} {'相似':>4} {'协整天':>5} {'PCA天':>5}"
        print(header)
        print("-" * 70)
        for i, c in enumerate(selected, 1):
            a, b = c["pair"]
            sector = guess_pair_sector(a, b)
            sim = "✓" if c.get("in_similar") else ""
            print(f"{i:>4}  {a+'/'+b:<16} {sector:<12} {c['score']:>6.3f} {c['coint_rate']:>5.0%} {c['pca_rate']:>5.0%} {sim:>4} {c['coint_days']:>5} {c['pca_days']:>5}")
    else:
        header = f"{'排名':>4}  {'s1→s2':<20} {'行业':<12} {'得分':>6} {'PCA率':>6} {'协整率':>6} {'相似率':>6} {'s1收益':>7} {'s2收益':>7}"
        print(header)
        print("-" * 90)
        for i, c in enumerate(selected, 1):
            a, b = c["pair"]
            sector = guess_pair_sector(a, b)
            sim_rate = c.get("similar_rate", 0.0)
            if returns is not None:
                s1, s2, rs1, rs2, uncertain = orient_pair(a, b, returns)
                direction = f"{s1}→{s2}" + ("?" if uncertain else "")
                rs1_str = f"{rs1:+.1%}" if rs1 is not None else "  N/A"
                rs2_str = f"{rs2:+.1%}" if rs2 is not None else "  N/A"
            else:
                direction = f"{a}/{b}"
                rs1_str = rs2_str = "  N/A"
            print(f"{i:>4}  {direction:<20} {sector:<12} {c['score']:>6.3f} {c['pca_rate']:>5.0%} {c['coint_rate']:>5.0%} {sim_rate:>5.0%} {rs1_str:>7} {rs2_str:>7}")


def save_json(data, filepath):
    with open(filepath, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    print(f"已保存: {filepath}")


# ---------------------------------------------------------------------------
# 7. 主流程
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="从 someopark 数据库筛选 MRPT/MTFS 配对")
    parser.add_argument("--days", type=int, default=30, help="分析最近多少天 (默认30)")
    parser.add_argument("--n", type=int, default=15, help="每个策略选多少对 (默认15)")
    parser.add_argument("--save", action="store_true", help="覆写 pair_universe_*.json")
    args = parser.parse_args()

    # 读取数据
    records = fetch_pair_records(args.days)
    if not records:
        print("未读取到任何记录，请检查数据库连接")
        sys.exit(1)

    # 数据概览
    print("\n--- 数据概览 ---")
    for rec in records[:10]:
        day = rec.get("day", "?")
        nc = len(rec.get("coint_pairs") or [])
        ns = len(rec.get("similar_pairs") or [])
        np_ = len(rec.get("pca_pairs") or [])
        print(f"  {day}: 协整={nc} 相似={ns} PCA={np_}")
    if len(records) > 10:
        print(f"  ... 还有 {len(records) - 10} 天")

    # 构建频率统计
    coint_freq, similar_freq, pca_freq, total_days = build_frequency_stats(records)

    # MRPT 筛选
    mrpt_selected = select_mrpt(coint_freq, similar_freq, pca_freq, total_days, n=args.n)
    print_table(f"MRPT 推荐配对 (均值回归) — Top {args.n}", mrpt_selected, "mrpt")

    if len(mrpt_selected) < args.n:
        print(f"\n⚠ MRPT 仅筛出 {len(mrpt_selected)} 对 (目标 {args.n})，可尝试 --days 60 扩大样本")

    # MTFS 筛选 (先去重)
    mtfs_selected = select_mtfs(coint_freq, similar_freq, pca_freq, total_days, n=args.n + 5)
    mtfs_selected = remove_overlap(mrpt_selected, mtfs_selected)
    mtfs_selected = mtfs_selected[:args.n]

    # 获取近期 return，决定 s1/s2 方向
    all_mtfs_tickers = list({t for c in mtfs_selected for t in c["pair"]})
    print(f"\n查询 {len(all_mtfs_tickers)} 个 ticker 的近期收益率...")
    returns = fetch_returns(all_mtfs_tickers, lookback_days=30)
    print(f"获取到 {len(returns)}/{len(all_mtfs_tickers)} 个 ticker 的收益数据")

    print_table(f"MTFS 推荐配对 (动量分化) — Top {args.n}", mtfs_selected, "mtfs", returns=returns)

    if len(mtfs_selected) < args.n:
        print(f"\n⚠ MTFS 仅筛出 {len(mtfs_selected)} 对 (目标 {args.n})，可尝试 --days 60 或降低 pca_rate 阈值")

    # 重叠检查
    mrpt_set = {c["pair"] for c in mrpt_selected}
    mtfs_set = {c["pair"] for c in mtfs_selected}
    overlap = mrpt_set & mtfs_set
    print(f"\n两策略重叠配对: {len(overlap)} 对")

    # 保存
    if args.save:
        base = os.path.dirname(os.path.abspath(__file__))
        mrpt_json = format_mrpt_json(mrpt_selected)
        mtfs_json = format_mtfs_json(mtfs_selected, returns)
        save_json(mrpt_json, os.path.join(base, "pair_universe_mrpt.json"))
        save_json(mtfs_json, os.path.join(base, "pair_universe_mtfs.json"))
        print("\n✓ pair_universe_mrpt.json 和 pair_universe_mtfs.json 已更新")
    else:
        print("\n提示: 加 --save 参数可覆写 pair_universe_*.json")


if __name__ == "__main__":
    main()
