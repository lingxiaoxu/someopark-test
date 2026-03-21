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

// Graceful shutdown
process.on('SIGTERM', async () => {
  if (client) await client.close();
});

process.on('SIGINT', async () => {
  if (client) await client.close();
});
