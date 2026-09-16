import { useEffect, useMemo, useState } from "react";
import {
  Activity,
  BookOpen,
  ClipboardList,
  HeartPulse,
  Landmark,
  Layers,
  LineChart as LineChartIcon,
  Target,
  type LucideIcon,
} from "lucide-react";
import {
  Area,
  AreaChart,
  CartesianGrid,
  Line,
  LineChart,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import type { ArtifactType, Market, Strategy } from "./types";
import { useCryptoPrediction } from "./CryptoContext";
import HealthIssues from "./HealthIssues";
import { runtimeFreshness } from "./healthSemantics";
import { RESULT_BOOKS, resultNetLabel, resultDrawdown, selectResultBook } from "./resultBooks";
import {
  age,
  date,
  Empty,
  Metric,
  money,
  number,
  orderStatusLabel,
  Pagination,
  Panel,
  pnlClass,
  price,
  publicText,
  ratio,
  Segments,
  Status,
  statusLabel,
  Ticker,
  underlyingPrice,
} from "./primitives";
import "./artifacts.css";

export const ARTIFACTS: {
  type: ArtifactType;
  title: string;
  description: string;
  group: string;
  icon: LucideIcon;
}[] = [
  {
    type: "performance",
    title: "收益与回撤",
    description: "纸面策略评估与独立交易所账本",
    group: "交易表现",
    icon: LineChartIcon,
  },
  {
    type: "orders",
    title: "订单与成交",
    description: "逐笔入场、退出与实际成交数量",
    group: "交易表现",
    icon: ClipboardList,
  },
  {
    type: "positions",
    title: "当前持仓",
    description: "真实归属、双边数量与退出状态",
    group: "交易表现",
    icon: Layers,
  },
  {
    type: "settlements",
    title: "结算账本",
    description: "官方结果、兑付与扣费净收益",
    group: "交易表现",
    icon: Landmark,
  },
  {
    type: "execution",
    title: "执行质量",
    description: "信号、订单与张数的不同成交分母",
    group: "市场与执行",
    icon: Activity,
  },
  {
    type: "markets",
    title: "二元市场",
    description: "精确合约、Prod 行情与结算时间",
    group: "市场与执行",
    icon: Target,
  },
  {
    type: "rules",
    title: "策略规则与验证",
    description: "生效参数、交易机制与固定验收",
    group: "策略与运行",
    icon: BookOpen,
  },
  {
    type: "health",
    title: "运行与数据健康",
    description: "逐源更新时间、核验范围与问题",
    group: "策略与运行",
    icon: HeartPulse,
  },
];
const PAGE_SIZE = 12;
const byNewest = <T extends { at?: string | null }>(rows: T[]) =>
  [...rows].sort((a, b) =>
    String(b.at ?? "").localeCompare(String(a.at ?? "")),
  );
const shortTime = (value: string) => {
  const d = new Date(value);
  return Number.isNaN(d.getTime())
    ? value
    : `${d.toISOString().slice(5, 10)} ${d.toISOString().slice(11, 16)}`;
};

function PerformanceView({ strategy }: { strategy: Strategy }) {
  const { resultBasis: basis, setResultBasis: setBasis } = useCryptoPrediction();
  const book = selectResultBook(strategy, basis);
  const data = book.performance;
  const curve = useMemo(
    () => [...(data?.curve ?? [])].sort((a, b) => a.at.localeCompare(b.at)),
    [data?.curve],
  );
  const worstDrawdown = resultDrawdown(data);
  const selector = (
    <div className="cm-toolbar">
      <Segments value={basis} onChange={setBasis} label="收益账本" options={[...RESULT_BOOKS]} />
      {data && <Status value={data.status} />}
    </div>
  );
  if (!data) return <>
    {selector}
    <Panel title={book.title}>
      <Empty>尚未接入 Kalshi Prod 交易账本。</Empty>
      <p className="cm-prose">{book.role}</p>
      <p className="cm-note">当前下单环境仍是 Kalshi Demo。这里仅切换结果视图，不会启动或切换交易；接入 Prod 后将单列其订单、持仓和结算，保留纸面与 Demo 历史。</p>
    </Panel>
  </>;
  return (
    <>
      {selector}
      <div className="cm-source-banner"><strong>{book.title}</strong><span>{basis === "paper" ? "主评估口径" : "独立执行账本"}</span></div>
      <p className="cm-note">
        {book.role} {basis === "demo" && "只统计本策略归属且已核验的结算；未结算仓位不计入已实现收益。"}{" "}
        {publicText(data.scope)}
      </p>
      <div className="cm-metrics">
        <Metric
          label={resultNetLabel(basis, data)}
          value={money(data.net_pnl_usd)}
          tone={pnlClass(data.net_pnl_usd)}
        />
        <Metric
          label={basis === "demo" ? "Demo 实际费用" : "纸面模型费用"}
          value={money(data.fees_usd)}
        />
        <Metric label="结算记录" value={number(data.settled_count, 0)} />
        <Metric label="未平仓记录" value={number(data.open_count, 0)} />
        <Metric
          label="曲线最大回撤"
          value={money(worstDrawdown)}
          tone={pnlClass(worstDrawdown)}
        />
        <Metric label="独立观察窗口" value={number(data.sample_windows, 0)} />
      </div>
      <p className="cm-note">
        来源时间：{date(data.source_as_of)}。待解决 / 未核验记录：
        {number(data.unresolved_count, 0)}。{publicText(data.note)}
      </p>
      {curve.length === 0 ? (
        <Empty>当前账本没有可核验的收益曲线；未使用其他账本替代。</Empty>
      ) : (
        <>
          <Panel
            title="累计已结算净收益"
            aside={<span className="cm-meta">USD · UTC</span>}
          >
            <div
              className="cm-chart"
              role="img"
              aria-label={`${strategy.acronym} ${basis === "demo" ? "Demo" : "纸面"}累计净收益曲线，共${curve.length}个结算点`}
            >
              <ResponsiveContainer
                width="100%"
                height="100%"
                minWidth={0}
                initialDimension={{ width: 400, height: 260 }}
              >
                <LineChart
                  data={curve}
                  margin={{ top: 12, right: 14, bottom: 10, left: 8 }}
                >
                  <CartesianGrid strokeDasharray="3 3" stroke="var(--hairline)" />
                  <XAxis
                    dataKey="at"
                    tickFormatter={shortTime}
                    minTickGap={36}
                    stroke="var(--ink-dim)"
                  />
                  <YAxis tickFormatter={(value) => `$${value}`} width={64} stroke="var(--ink-dim)" />
                  <Tooltip
                    contentStyle={{ background: "var(--paper)", color: "var(--ink)", border: "1px solid var(--ink)" }}
                    labelStyle={{ color: "var(--ink-dim)" }}
                    itemStyle={{ color: "var(--ink)" }}
                    labelFormatter={(label) => date(String(label))}
                    formatter={(value) => [
                      money(typeof value === "number" ? value : null),
                      "累计净收益",
                    ]}
                  />
                  <ReferenceLine y={0} stroke="var(--ink-mute)" />
                  <Line
                    type="linear"
                    dataKey="cumulative_usd"
                    stroke="var(--ink)"
                    strokeWidth={2}
                    dot={curve.length === 1}
                    isAnimationActive={false}
                  />
                </LineChart>
              </ResponsiveContainer>
            </div>
            <p className="cm-note">
              {date(curve[0].at)} — {date(curve[curve.length - 1].at)}
            </p>
          </Panel>
          <Panel title="已实现收益回撤">
            <div
              className="cm-chart cm-chart-short"
              role="img"
              aria-label={`${strategy.acronym}已实现收益回撤曲线`}
            >
              <ResponsiveContainer
                width="100%"
                height="100%"
                minWidth={0}
                initialDimension={{ width: 400, height: 200 }}
              >
                <AreaChart
                  data={curve}
                  margin={{ top: 10, right: 14, bottom: 10, left: 8 }}
                >
                  <CartesianGrid strokeDasharray="3 3" stroke="var(--hairline)" />
                  <XAxis
                    dataKey="at"
                    tickFormatter={shortTime}
                    minTickGap={36}
                    stroke="var(--ink-dim)"
                  />
                  <YAxis tickFormatter={(value) => `$${value}`} width={64} stroke="var(--ink-dim)" />
                  <Tooltip
                    contentStyle={{ background: "var(--paper)", color: "var(--ink)", border: "1px solid var(--ink)" }}
                    labelStyle={{ color: "var(--ink-dim)" }}
                    itemStyle={{ color: "var(--error)" }}
                    labelFormatter={(label) => date(String(label))}
                    formatter={(value) => [
                      money(typeof value === "number" ? value : null),
                      "回撤",
                    ]}
                  />
                  <ReferenceLine y={0} stroke="var(--ink-mute)" />
                  <Area
                    type="linear"
                    dataKey="drawdown_usd"
                    stroke="var(--error)"
                    fill="var(--error)"
                    fillOpacity={0.15}
                    isAnimationActive={false}
                  />
                </AreaChart>
              </ResponsiveContainer>
            </div>
            <p className="cm-note">
              相对于曲线此前最高累计收益，回撤以美元表示；没有假设初始本金或年化回报。
            </p>
          </Panel>
        </>
      )}
      {data.verdict !== null && (
        <Panel title="已记录的验证结论">
          <p className="cm-prose">{publicText(data.verdict)}</p>
        </Panel>
      )}
    </>
  );
}

function OrdersView({ strategy }: { strategy: Strategy }) {
  const [kind, setKind] = useState<"all" | "entry" | "exit">("all");
  const [status, setStatus] = useState("all");
  const [page, setPage] = useState(0);
  const [selected, setSelected] = useState<string | null>(null);
  const statuses = [
    ...new Set(strategy.orders.map((order) => order.status)),
  ].sort();
  const rows = byNewest(
    strategy.orders.filter(
      (order) =>
        (kind === "all" || order.entry === (kind === "entry")) &&
        (status === "all" || order.status === status),
    ),
  );
  const detail = strategy.orders.find((order) => order.id === selected);
  const safePage = Math.min(
    page,
    Math.max(0, Math.ceil(rows.length / PAGE_SIZE) - 1),
  );
  return (
    <>
      <div className="cm-toolbar">
        <Segments
          value={kind}
          label="订单用途"
          options={[
            { value: "all", label: "全部订单" },
            { value: "entry", label: "入场" },
            { value: "exit", label: "退出对冲" },
          ]}
          onChange={(value) => {
            setKind(value);
            setPage(0);
            setSelected(null);
          }}
        />
        <label className="cm-filter">
          状态
          <select
            value={status}
            onChange={(event) => {
              setStatus(event.target.value);
              setPage(0);
              setSelected(null);
            }}
          >
            <option value="all">全部状态</option>
            {statuses.map((value) => (
              <option key={value} value={value}>
                {orderStatusLabel(value)}
              </option>
            ))}
          </select>
        </label>
      </div>
      <p className="cm-note">
        Kalshi Demo ·
        数量单位为张。退出单是买入对侧合约以抵消净仓，不是卖出；待核验记录不代表已成交。
      </p>
      {rows.length === 0 ? (
        <Empty>该筛选条件下没有订单记录。</Empty>
      ) : (
        <div className="cm-table-scroll">
          <table className="table cm-table">
            <thead>
              <tr>
                <th>下单时间 / 合约</th>
                <th>用途 / 方向</th>
                <th>限价</th>
                <th>申请 / 成交</th>
                <th>费用</th>
                <th>状态</th>
                <th>核对</th>
              </tr>
            </thead>
            <tbody>
              {rows
                .slice(safePage * PAGE_SIZE, (safePage + 1) * PAGE_SIZE)
                .map((order) => (
                  <tr key={order.id}>
                    <td>
                      <div>{date(order.at)}</div>
                      <Ticker value={order.ticker} />
                    </td>
                    <td>
                      {order.entry ? "入场" : "退出对冲"}
                      <br />
                      <b>{order.side.toUpperCase()}</b>
                    </td>
                    <td>{price(order.price)}</td>
                    <td>
                      {number(order.quantity)} / {number(order.filled)}
                    </td>
                    <td>{money(order.fees_usd)}</td>
                    <td>
                      <Status
                        value={order.status}
                        label={orderStatusLabel(order.status)}
                      />
                      <small className="cm-cell-note">
                        {order.verified ? "成交已核验" : "成交待核验"}
                      </small>
                    </td>
                    <td>
                      <button
                        className="cm-link-button"
                        type="button"
                        aria-label={`查看 ${order.asset} ${order.side.toUpperCase()} 订单详情`}
                        onClick={() =>
                          setSelected(selected === order.id ? null : order.id)
                        }
                      >
                        {selected === order.id ? "收起" : "详情"}
                      </button>
                    </td>
                  </tr>
                ))}
            </tbody>
          </table>
        </div>
      )}
      <Pagination
        count={rows.length}
        page={safePage}
        pageSize={PAGE_SIZE}
        onChange={setPage}
      />
      {detail && (
        <Panel title="订单核对">
          <dl className="cm-details">
            <dt>订单标识</dt>
            <dd className="cm-break">{detail.id}</dd>
            <dt>精确合约</dt>
            <dd className="cm-break">{detail.ticker}</dd>
            <dt>到期时间</dt>
            <dd>{date(detail.close_at)}</dd>
            <dt>已成交成本</dt>
            <dd>{money(detail.cost_usd)}</dd>
            <dt>实际费用</dt>
            <dd>{money(detail.fees_usd)}</dd>
            <dt>核验状态</dt>
            <dd>
              {detail.verified
                ? "已按实际成交记录核验"
                : "尚未完成核验，不能视为最终成交"}
            </dd>
          </dl>
        </Panel>
      )}
    </>
  );
}

function PositionsView({ strategy }: { strategy: Strategy }) {
  return (
    <>
      <p className="cm-note">
        仅本策略 Demo 归属仓位。净仓 = YES 数量 − NO 数量；正数偏 YES，负数偏
        NO。双边配对数量单列，结算前不提前记为收益。
      </p>
      {strategy.positions.length === 0 ? (
        <Empty>
          {strategy.demo.status === "verified"
            ? "当前可核验账本中没有未结算持仓。"
            : "当前快照未提供可确认的持仓记录，请结合数据健康检查覆盖范围。"}
        </Empty>
      ) : (
        strategy.positions.map((position) => (
          <Panel
            key={position.ticker}
            title={`${position.asset} · 二元合约`}
            aside={<Status value={position.exit_status} />}
          >
            <Ticker value={position.ticker} />
            <p className="cm-note">
              {position.verified ? "仓位已核验" : "仓位待核验"} · 来源：
              {date(position.source_as_of)}
            </p>
            <div className="cm-metrics cm-position-metrics">
              <Metric label="YES 持有" value={number(position.yes_quantity)} />
              <Metric label="NO 持有" value={number(position.no_quantity)} />
              <Metric label="已配对" value={number(position.paired_quantity)} />
              <Metric
                label="净仓（YES − NO）"
                value={number(position.net_quantity)}
              />
              <Metric label="已成交成本" value={money(position.cost_usd)} />
              <Metric label="累计费用" value={money(position.fees_usd)} />
            </div>
            <p className="cm-note">
              到期：{date(position.close_at)}
              。浮动盈亏未展示：当前数据合同未提供经过核验的可执行平仓估值。
            </p>
          </Panel>
        ))
      )}
    </>
  );
}

function SettlementsView({
  strategy,
  marketTicker,
}: {
  strategy: Strategy;
  marketTicker?: string;
}) {
  const [page, setPage] = useState(0);
  const [asset, setAsset] = useState("all");
  const [exactTicker, setExactTicker] = useState(marketTicker ?? "");
  useEffect(() => {
    setExactTicker(marketTicker ?? "");
    setAsset("all");
    setPage(0);
  }, [marketTicker]);
  const rows = byNewest(
    strategy.settlements.filter(
      (row) =>
        (asset === "all" || row.asset === asset) &&
        (!exactTicker.trim() || row.ticker === exactTicker.trim()),
    ),
  );
  const safePage = Math.min(
    page,
    Math.max(0, Math.ceil(rows.length / PAGE_SIZE) - 1),
  );
  return (
    <>
      <div className="cm-toolbar">
        <label className="cm-filter">
          币种
          <select
            value={asset}
            onChange={(event) => {
              setAsset(event.target.value);
              setPage(0);
            }}
          >
            <option value="all">全部币种</option>
            {[...new Set(strategy.settlements.map((row) => row.asset))]
              .sort()
              .map((value) => (
                <option key={value}>{value}</option>
              ))}
          </select>
        </label>
        <Status value={strategy.demo.status} />
      </div>
      <label className="cm-filter cm-search">
        精确合约代码
        <input
          value={exactTicker}
          placeholder="输入完整 ticker 精确匹配"
          spellCheck={false}
          onChange={(event) => {
            setExactTicker(event.target.value);
            setPage(0);
          }}
        />
      </label>
      {exactTicker && (
        <button
          type="button"
          className="cm-link-button"
          onClick={() => {
            setExactTicker("");
            setPage(0);
          }}
        >
          清除合约筛选
        </button>
      )}
      <p className="cm-note">
        Demo 官方结算方向与本策略实际成交归属。净收益 = 兑付 − 成交成本 −
        费用。表内没有将账户其他策略的结算收益分摊到本策略。
      </p>
      {rows.length === 0 ? (
        <Empty>该筛选条件下没有已核验的结算记录。</Empty>
      ) : (
        <>
          <div className="cm-metrics">
            <Metric label="当前筛选结算记录" value={number(rows.length, 0)} />
            <Metric
              label="表内净收益合计"
              value={money(rows.reduce((sum, row) => sum + row.net_usd, 0))}
              detail="仅当前筛选可见记录"
            />
          </div>
          <div className="cm-table-scroll">
            <table className="table cm-table">
              <thead>
                <tr>
                  <th>结算时间 / 合约</th>
                  <th>官方结果</th>
                  <th>成交张数</th>
                  <th>成本</th>
                  <th>费用</th>
                  <th>兑付</th>
                  <th>净收益</th>
                </tr>
              </thead>
              <tbody>
                {rows
                  .slice(safePage * PAGE_SIZE, (safePage + 1) * PAGE_SIZE)
                  .map((row, index) => (
                    <tr key={`${row.ticker}:${row.at}:${index}`}>
                      <td>
                        <div>{date(row.at)}</div>
                        <Ticker value={row.ticker} />
                      </td>
                      <td>{row.result.toUpperCase()}</td>
                      <td>{number(row.quantity)}</td>
                      <td>{money(row.cost_usd)}</td>
                      <td>{money(row.fees_usd)}</td>
                      <td>{money(row.payout_usd)}</td>
                      <td className={pnlClass(row.net_usd)}>
                        {money(row.net_usd)}
                      </td>
                    </tr>
                  ))}
              </tbody>
            </table>
          </div>
        </>
      )}
      <Pagination
        page={safePage}
        count={rows.length}
        pageSize={PAGE_SIZE}
        onChange={setPage}
      />
      <p className="cm-note">
        待解决 / 未核验记录：{number(strategy.demo.unresolved_count, 0)}
        。到期但结果未确认的记录未混入已结算净收益。
      </p>
      <p className="cm-note">
        账本范围：{publicText(strategy.demo.scope)}。
        {publicText(strategy.demo.note)}
      </p>
    </>
  );
}

function ExecutionView({ strategy }: { strategy: Strategy }) {
  const [period, setPeriod] = useState<"recent" | "all">("recent");
  const [kind, setKind] = useState<"entry" | "exit">("entry");
  const data =
    kind === "entry"
      ? strategy.execution[period]
      : strategy.execution.exits[period];
  return (
    <>
      <div className="cm-toolbar">
        <Segments
          value={period}
          label="执行统计范围"
          options={[
            { value: "recent", label: "最近 48 小时" },
            { value: "all", label: "全部可核验期间" },
          ]}
          onChange={setPeriod}
        />
        <Segments
          value={kind}
          label="执行用途"
          options={[
            { value: "entry", label: "入场" },
            { value: "exit", label: "退出对冲" },
          ]}
          onChange={setKind}
        />
      </div>
      <p className="cm-note">
        {publicText(data.scope)}。
        {data.since === null ? "起始时间未提供" : `起始：${date(data.since)}`}
        ；来源截至 {date(data.source_as_of)}。
      </p>
      <Panel title={kind === "entry" ? "入场执行" : "退出对冲执行"}>
        <div className="cm-metrics">
          <Metric label="信号 / 尝试" value={number(data.signals, 0)} />
          <Metric label="被接受订单" value={number(data.accepted, 0)} />
          <Metric label="有成交订单" value={number(data.filled_orders, 0)} />
          <Metric
            label="实际成交张数"
            value={number(data.filled_contracts)}
            detail={`请求 ${number(data.requested_contracts)} 张`}
          />
        </div>
        <div className="cm-metrics">
          <Metric
            label="信号成交比例"
            value={ratio(data.filled_orders, data.signals)}
            detail={`${number(data.filled_orders, 0)} 有成交订单 / ${number(data.signals, 0)} 信号或尝试`}
          />
          <Metric
            label="已接受订单成交比例"
            value={ratio(data.filled_orders, data.accepted)}
            detail={`${number(data.filled_orders, 0)} 有成交 / ${number(data.accepted, 0)} 被接受`}
          />
          <Metric
            label="张数成交比例"
            value={ratio(data.filled_contracts, data.requested_contracts)}
            detail={`${number(data.filled_contracts)} 成交 / ${number(data.requested_contracts)} 请求`}
          />
        </div>
      </Panel>
      <Panel title="成交与未执行原因">
        <div className="cm-table-scroll">
          <table className="table cm-table">
            <thead>
              <tr>
                <th>类别</th>
                <th>数量</th>
                <th>解释</th>
              </tr>
            </thead>
            <tbody>
              {[
                ["完整成交", data.full_fills, "申请数量全部成交"],
                [
                  "部分成交",
                  data.partial_fills,
                  "实际成交大于零、少于申请数量",
                ],
                ["零成交", data.zero_fills, "订单已接受但最终未成交"],
                [
                  "所需方向空盘口",
                  data.empty_side,
                  "已确认该方向没有可执行对手盘",
                ],
                ["数据 / 请求不可用", data.unavailable, "不能解读为空盘口"],
                ["其他跳过", data.other_skips, "不符合执行条件或其他记录原因"],
              ].map(([label, value, note]) => (
                <tr key={String(label)}>
                  <td>{label}</td>
                  <td>{number(value as number | null, 0)}</td>
                  <td>{note}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <p className="cm-note">
          缺失值显示“—”，不按零计算；各项是否完整及是否覆盖同一阶段，以来源说明为准。
          {publicText(data.note)}
        </p>
      </Panel>
      <p className="cm-note">
        入场与退出独立统计。退出可能为同一持仓持续重试，尝试数不等于独立持仓数；本页不将两类分母合并。
      </p>
    </>
  );
}

function MarketDetail({
  market,
  strategy,
  now,
}: {
  market: Market;
  strategy: Strategy;
  now: number;
}) {
  const orders = strategy.orders.filter(
    (order) => order.ticker === market.ticker,
  );
  const positions = strategy.positions.filter(
    (position) => position.ticker === market.ticker,
  );
  return (
    <Panel
      title={`${market.asset} · 合约详情`}
      aside={<Status value={market.status} />}
    >
      <dl className="cm-details">
        <dt>精确合约代码</dt>
        <dd className="cm-break">{market.ticker}</dd>
        <dt>结束时间</dt>
        <dd>{date(market.close_at)}</dd>
        <dt>参考阈值</dt>
        <dd>{underlyingPrice(market.threshold)}</dd>
        <dt>录制时现货估值</dt>
        <dd>{underlyingPrice(market.spot)}</dd>
        <dt>官方结果</dt>
        <dd>
          {market.result === null
            ? market.close_at !== null &&
              new Date(market.close_at).getTime() <= now
              ? "已到期，官方结果待确认"
              : "尚无结果"
            : market.result.toUpperCase()}
        </dd>
        <dt>行情环境</dt>
        <dd>Kalshi Prod · 只读行情</dd>
        <dt>行情时间</dt>
        <dd>
          {date(market.quote_at)} · {age(market.quote_at, now)}
        </dd>
        <dt>盘口状态</dt>
        <dd>
          <Status value={market.quote_status} />
        </dd>
        <dt>行情来源</dt>
        <dd>
          {market.quote_source === "orderbook"
            ? "实际盘口买单阶梯换算"
            : market.quote_source === "market_summary"
              ? "市场摘要报价，非盘口深度"
              : "行情不可用"}
        </dd>
        <dt>本策略 Demo 订单</dt>
        <dd>{orders.length} 条</dd>
        <dt>本策略 Demo 持仓记录</dt>
        <dd>{positions.length} 条</dd>
      </dl>
      <div className="cm-metrics">
        <Metric
          label="Prod 买入 YES"
          value={price(market.yes_ask)}
          detail={`最优档 ${number(market.yes_quantity)} 张`}
        />
        <Metric
          label="Prod 买入 NO"
          value={price(market.no_ask)}
          detail={`最优档 ${number(market.no_quantity)} 张`}
        />
      </div>
      <p className="cm-note">
        YES 买价由 NO 最高买价的补数换算；NO 买价同理。Prod 行情不能作为 Demo
        可成交的证明，也不能重建未录制时点的成交条件。
      </p>
    </Panel>
  );
}
function MarketsView({
  strategy,
  marketTicker,
  now,
}: {
  strategy: Strategy;
  marketTicker?: string;
  now: number;
}) {
  const [asset, setAsset] = useState("all");
  const [status, setStatus] = useState("all");
  const [exactTicker, setExactTicker] = useState(marketTicker ?? "");
  const [selected, setSelected] = useState<string | null>(marketTicker ?? null);
  const [page, setPage] = useState(0);
  useEffect(() => {
    setExactTicker(marketTicker ?? "");
    setSelected(marketTicker ?? null);
    setAsset("all");
    setStatus("all");
    setPage(0);
  }, [marketTicker]);
  const rows = [...strategy.markets]
    .filter(
      (market) =>
        (asset === "all" || market.asset === asset) &&
        (status === "all" || market.status === status) &&
        (!exactTicker.trim() || market.ticker === exactTicker.trim()),
    )
    .sort((a, b) =>
      String(b.close_at ?? "").localeCompare(String(a.close_at ?? "")),
    );
  const detail = strategy.markets.find((market) => market.ticker === selected);
  const safePage = Math.min(
    page,
    Math.max(0, Math.ceil(rows.length / PAGE_SIZE) - 1),
  );
  return (
    <>
      <div className="cm-env-note">
        <b>Prod 行情</b>
        <span>当前交易执行：Kalshi Demo</span>
      </div>
      <div className="cm-toolbar">
        <label className="cm-filter">
          币种
          <select
            value={asset}
            onChange={(event) => {
              setAsset(event.target.value);
              setPage(0);
              setSelected(null);
            }}
          >
            <option value="all">全部币种</option>
            {[...new Set(strategy.markets.map((market) => market.asset))]
              .sort()
              .map((value) => (
                <option key={value}>{value}</option>
              ))}
          </select>
        </label>
        <label className="cm-filter">
          合约状态
          <select
            value={status}
            onChange={(event) => {
              setStatus(event.target.value);
              setPage(0);
              setSelected(null);
            }}
          >
            <option value="all">全部状态</option>
            {[...new Set(strategy.markets.map((market) => market.status))]
              .sort()
              .map((value) => (
                <option key={value} value={value}>
                  {statusLabel(value)}
                </option>
              ))}
          </select>
        </label>
      </div>
      <label className="cm-filter cm-search">
        精确合约代码
        <input
          value={exactTicker}
          placeholder="输入完整 ticker 精确匹配"
          spellCheck={false}
          onChange={(event) => {
            setExactTicker(event.target.value);
            setPage(0);
            setSelected(null);
          }}
        />
      </label>
      {exactTicker && (
        <button
          type="button"
          className="cm-link-button"
          onClick={() => {
            setExactTicker("");
            setSelected(null);
            setPage(0);
          }}
        >
          清除合约筛选
        </button>
      )}
      {rows.length === 0 ? (
        <Empty>
          当前快照中没有匹配的精确合约，未替换为其他币种或到期窗口。
        </Empty>
      ) : (
        <div className="cm-table-scroll">
          <table className="table cm-table">
            <thead>
              <tr>
                <th>合约 / 结束时间</th>
                <th>参考阈值</th>
                <th>YES 买价</th>
                <th>NO 买价</th>
                <th>状态 / 来源</th>
                <th>查看</th>
              </tr>
            </thead>
            <tbody>
              {rows
                .slice(safePage * PAGE_SIZE, (safePage + 1) * PAGE_SIZE)
                .map((market) => (
                  <tr key={market.ticker}>
                    <td>
                      <Ticker value={market.ticker} />
                      <small className="cm-cell-note">
                        {date(market.close_at)}
                      </small>
                    </td>
                    <td>{underlyingPrice(market.threshold)}</td>
                    <td>
                      {price(market.yes_ask)}
                      <small className="cm-cell-note">
                        {number(market.yes_quantity)} 张
                      </small>
                    </td>
                    <td>
                      {price(market.no_ask)}
                      <small className="cm-cell-note">
                        {number(market.no_quantity)} 张
                      </small>
                    </td>
                    <td>
                      <Status value={market.status} />
                      <small className="cm-cell-note">
                        {statusLabel(market.quote_status)}
                      </small>
                      <small className="cm-cell-note">
                        {market.quote_source === "orderbook"
                          ? "盘口"
                          : market.quote_source === "market_summary"
                            ? "摘要"
                            : "不可用"}{" "}
                        · {age(market.quote_at, now)}
                      </small>
                    </td>
                    <td>
                      <button
                        type="button"
                        className="cm-link-button"
                        aria-label={`查看 ${market.ticker} 合约详情`}
                        onClick={() => setSelected(market.ticker)}
                      >
                        详情
                      </button>
                    </td>
                  </tr>
                ))}
            </tbody>
          </table>
        </div>
      )}
      <Pagination
        page={safePage}
        count={rows.length}
        pageSize={PAGE_SIZE}
        onChange={setPage}
      />
      {detail && <MarketDetail market={detail} strategy={strategy} now={now} />}
    </>
  );
}

function RulesView({ strategy }: { strategy: Strategy }) {
  return (
    <>
      <Panel title={`${strategy.acronym} · ${strategy.name}`}>
        <p className="cm-prose">{publicText(strategy.english_name)}</p>
        <p className="cm-prose">{publicText(strategy.description)}</p>
        <p className="cm-note">
          来源记录版本：
          <span className="cm-break">{publicText(strategy.version)}</span>
          。本页展示来源记录，不通过页面更改策略参数。
        </p>
      </Panel>
      <Panel title="实际记录参数">
        {strategy.parameters.length === 0 ? (
          <Empty>快照未提供有效参数，不能推断当前生效配置。</Empty>
        ) : (
          <dl className="cm-details">
            {strategy.parameters.map((parameter, index) => (
              <div
                className="cm-detail-row"
                key={`${parameter.label}:${index}`}
              >
                <dt>{publicText(parameter.label)}</dt>
                <dd className="cm-break">{publicText(parameter.value)}</dd>
              </div>
            ))}
          </dl>
        )}
      </Panel>
      <Panel title="观察与固定验收">
        <div className="cm-metrics">
          <Metric
            label="纸面独立窗口"
            value={number(strategy.paper.sample_windows, 0)}
          />
          <Metric
            label="Demo 结算记录"
            value={number(strategy.demo.settled_count, 0)}
          />
        </div>
        <p className="cm-prose">
          {strategy.paper.verdict === null
            ? "来源尚未提供固定验收结论；不以当前累计盈利代替验收结果。"
            : publicText(strategy.paper.verdict)}
        </p>
        <p className="cm-note">
          {publicText(strategy.paper.scope)}。{publicText(strategy.paper.note)}
        </p>
      </Panel>
      <Panel title="执行环境">
        <dl className="cm-details">
          <dt>当前下单</dt>
          <dd>Kalshi Demo</dd>
          <dt>市场行情</dt>
          <dd>Kalshi Prod，只读</dd>
          <dt>Prod 交易</dt>
          <dd>未开启</dd>
          <dt>验收口径</dt>
          <dd>
            以纸面账本评估策略；Demo 用于验证实际下单和管理。未来 Prod 交易账本独立保存，不与纸面或 Demo 收益合并。Prod 可用盘口只是行情。
          </dd>
        </dl>
      </Panel>
    </>
  );
}

function HealthView({ strategy, now }: { strategy: Strategy; now: number }) {
  return (
    <>
      <p className="cm-note">
        按每个来源自身时间判断新鲜度。刚生成快照不等于行情或订单刚更新；限流等待与请求失败不会被新心跳掩盖。
        快照发布与网页读取各约 60 秒，Demo 账户完整核验至少间隔 180 秒；结果为分钟级更新。
      </p>
      <Panel title="运行来源">
        {strategy.runtime.length === 0 ? (
          <Empty>未提供可核验的运行来源。</Empty>
        ) : (
          strategy.runtime.map((runtime, index) => {
            const freshness = runtimeFreshness(runtime, now);
            return (
              <div className="cm-runtime" key={`${runtime.source}:${index}`}>
                <div className="cm-runtime-heading">
                  <b className="cm-break">{publicText(runtime.source)}</b>
                  <Status value={freshness.status} label={freshness.label} />
                </div>
                <dl className="cm-details">
                  <dt>来源时间</dt>
                  <dd>{date(runtime.as_of)}</dd>
                  <dt>新鲜度</dt>
                  <dd className={freshness.freshnessIssue ? "cm-negative" : ""}>
                    {age(runtime.as_of, now)}
                    {freshness.status === "stale" ? " · 已超时" : ""}
                  </dd>
                  <dt>过期阈值</dt>
                  <dd>{number(runtime.stale_after_seconds, 0)} 秒</dd>
                  <dt>来源说明</dt>
                  <dd className="cm-break">{publicText(runtime.detail)}</dd>
                </dl>
              </div>
            );
          })
        )}
      </Panel>
      <Panel title="账本覆盖">
        <div className="cm-runtime">
          <div className="cm-runtime-heading">
            <b>纸面策略评估 · 主口径</b>
            <Status value={strategy.paper.status} />
          </div>
          <p className="cm-note">
            {date(strategy.paper.source_as_of)} ·{" "}
            {publicText(strategy.paper.scope)}。{publicText(strategy.paper.note)}
          </p>
        </div>
        <div className="cm-runtime">
          <div className="cm-runtime-heading">
            <b>Kalshi Demo · 独立执行账本</b>
            <Status value={strategy.demo.status} />
          </div>
          <p className="cm-note">
            {date(strategy.demo.source_as_of)} ·{" "}
            {publicText(strategy.demo.scope)}。
            {publicText(strategy.demo.note)}
          </p>
        </div>
        <p className="cm-note">Kalshi Prod 交易账本尚未接入；纸面、Demo 和未来 Prod 的核验状态分别记录，不以收益正负替代验证。</p>
      </Panel>
      <Panel title={`运行提示（${strategy.issues.length}）`}>
        <p className="cm-note">近期记录不等同于当前停机；是否恢复仍以最新来源证据为准。待核验项目不会标为已恢复。</p>
        {strategy.issues.length === 0 ? (
          <Empty>
            快照未报告本策略来源问题；仍需结合上方覆盖状态与更新时间判断。
          </Empty>
        ) : (
          <HealthIssues issues={strategy.issues} />
        )}
      </Panel>
    </>
  );
}

function ArtifactBody({
  type,
  strategy,
  marketTicker,
  now,
}: {
  type: ArtifactType;
  strategy: Strategy;
  marketTicker?: string;
  now: number;
  key?: string;
}) {
  switch (type) {
    case "performance":
      return <PerformanceView strategy={strategy} />;
    case "orders":
      return <OrdersView strategy={strategy} />;
    case "positions":
      return <PositionsView strategy={strategy} />;
    case "settlements":
      return (
        <SettlementsView strategy={strategy} marketTicker={marketTicker} />
      );
    case "execution":
      return <ExecutionView strategy={strategy} />;
    case "markets":
      return (
        <MarketsView
          strategy={strategy}
          marketTicker={marketTicker}
          now={now}
        />
      );
    case "rules":
      return <RulesView strategy={strategy} />;
    case "health":
      return <HealthView strategy={strategy} now={now} />;
  }
}
export default function CryptoArtifact(props: {
  type: ArtifactType;
  strategy: Strategy;
  marketTicker?: string;
  now: number;
  key?: string;
}) {
  return (
    <div className="cm-artifact">
      <ArtifactBody key={`${props.strategy.id}:${props.type}`} {...props} />
    </div>
  );
}
