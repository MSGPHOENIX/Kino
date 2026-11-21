import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from .admin import is_admin, router as admin_router
from .auth import create_token, current_user, hash_password, verify_password
from .db import engine, get_db
from .models import AudioTrack, Base, Episode, Season, Title, User, WatchProgress, now

MEDIA_BASE_URL = os.environ.get("MEDIA_BASE_URL", "").rstrip("/")  # откуда отдаётся видео (например, адрес R2)
COMPLETED_RATIO = 0.92  # серия считается просмотренной после 92%


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        Base.metadata.create_all(engine)  # на этапе 1 вместо миграций
    except Exception as e:
        print("DB init failed:", e)
    yield


app = FastAPI(title="My Cinema API", lifespan=lifespan)
app.include_router(admin_router)
origins = [o.strip() for o in os.environ.get("FRONTEND_ORIGIN", "*").split(",") if o.strip()]
app.add_middleware(CORSMiddleware, allow_origins=origins, allow_methods=["*"], allow_headers=["*"])


class Credentials(BaseModel):
    username: str = Field(min_length=3, max_length=50)
    password: str = Field(min_length=6, max_length=200)


class ProgressIn(BaseModel):
    episode_id: int
    position_sec: float = Field(ge=0)
    duration_sec: float = Field(ge=0)
    audio_track_id: int | None = None


@app.get("/health")
def health():
    # Лёгкий эндпоинт для пинга: не трогает базу, чтобы Neon мог спать
    return {"ok": True}


@app.post("/auth/register")
def register(data: Credentials, db: Session = Depends(get_db)):
    username = data.username.strip().lower()
    if db.scalar(select(User).where(User.username == username)):
        raise HTTPException(409, "Логин занят")
    user = User(username=username, password_hash=hash_password(data.password))
    db.add(user)
    db.commit()
    return {"token": create_token(user.id)}


@app.post("/auth/login")
def login(data: Credentials, db: Session = Depends(get_db)):
    user = db.scalar(select(User).where(User.username == data.username.strip().lower()))
    if user is None or not verify_password(data.password, user.password_hash):
        raise HTTPException(401, "Неверный логин или пароль")
    return {"token": create_token(user.id)}


@app.get("/me")
def me(user: User = Depends(current_user)):
    return {"id": user.id, "username": user.username, "is_admin": is_admin(user)}


@app.put("/progress")
def save_progress(data: ProgressIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    if db.get(Episode, data.episode_id) is None:
        raise HTTPException(404, "Серия не найдена")
    done = data.duration_sec > 0 and data.position_sec / data.duration_sec >= COMPLETED_RATIO
    status = "completed" if done else "watching"
    values = dict(
        user_id=user.id,
        episode_id=data.episode_id,
        position_sec=data.position_sec,
        duration_sec=data.duration_sec,
        status=status,
        audio_track_id=data.audio_track_id,
        updated_at=now(),
    )
    stmt = insert(WatchProgress).values(**values)
    stmt = stmt.on_conflict_do_update(
        index_elements=["user_id", "episode_id"],
        set_={k: v for k, v in values.items() if k not in ("user_id", "episode_id")},
    )
    db.execute(stmt)
    db.commit()
    return {"status": status}


@app.get("/progress/{episode_id}")
def get_progress(episode_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    p = db.scalar(
        select(WatchProgress).where(WatchProgress.user_id == user.id, WatchProgress.episode_id == episode_id)
    )
    if p is None:
        return {"position_sec": 0, "duration_sec": 0, "status": "new", "audio_track_id": None}
    return {
        "position_sec": p.position_sec,
        "duration_sec": p.duration_sec,
        "status": p.status,
        "audio_track_id": p.audio_track_id,
    }


@app.get("/continue")
def continue_watching(user: User = Depends(current_user), db: Session = Depends(get_db)):
    rows = db.execute(
        select(WatchProgress, Episode, Season, Title)
        .join(Episode, WatchProgress.episode_id == Episode.id)
        .join(Season, Episode.season_id == Season.id)
        .join(Title, Season.title_id == Title.id)
        .where(WatchProgress.user_id == user.id, WatchProgress.status == "watching")
        .order_by(WatchProgress.updated_at.desc())
        .limit(10)
    ).all()
    return [
        {
            "episode_id": e.id,
            "title": t.name,
            "season": s.number,
            "episode": e.number,
            "position_sec": p.position_sec,
            "duration_sec": p.duration_sec,
        }
        for p, e, s, t in rows
    ]


@app.get("/titles")
def list_titles(db: Session = Depends(get_db)):
    titles = db.scalars(select(Title).order_by(Title.name)).all()
    return [{"id": t.id, "name": t.name, "kind": t.kind, "poster_url": t.poster_url} for t in titles]


@app.get("/titles/{title_id}")
def get_title(title_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    t = db.get(Title, title_id)
    if t is None:
        raise HTTPException(404, "Не найдено")
    seasons = db.scalars(select(Season).where(Season.title_id == t.id).order_by(Season.number)).all()
    result = []
    for s in seasons:
        eps = db.scalars(select(Episode).where(Episode.season_id == s.id).order_by(Episode.number)).all()
        result.append(
            {
                "id": s.id,
                "number": s.number,
                "episodes": [{"id": e.id, "number": e.number, "name": e.name, "status": e.status} for e in eps],
            }
        )
    return {"id": t.id, "name": t.name, "kind": t.kind, "description": t.description, "seasons": result}


@app.get("/episodes/{episode_id}")
def get_episode(episode_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    e = db.get(Episode, episode_id)
    if e is None:
        raise HTTPException(404, "Серия не найдена")
    s = db.get(Season, e.season_id)
    t = db.get(Title, s.title_id)
    # следующая серия: в этом сезоне, иначе первая готовая в следующих сезонах
    nxt = db.scalar(
        select(Episode)
        .where(Episode.season_id == s.id, Episode.number > e.number, Episode.status == "ready")
        .order_by(Episode.number)
        .limit(1)
    )
    if nxt is None:
        nxt = db.scalar(
            select(Episode)
            .join(Season, Episode.season_id == Season.id)
            .where(Season.title_id == t.id, Season.number > s.number, Episode.status == "ready")
            .order_by(Season.number, Episode.number)
            .limit(1)
        )
    tracks = db.scalars(select(AudioTrack).where(AudioTrack.episode_id == e.id).order_by(AudioTrack.id)).all()
    ready = e.status == "ready" and e.hls_path and MEDIA_BASE_URL
    return {
        "id": e.id,
        "title_id": t.id,
        "title": t.name,
        "season": s.number,
        "episode": e.number,
        "name": e.name,
        "hls_url": f"{MEDIA_BASE_URL}/{e.hls_path.lstrip('/')}" if ready else None,
        "audio_tracks": [{"id": a.id, "name": a.name, "language": a.language} for a in tracks],
        "next_episode_id": nxt.id if nxt else None,
    }


# Сам сайт отдаётся с главной страницы сервера
WEB_INDEX = Path(__file__).resolve().parent.parent / "web" / "index.html"


@app.get("/", include_in_schema=False)
def site():
    return FileResponse(WEB_INDEX)
