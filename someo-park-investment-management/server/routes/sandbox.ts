import { Router, Request, Response } from 'express'
import { Sandbox } from '@e2b/code-interpreter'
import { StanseAgentSchema } from '../../src/lib/schema.js'
import { ExecutionResultInterpreter, ExecutionResultWeb } from '../../src/lib/types.js'

const router = Router()

const sandboxTimeout = 10 * 60 * 1000 // 10 minutes

// Valid E2B templates. Some models (esp. local/open-source ones like the Ollama
// Nemotron) fill `template`/`file_path` with invalid values in their structured
// output, which makes Sandbox.create() 500. Fall back to the Python interpreter so
// the generated code still runs instead of erroring out.
// Each template's image auto-runs a FIXED entry file on a FIXED port. If the model
// writes the code to a different file_path or reports a different port, the template
// runs its empty default file → blank preview. So we always write to / serve from the
// template's canonical file+port (from src/lib/templates.ts), regardless of what the
// model put in those fields. This is the template's own spec, not a content rule.
const TEMPLATE_SPEC: Record<string, { file: string; port: number | null }> = {
  'code-interpreter-v1': { file: 'script.py',      port: null },
  'nextjs-developer':    { file: 'pages/index.tsx', port: 3000 },
  'vue-developer':       { file: 'app/app.vue',     port: 3000 },
  'streamlit-developer': { file: 'app.py',          port: 8501 },
  'gradio-developer':    { file: 'app.py',          port: 7860 },
}
const VALID_TEMPLATES = new Set(Object.keys(TEMPLATE_SPEC))

router.post('/', async (req: Request, res: Response) => {
  const {
    stanseAgent,
    userID,
  }: {
    stanseAgent: StanseAgentSchema
    userID: string | undefined
  } = req.body

  console.log('Sandbox request:', { template: stanseAgent.template, userID })

  // Guard against invalid model-produced metadata (see VALID_TEMPLATES note above).
  if (!stanseAgent.template || !VALID_TEMPLATES.has(stanseAgent.template)) {
    console.warn(`[Sandbox] invalid template '${stanseAgent.template}' — falling back to code-interpreter-v1`)
    stanseAgent.template = 'code-interpreter-v1'
  }
  // Pin file_path + port to the template's canonical entry point so the app actually
  // runs (a wrong file_path/port → the template serves its empty default → blank preview).
  const spec = TEMPLATE_SPEC[stanseAgent.template]
  if (typeof stanseAgent.code === 'string') {
    stanseAgent.file_path = spec.file
  } else if (!stanseAgent.file_path || typeof stanseAgent.file_path !== 'string') {
    stanseAgent.file_path = spec.file
  }
  stanseAgent.port = spec.port

  // Safety net (same spirit as the guards above): local models occasionally emit the
  // Next.js directive UNQUOTED (`use client;`) — never valid JS, so the build always
  // dies with "Parsing ecmascript source code failed" before anything renders. Repair
  // exactly that broken form at the top of the file; correctly quoted directives
  // ('use client' / "use client") don't match, and `use somethingElse` is left alone.
  const repairDirective = (src: string): string =>
    src.replace(/^(\s*)use (client|server)\s*;?(?=\s*(?:\r?\n|$))/, "$1'use $2';")
  if (typeof stanseAgent.code === 'string') {
    stanseAgent.code = repairDirective(stanseAgent.code)
  } else if (Array.isArray(stanseAgent.code)) {
    for (const f of stanseAgent.code as any[]) {
      if (f && typeof f.file_content === 'string') f.file_content = repairDirective(f.file_content)
    }
  }

  try {
    const sbx = await Sandbox.create(stanseAgent.template, {
      metadata: {
        template: stanseAgent.template,
        userID: userID ?? '',
      },
      timeoutMs: sandboxTimeout,
      secure: false, // older templates (nextjs-developer, etc.) require secure:false
    })

    // Install packages
    if (stanseAgent.has_additional_dependencies) {
      await sbx.commands.run(stanseAgent.install_dependencies_command)
      console.log(
        `Installed dependencies: ${stanseAgent.additional_dependencies.join(', ')} in sandbox ${sbx.sandboxId}`,
      )
    }

    // Write code to file
    if (stanseAgent.code && Array.isArray(stanseAgent.code)) {
      for (const file of stanseAgent.code as any[]) {
        await sbx.files.write(file.file_path, file.file_content)
      }
    } else {
      await sbx.files.write(stanseAgent.file_path, stanseAgent.code)
    }

    // Execute code or return URL
    if (stanseAgent.template === 'code-interpreter-v1') {
      // Model-independent safety net: a desktop-GUI library can never render in the
      // headless interpreter (no display) — it would just hang / show blank. Detect it
      // up front and return clear guidance instead of a silent blank preview.
      const codeStr = typeof stanseAgent.code === 'string' ? stanseAgent.code : ''
      if (/\b(import\s+(tkinter|pygame|turtle)|from\s+(tkinter|pygame|turtle)\b)/i.test(codeStr)) {
        res.json({
          sbxId: sbx?.sandboxId,
          template: stanseAgent.template,
          stdout: [],
          stderr: [],
          runtimeError: {
            name: 'UnsupportedEnvironment',
            value: '这段代码用的是桌面 GUI 库(tkinter / pygame / turtle),而 Python 解释器是无头环境(没有显示器),没法显示窗口。做有界面、会刷新的应用,请把上方「角色」切到 Streamlit 或 Gradio —— 它会返回一个实时网页。',
            traceback: '',
          },
          cellResults: [],
        } as ExecutionResultInterpreter)
        return
      }

      // Collect stdout/stderr live so that long-running / infinite-loop code (e.g. a
      // `while True` loop) still returns whatever it printed when it hits the timeout,
      // instead of throwing a bare 500 with no output.
      const liveStdout: string[] = []
      const liveStderr: string[] = []
      const EXEC_TIMEOUT_MS = 15_000
      let timedOut = false
      let exec: any = null
      try {
        exec = await sbx.runCode(stanseAgent.code || '', {
          timeoutMs: EXEC_TIMEOUT_MS,
          onStdout: (o: any) => { liveStdout.push(o?.line ?? String(o)) },
          onStderr: (o: any) => { liveStderr.push(o?.line ?? String(o)) },
        })
      } catch (e: any) {
        if (e?.name === 'TimeoutError') timedOut = true
        else throw e
      }

      res.json({
        sbxId: sbx?.sandboxId,
        template: stanseAgent.template,
        stdout: exec ? exec.logs.stdout : liveStdout,
        stderr: exec ? exec.logs.stderr : liveStderr,
        runtimeError: timedOut
          ? {
              name: 'TimeoutError',
              value: `代码运行超过 ${EXEC_TIMEOUT_MS / 1000} 秒被停止(很可能是无限循环或长时间运行的程序)。Python 解释器是"跑完返回结果"的环境,不适合会持续刷新或交互的应用。这类"活的"应用请把上方「角色」切到 Streamlit 或 Gradio —— 它会返回一个实时刷新的网页。`,
              traceback: '',
            }
          : (exec?.error ?? null),
        cellResults: exec?.results ?? [],
      } as ExecutionResultInterpreter)
    } else {
      res.json({
        sbxId: sbx?.sandboxId,
        template: stanseAgent.template,
        url: `https://${sbx?.getHost(stanseAgent.port || 80)}`,
      } as ExecutionResultWeb)
    }
  } catch (error: any) {
    console.error('Sandbox error:', error)
    res.status(500).json({ error: error.message || 'Failed to create sandbox' })
  }
})

export default router
