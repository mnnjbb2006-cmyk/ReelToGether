# ReelTogether

A small watch-party web app with a **Python / FastAPI backend**. Friends join a room link, watch the same direct video URL in sync, chat, react, and talk over browser-based voice chat.

## Features

- Create a room with a short invite code; no account is needed.
- Live participant list and host handoff when the host leaves.
- Synchronized play, pause, and seeking. The first person in the room is the host and controls the shared movie.
- Browser-to-browser microphone audio using WebRTC (mesh voice).
- Room text chat and quick emoji reactions.
- Responsive layout for desktop and mobile.
- Optional TURN server configuration for voice connections on restrictive networks.

## Requirements

- Python 3.10 or newer (Python 3.11/3.12 recommended).
- A modern browser with WebSocket, WebRTC, and HTML5 video support.
- Microphone access. Browsers generally require **HTTPS** for microphone access on a deployed site; `localhost` is allowed for local development.

## Run locally

```bash
# 1) Create and activate a virtual environment
python -m venv .venv

# Windows PowerShell
.venv\Scripts\Activate.ps1

# macOS / Linux
source .venv/bin/activate

# 2) Install dependencies
pip install -r requirements.txt

# 3) Start the server
uvicorn app.main:app --reload
```

Open <http://127.0.0.1:8000> in your browser. Create a room, then copy the invite link to your friends. For a local multi-device test, all devices must be able to reach the computer running the server; use that computer's LAN IP instead of `127.0.0.1`. Microphone access on other devices also requires HTTPS (plain HTTP on a LAN IP is usually not a secure browser context).

## Movie URLs and usage rights

The host pastes a **direct, browser-accessible video file URL** such as an `.mp4` or `.webm` URL. Every guest's browser fetches the stream directly from that URL. The URL must permit access from each guest's browser and should support normal HTML5 video playback.

This app does not bypass DRM, subscription restrictions, paywalls, or access controls. YouTube watch-page URLs, Netflix pages, and similar protected streaming pages are not direct video-file URLs. Only share content you have the right to watch and share with your group.

## Voice chat notes

Voice uses peer-to-peer WebRTC audio. The default configuration includes public STUN servers, which is enough for many home and office networks. Some networks (symmetric NATs, corporate firewalls, restrictive mobile networks) require a TURN relay. For a public deployment, configure your own TURN provider:

1. Copy `.env.example` to `.env` and fill in `TURN_URLS`, `TURN_USERNAME`, and `TURN_CREDENTIAL`. The app loads this local `.env` file automatically. Alternatively, provide the variables through your process manager or deployment environment.
2. To set the values directly in your shell instead, use the following example on macOS/Linux:

   ```bash
   export TURN_URLS='turn:turn.example.com:3478?transport=udp,turns:turn.example.com:5349'
   export TURN_USERNAME='your-turn-username'
   export TURN_CREDENTIAL='your-turn-credential'
   uvicorn app.main:app --host 0.0.0.0 --port 8000
   ```

   On Windows PowerShell, use `$env:TURN_URLS=...` etc. The app exposes these ICE settings through `/api/rtc-config` to room clients.

For production, serve the app behind HTTPS (for example, with Caddy or Nginx and a TLS certificate). Do not commit TURN credentials to source control.

## Run with Docker

```bash
docker build -t reeltogether .
docker run --rm -p 8000:8000 reeltogether

# For a configured TURN server, pass your local environment file:
# docker run --rm --env-file .env -p 8000:8000 reeltogether
```

Then open <http://127.0.0.1:8000>.

## Project layout

```text
reeltogether/
├── app/
│   ├── __init__.py
│   ├── main.py                 # FastAPI routes, room state, WebSocket signaling
│   ├── templates/
│   │   ├── index.html          # Create / join screen
│   │   └── room.html           # Watch room UI
│   └── static/
│       ├── styles.css
│       └── room.js             # Playback sync, chat, WebRTC voice client
├── .env.example
├── Dockerfile
└── requirements.txt
```

## Important MVP limitations

- Room state and chat are in memory; restarting the server clears rooms, and this single-process implementation is intended as a starter project. For multi-worker production deployment, move room state and WebSocket fan-out to Redis (or another shared pub/sub layer).
- There is no user account system or room password. Treat invite links as private.
- Voice uses mesh WebRTC, where each participant connects to each other participant. This is straightforward for small groups; for larger rooms, use an SFU such as LiveKit, mediasoup, or Janus.
- The movie is streamed by each guest's browser from the supplied URL, not proxied or stored by the Python server. Playback compatibility depends on the source server, codec, browser, and CORS / access configuration.

## Troubleshooting: `GET /` returns 500 in `TemplateResponse`

If the traceback ends in Jinja2 with `TypeError: cannot use 'tuple' as a dict key (unhashable type: 'dict')`, the installed Starlette version is interpreting the older `TemplateResponse` positional arguments differently. The current project avoids that version-sensitive API by rendering the Jinja templates directly and returning an `HTMLResponse`. Make sure you are running the updated `app/main.py` from this project, then restart Uvicorn.
