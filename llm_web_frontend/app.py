#!/usr/bin/env python3
import base64
import json
import mimetypes
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib import error, parse, request


APP_DIR = Path(__file__).resolve().parent
REPO_ROOT = APP_DIR.parent
INDEX_FILE = APP_DIR / "index.html"
STATE_FILE = APP_DIR / "frontend_state.json"


def load_env_file(path):
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'").strip('"')
        if key and key not in os.environ:
            os.environ[key] = value


load_env_file(REPO_ROOT / ".env")
load_env_file(APP_DIR / ".env")

BACKEND_REPO = Path(os.getenv("LLM_WEB_API_REPO", str(REPO_ROOT))).expanduser()
BACKEND_DATA_DIR = Path(os.getenv("LLM_WEB_API_DATA_DIR", str(BACKEND_REPO / "data"))).expanduser()
BACKEND_BASE_URL = "http://127.0.0.1:5000/v1"
IMAGE_NAME = "llm-web-api-fixed"
CONTAINER_NAME = "llm-web-api"
DEFAULT_PROXY = "http://host.docker.internal:7890"
DEFAULT_TOKEN = "anything"
DEFAULT_MODEL = "gpt-5-3"
DEFAULT_IMAGE_MODEL = "gpt-image-1"
DEFAULT_TOS_EXPIRES = 3600
APP_HOST = "127.0.0.1"
APP_PORT = 7860


def read_state():
    env_defaults = {
        "email": os.getenv("OPENAI_LOGIN_EMAIL", ""),
        "password": os.getenv("OPENAI_LOGIN_PASSWORD", ""),
        "otp_secret": os.getenv("OPENAI_LOGIN_OTP_SECRET", ""),
        "proxy_server": os.getenv("PROXY_SERVER", DEFAULT_PROXY) or DEFAULT_PROXY,
        "token": os.getenv("OPENAI_API_TOKEN", DEFAULT_TOKEN) or DEFAULT_TOKEN,
        "selected_model": os.getenv("OPENAI_CHAT_MODEL", DEFAULT_MODEL) or DEFAULT_MODEL,
    }
    if not STATE_FILE.exists():
        return env_defaults

    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        merged = {**env_defaults, **state}
        for key, value in env_defaults.items():
            if value and not str(merged.get(key, "")).strip():
                merged[key] = value
        return merged
    except Exception:
        return env_defaults


def write_state(state):
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def run_command(args, cwd=None, check=True):
    result = subprocess.run(
        args,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
    )
    if check and result.returncode != 0:
        message = result.stderr.strip() or result.stdout.strip() or "command failed"
        raise RuntimeError(message)
    return result


def docker_image_exists():
    result = run_command(["docker", "image", "inspect", IMAGE_NAME], check=False)
    return result.returncode == 0


def ensure_backend_image():
    if os.getenv("LLM_WEB_API_SKIP_BUILD", "").strip().lower() in {"1", "true", "yes"} and docker_image_exists():
        return
    run_command(["docker", "build", "-t", IMAGE_NAME, "."], cwd=BACKEND_REPO)


def remove_existing_container():
    run_command(["docker", "rm", "-f", CONTAINER_NAME], check=False)


def normalize_config(payload):
    state = read_state()
    normalized = {
        "email": str(payload.get("email", state.get("email", ""))).strip(),
        "password": str(payload.get("password", state.get("password", ""))),
        "otp_secret": str(payload.get("otp_secret", state.get("otp_secret", ""))).strip(),
        "proxy_server": str(payload.get("proxy_server", state.get("proxy_server", DEFAULT_PROXY))).strip() or DEFAULT_PROXY,
        "token": str(payload.get("token", state.get("token", DEFAULT_TOKEN))).strip() or DEFAULT_TOKEN,
        "selected_model": str(payload.get("selected_model", state.get("selected_model", DEFAULT_MODEL))).strip() or DEFAULT_MODEL,
    }
    return normalized


def start_backend(config):
    if not BACKEND_REPO.exists():
        raise RuntimeError(f"后端目录不存在：{BACKEND_REPO}")

    BACKEND_DATA_DIR.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(BACKEND_DATA_DIR, 0o777)
    except PermissionError:
        pass

    ensure_backend_image()
    remove_existing_container()

    command = [
        "docker", "run",
        "--name", CONTAINER_NAME,
        "--rm",
        "-d",
        "-p", "5000:5000",
        "--add-host=host.docker.internal:host-gateway",
        "-v", f"{BACKEND_DATA_DIR}:/app/data",
        "-e", f"PROXY_SERVER={config['proxy_server']}",
        "-e", "OPENAI_LOGIN_TYPE=email",
        "-e", f"OPENAI_LOGIN_EMAIL={config['email']}",
        "-e", f"OPENAI_LOGIN_PASSWORD={config['password']}",
    ]

    if config["otp_secret"]:
        command.extend(["-e", f"OPENAI_LOGIN_OTP_SECRET={config['otp_secret']}"])

    command.append(IMAGE_NAME)
    result = run_command(command)
    return result.stdout.strip()


def backend_request(method, path, body=None, token=None, timeout=30):
    headers = {
        "Authorization": f"Bearer {token or DEFAULT_TOKEN}",
    }
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")

    req = request.Request(
        url=f"{BACKEND_BASE_URL}{path}",
        data=data,
        headers=headers,
        method=method,
    )

    try:
        with request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return resp.status, raw
    except error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        return exc.code, raw
    except error.URLError as exc:
        raise RuntimeError(f"无法连接后端：{exc.reason}") from exc


def _require_tos_config():
    config = {
        "access_key": os.getenv("TOS_ACCESS_KEY", "").strip(),
        "secret_key": os.getenv("TOS_SECRET_KEY", "").strip(),
        "endpoint": os.getenv("TOS_ENDPOINT", "").strip() or "tos-cn-beijing.volces.com",
        "region": os.getenv("TOS_REGION", "").strip() or "cn-beijing",
        "bucket": os.getenv("TOS_BUCKET", "").strip(),
    }
    missing = [
        name
        for name, value in (
            ("TOS_ACCESS_KEY", config["access_key"]),
            ("TOS_SECRET_KEY", config["secret_key"]),
            ("TOS_BUCKET", config["bucket"]),
        )
        if not value
    ]
    if missing:
        raise RuntimeError("缺少 TOS 环境变量：" + ", ".join(missing))
    return config


def _make_tos_client(config):
    try:
        import tos
    except ImportError as exc:
        raise RuntimeError("当前 Python 环境缺少 tos 包，无法上传火山 TOS。") from exc

    return tos.TosClientV2(
        config["access_key"],
        config["secret_key"],
        config["endpoint"],
        config["region"],
    )


def _extract_signed_url(pre):
    if isinstance(pre, str):
        return pre
    for attr in ("signed_url", "url"):
        value = getattr(pre, attr, None)
        if value:
            return value
    raise RuntimeError(f"无法解析预签名 URL: {pre}")


def _guess_extension(content_type, fallback=".png"):
    ext = mimetypes.guess_extension((content_type or "").split(";", 1)[0].strip())
    if ext == ".jpe":
        return ".jpg"
    return ext or fallback


def _read_image_source(source, timeout=120):
    if isinstance(source, dict):
        source = (
            source.get("data_url")
            or source.get("b64_json")
            or source.get("url")
            or source.get("origin_url")
            or ""
        )
    source = str(source or "").strip()
    if not source:
        raise RuntimeError("图片来源为空。")

    if source.startswith("data:"):
        header, encoded = source.split(",", 1)
        content_type = header[5:].split(";", 1)[0] or "application/octet-stream"
        return base64.b64decode(encoded), content_type

    parsed = parse.urlparse(source)
    if parsed.scheme not in {"http", "https"}:
        path = Path(source)
        if not path.exists():
            raise RuntimeError(f"不支持的图片来源：{source[:120]}")
        content_type = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        return path.read_bytes(), content_type

    req = request.Request(source, headers={"User-Agent": "llm-local-image-proxy/1.0"})
    with request.urlopen(req, timeout=timeout) as resp:
        return resp.read(), resp.headers.get("Content-Type") or "application/octet-stream"


def _upload_image_bytes_to_tos(raw, content_type, prefix="generated-images", expires=DEFAULT_TOS_EXPIRES):
    config = _require_tos_config()
    client = _make_tos_client(config)

    try:
        from tos import HttpMethodType
    except ImportError as exc:
        raise RuntimeError("当前 Python 环境缺少 tos.HttpMethodType。") from exc

    ext = _guess_extension(content_type)
    object_key = f"{prefix.rstrip('/')}/{time.strftime('%Y/%m/%d')}/{uuid.uuid4().hex}{ext}"
    with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
        tmp.write(raw)
        tmp_path = tmp.name

    try:
        client.upload_file(config["bucket"], object_key, tmp_path)
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

    pre = client.pre_signed_url(
        http_method=HttpMethodType.Http_Method_Get,
        bucket=config["bucket"],
        key=object_key,
        expires=int(expires or DEFAULT_TOS_EXPIRES),
    )
    return {
        "url": _extract_signed_url(pre),
        "object_key": object_key,
        "content_type": content_type,
        "bytes": len(raw),
    }


def _extract_image_sources(data):
    sources = []

    def add(value):
        if not value:
            return
        if isinstance(value, str):
            sources.append(value)
            return
        if isinstance(value, dict):
            for key in ("data_url", "b64_json", "url", "origin_url"):
                if value.get(key):
                    sources.append(value)
                    return

    for item in data.get("images", []) if isinstance(data, dict) else []:
        add(item)

    meta = data.get("meta", {}) if isinstance(data, dict) else {}
    if isinstance(meta, dict):
        for item in meta.get("images", []):
            add(item)

    choices = data.get("choices", []) if isinstance(data, dict) else []
    for choice in choices:
        message = choice.get("message", {}) if isinstance(choice, dict) else {}
        for item in message.get("images", []) if isinstance(message, dict) else []:
            add(item)

        content = message.get("content", "") if isinstance(message, dict) else ""
        if isinstance(content, list):
            for part in content:
                if not isinstance(part, dict):
                    continue
                image_url = part.get("image_url") or part.get("url")
                if isinstance(image_url, dict):
                    image_url = image_url.get("url")
                add(image_url)
        elif isinstance(content, str):
            for token in content.replace("\n", " ").split():
                cleaned = token.strip("()[]<>\"'")
                if cleaned.startswith(("http://", "https://", "data:image/")):
                    add(cleaned)

    deduped = []
    seen = set()
    for source in sources:
        marker = json.dumps(source, sort_keys=True) if isinstance(source, dict) else str(source)
        if marker in seen:
            continue
        seen.add(marker)
        deduped.append(source)
    return deduped


def _rank_image_source(source):
    if isinstance(source, dict):
        url = str(source.get("url") or source.get("origin_url") or "")
        width = int(source.get("width") or 0)
        height = int(source.get("height") or 0)
        source_type = str(source.get("source") or "")
    else:
        url = str(source or "")
        width = 0
        height = 0
        source_type = ""

    area = width * height
    score = area
    if "/backend-api/estuary/content" in url:
        score += 10_000_000
    if "public_content" in url or "thumbnail" in url:
        score -= 10_000_000
    if source_type == "background":
        score -= 1_000_000
    if width and height and min(width, height) < 256:
        score -= 5_000_000
    return score


def _filter_result_image_sources(sources):
    filtered = []
    for source in sources:
        if isinstance(source, dict):
            url = str(source.get("url") or source.get("origin_url") or "")
            width = int(source.get("width") or 0)
            height = int(source.get("height") or 0)
            if ("public_content" in url or "thumbnail" in url) and "/backend-api/estuary/content" not in url:
                continue
            if width and height and min(width, height) < 256:
                continue
        filtered.append(source)

    candidates = filtered or sources
    return sorted(candidates, key=_rank_image_source, reverse=True)


def _image_source_marker(source):
    if isinstance(source, dict):
        for key in ("url", "origin_url", "data_url", "b64_json"):
            value = source.get(key)
            if value:
                return f"{key}:{value}"
        return json.dumps(source, sort_keys=True)
    return str(source)


def _current_image_markers(token):
    try:
        status, raw = backend_request(
            "GET",
            "/images/current",
            token=token,
            timeout=30,
        )
        if status >= 400:
            return set()
        data = json.loads(raw or "{}")
        return {_image_source_marker(source) for source in _extract_image_sources(data)}
    except Exception:
        return set()


def _exclude_known_sources(sources, known_markers):
    if not known_markers:
        return sources
    return [
        source
        for source in sources
        if _image_source_marker(source) not in known_markers
    ]


def generate_image_and_upload(payload, token):
    prompt = str(payload.get("prompt") or payload.get("text") or "").strip()
    if not prompt:
        raise RuntimeError("prompt 不能为空。")
    _require_tos_config()

    model = str(payload.get("model") or DEFAULT_IMAGE_MODEL).strip() or DEFAULT_IMAGE_MODEL
    chat_model = str(payload.get("chat_model") or read_state().get("selected_model", DEFAULT_MODEL)).strip() or DEFAULT_MODEL
    size = str(payload.get("size") or payload.get("image_size") or "").strip()
    n = max(1, min(int(payload.get("n") or payload.get("max_images") or 1), 4))
    output_format = str(payload.get("response_format") or payload.get("output_format") or "png").strip().lower()
    prompt_parts = [f"Generate {n} image(s) from this prompt:", prompt]
    if size:
        prompt_parts.append(f"Image size: {size}.")
    if output_format:
        prompt_parts.append(f"Output format: {output_format}.")
    prompt_parts.append("Return the generated image in the conversation, not a description.")

    reference_images = []
    for key in ("image", "image_url", "input_image"):
        if payload.get(key):
            reference_images.append(payload[key])
    for value in payload.get("image_urls", []) or payload.get("images", []) or []:
        reference_images.append(value)

    message_content = "\n".join(prompt_parts)
    if reference_images:
        content_parts = [{"type": "text", "text": message_content}]
        for image_url in reference_images:
            if isinstance(image_url, dict):
                image_url = image_url.get("url") or image_url.get("image_url")
            content_parts.append({"type": "image_url", "image_url": {"url": str(image_url)}})
        message_content = content_parts

    chat_body = {
        "model": chat_model,
        "messages": [{"role": "user", "content": message_content}],
        "chat_mode": payload.get("chat_mode", "new"),
        "image_generation": True,
        "response_timeout_ms": int(payload.get("response_timeout_ms") or 600000),
    }
    if payload.get("chat_name"):
        chat_body["chat_name"] = payload["chat_name"]
        chat_body["create_if_missing"] = payload.get("create_if_missing", True)

    known_image_markers = _current_image_markers(token)
    result_holder = {"done": False, "error": None, "data": None}

    def submit_generation():
        try:
            status, raw = backend_request(
                "POST",
                "/chat/completions",
                body=chat_body,
                token=token,
                timeout=900,
            )
            try:
                result_holder["data"] = json.loads(raw or "{}")
            except json.JSONDecodeError as exc:
                raise RuntimeError("图片生成后端返回的不是合法 JSON。") from exc

            if status >= 400:
                data = result_holder["data"] if isinstance(result_holder["data"], dict) else {}
                message = data.get("error", {}).get("message") or f"图片生成请求失败：{status}"
                raise RuntimeError(message)
        except Exception as exc:
            result_holder["error"] = str(exc)
        finally:
            result_holder["done"] = True

    thread = threading.Thread(target=submit_generation, daemon=True)
    thread.start()

    sources = []
    deadline = time.time() + (int(payload.get("poll_timeout") or 600))
    last_error = ""
    while time.time() < deadline:
        chat_data = result_holder["data"] if isinstance(result_holder.get("data"), dict) else {}
        sources = _exclude_known_sources(_extract_image_sources(chat_data), known_image_markers)
        if sources:
            break

        try:
            current_status, current_raw = backend_request(
                "GET",
                "/images/current",
                token=token,
                timeout=30,
            )
            current_data = json.loads(current_raw or "{}")
            if current_status < 400:
                sources = _exclude_known_sources(
                    _extract_image_sources(current_data),
                    known_image_markers,
                )
                if sources:
                    break
            else:
                last_error = current_data.get("error") or f"当前页面图片接口请求失败：{current_status}"
        except Exception as exc:
            last_error = str(exc)

        if result_holder["done"] and result_holder["error"]:
            last_error = str(result_holder["error"])
        time.sleep(3)

    if not sources:
        raw_result = result_holder["data"] if result_holder.get("data") is not None else {}
        detail = json.dumps(raw_result, ensure_ascii=False)[:800] if raw_result else last_error
        raise RuntimeError("后端没有返回可代理的图片。" + (f"详情：{detail}" if detail else ""))
    sources = _filter_result_image_sources(sources)

    expires = int(payload.get("expires") or DEFAULT_TOS_EXPIRES)
    prefix = str(payload.get("tos_prefix") or "generated-images").strip() or "generated-images"
    uploaded = []
    for source in sources[:n]:
        raw_image, content_type = _read_image_source(source)
        item = _upload_image_bytes_to_tos(raw_image, content_type, prefix=prefix, expires=expires)
        item["origin_url"] = source.get("url") if isinstance(source, dict) else source
        uploaded.append(item)

    return {
        "created": int(time.time()),
        "model": model,
        "data": [
            {
                "url": item["url"],
                "object_key": item["object_key"],
                "content_type": item["content_type"],
                "bytes": item["bytes"],
            }
            for item in uploaded
        ],
        "usage": {"image_count": len(uploaded)},
    }


def proxy_images_to_tos(payload):
    sources = _extract_image_sources(payload)
    for key in ("url", "image_url", "source_url", "data_url", "b64_json"):
        if payload.get(key):
            sources.append({key: payload[key]})

    if not sources:
        raise RuntimeError("缺少 url / image_url / source_url / data_url / b64_json。")

    expires = int(payload.get("expires") or DEFAULT_TOS_EXPIRES)
    prefix = str(payload.get("tos_prefix") or "proxied-images").strip() or "proxied-images"
    uploaded = []
    seen = set()
    for source in sources:
        marker = json.dumps(source, sort_keys=True) if isinstance(source, dict) else str(source)
        if marker in seen:
            continue
        seen.add(marker)
        raw_image, content_type = _read_image_source(source)
        item = _upload_image_bytes_to_tos(raw_image, content_type, prefix=prefix, expires=expires)
        item["origin_url"] = source.get("url") if isinstance(source, dict) else source
        uploaded.append(item)

    return {
        "created": int(time.time()),
        "data": [
            {
                "url": item["url"],
                "object_key": item["object_key"],
                "content_type": item["content_type"],
                "bytes": item["bytes"],
            }
            for item in uploaded
        ],
        "usage": {"image_count": len(uploaded)},
    }


def upload_current_page_images(token, payload=None):
    payload = payload or {}
    status, raw = backend_request(
        "GET",
        "/images/current",
        token=token,
        timeout=60,
    )
    try:
        data = json.loads(raw or "{}")
    except json.JSONDecodeError as exc:
        raise RuntimeError("当前页面图片接口返回的不是合法 JSON。") from exc

    if status >= 400:
        raise RuntimeError(data.get("error") or f"当前页面图片接口请求失败：{status}")

    images = data.get("images") or []
    if not images:
        raise RuntimeError(data.get("error") or "当前 ChatGPT 页面没有提取到图片。")
    images = _filter_result_image_sources(images)

    proxy_payload = {
        "images": images,
        "tos_prefix": payload.get("tos_prefix", "current-page-images"),
        "expires": payload.get("expires", DEFAULT_TOS_EXPIRES),
    }
    result = proxy_images_to_tos(proxy_payload)
    result["source_url"] = data.get("url", "")
    return result


def fetch_models(token, timeout=30):
    status, raw = backend_request("GET", "/models", token=token, timeout=timeout)
    try:
        data = json.loads(raw or "{}")
    except json.JSONDecodeError as exc:
        raise RuntimeError("模型接口返回的不是合法 JSON。") from exc

    if status >= 400:
        raise RuntimeError(data.get("error", {}).get("message") or f"模型接口请求失败：{status}")

    models = []
    for item in data.get("data", []):
        model_id = item.get("id") or item.get("name")
        if model_id:
            models.append(model_id)
    return models


def wait_for_backend(token):
    last_error = "后端还没准备好。"
    start = time.time()
    while time.time() - start < 180:
        try:
            models = fetch_models(token, timeout=20)
            if models:
                return models
            last_error = "后端已启动，但模型列表还没准备好。"
        except Exception as exc:
            last_error = str(exc)
        time.sleep(2)
    raise RuntimeError(last_error)


def best_effort_open_browser():
    url = f"http://{APP_HOST}:{APP_PORT}/"
    try:
        if "WSL_DISTRO_NAME" in os.environ and shutil.which("cmd.exe"):
            subprocess.Popen(["cmd.exe", "/C", "start", "", url])
            return
        if shutil.which("xdg-open"):
            subprocess.Popen(["xdg-open", url])
            return
        if sys.platform == "darwin" and shutil.which("open"):
            subprocess.Popen(["open", url])
    except Exception:
        pass


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        return

    def _send_json(self, payload, status=200):
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _send_html(self, content):
        raw = content.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _send_bytes(self, raw, content_type="application/octet-stream", status=200):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _read_json(self):
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length).decode("utf-8", errors="replace") if length else "{}"
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            raise RuntimeError("请求体不是合法 JSON。")

    def do_GET(self):
        parsed_path = parse.urlparse(self.path)
        route = parsed_path.path

        if route in ("/", "/index.html"):
            self._send_html(INDEX_FILE.read_text(encoding="utf-8"))
            return

        if route == "/api/state":
            state = read_state()
            models = []
            backend_ready = False
            try:
                models = fetch_models(state.get("token", DEFAULT_TOKEN), timeout=10)
                backend_ready = True
            except Exception:
                backend_ready = False

            self._send_json({
                "config": state,
                "models": models,
                "backend_ready": backend_ready,
            })
            return

        if route == "/api/models":
            state = read_state()
            try:
                models = fetch_models(state.get("token", DEFAULT_TOKEN), timeout=20)
                self._send_json({"models": models})
            except Exception as exc:
                self._send_json({"error": str(exc)}, status=502)
            return

        if route == "/api/image-proxy":
            params = parse.parse_qs(parsed_path.query)
            image_url = (params.get("url") or [""])[0]
            try:
                raw, content_type = _read_image_source(image_url)
                self._send_bytes(raw, content_type=content_type)
            except Exception as exc:
                self._send_json({"error": str(exc)}, status=502)
            return

        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self):
        route = parse.urlparse(self.path).path
        try:
            payload = self._read_json()
        except RuntimeError as exc:
            self._send_json({"error": str(exc)}, status=400)
            return

        if route == "/api/save-config":
            state = normalize_config(payload)
            write_state(state)
            self._send_json({"ok": True, "config": state})
            return

        if route == "/api/start":
            try:
                state = normalize_config(payload)
                if not state["email"] or not state["password"]:
                    raise RuntimeError("邮箱和密码不能为空。")
                write_state(state)
                container_id = start_backend(state)
                models = wait_for_backend(state["token"])
                if state["selected_model"] not in models:
                    state["selected_model"] = models[0]
                    write_state(state)
                self._send_json({
                    "ok": True,
                    "container_id": container_id,
                    "models": models,
                    "config": state,
                })
            except Exception as exc:
                self._send_json({"error": str(exc)}, status=500)
            return

        if route == "/api/chat":
            state = read_state()
            try:
                status, raw = backend_request(
                    "POST",
                    "/chat/completions",
                    body=payload,
                    token=state.get("token", DEFAULT_TOKEN),
                    timeout=600,
                )
                try:
                    data = json.loads(raw or "{}")
                except json.JSONDecodeError as exc:
                    raise RuntimeError("聊天接口返回的不是合法 JSON。") from exc

                if status >= 400:
                    message = data.get("error", {}).get("message") or f"聊天接口请求失败：{status}"
                    raise RuntimeError(message)

                self._send_json(data)
            except Exception as exc:
                self._send_json({"error": str(exc)}, status=502)
            return

        if route in ("/api/images/generations", "/v1/images/generations"):
            state = read_state()
            try:
                data = generate_image_and_upload(
                    payload,
                    token=state.get("token", DEFAULT_TOKEN),
                )
                self._send_json(data)
            except Exception as exc:
                self._send_json({"error": str(exc)}, status=502)
            return

        if route in ("/api/images/proxy", "/v1/images/proxy"):
            try:
                self._send_json(proxy_images_to_tos(payload))
            except Exception as exc:
                self._send_json({"error": str(exc)}, status=502)
            return

        if route in ("/api/images/current", "/v1/images/current"):
            state = read_state()
            try:
                self._send_json(upload_current_page_images(state.get("token", DEFAULT_TOKEN), payload))
            except Exception as exc:
                self._send_json({"error": str(exc)}, status=502)
            return

        self.send_error(HTTPStatus.NOT_FOUND)


def main():
    server = ThreadingHTTPServer((APP_HOST, APP_PORT), Handler)
    print(f"LLM Web Chat is running at http://{APP_HOST}:{APP_PORT}/")
    best_effort_open_browser()
    server.serve_forever()


if __name__ == "__main__":
    main()
