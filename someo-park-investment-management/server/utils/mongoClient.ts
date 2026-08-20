import { MongoClient, Db } from 'mongodb';

let client: MongoClient | null = null;

export async function getMongoClient(): Promise<MongoClient> {
  if (!client) {
    const uri = process.env.MONGO_URI;
    if (!uri) throw new Error('MONGO_URI not set');
    client = new MongoClient(uri);
    await client.connect();
  }
  return client;
}

export async function getMongoDb(dbName: string): Promise<Db> {
  const c = await getMongoClient();
  return c.db(dbName);
}

// Graceful shutdown。注意:注册了 SIGTERM/SIGINT 监听器就会**取消 Node 的默认退出**,
// 所以关完连接必须自己 exit —— 否则 `kill` 返回 0 但进程永远不死(2026-08-19 实测:
// API server 免疫 SIGTERM,只能 -9)。
const shutdown = (sig: NodeJS.Signals) => async () => {
  try {
    if (client) await client.close();
  } finally {
    process.exit(sig === 'SIGINT' ? 130 : 143);
  }
};
process.on('SIGTERM', shutdown('SIGTERM'));
process.on('SIGINT', shutdown('SIGINT'));
