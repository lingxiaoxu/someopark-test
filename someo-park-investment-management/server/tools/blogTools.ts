// server/tools/blogTools.ts
// Blog RAG tools — fetches articles from someopark.com RSS feed,
// caches 1 hour, infers category from title+description (not RSS tags).
// 4 tools: blog_search, blog_list, blog_summary, blog_read
// Also exports getBlogGrounding() for plain-chat context injection.

import type { AgentTool } from './index.js'

// ── RSS feed URL ────────────────────────────────────────────────────────────
const RSS_URL = 'https://www.someopark.com/blog-feed.xml'
const CACHE_TTL_MS = 60 * 60 * 1000  // 1 hour

// ── Category inference ──────────────────────────────────────────────────────
const CATEGORIES = [
  'Market Observe of Signal and Event',
  'Private Market Ideas',
  'Public Market Ideas',
  'Tech',
] as const
type Category = typeof CATEGORIES[number]

// Keyword-based category inference from title + description (not relying on RSS tags)
const CATEGORY_RULES: Array<{ category: Category; patterns: RegExp[] }> = [
  {
    category: 'Tech',
    patterns: [
      /\b(dashboard|frontend|agent|AI\s+quant|prediction\s+market|system|code|deploy|hackathon|NBA|quantitative.*framework|asset.pricing.*framework|stanse)\b/i,
      /\b(仪表盘|前端|代理|量化.*工具|预测市场|系统|部署|黑客马拉松|驾驶舱|评估体系)\b/,
    ],
  },
  {
    category: 'Private Market Ideas',
    patterns: [
      /\b(private\s+credit|private\s+market|BDC|direct\s+lending|CLO|leveraged\s+loan|middle\s+market|mezzanine|covenant)\b/i,
      /\b(私募信贷|私募市场|直接贷款|杠杆贷款|中间市场|夹层)\b/,
    ],
  },
  {
    category: 'Public Market Ideas',
    patterns: [
      /\b(equity\s+valuation|stock\s+pick|sector\s+rotation|earnings|buyback|IPO|S&P\s*500|Russell|multiple|P\/E)\b/i,
      /\b(股票估值|选股|板块轮动|财报|回购|上市|标普|市盈率)\b/,
    ],
  },
  // Default fallback: Market Observe of Signal and Event (macro, rates, fed, inflation, labor, bonds, dollar)
]

function inferCategory(title: string, description: string): Category {
  const text = `${title} ${description}`
  for (const rule of CATEGORY_RULES) {
    if (rule.patterns.some(p => p.test(text))) return rule.category
  }
  return 'Market Observe of Signal and Event'
}

// ── Language detection ───────────────────────────────────────────────────────
function detectLanguage(title: string, url: string): 'en' | 'zh' {
  if (url.includes('/zh/')) return 'zh'
  // CJK character range
  if (/[\u4e00-\u9fff]/.test(title)) return 'zh'
  return 'en'
}

// ── Article type ────────────────────────────────────────────────────────────
export interface BlogArticle {
  title: string
  description: string
  url: string
  category: Category
  language: 'en' | 'zh'
  date: string       // ISO date string
  author: string
  imageUrl?: string
}

// ── RSS parsing (simple XML → articles, no external dep) ────────────────────
function parseRSS(xml: string): BlogArticle[] {
  const articles: BlogArticle[] = []
  // Split by <item>...</item>
  const items = xml.split('<item>').slice(1)
  for (const item of items) {
    const endIdx = item.indexOf('</item>')
    const block = endIdx >= 0 ? item.substring(0, endIdx) : item

    const title = extractCDATA(block, 'title') || extractTag(block, 'title') || ''
    const description = extractCDATA(block, 'description') || extractTag(block, 'description') || ''
    const link = extractTag(block, 'link') || ''
    const pubDate = extractTag(block, 'pubDate') || ''
    const creator = extractCDATA(block, 'dc:creator') || extractTag(block, 'dc:creator') || 'Lingxiao Xu'
    const enclosure = block.match(/enclosure\s+url="([^"]*)"/)
    const imageUrl = enclosure ? enclosure[1] : undefined

    if (!title || !link) continue

    const language = detectLanguage(title, link)
    const category = inferCategory(title, description)
    const date = pubDate ? new Date(pubDate).toISOString().slice(0, 10) : ''

    articles.push({ title, description, url: link, category, language, date, author: creator, imageUrl })
  }
  return articles
}

function extractCDATA(block: string, tag: string): string | null {
  const re = new RegExp(`<${tag}[^>]*>\\s*<!\\[CDATA\\[([\\s\\S]*?)\\]\\]>\\s*</${tag}>`)
  const m = block.match(re)
  return m ? m[1].trim() : null
}

function extractTag(block: string, tag: string): string | null {
  const re = new RegExp(`<${tag}[^>]*>([^<]*)</${tag}>`)
  const m = block.match(re)
  return m ? m[1].trim() : null
}

// ── Sitemap parsing (supplements RSS with older articles) ────────────────────
const SITEMAP_URL = 'https://www.someopark.com/blog-posts-sitemap.xml'

function slugToTitle(slug: string): string {
  // Convert URL slug to approximate title: "the-fed-s-work" → "The Fed S Work"
  return slug
    .replace(/-/g, ' ')
    .replace(/\b\w/g, c => c.toUpperCase())
    .replace(/\b(S|T|M|D|Re|Ve|Ll)\b/g, m => m.toLowerCase())  // fix common contractions
    .trim()
}

async function fetchSitemapArticles(): Promise<BlogArticle[]> {
  try {
    const res = await fetch(SITEMAP_URL, { signal: AbortSignal.timeout(15_000) })
    if (!res.ok) return []
    const xml = await res.text()
    const articles: BlogArticle[] = []
    const urlBlocks = xml.split('<url>').slice(1)
    for (const block of urlBlocks) {
      const locMatch = block.match(/<loc>([^<]+)<\/loc>/)
      const modMatch = block.match(/<lastmod>([^<]+)<\/lastmod>/)
      if (!locMatch) continue
      const url = locMatch[1]
      // Only include /post/ URLs (not /zh/post/ — those come from RSS or have their own sitemap entry)
      if (!url.includes('/post/')) continue
      const slug = url.split('/post/').pop() || ''
      const title = slugToTitle(slug)
      const language = url.includes('/zh/') ? 'zh' as const : 'en' as const
      const date = modMatch ? modMatch[1] : ''
      const category = inferCategory(title, '')
      articles.push({ title, description: '', url, category, language, date, author: 'Lingxiao Xu' })
    }
    return articles
  } catch {
    return []
  }
}

// ── Cache layer ─────────────────────────────────────────────────────────────
let _cache: { articles: BlogArticle[]; fetchedAt: number } | null = null

async function getArticles(): Promise<BlogArticle[]> {
  const now = Date.now()
  if (_cache && (now - _cache.fetchedAt) < CACHE_TTL_MS) {
    return _cache.articles
  }
  try {
    // Fetch RSS (rich metadata, ~20 latest articles) + sitemap (all URLs, no description)
    const [rssRes, sitemapArticles] = await Promise.all([
      fetch(RSS_URL, { signal: AbortSignal.timeout(15_000) }),
      fetchSitemapArticles(),
    ])
    if (!rssRes.ok) throw new Error(`RSS fetch failed: ${rssRes.status}`)
    const xml = await rssRes.text()
    const rssArticles = parseRSS(xml)

    // Merge: RSS articles take priority (have description); sitemap fills gaps
    const urlSet = new Set(rssArticles.map(a => a.url))
    const merged = [...rssArticles]
    for (const sm of sitemapArticles) {
      if (!urlSet.has(sm.url)) {
        urlSet.add(sm.url)
        merged.push(sm)
      }
    }

    if (merged.length > 0) {
      _cache = { articles: merged, fetchedAt: now }
    }
    return merged
  } catch (e: any) {
    // On fetch failure, return stale cache if available
    if (_cache) {
      console.warn(`[blog] RSS fetch failed (${e.message}), using stale cache (${_cache.articles.length} articles)`)
      return _cache.articles
    }
    throw e
  }
}

// ── Search helper ───────────────────────────────────────────────────────────
function searchArticles(
  articles: BlogArticle[],
  query: string,
  opts: { category?: string; language?: string; top_k?: number }
): BlogArticle[] {
  let filtered = articles

  if (opts.language && opts.language !== 'all') {
    filtered = filtered.filter(a => a.language === opts.language)
  }
  if (opts.category) {
    const cat = opts.category.toLowerCase()
    filtered = filtered.filter(a => a.category.toLowerCase().includes(cat))
  }

  // Score by keyword match in title + description
  const queryTerms = query.toLowerCase().split(/\s+/).filter(t => t.length > 1)
  const scored = filtered.map(a => {
    const text = `${a.title} ${a.description}`.toLowerCase()
    let score = 0
    for (const term of queryTerms) {
      if (a.title.toLowerCase().includes(term)) score += 3  // title match worth more
      if (a.description.toLowerCase().includes(term)) score += 1
    }
    return { article: a, score }
  })

  scored.sort((a, b) => b.score - a.score || b.article.date.localeCompare(a.article.date))
  const top_k = opts.top_k || 5
  // Only return articles with score > 0, or top_k by date if no keyword match
  const matched = scored.filter(s => s.score > 0).slice(0, top_k)
  if (matched.length > 0) return matched.map(s => s.article)
  // Fallback: most recent articles (filtered by category/language)
  return filtered.sort((a, b) => b.date.localeCompare(a.date)).slice(0, top_k)
}

// ── Plain chat grounding (exported for prompt.ts) ───────────────────────────
export async function getBlogGrounding(n: number = 10): Promise<string> {
  try {
    const articles = await getArticles()
    // Deduplicate: group EN/ZH pairs by slug, pick the language that matches
    // For grounding, just show the latest N unique articles (both languages)
    const recent = articles
      .sort((a, b) => b.date.localeCompare(a.date))
      .slice(0, n)
    if (recent.length === 0) return ''

    const lines = recent.map(a =>
      `- [${a.language.toUpperCase()}] ${a.title} (${a.category}, ${a.date}) → ${a.url}`
    )
    return `\n## Someo Park Blog (latest ${recent.length} articles)\n` +
      `The company publishes macro/market research at someopark.com/blog. ` +
      `When relevant, cite these articles by title and link.\n` +
      lines.join('\n') + '\n'
  } catch {
    return ''  // Silently degrade — blog grounding is optional
  }
}

// ── Tool 1: blog_search ─────────────────────────────────────────────────────
export const blogSearchTool: AgentTool = {
  definition: {
    name: 'blog_search',
    description: 'Search Someo Park blog articles (80+ macro/market research pieces) by keyword. ' +
      'Returns titles, excerpts, URLs, inferred categories, and publication dates. ' +
      'Categories: Market Observe of Signal and Event | Private Market Ideas | Public Market Ideas | Tech. ' +
      'Use this when the user asks about macro analysis, market views, research opinions, or Someo Park publications.',
    input_schema: {
      type: 'object',
      properties: {
        query: { type: 'string', description: 'Search keywords (English or Chinese)' },
        category: {
          type: 'string',
          description: 'Filter by category (optional). One of: market, private, public, tech',
          enum: ['market', 'private', 'public', 'tech'],
        },
        language: {
          type: 'string',
          description: 'Filter by language: en, zh, or all (default all)',
          enum: ['en', 'zh', 'all'],
        },
        top_k: { type: 'number', description: 'Max results (default 5)' },
      },
      required: ['query'],
    },
  },
  async execute(input: { query: string; category?: string; language?: string; top_k?: number }) {
    const articles = await getArticles()
    // Map short category names to full names
    const catMap: Record<string, string> = {
      market: 'market observe', private: 'private market', public: 'public market', tech: 'tech',
    }
    const category = input.category ? catMap[input.category] || input.category : undefined
    const results = searchArticles(articles, input.query, {
      category, language: input.language, top_k: input.top_k,
    })
    return {
      total_articles: articles.length,
      results_count: results.length,
      results: results.map(a => ({
        title: a.title,
        url: a.url,
        category: a.category,
        language: a.language,
        date: a.date,
        excerpt: a.description.slice(0, 300),
      })),
    }
  },
  isConcurrencySafe: () => true,
  isReadOnly: () => true,
}

// ── Tool 2: blog_list ───────────────────────────────────────────────────────
export const blogListTool: AgentTool = {
  definition: {
    name: 'blog_list',
    description: 'List all Someo Park blog articles, optionally filtered by category or language. ' +
      'Returns titles, URLs, dates, and inferred categories. Use this for browsing or when the user ' +
      'asks "what articles do you have about X category" or "list recent blog posts".',
    input_schema: {
      type: 'object',
      properties: {
        category: {
          type: 'string',
          description: 'Filter by category (optional)',
          enum: ['market', 'private', 'public', 'tech'],
        },
        language: {
          type: 'string',
          description: 'Filter: en, zh, or all (default all)',
          enum: ['en', 'zh', 'all'],
        },
      },
      required: [],
    },
  },
  async execute(input: { category?: string; language?: string }) {
    const articles = await getArticles()
    let filtered = articles
    if (input.language && input.language !== 'all') {
      filtered = filtered.filter(a => a.language === input.language)
    }
    if (input.category) {
      const catMap: Record<string, string> = {
        market: 'market observe', private: 'private market', public: 'public market', tech: 'tech',
      }
      const cat = (catMap[input.category] || input.category).toLowerCase()
      filtered = filtered.filter(a => a.category.toLowerCase().includes(cat))
    }
    filtered.sort((a, b) => b.date.localeCompare(a.date))

    // Group by category for summary
    const byCat: Record<string, number> = {}
    for (const a of filtered) {
      byCat[a.category] = (byCat[a.category] || 0) + 1
    }

    return {
      total: filtered.length,
      by_category: byCat,
      articles: filtered.map(a => ({
        title: a.title,
        url: a.url,
        category: a.category,
        language: a.language,
        date: a.date,
      })),
    }
  },
  isConcurrencySafe: () => true,
  isReadOnly: () => true,
}

// ── Tool 3: blog_summary ────────────────────────────────────────────────────
export const blogSummaryTool: AgentTool = {
  definition: {
    name: 'blog_summary',
    description: 'Get a summary/abstract of a specific blog article by URL or title keyword. ' +
      'Returns the article title, description/abstract, category, date, and full URL. ' +
      'Use this when the user wants to know what a specific article is about without reading the full text.',
    input_schema: {
      type: 'object',
      properties: {
        query: {
          type: 'string',
          description: 'Article URL, or a keyword/phrase from the title to find the article',
        },
      },
      required: ['query'],
    },
  },
  async execute(input: { query: string }) {
    const articles = await getArticles()
    const q = input.query.toLowerCase()

    // Try exact URL match first
    let match = articles.find(a => a.url === input.query || a.url.endsWith(q))

    // Then try title keyword match
    if (!match) {
      const scored = articles.map(a => {
        const title = a.title.toLowerCase()
        const desc = a.description.toLowerCase()
        let score = 0
        for (const term of q.split(/\s+/).filter(t => t.length > 1)) {
          if (title.includes(term)) score += 3
          if (desc.includes(term)) score += 1
        }
        return { article: a, score }
      }).filter(s => s.score > 0)
      scored.sort((a, b) => b.score - a.score)
      match = scored[0]?.article
    }

    if (!match) {
      return { found: false, message: `No article found matching "${input.query}". Try blog_search for broader results.` }
    }

    return {
      found: true,
      title: match.title,
      url: match.url,
      category: match.category,
      language: match.language,
      date: match.date,
      author: match.author,
      abstract: match.description,
      image: match.imageUrl || null,
    }
  },
  isConcurrencySafe: () => true,
  isReadOnly: () => true,
}

// ── Tool 4: blog_read ───────────────────────────────────────────────────────
export const blogReadTool: AgentTool = {
  definition: {
    name: 'blog_read',
    description: 'Fetch the full text content of a specific blog article by URL. ' +
      'Extracts the main article body from the page HTML. Use this when the user ' +
      'wants to read or deeply analyze a specific article\'s content (not just the abstract). ' +
      'Requires an article URL (get it from blog_search or blog_list first).',
    input_schema: {
      type: 'object',
      properties: {
        url: { type: 'string', description: 'Full article URL from someopark.com' },
      },
      required: ['url'],
    },
  },
  async execute(input: { url: string }) {
    if (!input.url.includes('someopark.com')) {
      return { error: 'Only someopark.com article URLs are supported.' }
    }
    try {
      const res = await fetch(input.url, {
        signal: AbortSignal.timeout(20_000),
        headers: { 'User-Agent': 'SomeoAgent/1.0 (internal research assistant)' },
      })
      if (!res.ok) return { error: `Failed to fetch article: HTTP ${res.status}` }
      const html = await res.text()

      // Extract main article content from Wix blog HTML
      // Wix uses data-hook="post-content" or class containing "post-content"
      let content = ''

      // Strategy 1: Look for post-description rich-text blocks
      const richTextMatch = html.match(/<div[^>]*class="[^"]*post-content[^"]*"[^>]*>([\s\S]*?)<\/div>\s*<\/div>\s*<\/div>/i)
      if (richTextMatch) {
        content = richTextMatch[1]
      }

      // Strategy 2: Extract all <p> tags within the main content area
      if (!content) {
        const paragraphs = html.match(/<p[^>]*>([\s\S]*?)<\/p>/gi) || []
        // Filter out nav/footer paragraphs (short, no substance)
        const substantive = paragraphs
          .map(p => p.replace(/<[^>]+>/g, '').trim())
          .filter(t => t.length > 50)
        content = substantive.join('\n\n')
      }

      // Clean HTML tags
      content = content
        .replace(/<[^>]+>/g, '')
        .replace(/&nbsp;/g, ' ')
        .replace(/&amp;/g, '&')
        .replace(/&lt;/g, '<')
        .replace(/&gt;/g, '>')
        .replace(/&quot;/g, '"')
        .replace(/&#39;/g, "'")
        .replace(/\n{3,}/g, '\n\n')
        .trim()

      // Find the article metadata from cache
      const articles = await getArticles()
      const meta = articles.find(a => a.url === input.url)

      if (!content || content.length < 100) {
        // Fallback: return the RSS description
        return {
          title: meta?.title || 'Unknown',
          url: input.url,
          content_source: 'rss_description',
          content: meta?.description || 'Could not extract article content. Visit the URL directly.',
          note: 'Full text extraction failed for this Wix page. The RSS abstract is shown instead.',
        }
      }

      // Truncate to ~8000 chars to stay within tool result limits
      const maxLen = 8000
      const truncated = content.length > maxLen
      if (truncated) content = content.slice(0, maxLen) + '\n\n[... truncated, visit URL for full article]'

      return {
        title: meta?.title || 'Article',
        url: input.url,
        category: meta?.category || 'Unknown',
        language: meta?.language || 'en',
        date: meta?.date || '',
        content_source: 'html_extraction',
        content,
        truncated,
        char_count: content.length,
      }
    } catch (e: any) {
      return { error: `Fetch failed: ${e.message}` }
    }
  },
  isConcurrencySafe: () => true,
  isReadOnly: () => true,
}
