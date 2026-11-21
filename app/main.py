from __future__ import annotations

import asyncio
import os
import secrets
import string
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from dotenv import load_dotenv
from fastapi.templating import Jinja2Templates

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR.parent / ".env")
ROOM_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"


@dataclass
class Participant:
    peer_id: str
    name: str
    websocket: WebSocket
    mic_on: bool = False
    joined_at: float = field(default_factory=time.time)

    def public(self) -> dict[str, Any]:
        return {
            "peer_id": self.peer_id,
            "name": self.name,
            "mic_on": self.mic_on,
        }


@dataclass
class Room:
    room_id: str
    participants: dict[str, Participant] = field(default_factory=dict)
    host_id: str | None = None
    playback: dict[str, Any] = field(
        default_factory=lambda: {
            "source": "",
            "current_time": 0.0,
            "playing": False,
            "updated_at": time.time(),
        }
    )
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def public_participants(self) -> list[dict[str, Any]]:
        return [person.public() for person in self.participants.values()]


app = FastAPI(title="ReelTogether", version="1.0.0")
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")
rooms: dict[str, Room] = {}
rooms_lock = asyncio.Lock()


async def get_or_create_room(room_id: str) -> Room:
    async with rooms_lock:
        room = rooms.get(room_id)
        if room is None:
            room = Room(room_id=room_id)
            rooms[room_id] = room
        return room


async def broadcast(room: Room, payload: dict[str, Any], exclude: str | None = None) -> None:
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
            # A disconnect event will remove stale participants.
            pass

    await asyncio.gather(*(send_one(ws) for ws in targets), return_exceptions=True)


def playback_snapshot(room: Room) -> dict[str, Any]:
    return dict(room.playback)


def valid_video_url(value: str) -> bool:
    if not value or len(value) > 2048:
        return False
    parsed = urlparse(value.strip())
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


@app.get("/", response_class=HTMLResponse)
async def home():
    # Render through Jinja2 directly rather than Starlette's TemplateResponse.
    # Starlette changed TemplateResponse's positional argument order across
    # releases; direct rendering keeps this route compatible with both older
    # and newer FastAPI/Starlette installations.
    html = templates.env.get_template("index.html").render()
    return HTMLResponse(content=html)


@app.get("/room/{room_id}", response_class=HTMLResponse)
async def room_page(room_id: str):
    room_id = room_id.upper()
    if len(room_id) < 4 or len(room_id) > 12 or any(c not in ROOM_ALPHABET for c in room_id):
        raise HTTPException(status_code=404, detail="Room not found")
    # See the note in home(): avoid version-sensitive TemplateResponse calls.
    html = templates.env.get_template("room.html").render(room_id=room_id)
    return HTMLResponse(content=html)


@app.post("/api/rooms")
async def create_room():
    async with rooms_lock:
        while True:
            room_id = "".join(secrets.choice(ROOM_ALPHABET) for _ in range(7))
            if room_id not in rooms:
                rooms[room_id] = Room(room_id=room_id)
                break
    return {"room_id": room_id, "url": f"/room/{room_id}"}


@app.get("/api/rtc-config")
async def rtc_config():
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
async def health():
    return {"status": "ok", "active_rooms": len(rooms)}


@app.websocket("/ws/{room_id}")
async def room_socket(websocket: WebSocket, room_id: str):
    room_id = room_id.upper()
    if len(room_id) < 4 or len(room_id) > 12 or any(c not in ROOM_ALPHABET for c in room_id):
        await websocket.close(code=1008)
        return

    name = (websocket.query_params.get("name") or "Guest").strip()
    name = " ".join(name.split())[:24] or "Guest"
    await websocket.accept()

    room = await get_or_create_room(room_id)
    peer_id = uuid.uuid4().hex[:12]
    participant = Participant(peer_id=peer_id, name=name, websocket=websocket)

    async with room.lock:
        if room.host_id is None:
            room.host_id = peer_id
        room.participants[peer_id] = participant
        welcome = {
            "type": "welcome",
            "peer_id": peer_id,
            "host_id": room.host_id,
            "participants": room.public_participants(),
            "playback": playback_snapshot(room),
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
                await broadcast(
                    room,
                    {
                        "type": "chat",
                        "message": {
                            "id": uuid.uuid4().hex[:10],
                            "peer_id": peer_id,
                            "name": participant.name,
                            "text": text,
                            "sent_at": time.time(),
                        },
                    },
                )

            elif event_type == "mic":
                enabled = bool(message.get("enabled", False))
                async with room.lock:
                    current = room.participants.get(peer_id)
                    if current:
                        current.mic_on = enabled
                await broadcast(
                    room,
                    {
                        "type": "participant_updated",
                        "peer_id": peer_id,
                        "mic_on": enabled,
                    },
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
                async with room.lock:
                    room.playback = {
                        "source": source,
                        "current_time": 0.0,
                        "playing": False,
                        "updated_at": time.time(),
                    }
                    state = playback_snapshot(room)
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
                async with room.lock:
                    room.playback["current_time"] = current_time
                    if action == "play":
                        room.playback["playing"] = True
                    elif action == "pause":
                        room.playback["playing"] = False
                    room.playback["updated_at"] = time.time()
                    state = playback_snapshot(room)
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
                room.host_id = next(iter(room.participants.keys()), None)
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
        # Empty rooms remain in memory so a copied invite remains usable during
        # the server process lifetime. They are intentionally ephemeral.
