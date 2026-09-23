"""W9-only copy of the audited recent-refit experiment's numerical kernel.
Source: w9_rnn_pit_expanded_20260916T001013Z/recent_refit_model/research.py.
Only adapter changes: DataFrame source input and empty evaluation support.
No stock imports, no exchange access. Caller sets HERE and CUTOFF per job.
"""
from pathlib import Path
import os
for key in ['OMP_NUM_THREADS', 'MKL_NUM_THREADS', 'OPENBLAS_NUM_THREADS']:
    os.environ[key] = '1'
import argparse
import copy
import hashlib
import json
import time
import numpy as np
import pandas as pd
import torch
from torch import nn
from sklearn.linear_model import Ridge
from sklearn.preprocessing import StandardScaler

HERE = Path(__file__).resolve().parent
CUTOFF = pd.Timestamp('2026-09-15T21:49:42Z').timestamp()
FIRST_DAY = pd.Timestamp('2026-08-17T00:00:00Z').timestamp()
DAYS = 30
ASSETS = ['BTC', 'ETH', 'SOL', 'DOGE', 'XRP']
FEATURES = ['ret1_bp', 'ret5_bp', 'ret15_bp', 'vol5_bp', 'vol15_bp', 'spread_bp',
            'log_volume1', 'log_volume5', 'log_volume15', 'sin_tod', 'cos_tod', 'weekend'] + ['coin_' + a for a in ASSETS[1:]]
CANDIDATES = [dict(name=f'lstm{h}_{target}', hidden=h, target=target) for h in [16, 32] for target in ['residual', 'direct']]
def utc(ts):
    return pd.Timestamp(ts, unit='s', tz='UTC').isoformat()


def prepare(source):
    raw = source.copy() if isinstance(source, pd.DataFrame) else pd.read_parquet(source)
    assert raw.loc[raw.valid_bar, 'ingested_at'].le(raw.loc[raw.valid_bar, 'bar_end_ts']).all()
    assert raw.loc[raw.valid_bar, 'quote_age_s'].between(0,15).all()
    assert raw.loc[raw.valid_bar, 'sample_interval_s'].between(45,75).all()
    assert raw.loc[raw.valid_bar, 'volume'].ge(0).all()
    assert raw.loc[raw.valid_bar, 'contract_size_base_units'].gt(0).all()
    raw = raw[raw.asset.isin(ASSETS) & (raw.bar_end_ts + 60 <= CUTOFF)].copy()
    frames = {}
    for asset in ASSETS:
        g = raw[raw.asset == asset].sort_values('bar_end_ts').copy()
        if g.empty:
            continue
        assert not g.bar_end_ts.duplicated().any()
        idx = np.arange(g.bar_end_ts.min(), g.bar_end_ts.max() + 1, 60, dtype='int64')
        g = g.set_index('bar_end_ts').reindex(idx).rename_axis('bar_end_ts').reset_index()
        g['asset'] = asset
        g['bar_start_ts'] = g.bar_end_ts - 60
        good = g.valid_bar.fillna(False).astype(bool)
        g.loc[~good, ['mid_close', 'volume', 'spread_bp']] = np.nan
        g['valid_bar'] = good
        g['decision_ts'] = g.bar_end_ts + 60
        g['target_start_ts'] = g.bar_end_ts + 120
        g['label_end_ts'] = g.bar_end_ts + 180
        p = g.mid_close
        g['ret1_bp'] = np.log(p / p.shift(1)) * 10000
        g['ret5_bp'] = np.log(p / p.shift(5)) * 10000
        g['ret15_bp'] = np.log(p / p.shift(15)) * 10000
        g['vol5_bp'] = np.sqrt(g.ret1_bp.pow(2).rolling(5, min_periods=5).sum())
        g['vol15_bp'] = np.sqrt(g.ret1_bp.pow(2).rolling(15, min_periods=15).sum())
        g['volume5'] = g.volume.rolling(5, min_periods=5).mean()
        g['volume15'] = g.volume.rolling(15, min_periods=15).mean()
        tod = (g.bar_end_ts.to_numpy() % 86400) / 86400 * 2 * np.pi
        g['sin_tod'], g['cos_tod'] = np.sin(tod), np.cos(tod)
        g['weekend'] = (pd.to_datetime(g.bar_end_ts, unit='s', utc=True).dt.dayofweek >= 5).astype(float)
        for a in ASSETS[1:]:
            g['coin_' + a] = float(asset == a)
        g['target_volume'] = g.volume.shift(-3)
        g['target_return_bp'] = np.log(p.shift(-3) / p.shift(-2)) * 10000
        g['target_abs_return_bp'] = g.target_return_bp.abs()
        g['target_actual_start_receipt_ts'] = g.ingested_at.shift(-2)
        g['target_actual_end_receipt_ts'] = g.ingested_at.shift(-3)
        g['target_last_receipt_ts'] = pd.concat([g.ingested_at.shift(-2), g.ingested_at.shift(-3)], axis=1).max(axis=1, skipna=False)
        g['future_label_available'] = good.shift(-3, fill_value=False) & good.shift(-2, fill_value=False) & (g.label_end_ts + 60 <= CUTOFF)
        known=g.future_label_available
        assert g.loc[known,'target_actual_start_receipt_ts'].ge(g.loc[known,'decision_ts']).all()
        assert (g.loc[known,'target_actual_end_receipt_ts']-g.loc[known,'target_actual_start_receipt_ts']).between(45,75).all()
        g.loc[~g.future_label_available, ['target_volume', 'target_return_bp', 'target_abs_return_bp']] = np.nan
        g['history_valid'] = good.rolling(16, min_periods=16).sum().eq(16)
        g['feature_first_bar_end_ts'] = g.bar_end_ts - 24 * 60
        g['feature_last_recorded_receipt_ts'] = g.ingested_at.rolling(25, min_periods=25).max()
        g['strict_feature_available'] = g.feature_last_recorded_receipt_ts.le(g.decision_ts)
        frames[asset] = g
    return frames


def dataset(frames, day, stage):
    assert stage in ['selection','refit']
    start, train_end, val_end = (day-33*86400,day-5*86400,day) if stage=='selection' else (day-28*86400,day,day)
    X, M, norms, receipt = [], [], {}, {}
    for asset, base in frames.items():
        g = base.copy()
        trainbar = (g.bar_end_ts >= start) & (g.bar_end_ts + 60 < train_end) & g.valid_bar & g.volume.gt(0) & g.ingested_at.lt(train_end)
        norm = float(g.loc[trainbar, 'volume'].median())
        assert np.isfinite(norm) and norm > 0
        norms[asset] = norm
        receipt[asset] = {'normalizer_rows': int(trainbar.sum()), 'normalizer_receipt_late_rows': int((trainbar & (g.ingested_at >= train_end)).sum()),
                          'normalizer_max_receipt_ts': float(g.loc[trainbar, 'ingested_at'].max()),
                          'normalizer_missing_receipts': int(g.loc[trainbar, 'ingested_at'].isna().sum()),
                          'train_raw_q99_contract_volume': float(g.loc[(g.bar_end_ts >= start) & (g.bar_end_ts+60 < train_end) & g.valid_bar & g.ingested_at.lt(train_end), 'volume'].quantile(.99)),
                          'train_raw_q90_contract_volume': float(g.loc[(g.bar_end_ts >= start) & (g.bar_end_ts+60 < train_end) & g.valid_bar & g.ingested_at.lt(train_end), 'volume'].quantile(.90))}
        g['log_volume1'] = np.log1p(g.volume / norm)
        g['log_volume5'] = np.log1p(g.volume5 / norm)
        g['log_volume15'] = np.log1p(g.volume15 / norm)
        g.loc[~g.history_valid, FEATURES] = np.nan
        g['target_log_volume'] = np.log1p(g.target_volume / norm)
        g['pred_ma5'] = g.log_volume5
        g['volume_normalizer'] = norm
        g['train_volume_q99'] = receipt[asset]['train_raw_q99_contract_volume']
        g['train_volume_q90'] = receipt[asset]['train_raw_q90_contract_volume']
        windows = np.lib.stride_tricks.sliding_window_view(g[FEATURES].to_numpy('float32'), 10, axis=0).transpose(0,2,1)
        cols = ['asset','bar_start_ts','bar_end_ts','decision_ts','target_start_ts','label_end_ts','future_label_available',
                'target_log_volume','target_volume','target_abs_return_bp','target_return_bp','pred_ma5','volume_normalizer','train_volume_q99','train_volume_q90',
                'contract_size_base_units','feature_first_bar_end_ts','feature_last_recorded_receipt_ts','strict_feature_available','target_last_receipt_ts','target_actual_start_receipt_ts','target_actual_end_receipt_ts']
        meta = g.iloc[9:][cols].copy().reset_index(drop=True)
        valid = np.isfinite(windows).all(axis=(1,2)) & meta.strict_feature_available.to_numpy()
        train = (meta.decision_ts >= start) & (meta.label_end_ts+60 < train_end) & meta.future_label_available & (meta.target_last_receipt_ts < train_end) & (meta.feature_last_recorded_receipt_ts < train_end) & (meta.decision_ts % 900 == 0)
        val = (stage=='selection') & (meta.decision_ts >= train_end) & (meta.label_end_ts+60 < val_end) & meta.future_label_available & (meta.target_last_receipt_ts < val_end) & (meta.feature_last_recorded_receipt_ts < val_end) & (meta.decision_ts % 300 == 0)
        cal = (stage=='refit') & (meta.decision_ts >= day-7*86400) & (meta.decision_ts < day) & (meta.label_end_ts+60 < day) & meta.future_label_available & (meta.target_last_receipt_ts < day)
        test = (meta.decision_ts >= day+300) & (meta.decision_ts < day+86400) & (meta.decision_ts <= CUTOFF)
        use = valid & (train|val|cal|test).to_numpy()
        meta['is_train']=train;meta['is_validation']=val;meta['is_calibration']=cal;meta['is_test']=test
        meta['split'] = np.select([test,val,cal], ['test','validation','calibration'], default='train')
        X.append(windows[use].copy()); M.append(meta[use])
    return np.concatenate(X), pd.concat(M,ignore_index=True), norms, receipt


class VolumeLSTM(nn.Module):
    def __init__(self, features, hidden):
        super().__init__()
        self.lstm = nn.LSTM(features, hidden, batch_first=True)
        with torch.no_grad():
            self.lstm.bias_hh_l0.zero_()
        self.lstm.bias_hh_l0.requires_grad_(False)
        self.dense = nn.Sequential(nn.Linear(hidden,16),nn.ReLU(),nn.Linear(16,8),nn.ReLU(),nn.Linear(8,1))
    def forward(self,x):
        h,_ = self.lstm(x)
        return self.dense(h[:,-1]).squeeze(-1)


def predict(model,x):
    model.eval()
    with torch.no_grad():
        return np.concatenate([model(torch.from_numpy(x[i:i+4096])).numpy() for i in range(0,len(x),4096)]) if len(x) else np.array([], dtype=np.float32)


def fit_candidate(xtrain, ytrain, xval, yval_log, valoffset, config, seed, epochs, selection):
    torch.manual_seed(seed); np.random.seed(seed)
    model=VolumeLSTM(len(FEATURES),config['hidden']); opt=torch.optim.Adam(model.parameters(),lr=.001)
    xt,yt=torch.from_numpy(xtrain),torch.from_numpy(ytrain)
    gen=torch.Generator().manual_seed(seed)
    curves=[]; beststate=None; bestscore=np.inf; bestepoch=None; fixedstate=None
    for epoch in range(1,epochs+1):
        model.train(); order=torch.randperm(len(xt),generator=gen); total=0
        for j in range(0,len(order),1024):
            b=order[j:j+1024];opt.zero_grad();loss=((model(xt[b])-yt[b])**2).mean();loss.backward();opt.step();total+=float(loss.detach())*len(b)
        rec={'epoch':epoch,'train_standardized_mse':total/len(xt)}
        if selection:
            p=predict(model,xval)*config['target_scale']+config['target_mean']+valoffset
            p=np.maximum(p,0)
            score=float(np.mean((yval_log-p)**2));rec['validation_log_volume_mse']=score
            if score<bestscore:
                bestscore=score;bestepoch=epoch;beststate=copy.deepcopy(model.state_dict())
        if epoch==20 and config['name']=='lstm32_residual' and seed==0:
            fixedstate=copy.deepcopy(model.state_dict())
        curves.append(rec)
    if selection:
        model.load_state_dict(beststate)
    else:
        bestepoch=epochs
    return model, curves, bestscore, bestepoch, fixedstate


def save_model(path, model):
    np.savez_compressed(path,**{k:v.detach().numpy() for k,v in model.state_dict().items()})


def causal_only(df):
    return df.drop(columns=[c for c in df if c.startswith('target_') or c in ['future_label_available','label_end_ts','split','is_train','is_validation','is_calibration','is_test']],errors='ignore')


def fit_fold(frames,day,prior_predictions):
    name=pd.Timestamp(day,unit='s',tz='UTC').strftime('%Y-%m-%d');dest=HERE/'folds'/name;dest.mkdir(parents=True,exist_ok=True)
    Ws,ms,selection_norms,selection_receipts=dataset(frames,day,'selection')
    st=ms.is_train.to_numpy();sv=ms.is_validation.to_numpy();se=ms.is_test.to_numpy()
    assert min(st.sum(),sv.sum())>1000
    selectscale=StandardScaler().fit(Ws[st].reshape(-1,len(FEATURES)))
    sx=np.clip(selectscale.transform(Ws.reshape(-1,len(FEATURES))).reshape(Ws.shape),-10,10).astype('float32');del Ws
    so=ms.pred_ma5.to_numpy();sy=ms.target_log_volume.to_numpy();stats=[];curves={};candidate_test=ms.loc[se,['asset','decision_ts']].copy()
    assert ms.loc[st,'target_last_receipt_ts'].max()<day-5*86400
    assert ms.loc[sv,'target_last_receipt_ts'].max()<day
    for candidate in CANDIDATES:
        c=dict(candidate);target=sy-so if c['target']=='residual' else sy
        c.update(target_mean=float(target[st].mean()),target_scale=float(target[st].std()))
        y=((target-c['target_mean'])/c['target_scale']).astype('float32')
        model,curve,score,epoch,_=fit_candidate(sx[st],y[st],sx[sv],sy[sv],so[sv] if c['target']=='residual' else np.zeros(sv.sum()),c,0,30,True)
        raw=predict(model,sx[se])*c['target_scale']+c['target_mean']+(so[se] if c['target']=='residual' else 0)
        physical=np.maximum(raw,0);candidate_test['candidate_'+c['name']+'_contracts']=np.expm1(np.minimum(physical,50))*ms.loc[se,'volume_normalizer'].to_numpy()
        save_model(dest/f"candidate_{c['name']}_seed0_best.npz",model)
        curves[c['name']]=curve;stats.append({'config':c,'best_epoch':epoch,'validation_log_volume_mse':score,'validation_rows':int(sv.sum()),'raw_negative_test_predictions':int((raw<0).sum())})
    selected=min(stats,key=lambda z:(z['validation_log_volume_mse'],[q['name'] for q in CANDIDATES].index(z['config']['name'])))
    winner=selected['config']['name'];epoch=selected['best_epoch']
    ms.loc[sv].to_parquet(dest/'selection_validation_inputs_and_labels.parquet',index=False)
    np.savez_compressed(dest/'selection_scalers.npz',feature_mean=selectscale.mean_,feature_scale=selectscale.scale_)
    del sx
    W,m,norms,receipts=dataset(frames,day,'refit');tr=m.is_train.to_numpy();te=m.is_test.to_numpy();calbase=m.is_calibration.to_numpy()
    assert tr.sum()>1000 and te.sum()>0
    assert m.loc[tr,'target_last_receipt_ts'].max()<day
    scale=StandardScaler().fit(W[tr].reshape(-1,len(FEATURES)))
    x=np.clip(scale.transform(W.reshape(-1,len(FEATURES))).reshape(W.shape),-10,10).astype('float32');del W
    flat=x.reshape(len(x),-1);offset=m.pred_ma5.to_numpy();truth=m.target_log_volume.to_numpy()
    forecast={};ensemble={};finalconfigs={}
    for variant,config,epochs in [('fixed_rnn',{'name':'lstm32_residual','hidden':32,'target':'residual'},20),('tuned_rnn',dict(selected['config']),epoch)]:
        target=truth-offset if config['target']=='residual' else truth
        c=dict(config);c.update(target_mean=float(target[tr].mean()),target_scale=float(target[tr].std()))
        finalconfigs[variant]=c;y=((target-c['target_mean'])/c['target_scale']).astype('float32');seedpred=[];ensemble[variant]=[]
        for seed in [0,1,2]:
            model,curve,_,_,_=fit_candidate(x[tr],y[tr],None,None,None,c,seed,epochs,False)
            # Current model is never used to produce historical risk-calibration forecasts.
            raw=predict(model,x[te])*c['target_scale']+c['target_mean']+(offset[te] if c['target']=='residual' else 0)
            seedpred.append(raw);save_model(dest/f'{variant}_seed{seed}.npz',model)
            ensemble[variant].append({'seed':seed,'epochs':epochs,'train_curve':curve})
            m.loc[te,f'pred_volume_{variant}_seed{seed}_unconstrained_log']=raw
        raw=np.mean(seedpred,axis=0);physical=np.maximum(raw,0)
        m.loc[te,f'pred_volume_{variant}_unconstrained_log']=raw;m.loc[te,f'pred_volume_{variant}']=physical
        m.loc[te,f'pred_volume_{variant}_contracts']=np.expm1(np.minimum(physical,50))*m.loc[te,'volume_normalizer'].to_numpy()
        forecast[variant]=physical-offset[te]
    rc=finalconfigs['fixed_rnn'];yr=(truth-offset-rc['target_mean'])/rc['target_scale']
    ridge=Ridge(alpha=10).fit(flat[tr],yr[tr]);rr=ridge.predict(flat[te])*rc['target_scale']+rc['target_mean']+offset[te]
    m.loc[te,'pred_volume_ridge_unconstrained_log']=rr;m.loc[te,'pred_volume_ridge']=np.maximum(rr,0)
    m.loc[te,'pred_volume_ridge_contracts']=np.expm1(np.minimum(np.maximum(rr,0),50))*m.loc[te,'volume_normalizer'].to_numpy()
    m.loc[te,'pred_volume_ma5_contracts']=np.expm1(np.minimum(offset[te],50))*m.loc[te,'volume_normalizer'].to_numpy()
    m.loc[te,'pred_volume_lstm']=m.loc[te,'pred_volume_tuned_rnn'];m.loc[te,'pred_volume_innovation']=forecast['tuned_rnn'];m.loc[te,'pred_volume_fixed_innovation']=forecast['fixed_rnn']
    candidate_test=candidate_test.set_index(['asset','decision_ts']);ix=pd.MultiIndex.from_frame(m.loc[te,['asset','decision_ts']])
    for c in CANDIDATES:
        col='candidate_'+c['name']+'_contracts';v=candidate_test[col].reindex(ix).to_numpy();assert np.isfinite(v).all()
        m.loc[te,col]=v;m.loc[te,'candidate_'+c['name']+'_log_volume']=np.log1p(v/m.loc[te,'volume_normalizer'].to_numpy())
    # Only pre-existing daily OOF forecasts can enter risk calibration.
    oof_columns=['asset','decision_ts','pred_volume_fixed_rnn_contracts','pred_volume_tuned_rnn_contracts','pred_volume_ridge_contracts','model_train_end','model_test_day','strict_recorded_pit_eligible']
    cal=np.zeros(len(m),dtype=bool);oof=None
    if prior_predictions:
        past=pd.concat([q[oof_columns] for q in prior_predictions],ignore_index=True)
        past=past[(past.decision_ts>=day-7*86400)&(past.decision_ts<day)].copy()
        assert not past.duplicated(['asset','decision_ts']).any()
        assert past.strict_recorded_pit_eligible.all() and (past.model_train_end<=past.decision_ts).all()
        m['_row']=np.arange(len(m))
        oof=m.loc[calbase,['asset','decision_ts','_row','target_last_receipt_ts']].merge(past,on=['asset','decision_ts'],validate='one_to_one',how='inner')
        assert (oof.target_last_receipt_ts<day).all() and (oof.model_train_end<day).all()
        cal[oof._row.to_numpy()]=True
    risk_available=bool(day>=FIRST_DAY+7*86400)
    if risk_available:
        assert cal.sum()>1000
        assert min(m.loc[cal].groupby('asset').size())>100
    risks={};riskstate={};base_mean=[];base_scale=[]
    for label in ['baseline','augmented','fixed_rnn','ridge_volume']:
        m['pred_abs_return_'+label]=np.nan;m['prior_calibration_risk_q90_'+label]=np.nan
    m['prior_calibration_volume_innovation_q90']=np.nan;m['prior_calibration_rows']=0
    if risk_available:
        rb=StandardScaler().fit(flat[cal]);rx=rb.transform(flat).astype('float32');base_mean=rb.mean_;base_scale=rb.scale_
        extras={}
        for label,variant in [('augmented','tuned_rnn'),('fixed_rnn','fixed_rnn'),('ridge_volume','ridge')]:
            arr=np.full(len(m),np.nan);ci=oof._row.to_numpy();contracts=oof['pred_volume_'+variant+'_contracts'].to_numpy()
            arr[ci]=np.log1p(contracts/m.loc[ci,'volume_normalizer'].to_numpy())-offset[ci]
            arr[te]=m.loc[te,'pred_volume_'+variant].to_numpy()-offset[te]
            extras[label]=arr
        evaluate=cal|te
        for label,extra in [('baseline',None)]+list(extras.items()):
            features=rx if extra is None else np.column_stack([rx,extra]);rs=StandardScaler().fit(features[cal])
            fitz=rs.transform(features[cal]);rm=Ridge(alpha=10).fit(fitz,m.loc[cal,'target_abs_return_bp'].to_numpy())
            pr=np.maximum(rm.predict(rs.transform(features[evaluate])),0);m.loc[evaluate,'pred_abs_return_'+label]=pr
            riskstate[label]={'mean':rs.mean_.tolist(),'scale':rs.scale_.tolist(),'coef':rm.coef_.tolist(),'intercept':float(rm.intercept_)}
        for asset in ASSETS:
            mask=m.asset.eq(asset).to_numpy();cm=cal&mask
            for label in ['baseline','augmented','fixed_rnn','ridge_volume']:
                m.loc[mask,'prior_calibration_risk_q90_'+label]=np.quantile(m.loc[cm,'pred_abs_return_'+label],.90)
            m.loc[mask,'prior_calibration_volume_innovation_q90']=np.quantile(extras['augmented'][cm],.90)
            m.loc[mask,'prior_calibration_rows']=int(cm.sum())
        audit=oof.copy();audit['current_fold']=name;audit['oof_forecast_age_days']=(day-audit.model_train_end)/86400
        audit.to_parquet(dest/'risk_oof_forecast_provenance.parquet',index=False)
        m.loc[cal].to_parquet(dest/'risk_calibration_predictions.parquet',index=False)
    fitok=all(v['normalizer_receipt_late_rows']==0 and v['normalizer_missing_receipts']==0 for v in list(receipts.values())+list(selection_receipts.values()))
    fitok=fitok and bool((m.loc[tr,'target_last_receipt_ts']<day).all() and (ms.loc[st,'target_last_receipt_ts']<day-5*86400).all() and (ms.loc[sv,'target_last_receipt_ts']<day).all())
    assert fitok
    m['strict_model_fit_available']=True;m['strict_recorded_pit_eligible']=m.strict_feature_available;m['risk_available']=risk_available
    m['model_train_start']=day-28*86400;m['model_train_end']=day;m['train_last_label_end']=float(m.loc[tr,'label_end_ts'].max())
    m['selection_train_start']=day-33*86400;m['selection_train_end']=day-5*86400;m['selection_train_last_label_end']=float(ms.loc[st,'label_end_ts'].max())
    m['validation_start']=day-5*86400;m['validation_end_cutoff']=day;m['validation_last_label_end']=float(ms.loc[sv,'label_end_ts'].max())
    m['calibration_start']=day-7*86400;m['calibration_end_cutoff']=day;m['calibration_label_end']=float(m.loc[cal,'label_end_ts'].max()) if risk_available else np.nan
    m['model_test_day']=name;m['selected_config']=winner;m['selected_epoch']=epoch;m['publication_lag_assumed_s']=60
    m['availability_mode']='recorded_postresponse_receipts_latest_refit_OOF_risk';m['source']='kalshi_prod_markets_postresponse_recorded_cumulative_volume_delta'
    output=m.loc[te].drop(columns=['_row'],errors='ignore').copy()
    output.to_parquet(dest/'outer_test_predictions_with_labels.parquet',index=False);causal_only(output).to_parquet(dest/'causal_predictions.parquet',index=False)
    np.savez_compressed(dest/'scalers_and_ridge.npz',feature_mean=scale.mean_,feature_scale=scale.scale_,target_residual_mean=rc['target_mean'],target_residual_scale=rc['target_scale'],ridge_coef=ridge.coef_,ridge_intercept=ridge.intercept_,risk_base_mean=base_mean,risk_base_scale=base_scale)
    metadata={'test_day':name,'selected_config':winner,'selected_epoch':epoch,'selected_validation_mse':selected['validation_log_volume_mse'],
        'selection_train_rows':int(st.sum()),'validation_rows':int(sv.sum()),'refit_train_rows':int(tr.sum()),'train_rows':int(tr.sum()),'calibration_rows':int(cal.sum()) if risk_available else 0,'test_rows':int(te.sum()),
        'train_volume_normalizers':norms,'selection_train_volume_normalizers':selection_norms,'normalizer_receipt_audit':receipts,'selection_normalizer_receipt_audit':selection_receipts,
        'selection_train_last_receipt':float(ms.loc[st,'target_last_receipt_ts'].max()),'validation_last_receipt':float(ms.loc[sv,'target_last_receipt_ts'].max()),'refit_train_last_receipt':float(m.loc[tr,'target_last_receipt_ts'].max()),
        'risk_available':risk_available,'risk_calibration_last_receipt':float(m.loc[cal,'target_last_receipt_ts'].max()) if risk_available else None,
        'candidate_stats':stats,'candidate_all_epoch_curves':curves,'final_configs':finalconfigs,'final_ensemble':ensemble,'risk_models':riskstate,
        'strict_model_fit_available':True,'strict_feature_available_test_rows':int(output.strict_feature_available.sum()),'strict_recorded_pit_eligible_rows':int(output.strict_recorded_pit_eligible.sum()),
        'fixed_rnn_unconstrained_negative_count':int(output.pred_volume_fixed_rnn_unconstrained_log.lt(0).sum()),'tuned_rnn_unconstrained_negative_count':int(output.pred_volume_tuned_rnn_unconstrained_log.lt(0).sum())}
    (dest/'metadata.json').write_text(json.dumps(metadata,indent=2,allow_nan=False))
    return output,metadata

