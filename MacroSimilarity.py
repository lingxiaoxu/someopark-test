#!/usr/bin/env python3
"""
MacroSimilarity.py — 宏观环境相似度预计算存储(黄金窗口评估的输入)。

目的:
  DailySignal 的"黄金窗口"评估需要回答:与最近 10 个交易日(T-9..T)宏观环境
  最相似的历史 10 日段在哪里?该查询必须在回测/信号运行时零计算——本脚本
  提前算好并存储,运行时只读。

方法(复用既有 SimilarityEngine.AutoencoderMethod):
  - 在 MacroStateStore 全历史快照(2017→今,PIT)上,用 AUTOENCODER_FEATURES
    (23 维)训练一次自编码器(seed=42) → 12 维隐空间;
  - encoder + 标准化参数持久化(encoder.pt / pca.pkl + encoder_meta.json),
    之后新交易日用【冻结的】encoder 嵌入 → 存储内部度量一致;
    (表示学习用了全历史——这是表示层的轻度 in-sample,距离仅作相对比较用,
     meta 记录 trained_through,建议季度手动 --build 重训)
  - latent.parquet: 每交易日 12 维隐向量;
  - similarity_store.json: 每交易日 T 存:
      nearest_day_any  — 与 T 最相似的更早单日(无禁区;通常是邻近日,仅参考)
      nearest_day_21   — 排除 T 前 21 个交易日后的最相似单日(去平凡化)
      span10_topk      — T-9..T 段(10日隐向量均值)与所有更早不重叠段
                         (段末 e ≤ T-10)的距离 top-K(全历史,K=30)
      span10_recent    — 同上但候选限制在 T 往前 360 个交易日内(覆盖当前
                         WF OOS 370td 跨度的查询;DailySignal 用这个)

用法:
  python MacroSimilarity.py --build              # 全量重建(重训 encoder)
  python MacroSimilarity.py --update             # 增量补新交易日(冻结 encoder)
  python MacroSimilarity.py --validate           # 抽样重算+完整性校验
  python MacroSimilarity.py --build --store-dir /tmp/x   # 测试用重定向

存储目录: macro_similarity/ (repo 根;非六大连续性文件,可重建)
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime

import numpy as np
import pandas as pd

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

from MacroStateStore import MacroStateStore
from SimilarityEngine import AutoencoderMethod, AUTOENCODER_FEATURES, _LOG_TRANSFORM_FEATURES

log = logging.getLogger('MacroSimilarity')

STORE_DIR = os.path.join(BASE_DIR, 'macro_similarity')
K_GLOBAL = 30          # 全历史 top-K
K_RECENT = 10          # 近端(360td 内) top-K
RECENT_TDAYS = 360     # span10_recent 候选窗:T 往前 360 个交易日
EMBARGO_TDAYS = 21     # nearest_day_21 的去平凡禁区
SPAN = 10              # 段长(交易日)
SEED = 42
LATENT_DIM = 12


# ── 嵌入层 ────────────────────────────────────────────────────────────────────

def _load_feature_matrix() -> pd.DataFrame:
    """MacroStateStore 全历史快照 → AUTOENCODER_FEATURES 完整行(dropna)。"""
    store = MacroStateStore()
    df = store.load()
    avail = [f for f in AUTOENCODER_FEATURES if f in df.columns]
    missing = [f for f in AUTOENCODER_FEATURES if f not in df.columns]
    if missing:
        log.warning(f'快照缺列(不参与嵌入): {missing}')
    sub = df[avail].dropna(how='any').astype(float)
    log.info(f'特征矩阵: {sub.index.min().date()} → {sub.index.max().date()}  '
             f'{len(sub)} 天 × {len(avail)} 特征 (快照总天数 {len(df)})')
    return sub


def _train_encoder(sub: pd.DataFrame) -> AutoencoderMethod:
    ae = AutoencoderMethod(latent_dim=LATENT_DIM, seed=SEED)
    ae._build_and_train(sub.values.copy(), list(sub.columns))
    return ae


def _embed(ae: AutoencoderMethod, sub: pd.DataFrame) -> pd.DataFrame:
    """用(已训练/已加载的)encoder 把每日特征嵌入到隐空间。"""
    mat = sub.values.copy()
    for i, f in enumerate(sub.columns):
        if f in _LOG_TRANSFORM_FEATURES:
            floor = max(float(mat[:, i].min()), 0.01)
            mat[:, i] = np.log(np.maximum(mat[:, i], floor))
    x = (mat - ae._scaler_mean) / ae._scaler_std
    latent = ae._encode(x).astype(np.float64)   # f64:cumsum/距离计算的精度基准
    return pd.DataFrame(latent, index=sub.index,
                        columns=[f'z{i:02d}' for i in range(latent.shape[1])])


def _save_encoder(ae: AutoencoderMethod, sub: pd.DataFrame, store_dir: str) -> None:
    meta = {
        'features': list(sub.columns),
        'latent_dim': LATENT_DIM, 'seed': SEED,
        'scaler_mean': [float(v) for v in ae._scaler_mean],
        'scaler_std': [float(v) for v in ae._scaler_std],
        'log_features': sorted(f for f in sub.columns if f in _LOG_TRANSFORM_FEATURES),
        'trained_through': str(sub.index.max().date()),
        'is_pca': bool(getattr(ae, '_is_pca', False)),
        'built_at': datetime.now().isoformat(),
    }
    with open(os.path.join(store_dir, 'encoder_meta.json'), 'w') as f:
        json.dump(meta, f, indent=2)
    if meta['is_pca']:
        import pickle
        with open(os.path.join(store_dir, 'pca.pkl'), 'wb') as f:
            pickle.dump(ae._pca, f)
    else:
        import torch
        torch.save(ae._encoder.state_dict(), os.path.join(store_dir, 'encoder.pt'))


def _load_encoder(store_dir: str) -> tuple[AutoencoderMethod, dict]:
    with open(os.path.join(store_dir, 'encoder_meta.json')) as f:
        meta = json.load(f)
    ae = AutoencoderMethod(latent_dim=meta['latent_dim'], seed=meta['seed'])
    ae._feature_names = meta['features']
    ae._scaler_mean = np.array(meta['scaler_mean'])
    ae._scaler_std = np.array(meta['scaler_std'])
    ae._trained = True
    if meta['is_pca']:
        import pickle
        with open(os.path.join(store_dir, 'pca.pkl'), 'rb') as f:
            ae._pca = pickle.load(f)
        ae._is_pca = True
    else:
        import torch
        import torch.nn as nn
        n_features = len(meta['features'])
        hidden = min(32, n_features * 2)
        enc = nn.Sequential(nn.Linear(n_features, hidden), nn.ReLU(),
                            nn.Linear(hidden, meta['latent_dim']))
        enc.load_state_dict(torch.load(os.path.join(store_dir, 'encoder.pt'),
                                       weights_only=True))
        enc.eval()
        ae._encoder = enc
        ae._is_pca = False
    return ae, meta


# ── 相似度记录 ────────────────────────────────────────────────────────────────

def _compute_records(latent: pd.DataFrame,
                     only_after: str | None = None) -> dict:
    """对每个交易日 T 计算 nearest_day / span10 记录。

    only_after: 只为该日期之后的 T 生成记录(增量模式);距离计算仍用全历史。
    """
    dates = latent.index
    Z = latent.values.astype(np.float64)               # (N, d) f64:防 float32 cumsum 精度漂移
    # 10 日段均值向量:span_mean[i] = mean(Z[i-9..i]),i>=9 有效
    csum = np.vstack([np.zeros((1, Z.shape[1])), np.cumsum(Z, axis=0)])
    span_mean = (csum[SPAN:] - csum[:-SPAN]) / SPAN    # (N-9, d) → 对应末日 idx 9..N-1
    records = {}
    start_i = 1
    if only_after is not None:
        cut = pd.Timestamp(only_after)
        start_i = max(1, int(dates.searchsorted(cut, side='right')))
    for i in range(start_i, len(dates)):
        T = dates[i]
        rec = {}
        # 单日最近邻
        d_day = np.linalg.norm(Z[:i] - Z[i], axis=1)
        j = int(np.argmin(d_day))
        rec['nearest_day_any'] = [str(dates[j].date()), round(float(d_day[j]), 6)]
        if i > EMBARGO_TDAYS:
            d_e = d_day[:i - EMBARGO_TDAYS]
            j = int(np.argmin(d_e))
            rec['nearest_day_21'] = [str(dates[j].date()), round(float(d_e[j]), 6)]
        # 10 日段:参考段末日=T(需要 i>=9);候选段末日 e_idx ≤ i-10(不重叠)
        if i >= SPAN - 1 + SPAN:                        # i>=19 才有 ≥1 个候选
            ref = span_mean[i - (SPAN - 1)]
            n_cand = i - SPAN - (SPAN - 1) + 1          # e_idx ∈ [9, i-10]
            cand = span_mean[:n_cand]
            d_sp = np.linalg.norm(cand - ref, axis=1)
            order = np.argsort(d_sp)
            topk = order[:K_GLOBAL]
            rec['span10_topk'] = [[str(dates[SPAN - 1 + int(j)].date()),
                                   round(float(d_sp[int(j)]), 6)] for j in topk]
            lo = max(0, (i - RECENT_TDAYS) - (SPAN - 1))   # e_idx ≥ i-360
            d_rec = d_sp[lo:]
            if len(d_rec):
                order_r = np.argsort(d_rec)[:K_RECENT]
                rec['span10_recent'] = [[str(dates[SPAN - 1 + lo + int(j)].date()),
                                         round(float(d_rec[int(j)]), 6)] for j in order_r]
        records[str(T.date())] = rec
    return records


# ── 主流程 ────────────────────────────────────────────────────────────────────

def build(store_dir: str) -> None:
    os.makedirs(store_dir, exist_ok=True)
    sub = _load_feature_matrix()
    ae = _train_encoder(sub)
    _save_encoder(ae, sub, store_dir)
    latent = _embed(ae, sub)
    latent.to_parquet(os.path.join(store_dir, 'latent.parquet'))
    records = _compute_records(latent)
    store = {
        'built_at': datetime.now().isoformat(),
        'trained_through': str(sub.index.max().date()),
        'latent_dim': LATENT_DIM, 'span': SPAN,
        'k_global': K_GLOBAL, 'k_recent': K_RECENT,
        'recent_tdays': RECENT_TDAYS, 'embargo_tdays': EMBARGO_TDAYS,
        'days': records,
    }
    with open(os.path.join(store_dir, 'similarity_store.json'), 'w') as f:
        json.dump(store, f)
    log.info(f'BUILD 完成: {len(records)} 天记录 → {store_dir}/similarity_store.json')


def update(store_dir: str) -> None:
    """增量:冻结 encoder,嵌入新交易日,只补新 T 的记录。"""
    sp = os.path.join(store_dir, 'similarity_store.json')
    if not (os.path.exists(sp) and os.path.exists(os.path.join(store_dir, 'encoder_meta.json'))):
        log.warning('存储不存在 → 转为全量 build')
        return build(store_dir)
    ae, meta = _load_encoder(store_dir)
    sub = _load_feature_matrix()
    feats = meta['features']
    missing = [f for f in feats if f not in sub.columns]
    if missing:
        raise RuntimeError(f'快照缺少 encoder 训练时的特征列: {missing}')
    sub = sub[feats]
    latent = _embed(ae, sub)                    # 全历史重嵌入(冻结 encoder,确定性)
    latent.to_parquet(os.path.join(store_dir, 'latent.parquet'))
    with open(sp) as f:
        store = json.load(f)
    have = store.get('days', {})
    last_have = max(have.keys()) if have else None
    new_records = _compute_records(latent, only_after=last_have)
    if not new_records:
        log.info('UPDATE: 无新交易日,存储已最新')
        return
    have.update(new_records)
    store['days'] = have
    store['updated_at'] = datetime.now().isoformat()
    with open(sp, 'w') as f:
        json.dump(store, f)
    log.info(f'UPDATE 完成: 新增 {len(new_records)} 天 ({min(new_records)} → {max(new_records)})')


def validate(store_dir: str, n_sample: int = 20) -> bool:
    """完整性 + 抽样重算校验。返回 True=全绿。"""
    ok = True
    with open(os.path.join(store_dir, 'similarity_store.json')) as f:
        store = json.load(f)
    latent = pd.read_parquet(os.path.join(store_dir, 'latent.parquet'))
    days = store['days']
    dates = latent.index
    dset = {str(d.date()) for d in dates}
    print(f'store 天数={len(days)}  latent 天数={len(dates)}  '
          f'范围 {min(days)} → {max(days)}')

    # 1. 完整性
    n_nan = int(latent.isna().sum().sum())
    if n_nan:
        print(f'❌ latent 含 NaN: {n_nan}'); ok = False
    bad_dates = [d for d in days if d not in dset]
    if bad_dates:
        print(f'❌ 记录日期不在 latent 索引: {bad_dates[:5]}'); ok = False
    expect = {str(d.date()) for d in dates[1:]}
    miss = expect - set(days)
    if miss:
        print(f'❌ 缺记录的交易日: {sorted(miss)[:5]} (共{len(miss)})'); ok = False

    # 2. 结构约束
    pos = {str(d.date()): i for i, d in enumerate(dates)}
    n_span = 0
    for dstr, rec in days.items():
        i = pos[dstr]
        na = rec.get('nearest_day_any')
        if na and not (pos[na[0]] < i and na[1] >= 0):
            print(f'❌ {dstr} nearest_day_any 违反约束: {na}'); ok = False; break
        n21 = rec.get('nearest_day_21')
        if n21 and not (pos[n21[0]] <= i - EMBARGO_TDAYS - 1):
            print(f'❌ {dstr} nearest_day_21 禁区违反: {n21}'); ok = False; break
        sp10 = rec.get('span10_topk')
        if sp10:
            n_span += 1
            ends = [pos[e] for e, _ in sp10]
            dists = [x for _, x in sp10]
            if max(ends) > i - SPAN:
                print(f'❌ {dstr} span 候选与参考段重叠'); ok = False; break
            if any(b < a for a, b in zip(dists, dists[1:])):
                print(f'❌ {dstr} span 距离未升序'); ok = False; break
            if any(x < 0 or x != x for x in dists):
                print(f'❌ {dstr} span 距离非法'); ok = False; break
        srec = rec.get('span10_recent')
        if srec and sp10:
            if min(pos[e] for e, _ in srec) < i - RECENT_TDAYS - 1:
                print(f'❌ {dstr} span10_recent 超出近端窗'); ok = False; break
    print(f'结构约束: {"✓" if ok else "✗"}  (含 span 记录的天数: {n_span})')

    # 3. 抽样重算(独立于 _compute_records 的直接暴力验证)
    rng = np.random.RandomState(7)
    Z = latent.values.astype(np.float64)
    csum = np.vstack([np.zeros((1, Z.shape[1])), np.cumsum(Z, axis=0)])
    span_mean = (csum[SPAN:] - csum[:-SPAN]) / SPAN
    cand_days = [d for d in days if days[d].get('span10_topk')]
    sample = rng.choice(cand_days, size=min(n_sample, len(cand_days)), replace=False)
    for dstr in sample:
        i = pos[dstr]
        d_day = np.linalg.norm(Z[:i] - Z[i], axis=1)
        exp_j = int(np.argmin(d_day))
        got = days[dstr]['nearest_day_any']
        if str(dates[exp_j].date()) != got[0] or abs(float(d_day[exp_j]) - got[1]) > 1e-4:
            print(f'❌ {dstr} nearest_day_any 重算不符: expect {dates[exp_j].date()} got {got}')
            ok = False
        ref = span_mean[i - (SPAN - 1)]
        n_cand = i - SPAN - (SPAN - 1) + 1
        d_sp = np.linalg.norm(span_mean[:n_cand] - ref, axis=1)
        exp_top = int(np.argmin(d_sp))
        got_top = days[dstr]['span10_topk'][0]
        if str(dates[SPAN - 1 + exp_top].date()) != got_top[0] or \
           abs(float(d_sp[exp_top]) - got_top[1]) > 1e-4:
            print(f'❌ {dstr} span10 top1 重算不符: expect {dates[SPAN-1+exp_top].date()} got {got_top}')
            ok = False
    print(f'抽样重算 {len(sample)} 天: {"✓ 全部一致" if ok else "✗ 有差异"}')
    return ok


def main():
    logging.basicConfig(level=logging.INFO, format='%(levelname)s:%(name)s:%(message)s')
    ap = argparse.ArgumentParser(description='宏观相似度预计算存储')
    ap.add_argument('--build', action='store_true', help='全量重建(重训 encoder)')
    ap.add_argument('--update', action='store_true', help='增量补新交易日(冻结 encoder)')
    ap.add_argument('--validate', action='store_true', help='完整性+抽样校验')
    ap.add_argument('--store-dir', default=STORE_DIR, help='存储目录(测试可重定向)')
    args = ap.parse_args()
    if args.build:
        build(args.store_dir)
    if args.update and not args.build:
        update(args.store_dir)
    if args.validate:
        ok = validate(args.store_dir)
        sys.exit(0 if ok else 1)
    if not (args.build or args.update or args.validate):
        ap.print_help()


if __name__ == '__main__':
    main()
