#!/usr/bin/env node
// Reconvert all 42 docx → MD using turndown (proper HTML→MD library) instead of regex
import mammoth from 'mammoth'
import TurndownService from 'turndown'
import fs from 'fs'
import path from 'path'

const SRC = path.resolve('..', 'public')
const OUT = path.resolve('..', 'public', 'knowledge_base')

// Read existing index to get frontmatter metadata
const indexPath = path.join(OUT, 'KB_INDEX.json')
const existingIndex = JSON.parse(fs.readFileSync(indexPath, 'utf8'))
const metaMap = {}
for (const entry of existingIndex) {
  metaMap[entry.file] = entry
}

// Configure turndown for maximum fidelity
const td = new TurndownService({
  headingStyle: 'atx',
  codeBlockStyle: 'fenced',
  bulletListMarker: '-',
  emDelimiter: '*',
  strongDelimiter: '**',
  hr: '---',
})

// Tables: turndown doesn't handle HTML tables by default, keep as-is (text preserved)
td.addRule('tableCell', {
  filter: ['th', 'td'],
  replacement: (content) => ' ' + content.trim() + ' |'
})
td.addRule('tableRow', {
  filter: 'tr',
  replacement: (content) => '|' + content + '\n'
})
td.addRule('table', {
  filter: 'table',
  replacement: (content) => '\n\n' + content + '\n\n'
})
td.addRule('thead', { filter: 'thead', replacement: (content) => content })
td.addRule('tbody', { filter: 'tbody', replacement: (content) => content })

async function reconvert() {
  const mdFiles = fs.readdirSync(OUT).filter(f => f.endsWith('.md')).sort()
  const newIndex = []
  let improved = 0
  let same = 0

  for (const mdFile of mdFiles) {
    const meta = metaMap[mdFile]
    if (!meta) {
      console.log('[SKIP] No metadata for ' + mdFile)
      continue
    }

    // Read existing MD to get frontmatter
    const existingMd = fs.readFileSync(path.join(OUT, mdFile), 'utf8')
    const fmMatch = existingMd.match(/^(---[\s\S]*?---\n)/)
    const frontmatter = fmMatch ? fmMatch[1] : ''

    // Find source docx
    const sourceMatch = frontmatter.match(/source:\s*"(.+)"/)
    if (!sourceMatch) {
      console.log('[SKIP] No source in frontmatter for ' + mdFile)
      continue
    }
    const srcFile = sourceMatch[1]
    const srcPath = path.join(SRC, srcFile)
    if (!fs.existsSync(srcPath)) {
      console.log('[SKIP] Source not found: ' + srcFile)
      continue
    }

    // Convert with mammoth → HTML → turndown → MD
    const { value: html } = await mammoth.convertToHtml({ path: srcPath })
    const mdBody = td.turndown(html)

    // Verify: compare HTML text vs MD text
    const htmlText = html.replace(/<[^>]+>/g, '').replace(/&amp;/g, '&').replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&nbsp;/g, ' ')
    const htmlNorm = htmlText.replace(/\s+/g, '').toLowerCase()
    const mdNorm = mdBody.replace(/[#*|\\`\[\]]/g, '').replace(/\s+/g, '').toLowerCase()

    // Sample test
    const windowSize = 15
    const step = Math.max(1, Math.floor(htmlNorm.length / 50))
    let found = 0, tested = 0
    for (let i = 0; i < htmlNorm.length - windowSize && tested < 50; i += step) {
      const w = htmlNorm.substring(i, i + windowSize)
      if (mdNorm.includes(w)) found++
      tested++
    }
    const ratio = tested > 0 ? Math.round(found / tested * 100) : 100

    // Compare with old ratio (re-check old MD)
    const oldBody = existingMd.replace(/^---[\s\S]*?---\n?/, '')
    const oldNorm = oldBody.replace(/[#*|\\`\[\]]/g, '').replace(/\s+/g, '').toLowerCase()
    let oldFound = 0, oldTested = 0
    for (let i = 0; i < htmlNorm.length - windowSize && oldTested < 50; i += step) {
      const w = htmlNorm.substring(i, i + windowSize)
      if (oldNorm.includes(w)) oldFound++
      oldTested++
    }
    const oldRatio = oldTested > 0 ? Math.round(oldFound / oldTested * 100) : 100

    const id = mdFile.substring(0, 5)
    if (ratio >= oldRatio) {
      // Write new version
      const fullMd = frontmatter + mdBody
      fs.writeFileSync(path.join(OUT, mdFile), fullMd, 'utf8')
      if (ratio > oldRatio) {
        console.log('[IMPROVED] ' + id + ': ' + oldRatio + '% → ' + ratio + '%')
        improved++
      } else {
        console.log('[SAME] ' + id + ': ' + ratio + '%')
        same++
      }
    } else {
      console.log('[KEPT OLD] ' + id + ': old=' + oldRatio + '% new=' + ratio + '% (keeping old)')
      same++
    }

    newIndex.push({
      id: meta.id,
      file: mdFile,
      title: meta.title,
      language: meta.language,
      topics: meta.topics,
      charCount: (frontmatter + mdBody).length
    })
  }

  // Update index
  fs.writeFileSync(indexPath, JSON.stringify(newIndex, null, 2), 'utf8')
  console.log('\n=== Improved: ' + improved + ', Same/Kept: ' + same + ' ===')
}

reconvert().catch(e => { console.error(e); process.exit(1) })
