// server/routes/controllerNav.ts — 实时净值看板数据(M7)
// HTTP 与聊天工具共用只读数据入口,保持看板响应口径。
import { Router, type Request, type RequestHandler } from 'express';
import {
  NavDataError,
  etToday,
  readNavLatest,
  readNavMirror,
  readNavPrevClose,
  readNavReconcile,
  readNavStream,
} from '../utils/controllerNavData.js';
import { readOfficialEquity } from '../utils/officialEquity.js';

const router = Router();

function respond(read: (req: Request) => unknown): RequestHandler {
  return (req, res) => {
    try {
      res.json(read(req));
    } catch (e) {
      if (e instanceof NavDataError) {
        res.status(e.status).json({ error: e.message });
      } else {
        // 原路由未捕获的读盘/解析错误继续交给 Express,不改变 HTTP 错误体。
        throw e;
      }
    }
  };
}

router.get('/latest', respond(() => readNavLatest()));
router.get('/stream', respond(req => readNavStream(String(req.query.date || etToday()))));
router.get('/prev-close', respond(() => readNavPrevClose()));

// 官方 EOD 锚仍复用原共享入口,响应与错误处理不变。
router.get('/official', (_req, res) => {
  try {
    res.json(readOfficialEquity());
  } catch (e) {
    res.status(500).json({ error: String(e) });
  }
});

router.get('/scalars', respond(() => readNavMirror()));
router.get('/reconcile', respond(() => readNavReconcile()));

export default router;
