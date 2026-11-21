import os
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .auth import current_user
from .db import get_db
from .models import AudioTrack, Episode, Season, Title, User

# Логины админов: переменная ADMIN_USERNAMES в Render, через запятую
ADMINS = {n.strip().lower() for n in os.environ.get("ADMIN_USERNAMES", "").split(",") if n.strip()}


def is_admin(user: User) -> bool:
    return user.username.lower() in ADMINS


def require_admin(user: User = Depends(current_user)) -> User:
    if not is_admin(user):
        raise HTTPException(403, "Нужны права админа")
    return user


router = APIRouter(prefix="/admin", dependencies=[Depends(require_admin)])


def clean(value: str | None) -> str | None:
    value = (value or "").strip()
    return value or None


class TitleIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    kind: Literal["movie", "series"] = "series"
    poster_url: str | None = Field(None, max_length=500)
    description: str | None = Field(None, max_length=2000)


class EpisodeIn(BaseModel):
    number: int | None = Field(None, ge=1, le=9999)
    name: str | None = Field(None, max_length=200)
    hls_path: str | None = Field(None, max_length=500)
    status: Literal["not_ready", "preparing", "ready"] | None = None


class SeasonIn(BaseModel):
    number: int | None = Field(None, ge=1, le=999)


class AudioIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    language: str | None = Field(None, max_length=10)


def get_or_404(db: Session, model, obj_id: int, what: str):
    obj = db.get(model, obj_id)
    if obj is None:
        raise HTTPException(404, f"{what} не найден(а)")
    return obj


def title_tree(db: Session, t: Title) -> dict:
    seasons = db.scalars(select(Season).where(Season.title_id == t.id).order_by(Season.number)).all()
    out = []
    for s in seasons:
        eps = db.scalars(select(Episode).where(Episode.season_id == s.id).order_by(Episode.number)).all()
        items = []
        for e in eps:
            tracks = db.scalars(select(AudioTrack).where(AudioTrack.episode_id == e.id).order_by(AudioTrack.id)).all()
            items.append(
                {
                    "id": e.id,
                    "number": e.number,
                    "name": e.name,
                    "hls_path": e.hls_path,
                    "status": e.status,
                    "audio": [{"id": a.id, "name": a.name} for a in tracks],
                }
            )
        out.append({"id": s.id, "number": s.number, "episodes": items})
    return {
        "id": t.id,
        "name": t.name,
        "kind": t.kind,
        "poster_url": t.poster_url,
        "description": t.description,
        "seasons": out,
    }


# ---------- Фильмы и сериалы ----------
@router.get("/titles")
def list_titles(db: Session = Depends(get_db)):
    result = []
    for t in db.scalars(select(Title).order_by(Title.name)).all():
        statuses = db.scalars(
            select(Episode.status).join(Season, Episode.season_id == Season.id).where(Season.title_id == t.id)
        ).all()
        result.append(
            {
                "id": t.id,
                "name": t.name,
                "kind": t.kind,
                "episodes": len(statuses),
                "ready": sum(1 for s in statuses if s == "ready"),
            }
        )
    return result


@router.get("/titles/{title_id}")
def get_title(title_id: int, db: Session = Depends(get_db)):
    return title_tree(db, get_or_404(db, Title, title_id, "Фильм или сериал"))


@router.post("/titles")
def create_title(data: TitleIn, db: Session = Depends(get_db)):
    t = Title(
        name=data.name.strip(),
        kind=data.kind,
        poster_url=clean(data.poster_url),
        description=clean(data.description),
    )
    db.add(t)
    db.flush()
    if t.kind == "movie":  # у фильма сразу есть «сезон 1, серия 1»: сюда кладётся видео
        s = Season(title_id=t.id, number=1)
        db.add(s)
        db.flush()
        db.add(Episode(season_id=s.id, number=1))
    db.commit()
    return {"id": t.id}


@router.put("/titles/{title_id}")
def update_title(title_id: int, data: TitleIn, db: Session = Depends(get_db)):
    t = get_or_404(db, Title, title_id, "Фильм или сериал")
    t.name = data.name.strip()
    t.kind = data.kind
    t.poster_url = clean(data.poster_url)
    t.description = clean(data.description)
    db.commit()
    return {"ok": True}


@router.delete("/titles/{title_id}")
def delete_title(title_id: int, db: Session = Depends(get_db)):
    db.delete(get_or_404(db, Title, title_id, "Фильм или сериал"))
    db.commit()
    return {"ok": True}


# ---------- Сезоны ----------
@router.post("/titles/{title_id}/seasons")
def create_season(title_id: int, data: SeasonIn, db: Session = Depends(get_db)):
    get_or_404(db, Title, title_id, "Фильм или сериал")
    number = data.number
    if number is None:
        number = (db.scalar(select(func.max(Season.number)).where(Season.title_id == title_id)) or 0) + 1
    if db.scalar(select(Season).where(Season.title_id == title_id, Season.number == number)):
        raise HTTPException(409, f"Сезон {number} уже есть")
    s = Season(title_id=title_id, number=number)
    db.add(s)
    db.commit()
    return {"id": s.id}


@router.delete("/seasons/{season_id}")
def delete_season(season_id: int, db: Session = Depends(get_db)):
    db.delete(get_or_404(db, Season, season_id, "Сезон"))
    db.commit()
    return {"ok": True}


@router.post("/seasons/{season_id}/audio")
def add_audio_to_season(season_id: int, data: AudioIn, db: Session = Depends(get_db)):
    """Добавляет озвучку сразу всем сериям сезона (кому ещё не добавлена)."""
    get_or_404(db, Season, season_id, "Сезон")
    name = data.name.strip()
    added = 0
    for e in db.scalars(select(Episode).where(Episode.season_id == season_id)).all():
        exists = db.scalar(select(AudioTrack).where(AudioTrack.episode_id == e.id, AudioTrack.name == name))
        if not exists:
            db.add(AudioTrack(episode_id=e.id, name=name, language=clean(data.language)))
            added += 1
    db.commit()
    return {"added": added}


# ---------- Серии ----------
@router.post("/seasons/{season_id}/episodes")
def create_episode(season_id: int, data: EpisodeIn, db: Session = Depends(get_db)):
    get_or_404(db, Season, season_id, "Сезон")
    number = data.number
    if number is None:
        number = (db.scalar(select(func.max(Episode.number)).where(Episode.season_id == season_id)) or 0) + 1
    if db.scalar(select(Episode).where(Episode.season_id == season_id, Episode.number == number)):
        raise HTTPException(409, f"Серия {number} уже есть в этом сезоне")
    e = Episode(season_id=season_id, number=number, name=clean(data.name))
    db.add(e)
    db.commit()
    return {"id": e.id}


@router.put("/episodes/{episode_id}")
def update_episode(episode_id: int, data: EpisodeIn, db: Session = Depends(get_db)):
    e = get_or_404(db, Episode, episode_id, "Серия")
    fields = data.model_dump(exclude_unset=True)
    if fields.get("number") is not None and fields["number"] != e.number:
        if db.scalar(select(Episode).where(Episode.season_id == e.season_id, Episode.number == fields["number"])):
            raise HTTPException(409, f"Серия {fields['number']} уже есть в этом сезоне")
        e.number = fields["number"]
    if "name" in fields:
        e.name = clean(fields["name"])
    if "hls_path" in fields:
        path = clean(fields["hls_path"])
        e.hls_path = path.lstrip("/") if path else None
    if fields.get("status") is not None:
        e.status = fields["status"]
    if e.status == "ready" and not e.hls_path:
        raise HTTPException(400, "Для статуса «Готова» нужен путь к .m3u8")
    db.commit()
    return {"ok": True}


@router.delete("/episodes/{episode_id}")
def delete_episode(episode_id: int, db: Session = Depends(get_db)):
    db.delete(get_or_404(db, Episode, episode_id, "Серия"))
    db.commit()
    return {"ok": True}


# ---------- Озвучки ----------
@router.post("/episodes/{episode_id}/audio")
def add_audio(episode_id: int, data: AudioIn, db: Session = Depends(get_db)):
    get_or_404(db, Episode, episode_id, "Серия")
    a = AudioTrack(episode_id=episode_id, name=data.name.strip(), language=clean(data.language))
    db.add(a)
    db.commit()
    return {"id": a.id}


@router.delete("/audio/{audio_id}")
def delete_audio(audio_id: int, db: Session = Depends(get_db)):
    db.delete(get_or_404(db, AudioTrack, audio_id, "Озвучка"))
    db.commit()
    return {"ok": True}
