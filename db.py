import os

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

url = os.environ["DATABASE_URL"]
if url.startswith("postgres://"):
    url = url.replace("postgres://", "postgresql+psycopg://", 1)
elif url.startswith("postgresql://"):
    url = url.replace("postgresql://", "postgresql+psycopg://", 1)

# pre_ping и recycle нужны, потому что Neon засыпает и рвёт старые соединения
engine = create_engine(url, pool_pre_ping=True, pool_recycle=240, pool_size=3, max_overflow=2)
SessionLocal = sessionmaker(engine, expire_on_commit=False)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
