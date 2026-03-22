import { Router, Request, Response } from 'express'
import { Sandbox } from '@e2b/code-interpreter'
import { StanseAgentSchema } from '../../src/lib/schema.js'
import { ExecutionResultInterpreter, ExecutionResultWeb } from '../../src/lib/types.js'

const router = Router()

const sandboxTimeout = 10 * 60 * 1000 // 10 minutes

router.post('/', async (req: Request, res: Response) => {
  const {
    stanseAgent,
    userID,
  }: {
    stanseAgent: StanseAgentSchema
    userID: string | undefined
  } = req.body

  console.log('Sandbox request:', { template: stanseAgent.template, userID })

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
      const { logs, error, results } = await sbx.runCode(stanseAgent.code || '')

      res.json({
        sbxId: sbx?.sandboxId,
        template: stanseAgent.template,
        stdout: logs.stdout,
        stderr: logs.stderr,
        runtimeError: error,
        cellResults: results,
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
