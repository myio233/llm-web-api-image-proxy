# LLM Web API Image Proxy

LLM Web API Image Proxy turns a logged-in ChatGPT web session into a local
OpenAI-compatible HTTP API. It builds on the browser automation approach used by
[`adryfish/llm-web-api`](https://github.com/adryfish/llm-web-api), then adds a
Docker patch layer for chat completions, image generation, response capture, and
Markdown/LaTeX preservation.

The project is useful when a local tool expects OpenAI-style endpoints such as
`/v1/chat/completions` or `/v1/images/generations`, but the actual backend is a
ChatGPT web session controlled through a browser.

## Features

- OpenAI-compatible `GET /v1/models`.
- OpenAI-compatible `POST /v1/chat/completions`.
- OpenAI-compatible `POST /v1/images/generations`.
- ChatGPT page `fetch` capture hook for structured backend messages.
- Better Markdown and LaTeX preservation.
- Generated image extraction from ChatGPT web responses.
- Optional image upload/proxy helper frontend.
- Docker-first deployment with local runtime patches.

## Repository Layout

```text
.
├── Dockerfile
├── docker/
│   └── patches/
│       ├── run.py
│       └── sitecustomize.py
├── docker-compose/
├── llm_web_frontend/
├── .env.example
└── README.md
```

`docker/patches/sitecustomize.py` is the main patch file. It monkey-patches the
upstream runtime at import time and implements most compatibility behavior.

## Requirements

- Docker
- A ChatGPT account that can be logged in through the browser flow
- Local API token configured in `.env`
- Optional TOS-compatible object storage if you want hosted image URLs

## Configure

Create a local `.env` from the example:

```bash
cp .env.example .env
```

Then edit `.env`:

```text
OPENAI_LOGIN_EMAIL=you@example.com
OPENAI_LOGIN_PASSWORD=replace_with_password
OPENAI_LOGIN_OTP_SECRET=
OPENAI_API_TOKEN=replace_with_local_api_token
OPENAI_CHAT_MODEL=gpt-5-3
```

If you do not need a proxy, keep:

```text
PROXY_SERVER=
```

Do not commit `.env`. It contains account credentials, OTP secrets, and local
API tokens.

## Build

```bash
docker build -t llm-web-api-fixed .
```

## Run

```bash
docker rm -f llm-web-api 2>/dev/null || true
docker run -d \
  --name llm-web-api \
  --env-file .env \
  -e PROXY_SERVER= \
  -p 5000:5000 \
  llm-web-api-fixed
```

Check logs:

```bash
docker logs --tail 160 llm-web-api
```

## API Examples

List models:

```bash
curl -sS http://127.0.0.1:5000/v1/models | python3 -m json.tool
```

Chat completion:

```bash
set -a
. ./.env
set +a

curl -sS http://127.0.0.1:5000/v1/chat/completions \
  -H "Authorization: Bearer ${OPENAI_API_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gpt-5-3",
    "messages": [
      {"role": "user", "content": "用 Markdown 和 LaTeX 简要解释逆矩阵。"}
    ],
    "chat_mode": "new",
    "meta": {"enable": true},
    "response_timeout_ms": 120000
  }' | python3 -m json.tool
```

Image generation:

```bash
curl -sS http://127.0.0.1:5000/v1/images/generations \
  -H "Authorization: Bearer ${OPENAI_API_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gpt-5-3",
    "prompt": "A clean product mockup of a small AI dashboard",
    "n": 1,
    "response_format": "b64_json"
  }' | python3 -m json.tool
```

## Development Notes

The patch layer is intentionally concentrated in `docker/patches/` so upstream
image updates are easier to compare. When changing behavior, rebuild the Docker
image and test `/v1/models`, `/v1/chat/completions`, and `/v1/images/generations`
before publishing.
