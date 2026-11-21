import os

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .auth import current_user
from .db import get_db
from .models import Episode, Season, Title, User

# Логины админов через запятую, задаются в Render (переменная ADMIN_USERNAMES)
ADMINS = {n.strip().lower() for n in os.environ.get("ADMIN_USERNAMES", "").split(",") if n.strip()}


def is_admin(user: User) -> bool:
    return user.username in ADMINS


def admin_user(user: User = Depends(current_user)) -> User:
    if not is_admin(user):
        raise HTTPException(403, "Только для админа")
    return user


router = APIRouter(prefix="/admin", dependencies=[Depends(admin_user)])


class TitleIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    kind: str = Field(default="series", pattern="^(movie|series)$")
    poster_url: str | None = Field(default=None, max_length=500)
    description: str | None = Field(default=None, max_length=2000)


class TitleUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    poster_url: str | None = Field(default=None, max_length=500)
    description: str | None = Field(default=None, max_length=2000)


class SeasonIn(BaseModel):
    number: int = Field(ge=1, le=999)


class EpisodeIn(BaseModel):
    number: int = Field(ge=1, le=9999)
    name: str | None = Field(default=None, max_length=200)


class EpisodeUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=200)
    hls_path: str | None = Field(default=None, max_length=500)
    status: str | None = Field(default=None, pattern="^(not_ready|preparing|ready)$")


def _title_or_404(db: Session, title_id: int) -> Title:
    t = db.get(Title, title_id)
    if t is None:
        raise HTTPException(404, "Не найдено")
    return t


@router.post("/titles")
def create_title(data: TitleIn, db: Session = Depends(get_db)):
    t = Title(
        name=data.name.strip(),
        kind=data.kind,
        poster_url=(data.poster_url or "").strip() or None,
        description=(data.description or "").strip() or None,
    )
    db.add(t)
    db.flush()
    if t.kind == "movie":  # у фильма сразу одна «серия»
        s = Season(title_id=t.id, number=1)
        db.add(s)
        db.flush()
        db.add(Episode(season_id=s.id, number=1, name=t.name))
    db.commit()
    return {"id": t.id}


@router.get("/titles/{title_id}")
def admin_get_title(title_id: int, db: Session = Depends(get_db)):
    t = _title_or_404(db, title_id)
    seasons = db.scalars(select(Season).where(Season.title_id == t.id).order_by(Season.number)).all()
    out = []
    for s in seasons:
        eps = db.scalars(select(Episode).where(Episode.season_id == s.id).order_by(Episode.number)).all()
        out.append(
            {
                "id": s.id,
                "number": s.number,
                "episodes": [
                    {"id": e.id, "number": e.number, "name": e.name, "status": e.status, "hls_path": e.hls_path}
                    for e in eps
                ],
            }
        )
    return {
        "id": t.id,
        "name": t.name,
        "kind": t.kind,
        "poster_url": t.poster_url,
        "description": t.description,
        "seasons": out,
    }


@router.put("/titles/{title_id}")
def update_title(title_id: int, data: TitleUpdate, db: Session = Depends(get_db)):
    t = _title_or_404(db, title_id)
    changes = data.model_dump(exclude_unset=True)
    if changes.get("name") is not None:
        t.name = changes["name"].strip()
    if "poster_url" in changes:
        t.poster_url = (changes["poster_url"] or "").strip() or None
    if "description" in changes:
        t.description = (changes["description"] or "").strip() or None
    db.commit()
    return {"ok": True}


@router.delete("/titles/{title_id}")
def delete_title(title_id: int, db: Session = Depends(get_db)):
    _title_or_404(db, title_id)
    db.execute(delete(Title).where(Title.id == title_id))  # сезоны и серии удалит база (CASCADE)
    db.commit()
    return {"ok": True}


@router.post("/titles/{title_id}/seasons")
def create_season(title_id: int, data: SeasonIn, db: Session = Depends(get_db)):
    _title_or_404(db, title_id)
    if db.scalar(select(Season).where(Season.title_id == title_id, Season.number == data.number)):
        raise HTTPException(409, "Такой сезон уже есть")
    s = Season(title_id=title_id, number=data.number)
    db.add(s)
    db.commit()
    return {"id": s.id}


@router.delete("/seasons/{season_id}")
def delete_season(season_id: int, db: Session = Depends(get_db)):
    if db.get(Season, season_id) is None:
        raise HTTPException(404, "Не найдено")
    db.execute(delete(Season).where(Season.id == season_id))
    db.commit()
    return {"ok": True}


@router.post("/seasons/{season_id}/episodes")
def create_episode(season_id: int, data: EpisodeIn, db: Session = Depends(get_db)):
    if db.get(Season, season_id) is None:
        raise HTTPException(404, "Сезон не найден")
    if db.scalar(select(Episode).where(Episode.season_id == season_id, Episode.number == data.number)):
        raise HTTPException(409, "Такая серия уже есть")
    e = Episode(season_id=season_id, number=data.number, name=(data.name or "").strip() or None)
    db.add(e)
    db.commit()
    return {"id": e.id}


@router.put("/episodes/{episode_id}")
def update_episode(episode_id: int, data: EpisodeUpdate, db: Session = Depends(get_db)):
    e = db.get(Episode, episode_id)
    if e is None:
        raise HTTPException(404, "Серия не найдена")
    changes = data.model_dump(exclude_unset=True)
    if "name" in changes:
        e.name = (changes["name"] or "").strip() or None
    if "hls_path" in changes:
        e.hls_path = (changes["hls_path"] or "").strip().lstrip("/") or None
    if changes.get("status") is not None:
        e.status = changes["status"]
    db.commit()
    return {"ok": True}


@router.delete("/episodes/{episode_id}")
def delete_episode(episode_id: int, db: Session = Depends(get_db)):
    if db.get(Episode, episode_id) is None:
        raise HTTPException(404, "Серия не найдена")
    db.execute(delete(Episode).where(Episode.id == episode_id))
    db.commit()
    return {"ok": True}
