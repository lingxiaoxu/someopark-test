import { Router } from 'express';
import { readJsonFile } from '../utils/fileUtils.js';
import { getBackendPath } from '../config.js';
import { getMongoDb } from '../utils/mongoClient.js';

const router = Router();

// GET /api/pairs/mrpt — selected 15 pairs from JSON
router.get('/:strategy', async (req, res) => {
  try {
    const { strategy } = req.params;
    if (!['mrpt', 'mtfs'].includes(strategy)) {
      return res.status(400).json({ error: 'Invalid strategy' });
    }
    const filePath = getBackendPath(`pair_universe_${strategy}.json`);
    const data = await readJsonFile(filePath);
    res.json(data);
  } catch (err: any) {
    res.status(500).json({ error: err.message });
  }
});

// GET /api/pairs/db/coint — MongoDB pairs_day_select
router.get('/db/:collection', async (req, res) => {
  try {
    const { collection } = req.params;
    const fieldMap: Record<string, string> = {
      coint: 'coint_pairs',
      similar: 'similar_pairs',
      pca: 'pca_pairs',
    };
    const field = fieldMap[collection];
    if (!field) return res.status(400).json({ error: 'Invalid collection. Use coint, similar, or pca.' });

    const db = await getMongoDb('someopark');
    const doc = await db.collection('pairs_day_select')
      .find({}, { projection: { day: 1, [field]: 1 } })
      .sort({ day: -1 })
      .limit(1)
      .toArray();

    if (doc.length === 0) return res.json({ day: null, pairs: [], total: 0 });

    const pairs = doc[0][field] || [];

    // Load current pair universes for "selected" highlighting
    let mrptPairs: any[] = [];
    let mtfsPairs: any[] = [];
    try {
      mrptPairs = await readJsonFile(getBackendPath('pair_universe_mrpt.json'));
    } catch {}
    try {
      mtfsPairs = await readJsonFile(getBackendPath('pair_universe_mtfs.json'));
    } catch {}

    const selectedSet = new Set([
      ...mrptPairs.map((p: any) => `${p.s1}/${p.s2}`),
      ...mtfsPairs.map((p: any) => `${p.s1}/${p.s2}`),
    ]);

    const result = pairs.map((p: any) => {
      const s1 = Array.isArray(p) ? p[0] : p.s1 || p[0];
      const s2 = Array.isArray(p) ? p[1] : p.s2 || p[1];
      return {
        s1,
        s2,
        pair: `${s1}/${s2}`,
        selected: selectedSet.has(`${s1}/${s2}`) || selectedSet.has(`${s2}/${s1}`),
      };
    });

    res.json({ day: doc[0].day, pairs: result, total: result.length });
  } catch (err: any) {
    res.status(503).json({ error: `Database unavailable: ${err.message}` });
  }
});

export default router;
