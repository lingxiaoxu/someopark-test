// server/tools/soccerTriggers.ts
// Club Soccer (prediction_market_soccer) chat trigger vocabulary — 附录 C-32.
//
// Mirrors macroMarketTool's MACRO_KEYWORD_PATTERNS: the keyword dictionary lives next
// to the tool/grounding module, artifactDetector only loops over it. Two things make
// the soccer table structurally different from the wc_* / macro_* ones:
//
//  1. The "league → match" hierarchy (§3.7). A soccer question is almost always scoped
//     to ONE of the 12 competitions, so a detected trigger carries `params.league`
//     (and `params.clubs`) — the grounding loader uses that to inject one league's
//     slice instead of all twelve, which is the difference between 8 KB and 300 KB.
//  2. Club names are data, not prose: 399 clubs × (English | 中文 | 日本語) come from
//     the SAME i18n namespace the UI renders (soccer.club.*), so a name the user sees
//     on a card is a name chat understands. They are loaded lazily from disk rather
//     than hand-copied, because hand-copying 1,200 surface forms cannot be kept honest.
//
// The 12 competition names ARE hand-curated (not read from soccer.league.*) on purpose:
// several UI labels are unusable as free-text triggers — ja "CL"/"EL"/"ECL" and fr
// "Liga" would fire on ordinary Spanish/French/English words. The curated table keeps
// the safe forms and adds the colloquial ones no UI label carries (EPL, 英超, ラリーガ…).

import fs from 'fs'
import path from 'path'
import { fileURLToPath } from 'url'

const __dirname = path.dirname(fileURLToPath(import.meta.url))
const appRoot = path.resolve(__dirname, '..', '..')

/** Structurally identical to artifactDetector's ArtifactTrigger — declared locally so
 *  this module stays a leaf (artifactDetector imports us, never the reverse). */
export type SoccerTrigger = { type: string; title: string; params?: Record<string, any> }

// ── the 12 competitions ──────────────────────────────────────────────────────
// `kind` mirrors the backend registry (§3.0) and decides only ONE thing here: what a
// bare competition mention with no topic word should open — a table for a league,
// the champion board for a cup/Swiss competition (a cup has no standings to show).
export type SoccerLeagueDef = {
  id: string
  label: string
  kind: 'league' | 'league_playoffs' | 'swiss_ucl' | 'cup_two_leg'
  aliases: string[]
}

export const SOCCER_LEAGUES: SoccerLeagueDef[] = [
  { id: 'epl', label: 'Premier League', kind: 'league', aliases: [
    'premier league', 'epl', 'english premier league', 'the prem',
    '英超', '英格兰超级联赛', '英超联赛',
    'プレミアリーグ', 'プレミア',
    'liga inglesa', 'championnat anglais'] },
  { id: 'laliga', label: 'La Liga', kind: 'league', aliases: [
    'la liga', 'laliga', 'primera division',
    '西甲', '西班牙甲级联赛', '西班牙足球甲级联赛',
    'ラ・リーガ', 'ラリーガ',
    'liga espanola', 'liga espanhola', 'championnat espagnol'] },
  { id: 'seriea', label: 'Serie A', kind: 'league', aliases: [
    'serie a', 'seriea', 'calcio',
    '意甲', '意大利甲级联赛', '意大利足球甲级联赛',
    'セリエa', 'セリエ',
    'championnat italien'] },
  { id: 'bundesliga', label: 'Bundesliga', kind: 'league', aliases: [
    'bundesliga', 'german bundesliga',
    '德甲', '德国甲级联赛', '德国足球甲级联赛',
    'ブンデスリーガ', 'ブンデス',
    'championnat allemand'] },
  { id: 'ligue1', label: 'Ligue 1', kind: 'league', aliases: [
    'ligue 1', 'ligue1', 'french league',
    '法甲', '法国甲级联赛', '法国足球甲级联赛',
    'リーグ・アン', 'リーグアン',
    'liga francesa'] },
  { id: 'ucl', label: 'UEFA Champions League', kind: 'swiss_ucl', aliases: [
    'champions league', 'ucl', 'uefa champions league', 'champions',
    '欧冠', '欧洲冠军联赛', '冠军联赛',
    'チャンピオンズリーグ', 'チャンピオンズ',
    'liga de campeones', 'ligue des champions'] },
  { id: 'uel', label: 'UEFA Europa League', kind: 'swiss_ucl', aliases: [
    'europa league', 'uel', 'uefa europa league',
    '欧联', '欧联杯', '欧罗巴联赛',
    'ヨーロッパリーグ',
    'liga europa', 'ligue europa'] },
  { id: 'uecl', label: 'UEFA Conference League', kind: 'swiss_ucl', aliases: [
    'conference league', 'uecl', 'uefa conference league', 'europa conference',
    '欧协联', '欧会杯', '欧洲协会联赛',
    'カンファレンスリーグ',
    'liga conferencia', 'ligue conference'] },
  { id: 'libertadores', label: 'Copa Libertadores', kind: 'cup_two_leg', aliases: [
    'libertadores', 'copa libertadores',
    '解放者杯', '南美解放者杯',
    'リベルタドーレス', 'リベルタドーレス杯'] },
  { id: 'sudamericana', label: 'Copa Sudamericana', kind: 'cup_two_leg', aliases: [
    'sudamericana', 'copa sudamericana',
    '南美杯', '南美俱乐部杯',
    'スダメリカーナ', 'スダメリカーナ杯'] },
  { id: 'brasileirao', label: 'Brasileirão Série A', kind: 'league', aliases: [
    'brasileirao', 'brasileirao serie a', 'campeonato brasileiro', 'brazilian league',
    '巴甲', '巴西甲级联赛', '巴西足球甲级联赛',
    'ブラジル全国選手権', 'ブラジルリーグ',
    'championnat bresilien'] },
  { id: 'argentina', label: 'Liga Profesional Argentina', kind: 'league_playoffs', aliases: [
    'liga profesional', 'liga profesional argentina', 'lpf', 'argentine league',
    '阿甲', '阿根廷甲级联赛', '阿根廷足球甲级联赛',
    'アルゼンチン1部', 'アルゼンチンリーグ',
    'championnat argentin'] },
]

export const SOCCER_LEAGUE_IDS = SOCCER_LEAGUES.map((l) => l.id)
const LEAGUE_BY_ID = new Map(SOCCER_LEAGUES.map((l) => [l.id, l]))
export const soccerLeagueDef = (id?: string | null) => (id ? LEAGUE_BY_ID.get(id) ?? null : null)

// ── topic keyword table (EN / 中文 / 日本語 / ES / FR) ─────────────────────────
// Every UI label from the soccer.* i18n namespace is a trigger in all five languages,
// plus the way people actually ask the question. Detection is MODE-SCOPED (soccer mode
// only), which is what lets broad words like "risk"/"总览"/"calendrier" live here
// without colliding with the stock / wc_ / macro_ tables.
export const SOCCER_KEYWORD_PATTERNS: Array<{ type: string; title: string; keywords: string[] }> = [
  // The knockout bracket card. Cup vocabulary only — a bracket question is always
  // about a two-legged tie or a round of a cup, never about a league table.
  { type: 'soccer_bracket', title: 'Bracket', keywords: [
    'bracket', 'knockout', 'knockout round', 'last 16', 'round of 16', 'quarter final',
    'quarter-final', 'semi final', 'semi-final', 'two-legged', 'second leg', 'first leg',
    'who advances', 'advance to', 'draw for the', 'tie',
    '签表', '对阵表', '淘汰赛', '淘汰赛阶段', '八强', '四强', '半决赛', '决赛对阵', '两回合',
    '首回合', '次回合', '晋级形势', '抽签',
    'トーナメント表', 'ノックアウト', 'ベスト16', '準々決勝', '準決勝', '第1戦', '第2戦',
    'cuadro', 'eliminatoria', 'octavos', 'cuartos', 'semifinal', 'ida', 'vuelta',
    'tableau', 'huitiemes', 'quarts', 'demi-finale', 'aller', 'retour'] },
  { type: 'soccer_season_odds', title: 'Season Odds', keywords: [
    'season odds', 'title odds', 'win the league', 'win the title', 'league winner',
    'champion odds', 'relegation', 'relegated', 'top four', 'top 4', 'europe spots',
    '冠军与赛季盘', '夺冠', '夺冠概率', '冠军概率', '争冠', '保级', '降级', '前四', '欧战资格', '赛季盘',
    'シーズンオッズ', '優勝確率', '優勝オッズ', '降格', 'cl圏',
    'cuotas de temporada', 'probabilidad de titulo', 'descenso',
    'cotes de saison', 'titre', 'relegation'] },
  { type: 'soccer_league_table', title: 'League Table', keywords: [
    'league table', 'standings', 'the table', 'points table', 'table position',
    '积分榜', '联赛积分榜', '排名榜', '积分排名', '榜首', '排行榜',
    '順位表', '順位', '勝点',
    'clasificacion', 'tabla de posiciones',
    'classement', 'tableau du classement'] },
  { type: 'soccer_top_scorer', title: 'Top Scorer', keywords: [
    'top scorer', 'top goalscorer', 'golden boot', 'leading scorer', 'most goals',
    '射手王', '最佳射手', '金靴', '射手榜', '进球最多',
    '得点王', '得点ランキング',
    'maximo goleador', 'goleador', 'bota de oro',
    'meilleur buteur', 'soulier d or'] },
  { type: 'soccer_squad', title: 'Squad Strength', keywords: [
    'squad strength', 'squad quality', 'squad depth', 'best squad',
    '阵容强度', '阵容', '阵容深度', '球队实力',
    'スカッド総合力', 'スカッド', '戦力',
    'fuerza de plantilla', 'plantilla',
    'force de l effectif', 'effectif'] },
  { type: 'soccer_styles', title: 'Team Styles', keywords: [
    'team styles', 'playing style', 'play style', 'high press', 'low block', 'possession style',
    '球队风格', '打法', '踢法', '控球', '高位逼抢', '低位防反',
    'チームスタイル', 'スタイル', '戦術',
    'estilos de equipo', 'estilo de juego',
    'styles d equipe', 'style de jeu'] },
  { type: 'soccer_form', title: 'Recent Form', keywords: [
    'recent form', 'current form', 'in form', 'form guide', 'last five', 'last 5',
    '近期状态', '球队状态', '最近状态', '状态如何', '近况',
    '最近の調子', '調子', '直近の成績',
    'forma reciente', 'estado de forma',
    'forme recente', 'en forme'] },
  { type: 'soccer_match_pricing', title: 'Match Pricing', keywords: [
    'match pricing', 'match odds', 'match price', 'price this match', 'three-way', '3-way',
    'home draw away', 'over 2.5', 'btts', 'both teams to score',
    '比赛定价', '单场定价', '胜平负', '让球', '大小球', '双方进球',
    '試合プライシング', '試合オッズ', 'プライシング',
    'precios de partidos', 'cuotas del partido',
    'pricing des matchs', 'cotes du match'] },
  { type: 'soccer_predictions', title: "Today's Predictions", keywords: [
    "today's predictions", 'todays predictions', 'today predictions', 'upcoming matches',
    'next matches', 'what should i bet', 'value bets', 'picks today',
    '今日预测', '今天的预测', '今日比赛', '近期比赛', '即将开始', '推荐', '有什么价值',
    '本日の予測', '今日の予想', '今日の試合', '今後の試合',
    'predicciones de hoy', 'proximos partidos',
    'predictions du jour', 'prochains matchs'] },
  { type: 'soccer_divergence', title: 'Model vs Market', keywords: [
    'model vs market', 'model versus market', 'divergence', 'mispriced', 'vs the book',
    '模型 vs 市场', '模型vs市场', '分歧', '错价', '偏离',
    'モデル vs 市場', 'モデルと市場', '乖離',
    'modelo vs mercado', 'divergencia',
    'modele vs marche', 'divergence'] },
  { type: 'soccer_inplay', title: 'In-Play', keywords: [
    'in-play', 'in play', 'inplay', 'live match', 'live now', 'playing now', 'live betting',
    'hedge', 'cash out', 'cash-out',
    '滚球', '盘中', '实时比赛', '正在进行', '进行中', '现在的比赛', '正在踢', '对冲', '现金出',
    'インプレー', 'ライブ', '進行中', 'ヘッジ',
    'en vivo', 'en directo', 'partido en vivo',
    'en direct', 'match en direct', 'couverture'] },
  { type: 'soccer_pricetrack', title: 'Price Track', keywords: [
    'price track', 'pricetrack', 'price trajectory', 'milestone', 'mark to market', 'mark-to-market',
    '价格轨迹', '里程碑', '盯市', '价格追踪',
    '価格推移', 'マーク・トゥ・マーケット',
    'evolucion del precio', 'trayectoria del precio',
    'evolution du prix', 'trajectoire du prix'] },
  { type: 'soccer_schedule', title: 'Schedule', keywords: [
    'schedule', 'fixtures', 'kickoff time', 'kick off time', 'when do they play', 'next fixture',
    '赛程', '开赛时间', '什么时候踢', '赛程表', '下一场',
    '日程', 'キックオフ', '次の試合はいつ',
    'calendario', 'horario del partido',
    'calendrier', 'heure du coup d envoi'] },
  { type: 'soccer_performance', title: 'Accuracy & P&L', keywords: [
    'accuracy & p&l', 'accuracy and p&l', 'prediction accuracy', 'track record', 'brier',
    'how are we doing', 'bet log', 'profit and loss',
    '准确度 & 盈亏', '准确度', '盈亏', '战绩', '成绩单', '下注记录',
    '精度 & 損益', '損益', '成績', '的中率',
    'precision & p&l', 'historial', 'rendimiento',
    'precision & p&l', 'bilan', 'historique des paris'] },
  { type: 'soccer_calibration', title: 'Calibration (OOS)', keywords: [
    'calibration', 'reliability', 'out of sample', 'out-of-sample', 'oos',
    '校准', '可靠性', '样本外',
    'キャリブレーション', '較正', 'アウトオブサンプル',
    'calibracion', 'fiabilidad',
    'calibration', 'fiabilite'] },
  { type: 'soccer_backtest', title: 'Backtest (OOS)', keywords: [
    'backtest', 'back-test', 'blend curve', 'settled matches',
    '回测', '回溯测试', '混合曲线',
    'バックテスト', '検証',
    'backtest', 'prueba retrospectiva',
    'backtest', 'test retrospectif'] },
  { type: 'soccer_params', title: 'Parameter Sweep (OOS)', keywords: [
    'parameter sweep', 'param sweep', 'parameter set', 'param set', 'tuning',
    '参数搜索', '参数扫描', '参数集', '调参', '参数网格',
    'パラメータ探索', 'パラメータ',
    'busqueda de parametros', 'parametros',
    'recherche de parametres', 'parametres'] },
  { type: 'soccer_overview', title: 'System & Model Notes', keywords: [
    'system overview', 'how does it work', 'how it works', 'methodology', 'model notes',
    'what is this', 'model assumptions',
    '系统 & 模型说明', '系统总览', '总览', '系统概览', '方法论', '模型说明', '建模方法', '原理',
    'システム & モデル説明', 'システム概要', '方法論', 'モデル説明',
    'sistema & notas del modelo', 'como funciona', 'metodologia',
    'systeme & notes du modele', 'comment ca marche', 'methodologie'] },
  { type: 'soccer_venues', title: 'Venues & API', keywords: [
    'venue', 'venues', 'kalshi balance', 'polymarket balance', 'account balance',
    'api budget', 'request budget', 'risk', 'risk report', 'gate', 'pre-trade', 'order cap',
    'trade-grade', 'kill switch',
    '交易场所', '场所', '余额', '账户余额', 'api 预算', '预算', '额度', '风险', '风控', '闸门',
    '可交易等级', '下单上限', '熔断',
    '会場 & api', '残高', '予算', 'リスク', 'ゲート',
    'mercados & api', 'saldo', 'presupuesto', 'riesgo',
    'places & api', 'solde', 'budget', 'risque'] },
  { type: 'soccer_pdfs', title: 'Download Reports (PDF)', keywords: [
    'pdf', 'pdf report', 'download report', 'download the report',
    '下载报告', 'pdf 报告', 'pdf报告',
    'レポート', 'pdfレポート',
    'descargar informes', 'informe pdf',
    'telecharger', 'rapport pdf'] },
]

// ── text normalisation ───────────────────────────────────────────────────────
// Latin forms are compared as whole-word n-grams (never substrings) so "Lens" cannot
// fire inside "lenses"; CJK has no word boundaries, so those forms use indexOf.
const CJK_RE = /[぀-ヿ㐀-䶿一-鿿豈-﫿]/
const hasCjk = (s: string) => CJK_RE.test(s)

function normLatin(s: string): string {
  return (s || '')
    .normalize('NFD').replace(/[̀-ͯ]/g, '')   // Dečić -> decic, Žilina -> zilina
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, ' ')
    .trim()
}

/** Every 1..4-word window of the message, so a multi-word club/league name matches on
 *  word boundaries in one pass instead of 1,200 substring scans. */
function latinWindows(message: string, maxN = 4): Set<string> {
  const toks = normLatin(message).split(' ').filter(Boolean)
  const out = new Set<string>()
  for (let i = 0; i < toks.length; i++) {
    for (let n = 1; n <= maxN && i + n <= toks.length; n++) out.add(toks.slice(i, i + n).join(' '))
  }
  return out
}

// ── club index (lazy, from the SAME i18n the UI renders) ─────────────────────
// zh.json / ja.json carry soccer.club.<club_id> for all 399 clubs; the English name is
// the club's own `name` in the exported data. Loaded on first use and re-read after a
// TTL so a newly-promoted club (the Conference League long tail turns over) appears
// without a server restart. Any read failure degrades to "no club triggers", never a throw.
type ClubIndex = {
  latin: Map<string, string[]>   // normalised surface form → club_id[] (ties: 2 clubs share a name)
  cjk: Array<{ form: string; id: string; weak: boolean }>
  weakLatin: Set<string>
  names: Map<string, { id: string; name: string; league?: string }>
  loadedAt: number
}
const CLUB_TTL_MS = 15 * 60 * 1000
let _clubIndex: ClubIndex | null = null

// A surface form is "weak" when it is also an ordinary word in some language the user
// might be typing: 汉堡 = hamburger, 国际 = international, "Nice", "Como", "Lens".
// Weak forms only count when the message carries another soccer signal (a competition,
// a topic word, or a strong club name) — see matchClubs(). Short forms are weak by
// rule (≤5 Latin chars / ≤2 CJK chars); this set catches the longer offenders.
const EXTRA_WEAK_FORMS = new Set([
  'europa', 'junior', 'viking', 'national', 'union', 'sport', 'racing', 'progres',
  '流浪者', '游击队', '最强者', '欧罗巴', '大都会', '国际', '独立', '自由', '飓风', '大学生',
])

function readJsonSync(file: string): any {
  try { return JSON.parse(fs.readFileSync(file, 'utf-8')) } catch { return null }
}

function buildClubIndex(): ClubIndex {
  const idx: ClubIndex = {
    latin: new Map(), cjk: [], weakLatin: new Set(), names: new Map(), loadedAt: Date.now(),
  }
  // English names + league membership come from the exported style matrix (one row per
  // club in the whole system — the widest club roster we publish).
  const styles = readJsonSync(path.resolve(appRoot, 'public', 'data', 'soccer', 'team_styles.json'))
  for (const t of styles?.teams ?? []) {
    if (!t?.team_id || !t?.name) continue
    idx.names.set(t.team_id, { id: t.team_id, name: t.name, league: t.league })
    addForm(idx, t.team_id, t.name)
  }
  // 中文 / 日本語 names from the UI's own dictionary.
  for (const lang of ['zh', 'ja']) {
    const loc = readJsonSync(path.resolve(appRoot, 'src', 'i18n', 'locales', `${lang}.json`))
    for (const [id, name] of Object.entries<any>(loc?.soccer?.club ?? {})) {
      if (typeof name === 'string' && name) addForm(idx, id, name)
    }
  }
  return idx
}

function addForm(idx: ClubIndex, id: string, raw: string) {
  const form = raw.trim()
  if (!form) return
  if (hasCjk(form)) {
    const weak = form.length <= 2 || EXTRA_WEAK_FORMS.has(form)
    idx.cjk.push({ form, id, weak })
    return
  }
  const key = normLatin(form)
  if (!key) return
  const ids = idx.latin.get(key)
  if (ids) { if (!ids.includes(id)) ids.push(id) } else idx.latin.set(key, [id])
  if (key.replace(/ /g, '').length <= 5 || EXTRA_WEAK_FORMS.has(key)) idx.weakLatin.add(key)
}

function clubIndex(): ClubIndex {
  if (!_clubIndex || Date.now() - _clubIndex.loadedAt > CLUB_TTL_MS) _clubIndex = buildClubIndex()
  return _clubIndex
}

/** club_id → { name, league } for the clubs we know about (used by the tools to
 *  resolve "Arsenal" / "阿森纳" / "アーセナル" to one id before filtering the data). */
export function soccerClubMeta(id: string) { return clubIndex().names.get(id) ?? null }

/** Resolve a free-text club query to club_id(s). Used by the tools, where the caller
 *  named a club explicitly — so weak forms count here (no corroboration needed). */
export function resolveSoccerClub(query: string): string[] {
  if (!query) return []
  const idx = clubIndex()
  const exact = idx.latin.get(normLatin(query))
  if (exact?.length) return exact
  if (idx.names.has(query)) return [query]                       // already a club_id
  const hits = new Set<string>()
  for (const { form, id } of idx.cjk) if (form === query) hits.add(id)
  if (hits.size) return [...hits]
  // Loose fallback: the query contains a known name or vice versa (e.g. "曼联队",
  // "Man United" typed as "manchester united fc").
  const q = normLatin(query)
  if (q) for (const [key, ids] of idx.latin) {
    if (key.length >= 4 && (key.includes(q) || q.includes(key))) ids.forEach((i) => hits.add(i))
  }
  for (const { form, id } of idx.cjk) if (form.length >= 2 && query.includes(form)) hits.add(id)
  return [...hits]
}

// ── detection ────────────────────────────────────────────────────────────────
function matchLeagues(message: string, windows: Set<string>): string[] {
  const hits: string[] = []
  for (const lg of SOCCER_LEAGUES) {
    for (const alias of lg.aliases) {
      const hit = hasCjk(alias) ? message.includes(alias) : windows.has(normLatin(alias))
      if (hit) { hits.push(lg.id); break }
    }
  }
  return hits
}

function matchClubs(message: string, windows: Set<string>, corroborated: boolean): string[] {
  const idx = clubIndex()
  const strong = new Set<string>()
  const weak = new Set<string>()
  for (const key of windows) {
    const ids = idx.latin.get(key)
    if (ids) ids.forEach((id) => (idx.weakLatin.has(key) ? weak : strong).add(id))
  }
  for (const { form, id, weak: isWeak } of idx.cjk) {
    if (message.includes(form)) (isWeak ? weak : strong).add(id)
  }
  // A weak form ("Nice", 汉堡) only counts once something else in the message says
  // football — a competition, a topic word, or an unambiguous club name.
  if (corroborated || strong.size > 0) for (const id of weak) strong.add(id)
  return [...strong]
}

/**
 * Soccer-mode artifact detection: topic keywords in five languages, plus the
 * competition / club scoping that the "league → match" hierarchy needs.
 *
 * Returns the topic artifacts with `params` describing WHICH league and clubs the
 * question is about. When the user named only a team or a competition ("曼联?",
 * "how's Serie A looking"), we still open something useful rather than nothing:
 * two clubs → that matchup's pricing, one club → its upcoming predictions, a bare
 * competition → its table (or the champion board, for a cup with no standings).
 */
export function detectSoccerArtifacts(message: string): SoccerTrigger[] {
  if (!message) return []
  const lower = message.toLowerCase()
  const windows = latinWindows(lower)

  const leagues = matchLeagues(lower, windows)
  const topics: SoccerTrigger[] = []
  for (const pattern of SOCCER_KEYWORD_PATTERNS) {
    for (const keyword of pattern.keywords) {
      const kw = keyword.toLowerCase()
      const hit = hasCjk(kw) ? lower.includes(kw) : windows.has(normLatin(kw))
      if (hit) { topics.push({ type: pattern.type, title: pattern.title }); break }
    }
  }
  const clubs = matchClubs(lower, windows, leagues.length > 0 || topics.length > 0)

  // Scope every topic hit to the league/clubs the question named. When only clubs were
  // named, their league is implied — that is what the grounding loader filters on.
  const impliedLeagues = new Set(leagues)
  for (const id of clubs) {
    const lg = clubIndex().names.get(id)?.league
    if (lg) impliedLeagues.add(lg)
  }
  const params: Record<string, any> = {}
  if (leagues.length) params.league = leagues[0]
  else if (impliedLeagues.size === 1) params.league = [...impliedLeagues][0]
  if (leagues.length > 1) params.leagues = leagues
  if (clubs.length) params.clubs = clubs

  const scoped = (t: SoccerTrigger): SoccerTrigger =>
    Object.keys(params).length ? { ...t, params: { ...params } } : t

  if (topics.length) return topics.map(scoped)

  if (clubs.length >= 2) return [scoped({ type: 'soccer_match_pricing', title: 'Match Pricing' })]
  if (clubs.length === 1) return [scoped({ type: 'soccer_predictions', title: "Today's Predictions" })]
  if (leagues.length) {
    const kind = LEAGUE_BY_ID.get(leagues[0])?.kind
    const cup = kind === 'swiss_ucl' || kind === 'cup_two_leg'
    return [scoped(cup
      ? { type: 'soccer_season_odds', title: 'Season Odds' }
      : { type: 'soccer_league_table', title: 'League Table' })]
  }
  return []
}
