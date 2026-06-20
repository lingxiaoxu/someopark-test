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
  'PASS (calibrated)': {
    en: 'PASS (calibrated)', zh: '通过(已校准)',
    ja: '合格(較正済み)', fr: 'OK (calibré)', es: 'OK (calibrado)',
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
  // ── frontend_overview.json: interface category (执行 missing; others above) ──
  '执行': { en: 'Execute', zh: '执行', ja: '実行', fr: 'Exécution', es: 'Ejecución' },
  // ── frontend_overview.json: interface purposes ──
  '一次性拉全量(球队/球员/对阵/赛程),建立增量水位': {
    zh: '一次性拉全量(球队/球员/对阵/赛程),建立增量水位',
    en: 'One-time full pull (teams/players/fixtures/schedule), establishing the incremental watermark',
    ja: '全量を一度取得(チーム/選手/対戦/日程)、増分の基準位置を確立',
    fr: 'Extraction complète unique (équipes/joueurs/affiches/calendrier), pose le repère incrémental',
    es: 'Extracción completa única (equipos/jugadores/enfrentamientos/calendario), fija la marca incremental',
  },
  '增量刷新(赛果、比分、live 状态)': {
    zh: '增量刷新(赛果、比分、live 状态)',
    en: 'Incremental refresh (results, scores, live status)',
    ja: '増分リフレッシュ(結果、スコア、ライブ状態)',
    fr: 'Rafraîchissement incrémental (résultats, scores, statut live)',
    es: 'Actualización incremental (resultados, marcadores, estado en vivo)',
  },
  '单场 3-way 公允价(主/平/客),含点球大战建模': {
    zh: '单场 3-way 公允价(主/平/客),含点球大战建模',
    en: 'Single-match 3-way fair odds (home/draw/away), incl. penalty-shootout modelling',
    ja: '単一試合の3-way公正オッズ(ホーム/引分/アウェイ)、PK戦モデリング含む',
    fr: 'Cotes justes 3-way par match (domicile/nul/extérieur), avec modélisation des tirs au but',
    es: 'Cuotas justas 3-way por partido (local/empate/visitante), con modelado de tanda de penaltis',
  },
  '模拟冠军概率(48 队)': {
    zh: '模拟冠军概率(48 队)',
    en: 'Simulated title probabilities (48 teams)',
    ja: '優勝確率のシミュレーション(48チーム)',
    fr: 'Probabilités de titre simulées (48 équipes)',
    es: 'Probabilidades de título simuladas (48 equipos)',
  },
  '金靴(进球王)球员概率': {
    zh: '金靴(进球王)球员概率',
    en: 'Golden Boot (top scorer) player probabilities',
    ja: '得点王(ゴールデンブート)の選手確率',
    fr: 'Probabilités joueur du Soulier d’or (meilleur buteur)',
    es: 'Probabilidades de jugador para la Bota de Oro (máximo goleador)',
  },
  'xG-form 评分加成(PIT):球队近期 xG 比比分更低噪,提升预测': {
    zh: 'xG-form 评分加成(PIT):球队近期 xG 比比分更低噪,提升预测',
    en: "xG-form rating boost (PIT): a team's recent xG is less noisy than scores, improving predictions",
    ja: 'xG-formによる評価加点(PIT):チーム直近のxGはスコアより低ノイズで予測を改善',
    fr: "Bonus de note xG-form (PIT) : le xG récent d'une équipe est moins bruité que les scores, améliore les prédictions",
    es: 'Bonificación de rating xG-form (PIT): el xG reciente de un equipo tiene menos ruido que los marcadores, mejora las predicciones',
  },
  '赛前下注决策:价值选边(在可交易边里挑最优)+ 分数凯利 + 置信度定额($0.2–$2);决策阈值 0.02(择时保护)': {
    zh: '赛前下注决策:价值选边(在可交易边里挑最优)+ 分数凯利 + 置信度定额($0.2–$2);决策阈值 0.02(择时保护)',
    en: 'Pre-match betting decision: value side-pick (best among tradable sides) + fractional Kelly + confidence sizing ($0.2–$2); decision threshold 0.02 (timing guard)',
    ja: '試合前ベット判断:バリューでサイド選択(取引可能なサイドから最良)+ 分数ケリー + 信頼度に応じた定額($0.2–$2);判断閾値0.02(タイミング保護)',
    fr: 'Décision de pari avant-match : choix de côté valeur (le meilleur parmi les côtés négociables) + Kelly fractionnaire + mise selon la confiance (0,2–2 $) ; seuil de décision 0,02 (garde-fou de timing)',
    es: 'Decisión de apuesta previa: elección de lado por valor (el mejor entre los lados negociables) + Kelly fraccional + tamaño por confianza (0,2–2 $); umbral de decisión 0,02 (protección de timing)',
  },
  '智能择时/超调止盈:市场价超调高于 live 公允价时现金出锁利——实现口径的核心 alpha(39%→67% 盈利)': {
    zh: '智能择时/超调止盈:市场价超调高于 live 公允价时现金出锁利——实现口径的核心 alpha(39%→67% 盈利)',
    en: 'Smart timing / overshoot take-profit: cash out to lock gains when market price overshoots above the live fair value — the core alpha of the realized view (39%→67% profitable)',
    ja: 'スマートタイミング/オーバーシュート利確:市場価格がライブ公正価を上回り過剰反応した時に現金化して利益確定——実現ベースの中核アルファ(39%→67%が黒字)',
    fr: "Timing intelligent / prise de profit sur dépassement : encaisser pour verrouiller les gains quand le prix de marché dépasse la juste valeur live — l'alpha central de la vue réalisée (39 %→67 % gagnants)",
    es: 'Timing inteligente / toma de ganancias por sobrerreacción: liquidar para asegurar ganancias cuando el precio de mercado supera el valor justo en vivo — el alfa central de la vista realizada (39 %→67 % rentables)',
  },
  '决策模型 PIT 回测(选边/退出时点/CLV 实测)': {
    zh: '决策模型 PIT 回测(选边/退出时点/CLV 实测)',
    en: 'Decision-model PIT backtest (side pick / exit timing / measured CLV)',
    ja: '判断モデルのPITバックテスト(サイド選択/退出タイミング/CLV実測)',
    fr: 'Backtest PIT du modèle de décision (choix de côté / timing de sortie / CLV mesuré)',
    es: 'Backtest PIT del modelo de decisión (elección de lado / timing de salida / CLV medido)',
  },
  '每日赛前下单信号:decide() 选边定额 → $1 硬顶 cap → 闸门放行才下单': {
    zh: '每日赛前下单信号:decide() 选边定额 → $1 硬顶 cap → 闸门放行才下单',
    en: 'Daily pre-match order signal: decide() picks side & size → $1 hard cap → order only if the gate passes',
    ja: '毎日の試合前発注シグナル:decide()でサイドと定額を決定 → $1ハード上限 → ゲート通過時のみ発注',
    fr: "Signal d'ordre quotidien avant-match : decide() choisit côté et mise → plafond strict 1 $ → ordre seulement si la barrière passe",
    es: 'Señal de orden diaria previa al partido: decide() elige lado y tamaño → tope estricto de 1 $ → orden solo si la puerta lo permite',
  },
  '模型 vs 市场偏离扫描(赛前)': {
    zh: '模型 vs 市场偏离扫描(赛前)',
    en: 'Model vs market divergence scan (pre-match)',
    ja: 'モデル対市場の乖離スキャン(試合前)',
    fr: "Scan d'écart modèle vs marché (avant-match)",
    es: 'Escaneo de divergencia modelo vs mercado (previo al partido)',
  },
  '盘中每分钟:套利 / 相对价值 / 15 个战术(8 个数据挖掘:闷平爆发/应得未得/无效控球/阵型脆弱/单点失效/晚段进球/庄家交叉验证/临门修正 + 超调止盈)。信号按 intent 分区(持仓管理/新入场/事件,避免平仓与入场被读成矛盾);庄家交叉验证需「庄家+模型双双>可交易场价」确认(与模型同向);收敛锁定需市场 bid≈公允才卖(否则持有吃满额,不贱卖)': {
    zh: '盘中每分钟:套利 / 相对价值 / 15 个战术(8 个数据挖掘:闷平爆发/应得未得/无效控球/阵型脆弱/单点失效/晚段进球/庄家交叉验证/临门修正 + 超调止盈)。信号按 intent 分区(持仓管理/新入场/事件,避免平仓与入场被读成矛盾);庄家交叉验证需「庄家+模型双双>可交易场价」确认(与模型同向);收敛锁定需市场 bid≈公允才卖(否则持有吃满额,不贱卖)',
    en: 'In-play per minute: arbitrage / relative value / 15 tactics (8 data-mined: stalemate breakout / deserved-but-denied / sterile possession / fragile formation / single-point failure / late goal / bookmaker cross-validation / final-touch correction + overshoot take-profit). Signals are partitioned by intent (position management / new entry / event, so closing and entering aren\'t read as contradictory); bookmaker cross-validation requires "both bookmaker + model > the tradable venue price" confirmation (same direction as the model); convergence lock only sells when the market bid ≈ fair (otherwise hold for full settlement, don\'t sell cheap)',
    ja: 'ライブ毎分:裁定 / 相対価値 / 15戦術(データマイニング8:膠着爆発/報われない内容/無効な支配/脆い布陣/一点依存の崩壊/終盤ゴール/ブックメーカー交差検証/土壇場修正 + オーバーシュート利確)。シグナルはintentで分区(ポジション管理/新規エントリー/イベント、決済とエントリーが矛盾と読まれないように);ブックメーカー交差検証は「ブックメーカー+モデルの双方>取引可能な会場価格」の確認が必要(モデルと同方向);収束ロックは市場bid≈公正の時のみ売る(さもなくば満額決済まで保有し、安売りしない)',
    fr: "En direct chaque minute : arbitrage / valeur relative / 15 tactiques (8 issues du data-mining : déblocage de match fermé / mérité-mais-refusé / possession stérile / formation fragile / défaillance d'un seul point / but tardif / validation croisée bookmaker / correction de dernière minute + prise de profit sur dépassement). Les signaux sont répartis par intention (gestion de position / nouvelle entrée / événement, pour que clôturer et entrer ne paraissent pas contradictoires) ; la validation croisée bookmaker exige la confirmation « bookmaker + modèle tous deux > prix négociable du marché » (même sens que le modèle) ; le verrouillage de convergence ne vend que si le bid de marché ≈ juste valeur (sinon conserver jusqu'au règlement plein, ne pas brader)",
    es: 'En vivo cada minuto: arbitraje / valor relativo / 15 tácticas (8 de minería de datos: desbloqueo de empate cerrado / merecido-pero-negado / posesión estéril / formación frágil / fallo de un solo punto / gol tardío / validación cruzada de casa de apuestas / corrección de último momento + toma de ganancias por sobrerreacción). Las señales se reparten por intención (gestión de posición / nueva entrada / evento, para que cerrar y entrar no se lean como contradictorios); la validación cruzada de casa de apuestas exige confirmación «casa + modelo ambos > precio negociable del mercado» (misma dirección que el modelo); el bloqueo por convergencia solo vende cuando el bid de mercado ≈ valor justo (de lo contrario, mantener hasta la liquidación completa, no malvender)',
  },
  '球队风格分型(48队×10风格矩阵:每队1-2风格,手策研究先验+live指标混合,每周更新)': {
    zh: '球队风格分型(48队×10风格矩阵:每队1-2风格,手策研究先验+live指标混合,每周更新)',
    en: 'Team style typing (48-team × 10-style matrix: 1–2 styles per team, curated research prior blended with live metrics, weekly update)',
    ja: 'チームのスタイル分類(48チーム×10スタイルの行列:1チーム1-2スタイル、手作業の研究事前分布+ライブ指標の混合、週次更新)',
    fr: "Typage de style d'équipe (matrice 48 équipes × 10 styles : 1–2 styles par équipe, a priori de recherche curé mêlé aux métriques live, mise à jour hebdomadaire)",
    es: 'Tipificación de estilo de equipo (matriz 48 equipos × 10 estilos: 1–2 estilos por equipo, prior de investigación curado mezclado con métricas en vivo, actualización semanal)',
  },
  '晋级盘(48 队 × 5 轮:小组出线/16/8/4/决赛,模型%/Kalshi¢/Poly¢/边缘)': {
    zh: '晋级盘(48 队 × 5 轮:小组出线/16/8/4/决赛,模型%/Kalshi¢/Poly¢/边缘)',
    en: 'Advancement board (48 teams × 5 rounds: group-out/R16/QF/SF/final, model% / Kalshi¢ / Poly¢ / edge)',
    ja: '勝ち上がりボード(48チーム×5ラウンド:グループ突破/16強/8強/4強/決勝、モデル%/Kalshi¢/Poly¢/エッジ)',
    fr: 'Tableau de qualification (48 équipes × 5 tours : sortie de groupe/8e/4e/demi/finale, modèle % / Kalshi¢ / Poly¢ / avantage)',
    es: 'Tablero de avance (48 equipos × 5 rondas: salida de grupo/octavos/cuartos/semis/final, modelo % / Kalshi¢ / Poly¢ / ventaja)',
  },
  '每分钟价格回填(Poly Global prices-history),供细粒度择时研究': {
    zh: '每分钟价格回填(Poly Global prices-history),供细粒度择时研究',
    en: 'Per-minute price backfill (Poly Global prices-history), for fine-grained timing research',
    ja: '毎分の価格バックフィル(Poly Global prices-history)、細粒度のタイミング研究用',
    fr: 'Remplissage de prix à la minute (Poly Global prices-history), pour la recherche de timing fin',
    es: 'Relleno de precios por minuto (Poly Global prices-history), para investigación de timing de grano fino',
  },
  '赛程表(美东 ET + 美西 PT 双时区)': {
    zh: '赛程表(美东 ET + 美西 PT 双时区)',
    en: 'Schedule (US Eastern ET + US Pacific PT dual time zones)',
    ja: '日程表(米東部ET + 米西部PTの2タイムゾーン)',
    fr: 'Calendrier (double fuseau : Est ET + Pacifique PT des États-Unis)',
    es: 'Calendario (doble zona horaria: Este ET + Pacífico PT de EE. UU.)',
  },
  '健康报告(数据新鲜度/预算/校准/错误率)': {
    zh: '健康报告(数据新鲜度/预算/校准/错误率)',
    en: 'Health report (data freshness / budget / calibration / error rate)',
    ja: 'ヘルスレポート(データ鮮度/予算/較正/エラー率)',
    fr: "Rapport de santé (fraîcheur des données / budget / calibration / taux d'erreur)",
    es: 'Informe de salud (frescura de datos / presupuesto / calibración / tasa de error)',
  },
  '收益/准确度报告(本 PDF)': {
    zh: '收益/准确度报告(本 PDF)',
    en: 'Returns / accuracy report (this PDF)',
    ja: '収益/精度レポート(本PDF)',
    fr: 'Rapport rendement/précision (ce PDF)',
    es: 'Informe de rendimiento/precisión (este PDF)',
  },
  '风险报告(配套 PDF)': {
    zh: '风险报告(配套 PDF)',
    en: 'Risk report (companion PDF)',
    ja: 'リスクレポート(付属PDF)',
    fr: 'Rapport de risque (PDF compagnon)',
    es: 'Informe de riesgo (PDF complementario)',
  },
  '整点任务(刷数据+扫偏离+健康)': {
    zh: '整点任务(刷数据+扫偏离+健康)',
    en: 'On-the-hour job (refresh data + scan divergence + health)',
    ja: '正時タスク(データ更新+乖離スキャン+ヘルス)',
    fr: "Tâche à l'heure pile (rafraîchir données + scan d'écart + santé)",
    es: 'Tarea en punto (actualizar datos + escanear divergencia + salud)',
  },
  '盘中每分钟轮询(内部调 inplay_arb)': {
    zh: '盘中每分钟轮询(内部调 inplay_arb)',
    en: 'In-play per-minute polling (internally calls inplay_arb)',
    ja: 'ライブ毎分ポーリング(内部でinplay_arbを呼ぶ)',
    fr: 'Interrogation en direct à la minute (appelle inplay_arb en interne)',
    es: 'Sondeo en vivo por minuto (llama internamente a inplay_arb)',
  },
  // ── frontend_overview.json: schedule "when" + "frequency" ──
  '每天一次(赛前)': {
    zh: '每天一次(赛前)', en: 'Once daily (pre-match)', ja: '1日1回(試合前)',
    fr: 'Une fois/jour (avant-match)', es: 'Una vez/día (pre-partido)',
  },
  '整点': { zh: '整点', en: 'On the hour', ja: '毎正時', fr: "À l'heure pile", es: 'En punto' },
  '比赛进行中': { zh: '比赛进行中', en: 'During matches', ja: '試合中', fr: 'Pendant les matchs', es: 'Durante los partidos' },
  '随时查看': { zh: '随时查看', en: 'On demand', ja: '随時', fr: 'À la demande', es: 'Bajo demanda' },
  '1×/天': { zh: '1×/天', en: '1×/day', ja: '1×/日', fr: '1×/jour', es: '1×/día' },
  '每小时': { zh: '每小时', en: 'Hourly', ja: '毎時', fr: 'Toutes les heures', es: 'Cada hora' },
  '每分钟': { zh: '每分钟', en: 'Per minute', ja: '毎分', fr: 'Chaque minute', es: 'Cada minuto' },
  '按需': { zh: '按需', en: 'On demand', ja: '随時', fr: 'À la demande', es: 'Bajo demanda' },
};

// ── System Overview headline ──
// The backend headline (honest_headline) interpolates the live Brier numbers, so an
// exact-string M lookup can't match it. Rebuild it in the active language from the
// structured `performance` fields instead; fall back to tDyn(raw) if perf is absent.
type OverviewPerf = { trade_grade?: boolean; calibrated_brier?: number | null; brier_uniform?: number } | null | undefined;
const HEADLINE_PASS: Tr = {
  zh: '系统状态:已达可交易等级。经概率校准后模型 Brier {cb} ≤ 均匀基线 {ub},纪律闸门放行。实际下单仍受 $1 硬上限、场所可执行性与单场正向 edge 三重约束——闸门管「能不能交易」,这三项管「具体下不下、下多少」。',
  en: "System status: trade-grade reached. After probability calibration the model's Brier {cb} ≤ uniform baseline {ub}, so the discipline gate is open. Live orders are still bound by three limits — the $1 hard cap, venue executability, and a positive per-match edge — the gate governs 'whether we can trade at all', these three govern 'whether and how much to actually bet'.",
  ja: 'システム状態:取引グレード到達。確率較正後、モデルのBrier {cb} ≤ 一様基準 {ub} のため規律ゲートは開放。実際の発注は依然として3つの制約 — $1のハード上限、会場の執行可能性、試合ごとの正のエッジ — を受ける。ゲートは「そもそも取引できるか」を、この3つは「実際に賭けるか・いくら賭けるか」を司る。',
  fr: "État du système : niveau négociable atteint. Après calibration, le Brier du modèle {cb} ≤ référence uniforme {ub}, donc la barrière de discipline est ouverte. Les ordres réels restent soumis à trois limites — le plafond strict de 1 $, l'exécutabilité du marché et un avantage positif par match — la barrière régit « peut-on trader », ces trois régissent « parier ou non, et combien ».",
  es: 'Estado del sistema: nivel negociable alcanzado. Tras la calibración, el Brier del modelo {cb} ≤ línea base uniforme {ub}, así que la puerta de disciplina está abierta. Las órdenes reales siguen sujetas a tres límites — el tope estricto de 1 $, la ejecutabilidad del mercado y una ventaja positiva por partido — la puerta rige «si se puede operar», estos tres rigen «apostar o no, y cuánto».',
};
const HEADLINE_BLOCK: Tr = {
  zh: '系统状态:纪律闸门拦截中。校准后模型 Brier {cb} 仍劣于均匀基线 {ub},未达可交易等级,系统拒绝下任何真钱单。这是设计目标:宁可不交易,也不拿没验证过的边缘去亏钱。',
  en: "System status: discipline gate is blocking. After calibration the model's Brier {cb} is still worse than the uniform baseline {ub}, not yet trade-grade, so the system refuses any real-money order. This is by design: better not to trade than to lose on an unvalidated edge.",
  ja: 'システム状態:規律ゲートがブロック中。較正後もモデルのBrier {cb} は一様基準 {ub} に劣り、取引グレード未達のため、システムは実資金の注文を一切拒否する。これは設計目標:検証されていないエッジで損をするより、取引しない方が良い。',
  fr: "État du système : la barrière de discipline bloque. Après calibration, le Brier du modèle {cb} reste pire que la référence uniforme {ub}, niveau non atteint, donc le système refuse tout ordre en argent réel. C'est voulu : mieux vaut ne pas trader que perdre sur un avantage non validé.",
  es: 'Estado del sistema: la puerta de disciplina está bloqueando. Tras la calibración, el Brier del modelo {cb} sigue siendo peor que la línea base uniforme {ub}, nivel no alcanzado, así que el sistema rechaza toda orden con dinero real. Es por diseño: mejor no operar que perder con una ventaja no validada.',
};

/** Localized System Overview headline rebuilt from structured performance fields. */
export function overviewHeadline(perf: OverviewPerf, fallback?: string | null): string {
  if (!perf || perf.trade_grade == null) return tDyn(fallback);
  const lang = (i18n.language || 'en').slice(0, 2) as keyof Tr;
  const cb = perf.calibrated_brier != null ? perf.calibrated_brier.toFixed(4) : '—';
  const ub = (perf.brier_uniform ?? 0.6667).toFixed(4);
  const T = perf.trade_grade ? HEADLINE_PASS : HEADLINE_BLOCK;
  return (T[lang] ?? T.en).replace('{cb}', cb).replace('{ub}', ub);
}

/** Dynamic backend string in the active language (original fallback). */
export function tDyn(s?: string | null): string {
  if (!s) return '';
  const lang = (i18n.language || 'en').slice(0, 2) as keyof Tr;
  const hit = M[s];
  return hit ? (hit[lang] ?? hit.en ?? s) : s;
}
