# XO Cowork API

FastAPI server that interfaces with local Claude Code CLI, providing REST API endpoints for AI-powered conversations with session management.

## Architecture

```
┌─────────────────┐     ┌──────────────────┐     ┌─────────────────┐
│  Client Request │────▶│  xo-cowork-api   │────▶│  Claude Code    │
│                 │     │  (FastAPI)       │     │  CLI            │
└─────────────────┘     └────────┬─────────┘     └─────────────────┘
                                 │
                                 ▼
                        ┌──────────────────┐
                        │  Chat API        │
                        │  (optional)      │
                        └──────────────────┘
```

## Features

- **Session Management**: Automatic session creation and resumption per project
- **Streaming Support**: Real-time SSE streaming responses
- **Non-Streaming**: Standard JSON responses
- **Chat History**: Optional integration with external Chat API for message persistence

## Prerequisites

- Python 3.12+
- [Claude Code CLI](https://claude.ai/claude-code) installed and authenticated
- Conda (recommended) or virtualenv

## Quick Start

### 1. Clone the repository

```bash
git clone https://github.com/sharmasuraj0123/xo-cowork-api.git
cd xo-cowork-api
```

### 2. Create conda environment

```bash
conda create -n xo-cowork-api python=3.12 -y
conda activate xo-cowork-api
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. Configure environment

```bash
cp .env.example .env
```

Edit `.env` with your settings:

```env
# Server Configuration
HOST=0.0.0.0
PORT=5002

# Claude Code CLI path (find with: which claude)
CLAUDE_CLI_PATH=/Users/yourname/.local/bin/claude

# Optional: External Chat API for message persistence
CHAT_API_BASE_URL=http://localhost:5001

# Claude Code timeout in seconds
CLAUDE_TIMEOUT=300
```

### 5. Authenticate Claude Code

Before running the server, ensure Claude Code is authenticated:

```bash
claude /login
```

### 6. Run the server

```bash
python server.py
```

Server will start at `http://localhost:5002`

## API Endpoints

### Health Check

```bash
GET /health
```

Returns server status and configuration.

### List Sessions

```bash
GET /sessions
```

Returns all active project sessions.

### Delete Session

```bash
DELETE /sessions/{project_id}
```

Clears the session for a specific project.

### XO Browser Auth (for protected xo-swarm-api)

#### 1) Start auth flow

```bash
curl -X POST http://localhost:5002/xo-auth/start \
  -H "Content-Type: application/json" \
  -d '{"client_reference":"cowork-demo"}'
```

Response includes:
- `authorize_url` (open this in browser)
- `auth_session_id`
- `poll_token`

#### 2) Open `authorize_url` in browser

User logs in through Clerk. XO backend handles callback.

#### 3) Poll status

```bash
curl "http://localhost:5002/xo-auth/status/<auth_session_id>?poll_token=<poll_token>"
```

Wait for `status: "authorized"`.

#### 4) Consume and store token

```bash
curl -X POST http://localhost:5002/xo-auth/consume \
  -H "Content-Type: application/json" \
  -d '{"auth_session_id":"<auth_session_id>","poll_token":"<poll_token>"}'
```

Cowork stores the token in-memory and uses it automatically for outgoing calls to xo-swarm-api.
If request body values are omitted, cowork falls back to `XO_AUTH_SESSION_ID` and
`XO_POLL_TOKEN` from environment variables.
If both env vars are set at app startup, cowork also attempts one automatic consume on boot
(failures are logged as warnings; startup continues).

#### 5) Verify token/user mapping

```bash
curl http://localhost:5002/xo-auth/whoami
```

### Direct XO Swarm API auth flow (without cowork wrapper)

If you want to authenticate directly against `xo-swarm-api` (for debugging or integration docs), use these steps:

#### Step 1) Start browser auth

```bash
curl -X POST http://localhost:5001/auth/browser/start \
  -H "Content-Type: application/json" \
  -d '{"client_reference":"direct-test"}'
```

Copy from response:
- `authorize_url`
- `auth_session_id`
- `poll_token`

#### Step 2) Open `authorize_url` in browser

User logs in/consents in Clerk.

#### Step 3) Poll status

```bash
curl "http://localhost:5001/auth/browser/status/<auth_session_id>?poll_token=<poll_token>"
```

Wait until response has `status: "authorized"`.

#### Step 4) Consume token

```bash
curl -X POST http://localhost:5001/auth/browser/consume \
  -H "Content-Type: application/json" \
  -d '{"auth_session_id":"<auth_session_id>","poll_token":"<poll_token>"}'
```

Copy `access_token` from response.

#### Step 5) Validate token and get user id

```bash
curl http://localhost:5001/get-user-id \
  -H "Authorization: Bearer <access_token>"
```

---

### Ask Question (Non-Streaming)

```bash
POST /ask_question
Content-Type: application/json

{
  "project_name": "my-project",
  "question": "What is 2+2?",
  "user_id": "user_123",
  "message_type": "@xo"
}
```

**Response:**

```json
{
  "id": null,
  "message": "2+2 equals 4.",
  "project_id": "my-project",
  "user_id": "user_123",
  "session_id": "uuid-here",
  "is_new_session": true,
  "timestamp": "2024-01-01T12:00:00.000000"
}
```

### Ask Question (Streaming)

```bash
POST /ask_question_streaming
Content-Type: application/json

{
  "project_name": "my-project",
  "question": "Explain quantum computing",
  "user_id": "user_123",
  "message_type": "@xo"
}
```

**Response:** Server-Sent Events (SSE) stream

```
data: {"type": "token", "token": "Quantum "}
data: {"type": "token", "token": "computing "}
data: {"type": "token", "token": "is..."}
data: {"done": true}
```

## Session Management

- **First request** for a project creates a new Claude Code session
- **Subsequent requests** resume the existing session, maintaining conversation context
- Sessions are stored in-memory (consider Redis for production)
- Use `DELETE /sessions/{project_id}` to start fresh

## Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `HOST` | Server host | `0.0.0.0` |
| `PORT` | Server port | `5002` |
| `CLAUDE_CLI_PATH` | Path to Claude CLI | `claude` |
| `CLAUDE_TIMEOUT` | CLI timeout (seconds) | `300` |
| `CHAT_API_BASE_URL` | External Chat API URL | `http://localhost:5001` |
| `CHAT_API_TOKEN` | Optional static Bearer token fallback | unset |
| `XO_AUTH_START_PATH` | XO backend start auth path | `/auth/browser/start` |
| `XO_AUTH_STATUS_PATH` | XO backend status path | `/auth/browser/status` |
| `XO_AUTH_CONSUME_PATH` | XO backend consume path | `/auth/browser/consume` |
| `XO_GET_USER_ID_PATH` | XO backend user-id path | `/get-user-id` |
| `XO_AUTH_SESSION_ID` | Optional fallback for `/xo-auth/consume`; with `XO_POLL_TOKEN` enables one startup auto-consume attempt | unset |
| `XO_POLL_TOKEN` | Optional fallback for `/xo-auth/consume`; with `XO_AUTH_SESSION_ID` enables one startup auto-consume attempt | unset |

## Testing

```bash
# Non-streaming
curl -X POST http://localhost:5002/ask_question \
  -H "Content-Type: application/json" \
  -d '{"project_name": "test", "question": "Hello!"}'

# Streaming
curl -X POST http://localhost:5002/ask_question_streaming \
  -H "Content-Type: application/json" \
  -d '{"project_name": "test", "question": "Hello!"}'

# Check sessions
curl http://localhost:5002/sessions

# Clear session
curl -X DELETE http://localhost:5002/sessions/test
```

## Project Structure

```
xo-cowork-api/
├── server.py           # Main FastAPI application
├── requirements.txt    # Python dependencies
├── .env.example        # Environment template
├── .env                # Your configuration (not committed)
├── .gitignore          # Git ignore rules
└── README.md           # This file
```

## License

MIT
