import { Router, Request, Response } from 'express'
import { Sandbox } from '@e2b/code-interpreter'

const router = Router()

type Duration = '30m' | '1h' | '3h' | '6h' | '1d'

function ms(d: Duration): number {
  const map: Record<Duration, number> = {
    '30m': 30 * 60 * 1000,
    '1h': 60 * 60 * 1000,
    '3h': 3 * 60 * 60 * 1000,
    '6h': 6 * 60 * 60 * 1000,
    '1d': 24 * 60 * 60 * 1000,
  }
  return map[d]
}

router.post('/', async (req: Request, res: Response) => {
  const { sbxId, duration }: { sbxId: string; duration: Duration } = req.body

  if (!sbxId || !duration) {
    res.status(400).json({ error: 'sbxId and duration are required' })
    return
  }

  try {
    const expiration = ms(duration)
    await Sandbox.setTimeout(sbxId, expiration)
    res.json({ success: true, expiresInMs: expiration })
  } catch (error: any) {
    console.error('Publish error:', error)
    res.status(500).json({ error: error.message || 'Failed to extend sandbox' })
  }
})

export default router
