# ReelTogether

A watch-party web app built with **Python, FastAPI, SQLAlchemy, WebSockets, and WebRTC**. Every visitor must create an account or log in before creating or joining a room.

## Features

- Username/password accounts with passwords hashed using Python's `scrypt` password-hashing function.
- Optional **Remember this device for 1 month** login. Sessions are stored server-side and represented in the browser by an HttpOnly cookie.
- Regular logins use a browser-session cookie and a 12-hour server-side session lifetime.
- Rooms are stored in SQLite by default (or another SQLAlchemy-compatible database configured with `DATABASE_URL`). Rooms, movie URL/playback state, and chat history survive server restarts.
- Guests joining a room receive its saved chat history.
- Live participant list and host handoff when the host leaves.
- Synchronized play, pause, and seeking. The active room host controls the shared movie.
- Browser-to-browser microphone audio using WebRTC (mesh voice).
- Room text chat and quick emoji reactions.
- Responsive layout for desktop and mobile.
- Optional TURN server configuration for voice connections on restrictive networks.

## Requirements

- Python 3.10 or newer (Python 3.11/3.12 recommended).
- A modern browser with WebSocket, WebRTC, and HTML5 video support.
- Microphone access. Browsers generally require **HTTPS** for microphone access on a deployed site; `localhost` is allowed for local development.

## Run locally

From the `reeltogether` project directory:

```bash
# 1) Create and activate a virtual environment
python -m venv .venv

# Windows PowerShell
.venv\Scripts\Activate.ps1

# macOS / Linux
source .venv/bin/activate

# 2) Install dependencies
pip install -r requirements.txt

# 3) (Optional) configure environment variables
# Copy .env.example to .env and edit it if needed.

# 4) Start the server
uvicorn app.main:app --reload
```

Open <http://127.0.0.1:8000>. Create an account, then create a room or enter a friend's room code. Friends open the invite link and sign in or register before entering the room.

The SQLite database is created automatically at `data/reeltogether.db` on first startup. Keep this file (or the whole `data` directory) when upgrading the app; it contains account records, room records, playback state, login sessions, and chat history. Do not delete it unless you intend to reset the application.

## Authentication and remember-device behavior

- Usernames are 3–24 characters and may contain letters, numbers, and underscores.
- Passwords must be at least 8 characters.
- Passwords are never stored in plaintext; only a salted scrypt hash is stored.
- The remember checkbox stores a persistent HttpOnly cookie and server-side session for 30 days.
- Without the checkbox, the browser receives a session cookie and the server session expires after 12 hours.
- To use secure cookies on an HTTPS deployment, set `COOKIE_SECURE=true` in the environment.
- Logging out revokes the server-side session as well as clearing the cookie.

## Database configuration and backup

SQLite is used by default and requires no additional service. Set `DATABASE_URL` to change the database. For example:

```bash
# Default local SQLite database
DATABASE_URL=sqlite:///./data/reeltogether.db
```

The SQLAlchemy models are created automatically when the application starts. The current version uses `create_all` for initial/additive table creation, not a formal Alembic migration system. Back up `data/reeltogether.db` before replacing the project with a future version that changes its schema.

## Run with Docker

```bash
docker build -t reeltogether .
docker run --rm -p 8000:8000 -v reeltogether-data:/app/data reeltogether
```

For production HTTPS and a TURN server, pass your environment file:

```bash
docker run --rm -p 8000:8000 \
  -v reeltogether-data:/app/data \
  --env-file .env reeltogether
```

The named Docker volume preserves the database when the container is removed. Set `COOKIE_SECURE=true` when requests are served through HTTPS.

## Movie URLs and usage rights

The host pastes a **direct, browser-accessible video file URL** such as an `.mp4` or `.webm` URL. Every guest's browser fetches the stream directly from that URL. The URL must permit access from each guest's browser and should support normal HTML5 video playback.

This app does not bypass DRM, subscription restrictions, paywalls, or access controls. YouTube watch-page URLs, Netflix pages, and similar protected streaming pages are not direct video-file URLs. Only share content you have the right to watch and share with your group.

## Voice chat notes

Voice uses peer-to-peer WebRTC audio. The default configuration includes public STUN servers, which is enough for many home and office networks. Some networks (symmetric NATs, corporate firewalls, restrictive mobile networks) require a TURN relay. For a public deployment, configure your own TURN provider in `.env`:

```bash
TURN_URLS=turn:turn.example.com:3478?transport=udp,turns:turn.example.com:5349
TURN_USERNAME=your-turn-username
TURN_CREDENTIAL=your-turn-credential
```

The app exposes these ICE settings through `/api/rtc-config` to room clients. Do not commit TURN credentials to source control.

## Project layout

```text
reeltogether/
├── app/
│   ├── __init__.py
│   ├── main.py                 # FastAPI routes, SQLAlchemy models, auth, WebSocket signaling
│   ├── templates/
│   │   ├── index.html          # Authenticated create / join dashboard
│   │   ├── login.html          # Login screen and remember-device checkbox
│   │   ├── register.html       # Account registration
│   │   └── room.html           # Watch room UI
│   └── static/
│       ├── styles.css
│       └── room.js             # Playback sync, saved chat history, WebRTC voice client
├── data/                       # Created automatically; SQLite database
├── .env.example
├── Dockerfile
└── requirements.txt
```

## Important deployment notes

- Live WebSocket connections, current participants, and WebRTC signaling remain in process memory. Durable room data, playback state, accounts, sessions, and chat messages are in the database. This starter version is designed for a **single Uvicorn worker**; multiple workers/servers need Redis or another shared pub/sub layer for live WebSocket fan-out and shared active-host state.
- Room chat history is loaded when a user connects. It is currently sent as the full room history; for very active rooms, add paginated history endpoints to keep the initial payload small.
- Voice uses mesh WebRTC, where each participant connects to each other participant. For larger groups, use an SFU such as LiveKit, mediasoup, or Janus.
- The movie is streamed by each guest's browser from the supplied URL, not proxied or stored by the Python server. Playback compatibility depends on the source server, codec, browser, and CORS / access configuration.
- Use HTTPS in production, enable secure cookies, keep the database volume backed up, and use a strong unique password.

## Troubleshooting

- If `GET /` returns a Jinja2 `TypeError: cannot use 'tuple' as a dict key`, make sure the updated `app/main.py` is installed. It renders templates directly to avoid Starlette `TemplateResponse` signature differences.
- If the browser returns to the login page when opening an invite, sign in first. Invite links require a valid account.
- If you can enter a room but cannot hear someone, check microphone permission and configure a TURN server if the network blocks peer-to-peer connections.
