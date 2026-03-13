"""
MongoDB connection helpers for someopark-test.

Connection URIs are read from environment variables (set in .env):
  MONGO_URI     — main MongoDB instance  (mongodb://...)
  MONGO_VEC_URI — MongoDB Atlas vector DB  (mongodb+srv://...)

Each function returns an independent pymongo connection;
no shared state with any other project.
"""

import os
from pymongo import MongoClient

DB_MAIN = "someopark"
DB_STRATEGY = "someo_stra"
DB_VEC = "someopark_vec"


def _uri() -> str:
    uri = os.environ.get("MONGO_URI")
    if not uri:
        raise RuntimeError("MONGO_URI environment variable is not set. Load .env first.")
    return uri


def _vec_uri() -> str:
    uri = os.environ.get("MONGO_VEC_URI")
    if not uri:
        raise RuntimeError("MONGO_VEC_URI environment variable is not set. Load .env first.")
    return uri


def get_db(name: str = DB_MAIN):
    """Return a pymongo Database by name using the main MongoDB instance."""
    return MongoClient(_uri(), tz_aware=True)[name]


def get_main_db():
    """Return the 'someopark' main business database."""
    return get_db(DB_MAIN)


def get_strategy_db():
    """Return the 'someo_stra' strategy/backtest database."""
    return get_db(DB_STRATEGY)


def get_vec_db():
    """Return the 'someopark_vec' Atlas vector database.

    Note: Atlas requires IP whitelist authorisation — contact HuangYan.
    """
    return MongoClient(_vec_uri())[DB_VEC]
