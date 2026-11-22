from __future__ import annotations

import asyncio
import hashlib
import hmac
import os
import re
import secrets
import string
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from sqlalchemy import Boolean, Float, ForeignKey, Integer, String, create_engine, func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

BASE_DIR = Path(__file__).resolve().parent
PROJECT_DIR = BASE_DIR.parent
load_dotenv(PROJECT_DIR / ".env")

ROOM_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"
ROOM_CODE_PATTERN = re.compile(rf"^[{re.escape(ROOM_ALPHABET)}]{{4,12}}$")
USERNAME_PATTERN = re.compile(r"^[a-zA-Z0-9_]{3,24}$")
SESSION_COOKIE = "reeltogether_session"
DAY_SECONDS = 24 * 60 * 60
REMEMBER_SECONDS = 30 * DAY_SECONDS
NORMAL_SESSION_SECONDS = 12 * 60 * 60

# SQLite is the zero-configuration default. The data directory is kept outside
# the app package so it can be mounted as a persistent Docker volume.
# On PaaS, mount its persistent volume at /data and set DATA_DIR=/data.
# Locally, keep the database inside ./data by default.
DEFAULT_DATA_DIR = Path(os.getenv("DATA_DIR", str(PROJECT_DIR / "data"))).expanduser()
DEFAULT_DATA_DIR.mkdir(parents=True, exist_ok=True)
DEFAULT_DATABASE_URL = f"sqlite:///{(DEFAULT_DATA_DIR / 'reeltogether.db').as_posix()}"
DATABASE_URL = os.getenv("DATABASE_URL", DEFAULT_DATABASE_URL).strip()
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "false").strip().lower() in {"1", "true", "yes", "on"}

engine_options: dict[str, Any] = {"pool_pre_ping": True}
if DATABASE_URL.startswith("sqlite"):
    engine_options["connect_args"] = {"check_same_thread": False, "timeout": 30}
engine = create_engine(DATABASE_URL, **engine_options)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(24), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(256))
    created_at: Mapped[float] = mapped_column(Float, default=time.time)


class LoginSession(Base):
    __tablename__ = "login_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    created_at: Mapped[float] = mapped_column(Float, default=time.time)
    expires_at: Mapped[float] = mapped_column(Float, index=True)
    remember_device: Mapped[bool] = mapped_column(Boolean, default=False)


class RoomModel(Base):
    __tablename__ = "rooms"

    room_id: Mapped[str] = mapped_column(String(12), primary_key=True)
    created_by_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    host_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    source: Mapped[str] = mapped_column(String(2048), default="")
    current_time: Mapped[float] = mapped_column(Float, default=0.0)
    playing: Mapped[bool] = mapped_column(Boolean, default=False)
    playback_updated_at: Mapped[float] = mapped_column(Float, default=time.time)
    created_at: Mapped[float] = mapped_column(Float, default=time.time)
    updated_at: Mapped[float] = mapped_column(Float, default=time.time)


class ChatMessage(Base):
    __tablename__ = "chat_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    room_id: Mapped[str] = mapped_column(ForeignKey("rooms.room_id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    sender_name: Mapped[str] = mapped_column(String(24))
    text: Mapped[str] = mapped_column(String(1000))
    sent_at: Mapped[float] = mapped_column(Float, default=time.time, index=True)


# Create missing tables at startup/import. This is sufficient for this starter
# app's additive schema; back up the SQLite file before future schema changes.
Base.metadata.create_all(bind=engine)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=64)
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored_hash: str) -> bool:
    try:
        algorithm, salt_hex, digest_hex = stored_hash.split("$", 2)
        if algorithm != "scrypt":
            return False
        expected = bytes.fromhex(digest_hex)
        actual = hashlib.scrypt(
            password.encode("utf-8"), salt=bytes.fromhex(salt_hex), n=2**14, r=8, p=1, dklen=len(expected)
        )
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def find_user_for_token(db: Session, token: str | None) -> User | None:
    if not token:
        return None
    login_session = db.query(LoginSession).filter(LoginSession.token_hash == token_digest(token)).first()
    if login_session is None:
        return None
    if login_session.expires_at <= time.time():
        db.delete(login_session)
        db.commit()
        return None
    return db.get(User, login_session.user_id)


def get_request_user(request: Request, db: Session) -> User | None:
    return find_user_for_token(db, request.cookies.get(SESSION_COOKIE))


def require_user(request: Request, db: Session = Depends(get_db)) -> User:
    user = get_request_user(request, db)
    if user is None:
        raise HTTPException(status_code=401, detail="Please log in to continue.")
    return user


def set_login_cookie(response: Response, db: Session, user: User, remember: bool) -> None:
    raw_token = secrets.token_urlsafe(40)
    lifetime = REMEMBER_SECONDS if remember else NORMAL_SESSION_SECONDS
    db.add(
        LoginSession(
            user_id=user.id,
            token_hash=token_digest(raw_token),
            created_at=time.time(),
            expires_at=time.time() + lifetime,
            remember_device=remember,
        )
    )
    db.commit()
    cookie_options: dict[str, Any] = {
        "key": SESSION_COOKIE,
        "value": raw_token,
        "httponly": True,
        "secure": COOKIE_SECURE,
        "samesite": "lax",
        "path": "/",
    }
    if remember:
        cookie_options["max_age"] = REMEMBER_SECONDS
    response.set_cookie(**cookie_options)


def safe_next_path(value: str | None) -> str:
    if value and value.startswith("/") and not value.startswith("//") and "\\" not in value:
        return value
    return "/"


@dataclass
class Participant:
    peer_id: str
    user_id: int
    name: str
    websocket: WebSocket
    mic_on: bool = False
    joined_at: float = field(default_factory=time.time)

    def public(self) -> dict[str, Any]:
        return {
            "peer_id": self.peer_id,
            "user_id": self.user_id,
            "name": self.name,
            "mic_on": self.mic_on,
        }


@dataclass
class ActiveRoom:
    """Ephemeral live connections for a room whose durable record is in SQL."""

    room_id: str
    participants: dict[str, Participant] = field(default_factory=dict)
    host_id: str | None = None
    playback: dict[str, Any] = field(default_factory=dict)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def public_participants(self) -> list[dict[str, Any]]:
        return [person.public() for person in self.participants.values()]


app = FastAPI(title="ReelTogether", version="2.0.0")
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")
active_rooms: dict[str, ActiveRoom] = {}
active_rooms_lock = asyncio.Lock()


def valid_room_id(room_id: str) -> bool:
    return bool(ROOM_CODE_PATTERN.fullmatch(room_id.upper()))


def valid_video_url(value: str) -> bool:
    if not value or len(value) > 2048:
        return False
    parsed = urlparse(value.strip())
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def playback_from_model(room_record: RoomModel) -> dict[str, Any]:
    return {
        "source": room_record.source or "",
        "current_time": float(room_record.current_time or 0.0),
        "playing": bool(room_record.playing),
        "updated_at": float(room_record.playback_updated_at or time.time()),
    }


def history_item(record: ChatMessage) -> dict[str, Any]:
    return {
        "id": record.id,
        "user_id": record.user_id,
        "peer_id": None,
        "name": record.sender_name,
        "text": record.text,
        "sent_at": record.sent_at,
    }


async def get_active_room(room_id: str, playback: dict[str, Any]) -> ActiveRoom:
    async with active_rooms_lock:
        room = active_rooms.get(room_id)
        if room is None:
            room = ActiveRoom(room_id=room_id, playback=playback)
            active_rooms[room_id] = room
        return room


async def broadcast(room: ActiveRoom, payload: dict[str, Any], exclude: str | None = None) -> None:
    targets = [
        participant.websocket
        for peer_id, participant in list(room.participants.items())
        if peer_id != exclude
    ]
    if not targets:
        return

    async def send_one(ws: WebSocket) -> None:
        try:
            await ws.send_json(payload)
        except Exception:
            # Disconnect cleanup removes stale participants.
            pass

    await asyncio.gather(*(send_one(ws) for ws in targets), return_exceptions=True)


@app.get("/", response_class=HTMLResponse)
def home(request: Request, db: Session = Depends(get_db)):
    user = get_request_user(request, db)
    if user is None:
        return RedirectResponse(url="/login", status_code=303)
    html = templates.env.get_template("index.html").render(username=user.username)
    return HTMLResponse(content=html)


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str | None = None, db: Session = Depends(get_db)):
    if get_request_user(request, db) is not None:
        return RedirectResponse(url=safe_next_path(next), status_code=303)
    html = templates.env.get_template("login.html").render(next_url=safe_next_path(next))
    return HTMLResponse(content=html)


@app.get("/register", response_class=HTMLResponse)
def register_page(request: Request, next: str | None = None, db: Session = Depends(get_db)):
    if get_request_user(request, db) is not None:
        return RedirectResponse(url=safe_next_path(next), status_code=303)
    html = templates.env.get_template("register.html").render(next_url=safe_next_path(next))
    return HTMLResponse(content=html)


@app.get("/room/{room_id}", response_class=HTMLResponse)
def room_page(room_id: str, request: Request, db: Session = Depends(get_db)):
    room_id = room_id.upper()
    if not valid_room_id(room_id):
        raise HTTPException(status_code=404, detail="Room not found")
    user = get_request_user(request, db)
    if user is None:
        next_path = quote(f"/room/{room_id}", safe="")
        return RedirectResponse(url=f"/login?next={next_path}", status_code=303)
    room_record = db.get(RoomModel, room_id)
    if room_record is None:
        raise HTTPException(status_code=404, detail="Room not found. Check the invite code.")
    html = templates.env.get_template("room.html").render(
        room_id=room_id,
        username=user.username,
        user_id=user.id,
    )
    return HTMLResponse(content=html)


class Credentials(BaseModel):
    username: str = Field(min_length=3, max_length=24)
    password: str = Field(min_length=8, max_length=128)
    remember: bool = False


@app.post("/api/auth/register")
def register_account(data: Credentials, response: Response, db: Session = Depends(get_db)):
    username = data.username.strip().lower()
    if not USERNAME_PATTERN.fullmatch(username):
        raise HTTPException(status_code=422, detail="Username must be 3–24 characters and use only letters, numbers, or underscores.")
    if db.query(User).filter(func.lower(User.username) == username).first() is not None:
        raise HTTPException(status_code=409, detail="That username is already taken. Please choose another.")
    user = User(username=username, password_hash=hash_password(data.password), created_at=time.time())
    db.add(user)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="That username is already taken. Please choose another.")
    db.refresh(user)
    set_login_cookie(response, db, user, data.remember)
    return {"ok": True, "username": user.username}


@app.post("/api/auth/login")
def login_account(data: Credentials, response: Response, db: Session = Depends(get_db)):
    username = data.username.strip().lower()
    user = db.query(User).filter(func.lower(User.username) == username).first()
    if user is None or not verify_password(data.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Incorrect username or password.")
    set_login_cookie(response, db, user, data.remember)
    return {"ok": True, "username": user.username}


@app.post("/api/auth/logout")
def logout_account(request: Request, response: Response, db: Session = Depends(get_db)):
    raw_token = request.cookies.get(SESSION_COOKIE)
    if raw_token:
        record = db.query(LoginSession).filter(LoginSession.token_hash == token_digest(raw_token)).first()
        if record is not None:
            db.delete(record)
            db.commit()
    response.delete_cookie(key=SESSION_COOKIE, path="/", httponly=True, secure=COOKIE_SECURE, samesite="lax")
    return {"ok": True}


@app.get("/api/auth/me")
def auth_me(user: User = Depends(require_user)):
    return {"id": user.id, "username": user.username}


@app.post("/api/rooms")
def create_room(user: User = Depends(require_user), db: Session = Depends(get_db)):
    while True:
        room_id = "".join(secrets.choice(ROOM_ALPHABET) for _ in range(7))
        if db.get(RoomModel, room_id) is None:
            record = RoomModel(
                room_id=room_id,
                created_by_id=user.id,
                host_user_id=user.id,
                source="",
                current_time=0.0,
                playing=False,
                playback_updated_at=time.time(),
                created_at=time.time(),
                updated_at=time.time(),
            )
            db.add(record)
            try:
                db.commit()
                break
            except IntegrityError:
                db.rollback()
    return {"room_id": room_id, "url": f"/room/{room_id}"}


@app.get("/api/rtc-config")
def rtc_config():
    ice_servers: list[dict[str, Any]] = [
        {"urls": "stun:stun.l.google.com:19302"},
        {"urls": "stun:stun1.l.google.com:19302"},
    ]
    turn_urls = [url.strip() for url in os.getenv("TURN_URLS", "").split(",") if url.strip()]
    if turn_urls:
        turn_server: dict[str, Any] = {"urls": turn_urls}
        username = os.getenv("TURN_USERNAME", "").strip()
        credential = os.getenv("TURN_CREDENTIAL", "").strip()
        if username and credential:
            turn_server["username"] = username
            turn_server["credential"] = credential
        ice_servers.append(turn_server)
    return {"iceServers": ice_servers}


@app.get("/api/health")
def health(db: Session = Depends(get_db)):
    return {
        "status": "ok",
        "active_rooms": len(active_rooms),
        "saved_rooms": db.query(func.count(RoomModel.room_id)).scalar() or 0,
    }


@app.websocket("/ws/{room_id}")
async def room_socket(websocket: WebSocket, room_id: str):
    room_id = room_id.upper()
    if not valid_room_id(room_id):
        await websocket.close(code=1008)
        return
    origin = websocket.headers.get("origin")
    request_host = websocket.headers.get("host")
    if origin and request_host:
        origin_host = urlparse(origin).netloc
        if origin_host and origin_host.lower() != request_host.lower():
            await websocket.close(code=1008)
            return

    # The room and account must already exist in the database. Invite links
    # never create anonymous or transient rooms.
    with SessionLocal() as db:
        user = find_user_for_token(db, websocket.cookies.get(SESSION_COOKIE))
        room_record = db.get(RoomModel, room_id)
        if user is None:
            await websocket.close(code=4401)
            return
        if room_record is None:
            await websocket.close(code=4404)
            return
        user_id = user.id
        username = user.username
        playback = playback_from_model(room_record)
        chat_history = [history_item(item) for item in db.query(ChatMessage).filter(
            ChatMessage.room_id == room_id
        ).order_by(ChatMessage.id.asc()).all()]

    await websocket.accept()
    room = await get_active_room(room_id, playback)
    peer_id = uuid.uuid4().hex[:12]
    participant = Participant(peer_id=peer_id, user_id=user_id, name=username, websocket=websocket)

    async with room.lock:
        if not room.participants:
            # If the room has no live participants, its first returning member
            # becomes the new host; that assignment is persisted.
            room.host_id = peer_id
            with SessionLocal() as db:
                record = db.get(RoomModel, room_id)
                if record is not None:
                    record.host_user_id = user_id
                    record.updated_at = time.time()
                    db.commit()
        room.participants[peer_id] = participant
        welcome = {
            "type": "welcome",
            "peer_id": peer_id,
            "user_id": user_id,
            "host_id": room.host_id,
            "participants": room.public_participants(),
            "playback": dict(room.playback),
            "chat_history": chat_history,
        }

    await websocket.send_json(welcome)
    await broadcast(
        room,
        {"type": "participant_joined", "participant": participant.public()},
        exclude=peer_id,
    )

    try:
        while True:
            message = await websocket.receive_json()
            if not isinstance(message, dict):
                continue
            event_type = message.get("type")

            if event_type == "chat":
                text = str(message.get("text", "")).strip()
                if not text:
                    continue
                text = text[:1000]
                sent_at = time.time()
                with SessionLocal() as db:
                    saved_message = ChatMessage(
                        room_id=room_id,
                        user_id=user_id,
                        sender_name=participant.name,
                        text=text,
                        sent_at=sent_at,
                    )
                    db.add(saved_message)
                    db.commit()
                    db.refresh(saved_message)
                    outgoing_message = {
                        "id": saved_message.id,
                        "user_id": user_id,
                        "peer_id": peer_id,
                        "name": participant.name,
                        "text": text,
                        "sent_at": sent_at,
                    }
                await broadcast(room, {"type": "chat", "message": outgoing_message})

            elif event_type == "mic":
                enabled = bool(message.get("enabled", False))
                async with room.lock:
                    current = room.participants.get(peer_id)
                    if current:
                        current.mic_on = enabled
                await broadcast(
                    room,
                    {"type": "participant_updated", "peer_id": peer_id, "mic_on": enabled},
                )

            elif event_type == "signal":
                to_peer = str(message.get("to", ""))
                signal_data = message.get("signal")
                if not to_peer or to_peer == peer_id or not isinstance(signal_data, dict):
                    continue
                target = room.participants.get(to_peer)
                if target:
                    try:
                        await target.websocket.send_json(
                            {"type": "signal", "from": peer_id, "signal": signal_data}
                        )
                    except Exception:
                        pass

            elif event_type == "source":
                if peer_id != room.host_id:
                    continue
                source = str(message.get("url", "")).strip()
                if not valid_video_url(source):
                    await websocket.send_json(
                        {"type": "error", "message": "Enter a valid direct video URL (http or https)."}
                    )
                    continue
                updated_at = time.time()
                with SessionLocal() as db:
                    record = db.get(RoomModel, room_id)
                    if record is None:
                        continue
                    record.source = source
                    record.current_time = 0.0
                    record.playing = False
                    record.playback_updated_at = updated_at
                    record.updated_at = updated_at
                    db.commit()
                    state = playback_from_model(record)
                room.playback = state
                await broadcast(room, {"type": "playback_state", "playback": state})

            elif event_type == "playback":
                if peer_id != room.host_id:
                    continue
                action = str(message.get("action", ""))
                if action not in {"play", "pause", "seek", "sync"}:
                    continue
                try:
                    current_time = max(0.0, float(message.get("current_time", 0.0)))
                except (TypeError, ValueError):
                    current_time = 0.0
                updated_at = time.time()
                with SessionLocal() as db:
                    record = db.get(RoomModel, room_id)
                    if record is None:
                        continue
                    record.current_time = current_time
                    if action == "play":
                        record.playing = True
                    elif action == "pause":
                        record.playing = False
                    record.playback_updated_at = updated_at
                    record.updated_at = updated_at
                    db.commit()
                    state = playback_from_model(record)
                room.playback = state
                await broadcast(
                    room,
                    {"type": "playback_state", "playback": state, "action": action},
                    exclude=peer_id,
                )

            elif event_type == "reaction":
                emoji = str(message.get("emoji", ""))
                if emoji not in {"❤️", "😂", "👏", "🎉", "😮", "🔥"}:
                    continue
                await broadcast(
                    room,
                    {"type": "reaction", "peer_id": peer_id, "name": participant.name, "emoji": emoji},
                )

    except WebSocketDisconnect:
        pass
    except Exception:
        # Treat malformed or abruptly terminated client sessions as disconnects.
        pass
    finally:
        async with room.lock:
            removed = room.participants.pop(peer_id, None)
            was_host = room.host_id == peer_id
            if was_host:
                next_participant = next(iter(room.participants.values()), None)
                room.host_id = next_participant.peer_id if next_participant else None
                with SessionLocal() as db:
                    record = db.get(RoomModel, room_id)
                    if record is not None and next_participant is not None:
                        record.host_user_id = next_participant.user_id
                        record.updated_at = time.time()
                        db.commit()
            new_host_id = room.host_id
        if removed:
            await broadcast(
                room,
                {
                    "type": "participant_left",
                    "peer_id": peer_id,
                    "host_id": new_host_id,
                    "host_changed": was_host,
                },
            )
        # Only live sockets stay in memory. The room record, playback state,
        # users, sessions, and chat history remain in the database.
        if not room.participants:
            async with active_rooms_lock:
                if active_rooms.get(room_id) is room:
                    active_rooms.pop(room_id, None)
