#!/usr/bin/env node
// Verify knowledge base conversion: compare docx raw text vs MD content
// Checks paragraph-level coverage for all 42 files
import mammoth from 'mammoth'
import fs from 'fs'
import path from 'path'

const SRC = path.resolve('..', 'public')
const KB = path.resolve('..', 'public', 'knowledge_base')

async function verify() {
  const mdFiles = fs.readdirSync(KB).filter(f => f.endsWith('.md')).sort()
  const sourceToMd = {}
  for (const md of mdFiles) {
    const c = fs.readFileSync(path.join(KB, md), 'utf8')
    const m = c.match(/source:\s*"(.+)"/)
    if (m) sourceToMd[m[1]] = md
  }

  const docxFiles = fs.readdirSync(SRC).filter(f => f.endsWith('.docx')).sort()
  let pass = 0, warn = 0, fail = 0, skip = 0

  for (const docx of docxFiles) {
    const md = sourceToMd[docx]
    if (!md) {
      console.log(`[SKIP] No MD for: ${docx.substring(0, 60)}`)
      skip++
      continue
    }

    const { value: html } = await mammoth.convertToHtml({ path: path.join(SRC, docx) })
    const { value: rawText } = await mammoth.extractRawText({ path: path.join(SRC, docx) })
    const mdBody = fs.readFileSync(path.join(KB, md), 'utf8').replace(/^---[\s\S]*?---\n?/, '')

    // Extract paragraphs from HTML (the source of our MD)
    const pRegex = /<p[^>]*>([\s\S]*?)<\/p>/g
    const paras = []
    let match
    while ((match = pRegex.exec(html)) !== null) {
      const text = match[1].replace(/<[^>]+>/g, '').trim()
      if (text.length > 15) paras.push(text)
    }

    // Check each paragraph: 8-char substring from middle should exist in MD
    let found = 0
    const missing = []
    for (const p of paras) {
      const mid = Math.floor(p.length / 3)
      const check8 = p.substring(mid, mid + 8)
      if (check8.length < 6 || mdBody.includes(check8)) {
        found++
      } else {
        missing.push(p.substring(0, 80))
      }
    }

    const pct = paras.length > 0 ? Math.round(found / paras.length * 100) : 100
    const id = md.substring(0, 5)
    const rawChars = rawText.length
    const mdChars = mdBody.length

    if (pct >= 95) {
      console.log(`[PASS] ${id}: ${found}/${paras.length} paras (${pct}%) chars: docx=${rawChars} md=${mdChars}`)
      pass++
    } else if (pct >= 80) {
      console.log(`[WARN] ${id}: ${found}/${paras.length} paras (${pct}%) chars: docx=${rawChars} md=${mdChars} missing=${missing.length}`)
      if (missing.length > 0) console.log(`  ! ${missing[0]}`)
      warn++
    } else {
      console.log(`[FAIL] ${id}: ${found}/${paras.length} paras (${pct}%) chars: docx=${rawChars} md=${mdChars} missing=${missing.length}`)
      if (missing.length > 0) console.log(`  ! ${missing[0]}`)
      fail++
    }
  }

  console.log('')
  console.log('=== VERIFICATION SUMMARY ===')
  console.log(`PASS (95%+): ${pass}`)
  console.log(`WARN (80-94%): ${warn}`)
  console.log(`FAIL (<80%): ${fail}`)
  console.log(`SKIP: ${skip}`)
  console.log(`Total: ${docxFiles.length}`)

  if (fail > 0) {
    console.log('\nFAILED files need reconversion.')
    process.exit(1)
  }
}

verify().catch(e => { console.error(e); process.exit(1) })
