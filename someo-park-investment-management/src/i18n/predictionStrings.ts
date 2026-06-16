/**
 * Translations for the DYNAMIC strings the prediction_market backend emits in its
 * JSON (notes, blocked guard-rails, statuses, model notes, overview catalog).
 * Keyed by the exact source string (English from the report exporters, Chinese
 * from system_overview.py). tDyn() returns the active-language version, falling
 * back to the original string for anything unmapped.
 */
import i18n from '../i18n';

type Tr = { en: string; zh: string; ja: string; fr: string; es: string };

const M: Record<string, Tr> = {
  // ── performance_report.json notes ──
  'model Brier WORSE than uniform — not yet trade-grade (discipline gate blocks).': {
    en: 'model Brier WORSE than uniform — not yet trade-grade (discipline gate blocks).',
    zh: '模型 Brier 劣于均匀分布 — 未达交易等级(纪律闸门拦截)。',
    ja: 'モデルのBrierが一様分布より劣る — 取引グレード未達(規律ゲートがブロック)。',
    fr: 'Brier du modèle PIRE que l’uniforme — niveau insuffisant (barrière de discipline).',
    es: 'Brier del modelo PEOR que el uniforme — nivel insuficiente (puerta de disciplina).',
  },
  'Realized P&L ~0 by design: live trading gated; only demo order test placed.': {
    en: 'Realized P&L ~0 by design: live trading gated; only demo order test placed.',
    zh: '实现盈亏 ~0 属设计如此:实盘交易受闸;仅下过模拟测试单。',
    ja: '実現損益 ~0 は設計通り:ライブ取引はゲート済み、デモ注文テストのみ。',
    fr: 'P&L réalisé ~0 par conception : trading réel barré ; seul un ordre démo testé.',
    es: 'P&L realizado ~0 por diseño: trading real bloqueado; solo orden demo de prueba.',
  },
  'Calibration P&L is PAPER (fair-odds), measures model over/under-confidence.': {
    en: 'Calibration P&L is PAPER (fair-odds), measures model over/under-confidence.',
    zh: '校准盈亏为纸面(公允赔率),衡量模型过度/不足自信。',
    ja: '較正損益はペーパー(公正オッズ)、モデルの自信過剰/不足を測定。',
    fr: 'P&L de calibration sur PAPIER (cotes justes), mesure la sur/sous-confiance.',
    es: 'P&L de calibración en PAPEL (cuotas justas), mide la sobre/infraconfianza.',
  },
  // ── risk_report.json blocked_summary + status ──
  'Polymarket US orders BLOCKED (PMUS_TRADING_ENABLED=false, real money).': {
    en: 'Polymarket US orders BLOCKED (PMUS_TRADING_ENABLED=false, real money).',
    zh: 'Polymarket US 下单被拦(PMUS_TRADING_ENABLED=false,真钱)。',
    ja: 'Polymarket US 注文はブロック(PMUS_TRADING_ENABLED=false、実資金)。',
    fr: 'Ordres Polymarket US BLOQUÉS (PMUS_TRADING_ENABLED=false, argent réel).',
    es: 'Órdenes Polymarket US BLOQUEADAS (PMUS_TRADING_ENABLED=false, dinero real).',
  },
  'All edge signals BLOCKED by calibration gate (model not trade-grade).': {
    en: 'All edge signals BLOCKED by calibration gate (model not trade-grade).',
    zh: '所有边缘信号被校准闸门拦截(模型未达交易等级)。',
    ja: '全エッジシグナルが較正ゲートでブロック(モデルが取引グレード未達)。',
    fr: 'Tous les signaux bloqués par la barrière de calibration (niveau insuffisant).',
    es: 'Todas las señales bloqueadas por la puerta de calibración (nivel insuficiente).',
  },
  'Polymarket US has $0 USDC.e — cannot trade until funded.': {
    en: 'Polymarket US has $0 USDC.e — cannot trade until funded.',
    zh: 'Polymarket US 余额 $0 USDC.e — 注资前无法交易。',
    ja: 'Polymarket US 残高 $0 USDC.e — 入金まで取引不可。',
    fr: 'Polymarket US a $0 USDC.e — impossible de trader sans financement.',
    es: 'Polymarket US tiene $0 USDC.e — no se puede operar sin fondos.',
  },
  'Every order hard-capped at $1.00 notional.': {
    en: 'Every order hard-capped at $1.00 notional.',
    zh: '每单名义硬顶 $1.00。',
    ja: '全注文は名目 $1.00 で上限固定。',
    fr: 'Chaque ordre plafonné à $1.00 notionnel.',
    es: 'Cada orden limitada a $1.00 nominal.',
  },
  'BLOCK (model not yet calibrated)': {
    en: 'BLOCK (model not yet calibrated)', zh: '拦截(模型尚未校准)',
    ja: 'ブロック(モデル未較正)', fr: 'BLOQUÉ (modèle non calibré)', es: 'BLOQUEADO (modelo sin calibrar)',
  },
  // ── oos_report.json notes ──
  'model may UNDER-estimate draws (pred 0.22 vs obs 0.47)': {
    en: 'model may UNDER-estimate draws (pred 0.22 vs obs 0.47)',
    zh: '模型可能低估平局(预测 0.22 vs 实际 0.47)',
    ja: 'モデルは引分を過小評価の可能性(予測 0.22 vs 実際 0.47)',
    fr: 'le modèle peut SOUS-estimer les nuls (prév 0.22 vs obs 0.47)',
    es: 'el modelo puede SUBestimar empates (pred 0.22 vs obs 0.47)',
  },
  'DIRECTIONAL check only — do NOT fine-tune params to this (plan 03 §7).': {
    en: 'DIRECTIONAL check only — do NOT fine-tune params to this (plan 03 §7).',
    zh: '仅方向性检查 — 切勿据此微调参数(plan 03 §7)。',
    ja: '方向性チェックのみ — これにパラメータを微調整しないこと(plan 03 §7)。',
    fr: 'Vérification directionnelle seulement — ne pas ajuster les paramètres dessus (plan 03 §7).',
    es: 'Solo verificación direccional — NO ajustar parámetros a esto (plan 03 §7).',
  },
  // ── worldcup_model.json meta.model_notes ──
  'v1: ratings reverse-fit to external prior expected points (plan 10 §5.1).': {
    en: 'v1: ratings reverse-fit to external prior expected points (plan 10 §5.1).',
    zh: 'v1:评分反解拟合到外部先验期望积分(plan 10 §5.1)。',
    ja: 'v1:レーティングは外部事前期待勝点に逆フィット(plan 10 §5.1)。',
    fr: 'v1 : notes ajustées à rebours sur les points attendus a priori (plan 10 §5.1).',
    es: 'v1: ratings ajustados inversamente a puntos esperados a priori (plan 10 §5.1).',
  },
  'Group tie-breaks: points>GD>GF>random (no head-to-head / full official chain).': {
    en: 'Group tie-breaks: points>GD>GF>random (no head-to-head / full official chain).',
    zh: '小组同分判定:积分>净胜球>进球>随机(无对赛/完整官方链)。',
    ja: 'グループ同点処理:勝点>得失点>得点>ランダム(直接対決/公式の完全連鎖なし)。',
    fr: 'Départage de groupe : points>diff>BP>aléatoire (sans confrontation / chaîne officielle).',
    es: 'Desempate de grupo: puntos>dif>GF>aleatorio (sin enfrentamiento / cadena oficial).',
  },
  'Knockout bracket: fixed balanced mapping, NOT the official 2026 third-place table.': {
    en: 'Knockout bracket: fixed balanced mapping, NOT the official 2026 third-place table.',
    zh: '淘汰赛对阵:固定均衡映射,非官方 2026 第三名表。',
    ja: 'ノックアウト組合せ:固定の均衡マッピング、公式2026の3位表ではない。',
    fr: 'Tableau final : mapping équilibré fixe, PAS la table officielle 2026 des 3es.',
    es: 'Cuadro final: mapeo equilibrado fijo, NO la tabla oficial 2026 de terceros.',
  },
  'Golden-boot: real topscorers (shrunk rate + goals head-start) merged with seed favourites; mu lightly double-counts played games (v2, refine when sim conditions on results).': {
    en: 'Golden-boot: real topscorers (shrunk rate + goals head-start) merged with seed favourites; mu lightly double-counts played games (v2, refine when sim conditions on results).',
    zh: '金靴:真实射手榜(收缩进球率 + 进球先发)与种子热门合并;mu 轻度重复计入已踢场次(v2,待模拟以赛果为条件时再优化)。',
    ja: '得点王:実際の得点ランキング(縮小レート + 得点の先行)とシード本命を統合;muは消化試合をわずかに二重計上(v2、simが結果を条件付けする際に精緻化)。',
    fr: 'Soulier d’or : meilleurs buteurs réels (taux réduit + avance de buts) fusionnés avec favoris ; mu double-compte légèrement les matchs joués (v2).',
    es: 'Bota de oro: máximos goleadores reales (tasa reducida + ventaja de goles) con favoritos; mu cuenta levemente doble los partidos jugados (v2).',
  },
  'No ensemble dispersion yet; sigma is a placeholder constant.': {
    en: 'No ensemble dispersion yet; sigma is a placeholder constant.',
    zh: '尚无集成离散度;sigma 为占位常数。',
    ja: 'アンサンブル分散はまだなし;sigmaはプレースホルダ定数。',
    fr: 'Pas encore de dispersion d’ensemble ; sigma est une constante provisoire.',
    es: 'Aún sin dispersión de conjunto; sigma es una constante provisional.',
  },
  'NOT a tradable signal on its own — must be de-vigged vs live venue prices (plan 04).': {
    en: 'NOT a tradable signal on its own — must be de-vigged vs live venue prices (plan 04).',
    zh: '本身不是可交易信号 — 必须对实时场所价格去 vig(plan 04)。',
    ja: '単独では取引可能なシグナルではない — ライブ会場価格に対して控除除去が必要(plan 04)。',
    fr: 'Pas un signal négociable seul — doit être dévigé vs prix réels (plan 04).',
    es: 'No es señal negociable por sí sola — debe desvigarse vs precios reales (plan 04).',
  },
  // ── frontend_overview.json headline + value ──
  '诚实结论:系统现在是「只看不买」状态。不是没做好执行,而是纪律闸门(calibration gate)在主动拦截——模型在已结算的小组赛上 Brier 仍劣于均匀基线(0.667),尚未达到可交易等级,所以系统拒绝下任何真钱单。这正是设计目标:宁可不交易,也不拿没验证过的边缘去亏钱。': {
    zh: '诚实结论:系统现在是「只看不买」状态。不是没做好执行,而是纪律闸门(calibration gate)在主动拦截——模型在已结算的小组赛上 Brier 仍劣于均匀基线(0.667),尚未达到可交易等级,所以系统拒绝下任何真钱单。这正是设计目标:宁可不交易,也不拿没验证过的边缘去亏钱。',
    en: 'Honest take: the system is "look, don\'t buy" right now. Not for lack of execution — the calibration gate is actively blocking: on settled group matches the model\'s Brier is still worse than the uniform baseline (0.667), so it refuses any real-money order. That is the design goal: better not to trade than to lose on an unvalidated edge.',
    ja: '正直な結論:現在システムは「見るだけ、買わない」状態です。執行ができないのではなく、較正ゲートが能動的にブロック中——確定したグループ戦でモデルのBrierは一様基準(0.667)に劣り、取引グレード未達のため実資金注文を拒否します。これが設計目標:検証されていないエッジで損をするより、取引しない方が良い。',
    fr: 'En toute honnêteté : le système est en mode « regarder, pas acheter ». Non par manque d\'exécution — la barrière de calibration bloque activement : sur les matchs réglés, le Brier du modèle reste pire que la référence uniforme (0,667), donc il refuse tout ordre en argent réel. C\'est l\'objectif : mieux vaut ne pas trader que perdre sur un avantage non validé.',
    es: 'En honor a la verdad: el sistema está en modo «mirar, no comprar». No por falta de ejecución — la puerta de calibración bloquea activamente: en partidos liquidados el Brier del modelo sigue siendo peor que la referencia uniforme (0,667), así que rechaza toda orden con dinero real. Ese es el objetivo: mejor no operar que perder con una ventaja no validada.',
  },
  '一个校准过的赛事概率模型(已修巴西高估:France 18.5% / Brazil 11.2%)。': {
    zh: '一个校准过的赛事概率模型(已修巴西高估:France 18.5% / Brazil 11.2%)。',
    en: 'A calibrated match-probability model (fixed the Brazil over-rating: France 18.5% / Brazil 11.2%).',
    ja: '較正済みの試合確率モデル(ブラジル過大評価を修正:France 18.5% / Brazil 11.2%)。',
    fr: 'Un modèle de probabilités calibré (sur-cote du Brésil corrigée : France 18,5 % / Brésil 11,2 %).',
    es: 'Un modelo de probabilidades calibrado (corregida la sobrevaloración de Brasil: Francia 18,5 % / Brasil 11,2 %).',
  },
  '跨 Kalshi / Polymarket 的实时错价发现(赛前偏离 + 盘中每分钟套利)。': {
    zh: '跨 Kalshi / Polymarket 的实时错价发现(赛前偏离 + 盘中每分钟套利)。',
    en: 'Real-time mispricing discovery across Kalshi / Polymarket (pre-match divergence + per-minute in-play arb).',
    ja: 'Kalshi / Polymarket 横断のリアルタイム価格乖離発見(試合前の乖離 + ライブ毎分の裁定)。',
    fr: 'Détection de mauvais prix en temps réel sur Kalshi / Polymarket (divergence avant-match + arbitrage en direct).',
    es: 'Detección de precios erróneos en tiempo real en Kalshi / Polymarket (divergencia previa + arbitraje en vivo).',
  },
  '一套强制纪律——只在真有边缘且模型达标时才动钱,且每单硬顶 $1。': {
    zh: '一套强制纪律——只在真有边缘且模型达标时才动钱,且每单硬顶 $1。',
    en: 'Enforced discipline — only risk money when there is a real edge AND the model is trade-grade, with a hard $1 cap per order.',
    ja: '強制された規律——本当にエッジがあり、かつモデルが取引グレードのときのみ資金を動かし、注文ごとに$1の上限。',
    fr: 'Discipline imposée — n\'engager de l\'argent que s\'il y a un vrai avantage ET un modèle valide, avec un plafond strict de 1 $ par ordre.',
    es: 'Disciplina forzada — arriesgar dinero solo con una ventaja real Y un modelo válido, con un límite estricto de 1 $ por orden.',
  },
  // ── risk prod-balance note + overview interface categories ──
  'not queried (standing rule: prod key only on explicit instruction)': {
    en: 'not queried (standing rule: prod key only on explicit instruction)',
    zh: '未查询(标准规则:prod key 仅在明确指示下使用)',
    ja: '未照会(標準規則:prod keyは明示的指示のみ)',
    fr: 'non interrogé (règle : clé prod uniquement sur instruction explicite)',
    es: 'no consultado (regla: clave prod solo con instrucción explícita)',
  },
  '数据': { en: 'Data', zh: '数据', ja: 'データ', fr: 'Données', es: 'Datos' },
  '预测': { en: 'Predict', zh: '预测', ja: '予測', fr: 'Prédire', es: 'Predecir' },
  '策略': { en: 'Strategy', zh: '策略', ja: '戦略', fr: 'Stratégie', es: 'Estrategia' },
  '运维': { en: 'Ops', zh: '运维', ja: '運用', fr: 'Ops', es: 'Ops' },
  '调度': { en: 'Jobs', zh: '调度', ja: 'ジョブ', fr: 'Tâches', es: 'Tareas' },
  '怎么看到价值:performance_report(准确度/校准 P&L)+ risk_report(闸门/敞口/预算)两份报告即是答案。': {
    zh: '怎么看到价值:performance_report(准确度/校准 P&L)+ risk_report(闸门/敞口/预算)两份报告即是答案。',
    en: 'How to see the value: the two reports — performance_report (accuracy / calibration P&L) + risk_report (gates / exposure / budget) — are the answer.',
    ja: '価値の見方:performance_report(精度/較正損益)+ risk_report(ゲート/エクスポージャ/予算)の2つのレポートが答え。',
    fr: 'Comment voir la valeur : les deux rapports — performance_report (précision / P&L de calibration) + risk_report (barrières / exposition / budget) — sont la réponse.',
    es: 'Cómo ver el valor: los dos informes — performance_report (precisión / P&L de calibración) + risk_report (controles / exposición / presupuesto) — son la respuesta.',
  },
};

/** Dynamic backend string in the active language (original fallback). */
export function tDyn(s?: string | null): string {
  if (!s) return '';
  const lang = (i18n.language || 'en').slice(0, 2) as keyof Tr;
  const hit = M[s];
  return hit ? (hit[lang] ?? hit.en ?? s) : s;
}
