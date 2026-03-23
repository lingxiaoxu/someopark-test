import { initializeApp, getApps } from 'firebase/app';
import { getFirestore } from 'firebase/firestore';

const firebaseConfig = {
  projectId: 'someopark',
  appId: '1:692205032293:web:bf34833b8b6a1ce9bbc78c',
  storageBucket: 'someopark.firebasestorage.app',
  apiKey: import.meta.env.VITE_FIREBASE_API_KEY,
  authDomain: 'someopark.firebaseapp.com',
  messagingSenderId: '692205032293',
};

const app = getApps().length === 0 ? initializeApp(firebaseConfig) : getApps()[0];
export const db = getFirestore(app);
