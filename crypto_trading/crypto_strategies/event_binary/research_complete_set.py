"""W8 causal tape feasibility audit, not an HFT profitability claim.

All recorded BTC/ETH 15M depth over a frozen 14-day interval is inspected.
Contemporaneous taker pair costs include both fees and depth; a sequential
single-clip stress includes every unmatched first leg. Public prints on a
fixed, most-recent sample support the shared W8 queue model at the actual
90-second recorded quote-update cadence. No orders or live state are written.
"""
from __future__ import annotations

import argparse
from collections import defaultdict, Counter
from datetime import datetime, timezone, timedelta
from dataclasses import replace
import gzip
import hashlib
import json
import math
from pathlib import Path
import statistics

from crypto_trading.crypto_common.config import PRICE_DATA, SIGNALS_DIR
from crypto_trading.crypto_common.kalshi.rest_event import KalshiEventClient
from crypto_trading.crypto_strategies.event_binary import complete_set as cs

SERIES = ("KXBTC15M", "KXETH15M")
# Capture the module revision loaded by this process, not a later on-disk edit.
KERNEL_SOURCE = Path(cs.__file__).read_bytes()
RESEARCH_SOURCE = Path(__file__).read_bytes()


def timestamp(value: str) -> float:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def iso(value: float) -> str:
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


def read_lines(path: Path):
    with (gzip.open(path, "rt") if path.suffix == ".gz" else path.open()) as stream:
        for line in stream:
            if line.strip():
                yield json.loads(line)


def normalize_trade(row: dict) -> dict | None:
    """Parse official public trade precision and side fields without guessing."""
    try:
        side = row.get("taker_outcome_side") or row.get("taker_side")
        out = dict(trade_id=row["trade_id"], ticker=row["ticker"],
                   ts=timestamp(row["created_time"]),
                   quantity=float(row["count_fp"]),
                   yes_price=float(row["yes_price_dollars"]),
                   taker_side=side, is_block_trade=bool(row.get("is_block_trade")))
        if side not in ("yes", "no") or out["quantity"] <= 0:
            return None
        if not 0 < out["yes_price"] < 1:
            return None
        return out
    except (KeyError, ValueError, TypeError):
        return None


def load_books(start: float, end: float) -> tuple[dict, dict]:
    grouped = defaultdict(list)
    coverage = dict(files=[], raw_rows=0, valid_rows=0, rejected_books=0,
                    duplicates=0, future_or_outside_lifetime=0)
    seen = set()
    for series in SERIES:
        root = PRICE_DATA / "kalshi/event_strips/prod" / series / "orderbook"
        for path in sorted(root.glob("*.jsonl*")):
            if path.name[:10] < iso(start)[:10] or path.name[:10] > iso(end)[:10]:
                continue
            coverage["files"].append(dict(path=str(path), size=path.stat().st_size,
                                          mtime=iso(path.stat().st_mtime)))
            for row in read_lines(path):
                ts = float(row["recv_ts"])
                if not start <= ts <= end:
                    continue
                coverage["raw_rows"] += 1
                close = timestamp(row["close_time"])
                if not close-900 <= ts < close or close > end-180:
                    coverage["future_or_outside_lifetime"] += 1
                    continue
                key = (row["ticker"], ts)
                if key in seen:
                    coverage["duplicates"] += 1
                    continue
                seen.add(key)
                book = cs.normalize_book(row["ob"])
                if not book:
                    coverage["rejected_books"] += 1
                    continue
                grouped[row["ticker"]].append(dict(ts=ts, close=close, book=book,
                                                    ticker=row["ticker"], series=series))
                coverage["valid_rows"] += 1
    for rows in grouped.values():
        rows.sort(key=lambda r:r["ts"])
    coverage["markets"] = len(grouped)
    return dict(grouped), coverage


def fill_cost(book: dict, side: str, quantity: float, coefficient: float,
              max_price: float = .999) -> dict | None:
    fills = cs.walk_buy(book, side, quantity, max_price=max_price)
    if sum(q for _,q in fills) < quantity-1e-8:
        return None
    cost = sum(p*q for p,q in fills)
    fee = sum(cs.fee_usd(p,q,coefficient) for p,q in fills)
    return dict(cost=cost, fees=fee, all_in=(cost+fee)/quantity, fills=fills)


def simultaneous_pair(book: dict, quantity: float, coefficient: float) -> dict | None:
    y,n = fill_cost(book,"yes",quantity,coefficient),fill_cost(book,"no",quantity,coefficient)
    if y is None or n is None:
        return None
    return dict(quantity=quantity, cost_before_fees=(y["cost"]+n["cost"])/quantity,
                fees_per_pair=(y["fees"]+n["fees"])/quantity,
                all_in_per_pair=y["all_in"]+n["all_in"])


def sequential_taker(rows: list[dict], result: str, *, delayed: bool=False,
                     quantity: float=5., cap: float=.98) -> dict | None:
    """Frozen mechanism stress, not the W8 maker policy.

    At the first eligible snapshot >=15s after open, choose the contemporaneous
    favourite, buy one clip. Delayed mode buys at the next snapshot with an IOC
    limit equal to that original quote's VWAP. A later opposite clip is bought
    only when observed all-in combined cost <=.98; otherwise include the first
    leg's official settlement loss/profit. No hindsight price minimum or result
    is used in entry/pair decisions. The outcome is used only after all events.
    """
    first = None
    for i,row in enumerate(rows):
        remaining = row["close"]-row["ts"]
        if not 90 < remaining <= 885:
            continue
        b=row["book"]
        if b["yes_ask"]-b["yes_bid"] > .15:
            continue
        side="yes" if (b["yes_bid"]+b["yes_ask"])/2 >= .5 else "no"
        quote=fill_cost(b,side,quantity,.07)
        if quote is None:
            continue
        j=i+int(delayed)
        if j>=len(rows) or rows[j]["close"]-rows[j]["ts"]<=90:
            return None
        limit = quote["cost"]/quantity if delayed else .999
        execution=fill_cost(rows[j]["book"],side,quantity,.07,max_price=limit)
        if execution is None:
            return None  # causal IOC price limit failed: not filled later in hindsight
        first=(j,side,execution)
        break
    if first is None:
        return None
    j,side,bought=first
    m=cs.new_market(rows[j]["ticker"],rows[j]["series"],rows[j]["close"],rows[j]["close"]-900)
    for price,q in bought["fills"]:
        cs.add_fill(m,side,q,price,rows[j]["ts"],.07,liquidity="taker_snapshot_model",source="first_clip")
    other="no" if side=="yes" else "yes"
    pair_ts=None
    for row in rows[j+1:]:
        if row["close"]-row["ts"] < 30:
            break
        quote=fill_cost(row["book"],other,quantity,.07)
        if quote is not None and bought["all_in"]+quote["all_in"]<=cap+1e-9:
            for price,q in quote["fills"]:
                cs.add_fill(m,other,q,price,row["ts"],.07,liquidity="taker_snapshot_model",source="complete_set")
            pair_ts=row["ts"]
            break
    out=cs.settle(m,result)
    out.update(fill_model="sequential_taker_snapshot_limit_model" if delayed else "sequential_taker_snapshot_model",
               first_side=side, first_ts=rows[j]["ts"], pair_ts=pair_ts,
               completion_lag_s=pair_ts-rows[j]["ts"] if pair_ts else None)
    return out


def replay_prints(rows: list[dict], trades: list[dict], result: str,
                  parameters: cs.Parameters, *, residual: bool,
                  taker_pairing: bool = True) -> dict:
    """Order updates see only current/past books; fills see only later prints."""
    m=cs.new_market(rows[0]["ticker"],rows[0]["series"],rows[0]["close"],rows[0]["close"]-900)
    prints=sorted(trades,key=lambda t:(t["ts"],t["trade_id"]))
    idx=0
    for row in rows:
        now=row["ts"]
        end=idx
        while end<len(prints) and prints[end]["ts"]<=now:
            end+=1
        cs.process_trades(m,prints[idx:end],parameters)
        m["trade_watermark_ts"] = now
        idx=end
        if m["close_ts"]-now<=parameters.flatten_before_s or m.get("force_flatten"):
            cs.update_quotes(m,row["book"],now,parameters,residual=residual)
            cs.flatten(m,row["book"],now,parameters)
        else:
            if taker_pairing:
                cs.take_pair(m,row["book"],now,parameters,residual=residual)
            cs.update_quotes(m,row["book"],now,parameters,residual=residual)
    cs.process_trades(m,[t for t in prints[idx:] if t["ts"]<m["close_ts"]],parameters)
    out=cs.settle(m,result)
    out.update(quote_snapshots=len(rows), public_prints=len(prints),
               fill_model="public_print_queue_model_at_recorded_90s_quote_cadence",
               residual_enabled=residual, taker_pairing_enabled=taker_pairing,
               order_quotes=m["sequence"],
               pairs=m["pairs"], fill_details=m["fills"])
    return out


def summarize(rows: list[dict]) -> dict:
    if not rows:
        return dict(markets=0)
    out=dict(markets=len(rows))
    for k in ["net_usd","cost_usd","fees_usd","payout_usd","quantity","paired_quantity","paired_net_usd","residual_net_usd","residual_quantity"]:
        out[k]=sum(r[k] for r in rows)
    q=out["paired_quantity"]
    out["pair_cost_vwap"]=1-out["paired_net_usd"]/q if q else None
    out["markets_with_pairs"]=sum(r["paired_quantity"]>0 for r in rows)
    out["markets_with_unpaired_inventory"]=sum(r["residual_quantity"]>0 for r in rows)
    out["markets_with_fills"]=sum(r["quantity"]>0 for r in rows)
    out["fills"]=sum(r["fills"] for r in rows)
    groups=defaultdict(float)
    for r in rows:groups[r["close_ts"]]+=r["net_usd"]
    values=list(groups.values())
    out["windows"]=len(values)
    out["mean_window_usd"]=statistics.mean(values)
    se=statistics.stdev(values)/math.sqrt(len(values)) if len(values)>1 else None
    out["window_t"]=out["mean_window_usd"]/se if se else None
    running=peak=mdd=0.
    for _,pnl in sorted(groups.items()):
        running+=pnl;peak=max(peak,running);mdd=max(mdd,peak-running)
    out["max_drawdown_usd"]=mdd
    out["by_series"]={s:{"markets":sum(r["series"]==s for r in rows),"net_usd":sum(r["net_usd"] for r in rows if r["series"]==s)} for s in SERIES}
    assert abs(out["net_usd"]-out["paired_net_usd"]-out["residual_net_usd"])<1e-6
    return out


def quantiles(values: list[float]) -> dict:
    values=sorted(values)
    if not values:return dict(n=0)
    return dict(n=len(values),min=values[0],p10=values[int((len(values)-1)*.1)],
                median=statistics.median(values),p90=values[int((len(values)-1)*.9)],max=values[-1])


def fetch_market_evidence(client, output: Path, start: float, end: float) -> dict:
    evidence=dict(fetched_at=iso(datetime.now(timezone.utc).timestamp()),requests=[],markets=[])
    for series in SERIES:
        cursor=None
        for _ in range(8):
            params=dict(series_ticker=series,status="settled",limit=1000)
            if cursor:params["cursor"]=cursor
            page=client._get("/markets",params)
            markets=page.get("markets",[])
            evidence["requests"].append(dict(path="/markets",method="GET",series=series,records=len(markets)))
            evidence["markets"].extend(m for m in markets if m.get("result") in ("yes","no") and start<=timestamp(m["close_time"])<=end)
            cursor=page.get("cursor")
            if not cursor or markets and min(timestamp(m["close_time"]) for m in markets)<start:
                break
        else:
            raise RuntimeError("Market pagination incomplete")
    (output/"official_markets.json").write_text(json.dumps(evidence,indent=2))
    return evidence


def fetch_prints(client, ticker: str, close: float) -> tuple[list,dict]:
    allrows=[];cursor=None;pages=0
    while pages<40:
        params=dict(ticker=ticker,min_ts=int(close-900),max_ts=int(close),limit=1000)
        if cursor:params["cursor"]=cursor
        page=client._get("/markets/trades",params)
        allrows.extend(page.get("trades",[]));pages+=1;cursor=page.get("cursor")
        if not cursor:break
    if cursor:raise RuntimeError(f"Trade pagination incomplete: {ticker}")
    unique={r["trade_id"]:r for r in allrows}
    parsed=[normalize_trade(r) for r in unique.values()]
    valid=[r for r in parsed if r is not None]
    return valid,dict(ticker=ticker,method="GET",path="/markets/trades",pages=pages,
                      raw_records=len(allrows),unique_records=len(unique),parsed=len(valid),
                      block_trades=sum(r.get("is_block_trade",False) for r in valid),raw=list(unique.values()))


def main(argv=None) -> int:
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--output",type=Path,default=SIGNALS_DIR/"research/w8_complete_set")
    ap.add_argument("--days",type=int,default=14)
    ap.add_argument("--cutoff",help="UTC ISO timestamp; defaults to now")
    ap.add_argument("--print-markets-per-series",type=int,default=16)
    ap.add_argument("--offline",action="store_true",help="Reuse output's saved official markets and trade prints")
    ap.add_argument("--maker-only",action="store_true",help="Reproduce initial maker-only mechanism diagnostic")
    args=ap.parse_args(argv)
    output=args.output;output.mkdir(parents=True,exist_ok=True)
    (output/"kernel_snapshot.py.txt").write_bytes(KERNEL_SOURCE)
    (output/"research_snapshot.py.txt").write_bytes(RESEARCH_SOURCE)
    end=timestamp(args.cutoff) if args.cutoff else datetime.now(timezone.utc).timestamp()
    start=end-args.days*86400
    grouped,coverage=load_books(start,end)
    client=KalshiEventClient(env="prod")
    evidence=json.loads((output/"official_markets.json").read_text()) if args.offline else fetch_market_evidence(client,output,start,end)
    outcomes={m["ticker"]:m["result"] for m in evidence["markets"]}
    coverage["markets_with_official_result"]=len(set(outcomes)&set(grouped))
    coverage["unresolved_markets"]=sorted(set(grouped)-set(outcomes))
    # Outcomes are not read until a policy has completed its chronological loop.
    simultaneous={str(q):[] for q in (5.,25.)}
    contemporaneous_rows=[]
    for ticker,rows in grouped.items():
        for row in rows:
            for q in (5.,25.):
                pair=simultaneous_pair(row["book"],q,.07)
                if pair:
                    simultaneous[str(q)].append(pair["all_in_per_pair"])
                    contemporaneous_rows.append(dict(ticker=ticker,ts=row["ts"],**pair))
    seq={"same_snapshot":[],"one_snapshot_delay_limit":[]}
    for ticker,rows in grouped.items():
        if ticker not in outcomes:continue
        for name,delay in [("same_snapshot",False),("one_snapshot_delay_limit",True)]:
            row=sequential_taker(rows,outcomes[ticker],delayed=delay)
            if row:seq[name].append(row)
    print_root=output/"public_trades";print_root.mkdir(exist_ok=True)
    p=cs.Parameters();replays={"paired_only":[],"residual_proxy":[]};print_meta=[]
    selected=[]
    for series in SERIES:
        eligible=[t for t,r in grouped.items() if r[0]["series"]==series and t in outcomes and len(r)>=5]
        selected.extend(sorted(eligible,key=lambda t:grouped[t][0]["close"],reverse=True)[:args.print_markets_per_series])
    for number,ticker in enumerate(selected,1):
        rows=grouped[ticker];path=print_root/(ticker+".json")
        if args.offline:
            raw=json.loads(path.read_text());trades=[normalize_trade(r) for r in raw["raw"]];trades=[t for t in trades if t]
        else:
            trades,raw=fetch_prints(client,ticker,rows[0]["close"]);path.write_text(json.dumps(raw,indent=2))
        print_meta.append({k:v for k,v in raw.items() if k!="raw"})
        for name,residual in [("paired_only",False),("residual_proxy",True)]:
            replays[name].append(replay_prints(rows,trades,outcomes[ticker],p,residual=residual,
                                              taker_pairing=not args.maker_only))
        print(f"public-print replay {number}/{len(selected)} {ticker}: {len(trades)} prints",flush=True)
    out=dict(generated_at=iso(datetime.now(timezone.utc).timestamp()),start_utc=iso(start),cutoff_utc=iso(end),
             coverage=coverage,parameters=p.as_dict(),kernel_sha256=hashlib.sha256(KERNEL_SOURCE).hexdigest(),
             research_sha256=hashlib.sha256(RESEARCH_SOURCE).hexdigest(),
             taker_pairing_enabled=not args.maker_only,
             simultaneous_taker={q:{**quantiles(v),"observations_below_one":sum(x<1 for x in v),"observations_at_or_below_098":sum(x<=.98 for x in v)} for q,v in simultaneous.items()},
             sequential_taker={k:summarize(v) for k,v in seq.items()},
             public_print_queue={k:summarize(v) for k,v in replays.items()},public_print_requests=print_meta,
             limits=["Historical books were recorded roughly every90 seconds: cannot prove queue position or subsecond HFT, nor emulate continuous20-second repricing.",
                     "Contemporaneous taker YES+NO asks exceed1 before fees on every valid uncrossed book by construction; this is an executable-cost check, not a free-money arbitrage search.",
                     "Sequential-taker stress is a separate fixed one-clip diagnostic, not the maker W8 execution policy or the unknown Polymarket model.",
                     "All unmatched first-leg settlement gains/losses are included. Outcome data are never consulted by entry, quote, pair or fill decisions.",
                     "Public-print queue replay remains counterfactual and assumes our clip did not change other traders; cancellations do not advance queue without observed prints.",
                     "Market result coverage is explicit; missing outcomes are not inferred from spot and are not silently reported aszeroPnL.",
                     "Recent public-print sample is selected mechanically by latest complete market closes, not profitability. Two policies are diagnostics, not a multiple-testing-qualified winner.",
                     "Fee inputs follow sharedkernel assumptions; no rebates, funding, or privileged venue terms assumed."],
             sources=["https://docs.kalshi.com/api-reference/market/get-trades","https://docs.kalshi.com/getting_started/order_direction"])
    for name,data in [("sequential_rows",seq),("public_print_replay_rows",replays),("contemporaneous_pairs",contemporaneous_rows)]:
        (output/(name+".json")).write_text(json.dumps(data,indent=2))
    (output/"summary.json").write_text(json.dumps(out,indent=2))
    print(json.dumps({k:out[k] for k in ["start_utc","cutoff_utc","simultaneous_taker","sequential_taker","public_print_queue"]},indent=2))
    return 0


if __name__=="__main__":
    raise SystemExit(main())
