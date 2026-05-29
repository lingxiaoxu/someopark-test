// server/tools/runPythonTool.ts
// Reference: CC src/tools/BashTool/BashTool.tsx — execute, timeout, background mode
// Uses E2B Code Interpreter SDK v2: Sandbox.create() + sandbox.runCode() + sandbox.kill()

import { createTask, updateTaskStatus, getTask } from '../utils/taskManager.js'
import type { AgentTool } from './index.js'

// CJK font preamble — best-effort transparent fix.
// Registers CJK fonts, imports seaborn, patches set_theme.
// May or may not work on first try — FONT_RETRY handles failures.
const CJK_PREAMBLE = `
import matplotlib as _mpl, matplotlib.font_manager as _fm
import os as _os, glob as _glob

try: _fm._load_fontmanager(try_read_cache=False)
except: pass
for _p in ['/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc',
           '/usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.ttc',
           '/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc',
           '/usr/share/fonts/opentype/noto/NotoSerifCJK-Bold.ttc',
           '/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc']:
    if _os.path.exists(_p):
        try: _fm.fontManager.addfont(_p)
        except: pass
for _p in _glob.glob('/usr/share/fonts/**/Noto*CJK*.tt*', recursive=True):
    try: _fm.fontManager.addfont(_p)
    except: pass

def _ensure_cjk():
    _ss = list(_mpl.rcParams.get('font.sans-serif', []))
    if 'Noto Sans CJK JP' not in _ss:
        _mpl.rcParams['font.sans-serif'] = ['Noto Sans CJK JP'] + _ss
    _mpl.rcParams['axes.unicode_minus'] = False
    try: _fm.fontManager._findfont_cached.cache_clear()
    except: pass

import seaborn as _sns
_ensure_cjk()
_orig_set_theme = _sns.set_theme
def _patched_set_theme(*a, **kw):
    _orig_set_theme(*a, **kw)
    _ensure_cjk()
_sns.set_theme = _patched_set_theme
if hasattr(_sns, 'set_style'):
    _orig_set_style = _sns.set_style
    def _patched_set_style(*a, **kw):
        _orig_set_style(*a, **kw)
        _ensure_cjk()
    _sns.set_style = _patched_set_style
`

// Aggressive font retry preamble — used ONLY when first run has font warnings.
// Patches Figure.draw to force FontProperties(fname=CJK) on ALL Text objects
// right before rendering. This is the ONLY method that has 100% success rate
// across all tests. User code runs completely unmodified.
const FONT_RETRY_PREAMBLE = `
import matplotlib, matplotlib.pyplot as plt, matplotlib.font_manager as fm
import matplotlib.figure, matplotlib.text
import os, glob, warnings

plt.close('all')

# Suppress font warnings on retry
warnings.filterwarnings('ignore', message='.*Glyph.*')
warnings.filterwarnings('ignore', message='.*missing.*font.*')

# Rebuild font index and register CJK
try: fm._load_fontmanager(try_read_cache=False)
except: pass
_cjk_path = None
for _p in ['/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc',
           '/usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.ttc',
           '/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc']:
    if os.path.exists(_p):
        _cjk_path = _p
        fm.fontManager.addfont(_p)
        break
if not _cjk_path:
    _found = glob.glob('/usr/share/fonts/**/Noto*CJK*.tt*', recursive=True)
    if _found:
        _cjk_path = _found[0]
        fm.fontManager.addfont(_cjk_path)
for _p in glob.glob('/usr/share/fonts/**/Noto*CJK*.tt*', recursive=True):
    try: fm.fontManager.addfont(_p)
    except: pass

if _cjk_path:
    _cjk_fp = fm.FontProperties(fname=_cjk_path)

    # Also set rcParams (belt and suspenders)
    _cjk_name = _cjk_fp.get_name()
    matplotlib.rcParams['font.sans-serif'] = [_cjk_name]
    matplotlib.rcParams['font.family'] = 'sans-serif'
    matplotlib.rcParams['axes.unicode_minus'] = False
    try: fm.fontManager._findfont_cached.cache_clear()
    except: pass

    # Patch Figure.draw — forces CJK FontProperties on ALL Text objects
    # right before rendering. Works regardless of rcParams state.
    _orig_fig_draw = matplotlib.figure.Figure.draw
    def _cjk_fig_draw(self, renderer):
        for _txt in self.findobj(matplotlib.text.Text):
            _fp = _cjk_fp.copy()
            _old = _txt._fontproperties
            _fp.set_size(_old.get_size_in_points())
            _fp.set_weight(_old.get_weight())
            _fp.set_style(_old.get_style())
            _txt._fontproperties = _fp
        return _orig_fig_draw(self, renderer)
    matplotlib.figure.Figure.draw = _cjk_fig_draw

    # Also patch seaborn set_theme to keep our rcParams
    try:
        import seaborn as _sns
        _orig_theme = _sns.set_theme
        def _rt(*a, **kw):
            _orig_theme(*a, **kw)
            matplotlib.rcParams['font.sans-serif'] = [_cjk_name]
            matplotlib.rcParams['axes.unicode_minus'] = False
            try: fm.fontManager._findfont_cached.cache_clear()
            except: pass
        _sns.set_theme = _rt
    except: pass
`

// Check if stderr indicates CJK font rendering failure
function hasFontWarning(stderr: string): boolean {
  return stderr.includes('Glyph') && stderr.toLowerCase().includes('missing')
}

// Filter font warnings from stderr for clean output
function cleanStderr(stderr: string): string {
  return stderr.split('\n')
    .filter(line => !line.includes('Glyph') || !line.toLowerCase().includes('missing'))
    .join('\n')
    .trim()
}

// Extract images from E2B result
function extractImages(result: any): string[] {
  const images: string[] = []
  if (result.results) {
    for (const r of result.results) {
      if (r.png) images.push(r.png)
    }
  }
  return images
}

export const runPythonTool: AgentTool = {
  definition: {
    name: 'run_python',
    description: `Execute Python code in an isolated E2B sandbox. Two modes:
- background=false (default): Wait for result, return stdout (max timeout seconds)
- background=true: Submit async, return task_id; use get_task_output to poll result.
Requires E2B_API_KEY environment variable.`,
    input_schema: {
      type: 'object',
      properties: {
        code: { type: 'string', description: 'Python code to execute' },
        chart_labels: {
          type: 'array', items: { type: 'string' },
          description: 'Descriptive label for each chart produced by this code (e.g. ["收益率分布直方图","价格走势对比"]). Used as chart identity for smart dedup: same label = replace previous version, different label = new chart. When fixing/improving a chart, reuse the EXACT same label. When creating a new chart, use a new unique label.'
        },
        background: { type: 'boolean', description: 'Run in background (default false)' },
        timeout: { type: 'number', description: 'Timeout in seconds (default 30, max 120)' },
      },
      required: ['code']
    }
  },
  async execute({ code, chart_labels, background = false, timeout = 30 }: { code: string; chart_labels?: string[]; background?: boolean; timeout?: number }) {
    if (!process.env.E2B_API_KEY) throw new Error('E2B_API_KEY not configured')

    const { Sandbox } = await import('@e2b/code-interpreter')
    const task = createTask('python', `run_python: ${code.slice(0, 60)}...`)
    updateTaskStatus(task.id, 'running')

    const timeoutMs = Math.min(timeout, 120) * 1000

    // Common packages + CJK font for Chinese chart labels
    const PREINSTALL = 'pip install -q yfinance pandas numpy requests seaborn plotly kaleido scipy 2>/dev/null; apt-get update -qq >/dev/null 2>&1; apt-get install -y -qq fonts-noto-cjk >/dev/null 2>&1; fc-cache -f >/dev/null 2>&1'

    const fullCode = CJK_PREAMBLE + '\n' + code

    if (!background) {
      // Synchronous mode
      let sandbox: any
      try {
        sandbox = await Sandbox.create({ timeoutMs })
        await sandbox.runCode(`import subprocess; subprocess.run("${PREINSTALL}", shell=True, capture_output=True)`)

        // First attempt
        const result = await sandbox.runCode(fullCode)
        let stdout = (result.logs?.stdout ?? []).join('\n')
        let stderr = (result.logs?.stderr ?? []).join('\n')
        let images = extractImages(result)
        const error = result.error?.value || null

        // Auto-retry if font warning detected and there are images
        if (hasFontWarning(stderr) && images.length > 0) {
          try {
            const retryCode = FONT_RETRY_PREAMBLE + '\n' + code
            const retryResult = await sandbox.runCode(retryCode)
            const retryStdout = (retryResult.logs?.stdout ?? []).join('\n')
            const retryStderr = (retryResult.logs?.stderr ?? []).join('\n')
            const retryImages = extractImages(retryResult)

            // Use retry result if it produced images (even if fewer)
            if (retryImages.length > 0) {
              stdout = retryStdout
              stderr = cleanStderr(retryStderr)
              images = retryImages
            }
          } catch {
            // Retry failed — fall through to original result
          }
        }

        // Clean font warnings from stderr even if no retry
        stderr = cleanStderr(stderr)

        updateTaskStatus(task.id, 'completed', { stdout, stderr })
        await sandbox.kill().catch(() => {})
        return {
          task_id: task.id,
          status: 'completed',
          stdout: images.length > 0
            ? stdout + '\n' + images.map((img, i) => `![${chart_labels?.[i] || `Chart ${i + 1}`}](data:image/png;base64,${img})`).join('\n')
            : stdout,
          stderr,
          error,
        }
      } catch (err: any) {
        updateTaskStatus(task.id, 'failed', { stderr: err.message })
        if (sandbox) await sandbox.kill().catch(() => {})
        throw err
      }
    } else {
      // Background mode — return task_id immediately
      ;(async () => {
        let sandbox: any
        try {
          sandbox = await Sandbox.create({ timeoutMs: 120_000 })
          const state = getTask(task.id)
          if (state) state.sandbox = sandbox
          await sandbox.runCode(`import subprocess; subprocess.run("${PREINSTALL}", shell=True, capture_output=True)`)

          // First attempt
          const result = await sandbox.runCode(fullCode)
          let stdout = (result.logs?.stdout ?? []).join('\n')
          let stderr = (result.logs?.stderr ?? []).join('\n')

          // Extract images
          let images: string[] = []
          if (result.results) {
            for (const r of result.results) {
              if (r.png) images.push(r.png)
            }
          }

          // Auto-retry if font warning detected and there are images
          if (hasFontWarning(stderr) && images.length > 0) {
            try {
              const retryCode = FONT_RETRY_PREAMBLE + '\n' + code
              const retryResult = await sandbox.runCode(retryCode)
              const retryImages = extractImages(retryResult)
              if (retryImages.length > 0) {
                stdout = (retryResult.logs?.stdout ?? []).join('\n')
                stderr = cleanStderr((retryResult.logs?.stderr ?? []).join('\n'))
                images = retryImages
              }
            } catch {
              // Retry failed — use original
            }
          }

          stderr = cleanStderr(stderr)

          // Build final stdout with images
          if (images.length > 0) {
            stdout += '\n' + images.map((img, i) => `![${chart_labels?.[i] || `Chart ${i + 1}`}](data:image/png;base64,${img})`).join('\n')
          }

          updateTaskStatus(task.id, 'completed', { stdout, stderr })
        } catch (err: any) {
          updateTaskStatus(task.id, 'failed', { stderr: err.message })
        } finally {
          if (sandbox) await sandbox.kill().catch(() => {})
        }
      })().catch(() => {})

      return {
        task_id: task.id,
        status: 'pending',
        message: 'Task submitted. Use get_task_output to check status.',
      }
    }
  }
}
