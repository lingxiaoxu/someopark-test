#!/usr/bin/env node
// Convert 42 .docx files to Markdown with YAML frontmatter
import mammoth from 'mammoth'
import fs from 'fs'
import path from 'path'

const SRC = path.resolve('..', 'public')
const OUT = path.resolve('..', 'public', 'knowledge_base')

// Mapping: source filename → [KB-ID, output filename, title, language, topics[]]
const MAPPING = [
  ['44 Regression Models and Their Applications in Private Credit (PC) & Asset-Backed Finance (ABF).docx', 'KB-01', 'KB-01_44_Regression_Models_PC_ABF_English.md', '44 Regression Models in PC & ABF (English)', 'en', ['regression','private-credit','asset-backed-finance']],
  ['44种回归模型在私人信贷（PC）与资产支持融资（ABF）量化投资中的应用合集.docx', 'KB-02', 'KB-02_44种回归模型PC_ABF量化投资应用合集.md', '44种回归模型PC ABF量化投资应用合集', 'zh', ['regression','private-credit','asset-backed-finance']],
  ['44种回归模型在私募信贷（PC）与资产支持融资（ABF）应用关键信息总结表.docx', 'KB-03', 'KB-03_44种回归模型PC_ABF应用关键信息总结表.md', '44种回归模型PC ABF应用关键信息总结表', 'zh', ['regression','private-credit','summary']],
  ['A Collection of 44 Regression Models and Their Applications in Quantitative Investing for Private Credit (PC) and Asset-Backed Finance (ABF).docx', 'KB-04', 'KB-04_44_Regression_Models_Quantitative_Investing_PC_ABF.md', '44 Regression Models Quantitative Investing PC ABF', 'en', ['regression','private-credit','quantitative']],
  ['ABF Oaktree Capital.docx', 'KB-05', 'KB-05_ABF_Oaktree_Capital_Asset_Backed_Finance.md', 'ABF Oaktree Capital Asset-Backed Finance', 'en', ['oaktree','asset-backed-finance','distressed']],
  ['Convenant Quant Strats.docx', 'KB-06', 'KB-06_Covenant_Quantitative_Strategies.md', 'Covenant Quantitative Strategies', 'en', ['covenants','quantitative','strategies']],
  ['Convexity Analysis 3.docx', 'KB-07', 'KB-07_Convexity_Analysis_Fixed_Income.md', 'Convexity Analysis Fixed Income', 'en', ['convexity','fixed-income','duration']],
  ['Debt Instrument.docx', 'KB-08', 'KB-08_Debt_Instrument_Structures_Analysis.md', 'Debt Instrument Structures Analysis', 'en', ['debt','instruments','structures']],
  ['Designing And Pricing Bond Covenants中文（含注释与公式推导）.docx', 'KB-09', 'KB-09_Bond_Covenants_Design_Pricing_公式推导.md', 'Bond Covenants Design Pricing 公式推导', 'zh', ['covenants','bond-pricing','formulas']],
  ['Fixed-Income Arbitrage Practice.docx', 'KB-10', 'KB-10_Fixed_Income_Arbitrage_Practice.md', 'Fixed-Income Arbitrage Practice', 'en', ['fixed-income','arbitrage','practice']],
  ['Key Square Capital Management in English.docx', 'KB-11', 'KB-11_Key_Square_Capital_Management_English.md', 'Key Square Capital Management (English)', 'en', ['key-square','macro','capital-management']],
  ['Key Square Capital Management.docx', 'KB-12', 'KB-12_Key_Square_Capital_Management_中文.md', 'Key Square Capital Management 中文', 'zh', ['key-square','macro','capital-management']],
  ['NAV Fund.docx', 'KB-13', 'KB-13_NAV_Fund_Structure_Analysis.md', 'NAV Fund Structure Analysis', 'en', ['nav-fund','fund-structure','private-credit']],
  ['Oaktree Interview.docx', 'KB-14', 'KB-14_Oaktree_Interview_Distressed_Investing.md', 'Oaktree Interview Distressed Investing', 'en', ['oaktree','distressed','interview']],
  ['Private Credit - Ares.docx', 'KB-15', 'KB-15_Private_Credit_Ares_Management.md', 'Private Credit Ares Management', 'en', ['ares','private-credit','direct-lending']],
  ['VCOP Quant Strats.docx', 'KB-16', 'KB-16_VCOP_Quantitative_Strategies.md', 'VCOP Quantitative Strategies', 'en', ['vcop','goldman-sachs','quantitative']],
  ['估值重构.docx', 'KB-17', 'KB-17_估值重构_Valuation_Restructuring.md', '估值重构 Valuation Restructuring', 'zh', ['valuation','restructuring']],
  ['可转换债券套利.docx', 'KB-18', 'KB-18_可转换债券套利_Convertible_Bond_Arbitrage.md', '可转换债券套利 Convertible Bond Arbitrage', 'zh', ['convertible-bond','arbitrage']],
  ['因子、策略与资产组合层面的绩效衡量体系研究.docx', 'KB-19', 'KB-19_因子策略资产组合绩效衡量体系研究.md', '因子策略资产组合绩效衡量体系研究', 'zh', ['factor','performance','portfolio']],
  ['固定收益套利.docx', 'KB-20', 'KB-20_固定收益套利_Fixed_Income_Arbitrage.md', '固定收益套利 Fixed Income Arbitrage', 'zh', ['fixed-income','arbitrage']],
  ['宏观视角下的市场与投资.docx', 'KB-21', 'KB-21_宏观视角下的市场与投资_Macro_Perspectives.md', '宏观视角下的市场与投资', 'zh', ['macro','market','investment']],
  ['市场结构与宏观分析.docx', 'KB-22', 'KB-22_市场结构与宏观分析_Market_Structure_Macro.md', '市场结构与宏观分析', 'zh', ['market-structure','macro','analysis']],
  ['引言与核心投资哲学的数学诠释.docx', 'KB-23', 'KB-23_核心投资哲学数学诠释_Investment_Philosophy.md', '核心投资哲学数学诠释', 'zh', ['investment-philosophy','mathematics']],
  ['橡树资本：困境投资深度分析.docx', 'KB-24', 'KB-24_橡树资本困境投资深度分析_Oaktree_Distressed.md', '橡树资本困境投资深度分析', 'zh', ['oaktree','distressed','deep-analysis']],
  ['生成式AI时代的特征选择.docx', 'KB-25', 'KB-25_生成式AI时代特征选择_Feature_Selection.md', '生成式AI时代特征选择', 'zh', ['ai','feature-selection','machine-learning']],
  ['私人信贷市场的多维解析：结构、表现与前沿机遇.docx', 'KB-26', 'KB-26_私人信贷市场多维解析_Structure_Performance.md', '私人信贷市场多维解析', 'zh', ['private-credit','market-structure','performance']],
  ['私人信贷市场的演化与分化：一项关于投资级直接贷款及宏观趋势的综合分析.docx', 'KB-27', 'KB-27_私人信贷市场演化与分化_IG_Direct_Lending.md', '私人信贷市场演化与分化', 'zh', ['private-credit','investment-grade','direct-lending']],
  ['私人信贷市场的结构性崛起：另类资产管理公司的增长机遇与战略定位评估.docx', 'KB-28', 'KB-28_私人信贷结构性崛起_Alternative_AM_Growth.md', '私人信贷结构性崛起', 'zh', ['private-credit','alternative-assets','growth']],
  ['私人信贷观察 Neither systemic nor immune - summary.docx', 'KB-29', 'KB-29_私人信贷观察_Neither_Systemic_Nor_Immune_Summary.md', '私人信贷观察 Summary', 'zh', ['private-credit','systemic-risk','summary']],
  ['私人信贷观察 Neither systemic nor immune.docx', 'KB-30', 'KB-30_私人信贷观察_Neither_Systemic_Nor_Immune_Full.md', '私人信贷观察 Full', 'zh', ['private-credit','systemic-risk']],
  ['私人信贷：评估资产管理公司的增长机会.docx', 'KB-31', 'KB-31_私人信贷评估AM增长机会.md', '私人信贷评估AM增长机会', 'zh', ['private-credit','asset-management','growth']],
  ['私募信贷.docx', 'KB-32', 'KB-32_私募信贷_Private_Credit_Overview.md', '私募信贷 Private Credit Overview', 'zh', ['private-credit','overview']],
  ['私有信贷的重构：基于区块链的实物资产（RWA）协议化的理论与应用.docx', 'KB-33', 'KB-33_私有信贷重构_RWA_Blockchain_Protocol.md', '私有信贷重构 RWA Blockchain', 'zh', ['private-credit','rwa','blockchain']],
  ['系统化投资近期宏观观察.docx', 'KB-34', 'KB-34_系统化投资近期宏观观察_Systematic_Macro.md', '系统化投资近期宏观观察', 'zh', ['systematic','macro','observation']],
  ['系统性投资与规则化.docx', 'KB-35', 'KB-35_系统性投资与规则化_Systematic_Rules.md', '系统性投资与规则化', 'zh', ['systematic','rules','investment']],
  ['系统性投资的核心框架.docx', 'KB-36', 'KB-36_系统性投资核心框架_Core_Framework.md', '系统性投资核心框架', 'zh', ['systematic','framework']],
  ['解读霍华德·马克斯的访谈.docx', 'KB-37', 'KB-37_解读霍华德马克斯访谈_Howard_Marks.md', '解读霍华德马克斯访谈', 'zh', ['howard-marks','oaktree','interview']],
  ['非对称性策略执行摘要.docx', 'KB-38', 'KB-38_非对称性策略执行摘要_Asymmetric_Strategy.md', '非对称性策略执行摘要', 'zh', ['asymmetric','strategy']],
  ['高盛AWM私募渠道EaR模型深度解析：NII与EaR.docx', 'KB-39', 'KB-39_高盛AWM_EaR模型深度解析_NII.md', '高盛AWM EaR模型深度解析', 'zh', ['goldman-sachs','ear-model','nii']],
  ['高盛GBM与AWM信贷业务深度分析.docx', 'KB-40', 'KB-40_高盛GBM_AWM信贷业务深度分析.md', '高盛GBM AWM信贷业务深度分析', 'zh', ['goldman-sachs','credit','gbm','awm']],
  ['高盛Vintage信贷机会伙伴基金（VCOP）深度解析：策略、业绩与量化分析.docx', 'KB-41', 'KB-41_高盛VCOP深度解析_策略业绩量化分析.md', '高盛VCOP深度解析', 'zh', ['goldman-sachs','vcop','quantitative']],
  ['黑石私人信贷基金（BDC）招股说明书.docx', 'KB-42', 'KB-42_黑石BDC私人信贷基金招股说明书_Blackstone.md', '黑石BDC私人信贷基金招股说明书', 'zh', ['blackstone','bdc','private-credit','prospectus']],
]

// Simple HTML → Markdown converter
function htmlToMd(html) {
  let md = html
  // Remove style/script tags
  md = md.replace(/<style[^>]*>[\s\S]*?<\/style>/gi, '')
  md = md.replace(/<script[^>]*>[\s\S]*?<\/script>/gi, '')
  // Headings
  md = md.replace(/<h1[^>]*>([\s\S]*?)<\/h1>/gi, '\n# $1\n')
  md = md.replace(/<h2[^>]*>([\s\S]*?)<\/h2>/gi, '\n## $1\n')
  md = md.replace(/<h3[^>]*>([\s\S]*?)<\/h3>/gi, '\n### $1\n')
  md = md.replace(/<h4[^>]*>([\s\S]*?)<\/h4>/gi, '\n#### $1\n')
  md = md.replace(/<h5[^>]*>([\s\S]*?)<\/h5>/gi, '\n##### $1\n')
  md = md.replace(/<h6[^>]*>([\s\S]*?)<\/h6>/gi, '\n###### $1\n')
  // Bold/italic
  md = md.replace(/<(strong|b)>([\s\S]*?)<\/\1>/gi, '**$2**')
  md = md.replace(/<(em|i)>([\s\S]*?)<\/\1>/gi, '*$2*')
  // Links
  md = md.replace(/<a[^>]*href="([^"]*)"[^>]*>([\s\S]*?)<\/a>/gi, '[$2]($1)')
  // Images
  md = md.replace(/<img[^>]*src="([^"]*)"[^>]*alt="([^"]*)"[^>]*\/?>/gi, '![$2]($1)')
  md = md.replace(/<img[^>]*src="([^"]*)"[^>]*\/?>/gi, '![]($1)')
  // Lists
  md = md.replace(/<ul[^>]*>/gi, '\n')
  md = md.replace(/<\/ul>/gi, '\n')
  md = md.replace(/<ol[^>]*>/gi, '\n')
  md = md.replace(/<\/ol>/gi, '\n')
  md = md.replace(/<li[^>]*>([\s\S]*?)<\/li>/gi, '- $1\n')
  // Table handling
  md = md.replace(/<table[^>]*>([\s\S]*?)<\/table>/gi, (match, inner) => {
    const rows = []
    const rowRegex = /<tr[^>]*>([\s\S]*?)<\/tr>/gi
    let rowMatch
    while ((rowMatch = rowRegex.exec(inner)) !== null) {
      const cells = []
      const cellRegex = /<t[hd][^>]*>([\s\S]*?)<\/t[hd]>/gi
      let cellMatch
      while ((cellMatch = cellRegex.exec(rowMatch[1])) !== null) {
        cells.push(cellMatch[1].replace(/<[^>]+>/g, '').trim())
      }
      rows.push(cells)
    }
    if (rows.length === 0) return ''
    const maxCols = Math.max(...rows.map(r => r.length))
    const lines = []
    rows.forEach((row, i) => {
      while (row.length < maxCols) row.push('')
      lines.push('| ' + row.join(' | ') + ' |')
      if (i === 0) {
        lines.push('| ' + row.map(() => '---').join(' | ') + ' |')
      }
    })
    return '\n' + lines.join('\n') + '\n'
  })
  // Paragraphs and line breaks
  md = md.replace(/<br\s*\/?>/gi, '\n')
  md = md.replace(/<p[^>]*>([\s\S]*?)<\/p>/gi, '\n$1\n')
  md = md.replace(/<div[^>]*>([\s\S]*?)<\/div>/gi, '\n$1\n')
  // Remove remaining HTML tags
  md = md.replace(/<[^>]+>/g, '')
  // Decode HTML entities
  md = md.replace(/&amp;/g, '&').replace(/&lt;/g, '<').replace(/&gt;/g, '>')
  md = md.replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&nbsp;/g, ' ')
  // Clean up whitespace
  md = md.replace(/\n{3,}/g, '\n\n')
  md = md.trim()
  return md
}

async function convert() {
  const results = []
  const index = []

  for (const [srcFile, id, outFile, title, lang, topics] of MAPPING) {
    const srcPath = path.join(SRC, srcFile)
    const outPath = path.join(OUT, outFile)

    if (!fs.existsSync(srcPath)) {
      console.log(`[SKIP] ${id}: source not found: ${srcFile}`)
      results.push({ id, status: 'SKIP', reason: 'source not found' })
      continue
    }

    try {
      // Convert to HTML
      const { value: html } = await mammoth.convertToHtml({ path: srcPath })
      // Also get raw text for verification
      const { value: rawText } = await mammoth.extractRawText({ path: srcPath })

      // Convert HTML to Markdown
      const mdBody = htmlToMd(html)

      // Build frontmatter
      const frontmatter = [
        '---',
        `id: ${id}`,
        `title: "${title}"`,
        `source: "${srcFile}"`,
        `language: ${lang}`,
        `topics: [${topics.join(', ')}]`,
        '---',
        ''
      ].join('\n')

      const fullMd = frontmatter + mdBody

      fs.writeFileSync(outPath, fullMd, 'utf8')

      // Verification: compare char counts
      const rawChars = rawText.replace(/\s+/g, '').length
      const mdChars = mdBody.replace(/[#*|\-_>`\[\](){}]/g, '').replace(/\s+/g, '').length
      const diff = rawChars > 0 ? Math.abs(rawChars - mdChars) / rawChars : 0
      const pass = diff < 0.15 // 15% tolerance (markdown syntax adds/removes chars)

      results.push({ id, status: pass ? 'PASS' : 'WARN', rawChars, mdChars, diff: (diff * 100).toFixed(1) + '%' })
      console.log(`[${pass ? 'PASS' : 'WARN'}] ${id}: docx=${rawChars} md=${mdChars} diff=${(diff * 100).toFixed(1)}%`)

      index.push({
        id, file: outFile, title, language: lang, topics,
        charCount: fullMd.length
      })
    } catch (e) {
      console.log(`[FAIL] ${id}: ${e.message}`)
      results.push({ id, status: 'FAIL', error: e.message })
    }
  }

  // Write index
  fs.writeFileSync(path.join(OUT, 'KB_INDEX.json'), JSON.stringify(index, null, 2), 'utf8')

  // Summary
  const passed = results.filter(r => r.status === 'PASS').length
  const warned = results.filter(r => r.status === 'WARN').length
  const failed = results.filter(r => r.status === 'FAIL').length
  const skipped = results.filter(r => r.status === 'SKIP').length
  console.log(`\n=== SUMMARY ===`)
  console.log(`PASS: ${passed}, WARN: ${warned}, FAIL: ${failed}, SKIP: ${skipped} / ${MAPPING.length} total`)
  console.log(`KB_INDEX.json: ${index.length} entries`)
}

convert().catch(e => { console.error(e); process.exit(1) })
