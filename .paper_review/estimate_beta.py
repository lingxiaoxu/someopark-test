#!/usr/bin/env python3
"""实测 reinforcement strength β —— 从【开环消融 (MIRO_POSS_G=0)】跑出的控球时间序列估计。

理论(paper eq.1 / Thm.1): 开环控球份额 ρ 的更新映射 ρ_{k+1}=σ(4β(ρ_k−0.5)),
在 ρ*=0.5 处的导数 = β。β>1 → 该不动点失稳 → supercritical pitchfork → 份额双峰(垄断)。
因此 β 可由份额序列的【一阶自回归斜率】(ρ_{k+1}−0.5 对 ρ_k−0.5)直接估出;
logistic 非线性拟合作交叉校验;并报双峰系数与谷深作为 β>1 的直接证据。

用法: python research/estimate_beta.py <trajectory.jsonl> [more.jsonl ...] [--window 50]
  每个文件是一场(开环)的 trajectory.jsonl(逐拍 ball.team = 持球队ID)。
"""
import json, sys, math

W = 100  # 窗口(拍); 越大采样噪声越小(折叠偏置越低)。可 --window 覆盖


def load_holder_series(path):
    """→ 逐拍持球队ID列表(丢球拍剔除)。"""
    xs = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                t = (json.loads(line).get("ball") or {}).get("team")
            except Exception:
                continue
            if t is not None and t != "":
                xs.append(str(t))
    return xs


def windowed_shares(holders, w):
    """把持球队ID序列切成非重叠窗口, 返回 teamA 份额序列(teamA=最常持球队, 标签无关)。"""
    if len(holders) < 2 * w:
        return []
    ids = {}
    for h in holders:
        ids[h] = ids.get(h, 0) + 1
    teamA = max(ids, key=ids.get)
    shares = []
    for i in range(0, len(holders) - w + 1, w):
        seg = holders[i:i + w]
        shares.append(sum(1 for h in seg if h == teamA) / len(seg))
    return shares


def ar1_slope(pairs):
    """(x,y) 上过原点的最小二乘斜率 = Σxy/Σxx。x=ρ_k−0.5, y=ρ_{k+1}−0.5。斜率=β̂。
    注: 全域回归在 β>1 时被饱和分支(锁死极端, 斜率≈0)拉低 → 系统性低估, 视为 β 下界。"""
    sxx = sum(x * x for x, _ in pairs)
    sxy = sum(x * y for x, y in pairs)
    return (sxy / sxx) if sxx > 0 else float("nan")


def local_slope(pairs, delta=0.15):
    """不动点 ρ*=0.5 处的【局部导数】= β̂(主估)。只用 |ρ_k−0.5|<delta 的转移对(避开饱和分支)。
    映射 ρ_{k+1}=σ(4β(ρ_k−0.5)) 在 0.5 处导数 = 4β·σ'(0)=4β·(1/4)=β。"""
    loc = [(x, y) for x, y in pairs if abs(x) < delta]
    if len(loc) < 6:
        return float("nan"), len(loc)
    sxx = sum(x * x for x, _ in loc)
    sxy = sum(x * y for x, y in loc)
    return (sxy / sxx if sxx > 0 else float("nan")), len(loc)


def logistic_beta(pairs, iters=400, lr=0.5):
    """拟合 ρ_{k+1}=σ(4β(ρ_k−0.5)) 的 β(梯度下降, 交叉校验)。x,y 已减0.5。"""
    b = 1.0
    for _ in range(iters):
        g = 0.0
        for x, y in pairs:
            z = 4 * b * x
            p = 1.0 / (1.0 + math.exp(-max(-30, min(30, z)))) - 0.5   # σ−0.5
            g += (p - y) * (4 * x) * (p + 0.5) * (0.5 - p)            # dL/db
        b -= lr * g / max(len(pairs), 1)
    return b


def beta_from_mode(shares):
    """从【垄断模态位置】反解 β(主估, 对饱和稳健)。
    β>1 时稳定分支 ρ+ 满足 ρ+=σ(4β(ρ+−0.5)) → β=logit(ρ+)/(4(ρ+−0.5))。
    ρ+ 取领先队折叠份额 max(s,1−s)∈[0.5,1] 的中位数(稳健模态)。"""
    if not shares:
        return float("nan"), float("nan")
    lead = sorted(max(s, 1 - s) for s in shares)
    rho = lead[len(lead) // 2]                       # 折叠份额中位数 = 典型领先份额
    if rho <= 0.5 + 1e-6:
        return float("nan"), rho
    rho = min(rho, 0.999)
    beta = math.log(rho / (1 - rho)) / (4 * (rho - 0.5))
    return beta, rho


def bimodality(shares):
    """Sarle 双峰系数 BC=(skew²+1)/kurt; BC>0.555 → 双峰。+ 中央谷深(份额∈[.45,.55] 占比 vs 极端)。"""
    n = len(shares)
    if n < 4:
        return None, None
    mu = sum(shares) / n
    m2 = sum((s - mu) ** 2 for s in shares) / n
    if m2 == 0:
        return None, None
    m3 = sum((s - mu) ** 3 for s in shares) / n
    m4 = sum((s - mu) ** 4 for s in shares) / n
    skew = m3 / m2 ** 1.5
    kurt = m4 / m2 ** 2
    bc = (skew ** 2 + 1) / kurt
    central = sum(1 for s in shares if 0.45 <= s <= 0.55) / n
    extreme = sum(1 for s in shares if s <= 0.35 or s >= 0.65) / n
    return bc, (central, extreme)


def main(paths, w):
    all_pairs, all_shares, ticks = [], [], 0
    for p in paths:
        h = load_holder_series(p)
        ticks += len(h)
        sh = windowed_shares(h, w)
        all_shares += sh
        for k in range(len(sh) - 1):        # 非重叠窗口 → 相邻做转移对
            all_pairs.append((sh[k] - 0.5, sh[k + 1] - 0.5))
    if len(all_pairs) < 8:
        print(f"数据不足: {len(all_pairs)} 转移对(需 ≥8)。跑更长/更多消融场。")
        return
    beta_mode, rho_mode = beta_from_mode(all_shares)
    beta_loc, nloc = local_slope(all_pairs)
    beta_lo = logistic_beta(all_pairs)
    beta_ar = ar1_slope(all_pairs)
    bc, vale = bimodality(all_shares)
    beta = beta_mode if beta_mode == beta_mode else beta_lo   # 主估: 模态反解(nan 则 logistic)
    gstar = 4 * (beta - 1)
    print(f"=== 实测 β(开环 g=0) | {len(paths)}场 {ticks}拍 窗口{w} {len(all_shares)}窗 {len(all_pairs)}转移对 ===")
    print(f"β̂ (模态反解 ρ+={rho_mode:.3f}, 主估) = {beta_mode:.3f}")
    print(f"β̂ (局部斜率@ρ*=0.5)     = {beta_loc:.3f}  (用{nloc}个中段转移对; 强双峰时数据稀)")
    print(f"β̂ (logistic 拟合)       = {beta_lo:.3f}")
    print(f"β̂ (AR1 全域斜率, 下界)  = {beta_ar:.3f}  (β>1 时被饱和分支低估)")
    if bc is not None:
        c, e = vale
        _bctag = "过Sarle阈值0.555" if bc > 5 / 9 else "Sarle阈值0.555未过——饱和截断压低BC; 双峰判据以极端窗口占比为准"
        print(f"份额分布: 领先模态ρ+={rho_mode:.3f}  BC={bc:.3f} ({_bctag})  中央[.45,.55]{c*100:.0f}% 极端(≤.35|≥.65){e*100:.0f}%")
    print(f"结论: β̂>1 ? {'是 → ρ*=0.5 失稳 → pitchfork(垄断)' if beta > 1 else '否'}  (主估 β̂={beta:.2f})")
    print(f"临界增益 g*=4(β̂−1)={gstar:.2f}; 部署 g=22 {'>' if 22 > gstar else '≤'} g* → 反馈{'足以' if 22 > gstar else '不足'}稳住内点")


if __name__ == "__main__":
    argv = sys.argv[1:]
    if "--window" in argv:
        i = argv.index("--window")
        W = int(argv[i + 1])
        argv = argv[:i] + argv[i + 2:]   # 移除 --window 及其值(否则值被当文件名)
    args = [a for a in argv if not a.startswith("--")]
    if not args:
        print(__doc__)
        sys.exit(0)
    main(args, W)
