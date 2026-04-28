import logging
import os
import sys
import builtins
import asyncio
import base64
import inspect
import json
import mimetypes
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

from bs4 import BeautifulSoup
import pyotp


logger = logging.getLogger("llm_web_api_patch")
_CHAT_SESSION_STORE_PATH = Path("/app/data/chat_sessions.json")


class _LoginButtonProxy:
    def __init__(self, container):
        self._container = container
        self._modal = container.locator('[data-testid="modal-no-auth-login"]')
        self._homepage_button = container.locator('[data-testid="login-button"]')
        self._email_input = self._modal.locator('input[type="email"][name="email"]')
        self._continue_button = self._modal.locator('button[type="submit"]')

    async def click(self, *args, **kwargs):
        try:
            if await self._modal.count():
                login_type = os.getenv("OPENAI_LOGIN_TYPE", "").strip().lower()
                login_email = os.getenv("OPENAI_LOGIN_EMAIL", "").strip()

                if login_type == "email" and login_email and await self._email_input.count():
                    forced_kwargs = dict(kwargs)
                    forced_kwargs["force"] = True
                    try:
                        current_value = await self._email_input.input_value()
                    except Exception:
                        current_value = ""
                    try:
                        is_disabled = await self._email_input.is_disabled()
                    except Exception:
                        is_disabled = False

                    if not is_disabled and current_value != login_email:
                        await self._email_input.fill(login_email)
                        logger.info("Filled login modal email and submitting continue button.")
                    else:
                        logger.info(
                            "Login modal email input already populated; submitting continue button."
                        )
                    return await self._continue_button.click(*args, **forced_kwargs)

                # Once the login modal is open, the original page CTA is covered by an
                # overlay. Force the click so the upstream handler can complete instead
                # of timing out on intercepted pointer events.
                forced_kwargs = dict(kwargs)
                forced_kwargs["force"] = True
                logger.info("Login modal is open; force-clicking homepage login button.")
                return await self._homepage_button.click(*args, **forced_kwargs)
        except Exception as exc:
            if "Execution context was destroyed" in str(exc):
                # Navigation raced with our modal probe; let the upstream retry loop
                # continue instead of treating it as a hard selector failure.
                logger.info("Login modal probe raced with navigation; retrying homepage click.")
                forced_kwargs = dict(kwargs)
                forced_kwargs["force"] = True
                return await self._homepage_button.click(*args, **forced_kwargs)
            logger.warning("Login selector patch fallback triggered: %s", exc)
        return await self._homepage_button.click(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._homepage_button, name)


def _patch_login_button_selector() -> None:
    try:
        from playwright.async_api import Frame, Page
    except Exception as exc:
        logger.warning("Playwright patch skipped: %s", exc)
        return

    def wrap_get_by_role(original):
        def patched(self, role, *args, **kwargs):
            if role == "button" and kwargs.get("name") == "Log in":
                # ChatGPT home now opens a login modal on top of the page CTA.
                # Prefer the modal button when present, otherwise click the homepage CTA.
                return _LoginButtonProxy(self)
            if (
                role == "button"
                and kwargs.get("name") == "New chat"
                and kwargs.get("exact") is True
            ):
                return self.locator('[data-testid="create-new-chat-button"]')
            return original(self, role, *args, **kwargs)

        return patched

    def wrap_locator(original):
        def patched(self, selector, *args, **kwargs):
            if selector == "#password":
                return original(
                    self,
                    'input[type="password"][name="current-password"]',
                    *args,
                    **kwargs,
                )
            if selector == '[data-testid="create-new-chat-button"]':
                return original(self, selector, *args, **kwargs).first
            return original(self, selector, *args, **kwargs)

        return patched

    Page.get_by_role = wrap_get_by_role(Page.get_by_role)
    Frame.get_by_role = wrap_get_by_role(Frame.get_by_role)
    Page.locator = wrap_locator(Page.locator)
    Frame.locator = wrap_locator(Frame.locator)
    logger.info("Applied Playwright login selector patch for ChatGPT home page.")


def _patch_connect_over_cdp() -> None:
    try:
        from playwright.async_api import BrowserType
    except Exception as exc:
        logger.warning("Playwright CDP patch skipped: %s", exc)
        return

    if getattr(BrowserType, "_codex_connect_over_cdp_patch", False):
        return

    original_connect_over_cdp = BrowserType.connect_over_cdp

    async def patched_connect_over_cdp(self, endpoint_url, *args, **kwargs):
        last_error = None
        for attempt in range(30):
            try:
                return await original_connect_over_cdp(self, endpoint_url, *args, **kwargs)
            except Exception as exc:
                last_error = exc
                message = str(exc)
                if "ECONNREFUSED" not in message:
                    raise
                logger.warning(
                    "connect_over_cdp refused for %s on attempt %s/30; retrying.",
                    endpoint_url,
                    attempt + 1,
                )
                await asyncio.sleep(2)
        raise last_error

    BrowserType.connect_over_cdp = patched_connect_over_cdp
    BrowserType._codex_connect_over_cdp_patch = True
    logger.info("Applied Playwright connect_over_cdp retry patch.")


def _has_openai_login_credentials() -> bool:
    login_type = os.getenv("OPENAI_LOGIN_TYPE", "").strip().lower()
    login_email = os.getenv("OPENAI_LOGIN_EMAIL", "").strip()
    login_password = os.getenv("OPENAI_LOGIN_PASSWORD", "").strip()
    return login_type == "email" and bool(login_email) and bool(login_password)


def _cleanup_stale_browser_profile_locks() -> None:
    browser_root = Path("/app/data/browser")
    if not browser_root.exists():
        return

    removed_paths: list[str] = []
    lock_names = {
        "SingletonLock",
        "SingletonCookie",
        "SingletonSocket",
        "DevToolsActivePort",
    }

    for profile_dir in browser_root.iterdir():
        if not profile_dir.is_dir():
            continue

        for lock_name in lock_names:
            lock_path = profile_dir / lock_name
            if not lock_path.exists() and not lock_path.is_symlink():
                continue
            try:
                lock_path.unlink()
                removed_paths.append(str(lock_path))
            except FileNotFoundError:
                pass
            except Exception as exc:
                logger.warning("Failed to remove stale Chromium lock %s: %s", lock_path, exc)

        default_lock = profile_dir / "Default" / "LOCK"
        if default_lock.exists():
            try:
                default_lock.unlink()
                removed_paths.append(str(default_lock))
            except Exception as exc:
                logger.warning("Failed to remove profile DB lock %s: %s", default_lock, exc)

    if removed_paths:
        logger.warning(
            "Removed stale Chromium profile locks before startup: %s",
            ", ".join(removed_paths),
        )


def _patch_openai_login_handler(module) -> None:
    handler_cls = getattr(module, "OpenAILoginHandler", None)
    if handler_cls is None or getattr(handler_cls, "_codex_password_url_patch", False):
        return

    original_handle = handler_cls.handle
    original_handle_login = handler_cls.handle_login
    original_handle_login_password = handler_cls.handle_login_password
    original_handle_login_challenge = handler_cls.handle_login_challenge

    async def suppress_guest_login_ui(page) -> None:
        if page is None:
            return
        try:
            await page.evaluate(
                """() => {
                    const selectors = [
                        '[data-testid="login-button"]',
                        '#modal-no-auth-login',
                        '[data-testid="modal-no-auth-login"]',
                        '[data-testid="signup-button"]',
                    ];
                    for (const selector of selectors) {
                        for (const node of document.querySelectorAll(selector)) {
                            node.remove();
                        }
                    }
                    for (const node of document.querySelectorAll('button, a, div, p')) {
                        const text = (node.innerText || '').trim();
                        if (
                            text === 'Log in'
                            || text === 'Sign up'
                            || text === 'Get responses tailored to you'
                        ) {
                            node.remove();
                        }
                    }
                    return true;
                }"""
            )
        except Exception:
            pass

    async def _route_homepage_to_auth(page) -> bool:
        if page is None:
            return False
        current_url = str(getattr(page, "url", ""))
        if not current_url.startswith("https://chatgpt.com/"):
            return False
        try:
            await page.wait_for_load_state("domcontentloaded")
        except Exception:
            pass
        await page.wait_for_timeout(1500)
        authenticated = await _page_looks_authenticated(page)
        has_creds = _has_openai_login_credentials()
        logger.warning(
            "Homepage auth routing decision: authenticated=%s has_creds=%s url=%s",
            authenticated,
            has_creds,
            current_url,
        )
        if authenticated:
            logger.info("ChatGPT home already looks authenticated; keeping current session.")
            return False
        if not has_creds:
            return False

        await suppress_guest_login_ui(page)
        logger.info("Redirecting ChatGPT home page login flow to auth.openai.com.")
        await page.goto(
            "https://auth.openai.com/log-in-or-create-account",
            wait_until="domcontentloaded",
        )
        await page.wait_for_timeout(1500)
        try:
            await _complete_auth_login(page)
        except RuntimeError as exc:
            if not _is_missing_mfa_code_error(exc):
                raise
            logger.warning(
                "MFA challenge is waiting for a code; leaving auth page in place for request-time completion."
            )
            return True
        await page.goto("https://chatgpt.com/", wait_until="domcontentloaded")
        await page.wait_for_load_state("domcontentloaded")
        await page.wait_for_timeout(2000)
        return True

    async def _route_homepage_via_modal(page) -> bool:
        if page is None:
            return False
        current_url = str(getattr(page, "url", ""))
        if not current_url.startswith("https://chatgpt.com/"):
            return False
        if await _page_looks_authenticated(page):
            return False
        if not _has_openai_login_credentials():
            return False

        await _dismiss_cookie_banner(page)
        login_button = page.locator('[data-testid="login-button"]').first
        if not await login_button.count():
            return False

        logger.warning("Routing ChatGPT home page login flow through homepage modal.")
        await login_button.click(force=True)
        await page.wait_for_timeout(1000)
        if not await _submit_login_email_step(page):
            return False

        await _complete_auth_login(page)
        await page.goto("https://chatgpt.com/", wait_until="domcontentloaded")
        await page.wait_for_load_state("domcontentloaded")
        await page.wait_for_timeout(2000)
        return True

    async def patched_handle_login(self):
        page = _get_raw_page(getattr(self, "page", None))
        if page is not None and await _page_looks_authenticated(page):
            await _move_authenticated_page_out_of_login_route(page)
            logger.info("Authenticated ChatGPT session detected; skipping login handler.")
            return True
        if not getattr(self, "_codex_disable_modal_auth", False):
            try:
                if await _route_homepage_via_modal(page):
                    return True
            except Exception as exc:
                setattr(self, "_codex_disable_modal_auth", True)
                logger.warning("Homepage modal login flow failed: %s", exc)
        if not getattr(self, "_codex_disable_direct_auth", False):
            try:
                if await _route_homepage_to_auth(page):
                    return True
            except TimeoutError as exc:
                setattr(self, "_codex_disable_direct_auth", True)
                logger.warning(
                    "Direct auth routing from ChatGPT home timed out; falling back to upstream homepage login flow: %s",
                    exc,
                )
                if page is not None:
                    try:
                        await page.goto("https://chatgpt.com/", wait_until="domcontentloaded")
                        await page.wait_for_timeout(1500)
                    except Exception:
                        pass
        return await original_handle_login(self)

    async def patched_handle_login_password(self):
        result = await original_handle_login_password(self)
        page = _get_raw_page(getattr(self, "page", None))
        if page is None:
            return result

        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            current_url = str(getattr(page, "url", ""))
            if current_url.startswith("https://chatgpt.com/"):
                await page.wait_for_timeout(2000)
                logger.warning("Password login flow finished on ChatGPT.")
                return result
            if current_url.startswith("chrome-error://chromewebdata/"):
                logger.warning("Password login flow hit chrome-error page; retrying ChatGPT.")
                await page.goto("https://chatgpt.com/", wait_until="domcontentloaded")
                await page.wait_for_timeout(1500)
                continue
            if "auth.openai.com" not in current_url:
                await page.wait_for_timeout(1000)
                logger.warning("Password login flow left auth domain: %s", current_url)
                return result
            await page.wait_for_timeout(1000)

        logger.warning("Password login flow did not leave auth domain in time; forcing ChatGPT.")
        await page.goto("https://chatgpt.com/", wait_until="domcontentloaded")
        await page.wait_for_timeout(2000)
        return result

    async def patched_handle_login_challenge(self):
        page = _get_raw_page(getattr(self, "page", None))
        if page is None:
            return await original_handle_login_challenge(self)
        try:
            await _submit_mfa_challenge(page)
        except RuntimeError as exc:
            if not _is_missing_mfa_code_error(exc):
                raise
            logger.warning(
                "MFA challenge is active during startup but no code was provided; deferring completion."
            )
            return True
        return await original_handle_login_challenge(self)

    async def patched_handle(self):
        raw_page = _get_raw_page(getattr(self, "page", None))
        if raw_page is not None and await _page_looks_authenticated(raw_page):
            await _move_authenticated_page_out_of_login_route(raw_page)
            logger.info("Authenticated ChatGPT session detected; skipping login dispatcher.")
            return True
        if not getattr(self, "_codex_disable_modal_auth", False):
            try:
                if await _route_homepage_via_modal(raw_page):
                    return True
            except Exception as exc:
                setattr(self, "_codex_disable_modal_auth", True)
                logger.warning("Homepage modal login flow failed inside handler: %s", exc)
        if not getattr(self, "_codex_disable_direct_auth", False):
            try:
                if await _route_homepage_to_auth(raw_page):
                    return True
            except TimeoutError as exc:
                setattr(self, "_codex_disable_direct_auth", True)
                logger.warning(
                    "Direct auth routing timed out inside login handler; retrying with upstream homepage flow: %s",
                    exc,
                )
                if raw_page is not None:
                    try:
                        await raw_page.goto("https://chatgpt.com/", wait_until="domcontentloaded")
                        await raw_page.wait_for_timeout(1500)
                    except Exception:
                        pass
        try:
            return await original_handle(self)
        except Exception as exc:
            current_url = getattr(getattr(self, "page", None), "url", "")
            raw_page = _get_raw_page(getattr(self, "page", None))
            if current_url.startswith("chrome-error://chromewebdata/") and raw_page is not None:
                logger.warning("Recovering from chrome-error page during OpenAI login flow.")
                await raw_page.goto("https://chatgpt.com/", wait_until="domcontentloaded")
                await raw_page.wait_for_timeout(1500)
                return await original_handle(self)
            if current_url.startswith("https://auth.openai.com/log-in/password"):
                logger.info("Routing new OpenAI password URL to handle_login_password.")
                return await self.handle_login_password()
            if "/mfa-challenge/" in current_url:
                logger.warning("Routing MFA challenge URL to handle_login_challenge.")
                return await self.handle_login_challenge()
            raise

    handler_cls.handle = patched_handle
    handler_cls.handle_login = patched_handle_login
    handler_cls.handle_login_password = patched_handle_login_password
    handler_cls.handle_login_challenge = patched_handle_login_challenge
    handler_cls._codex_password_url_patch = True
    logger.info("Applied OpenAI password URL routing patch.")


def _patch_browser_handler(module) -> None:
    if module is None or getattr(module, "_codex_browser_handler_patch", False):
        return

    patched_any = False
    module_level_handle = getattr(module, "handle", None)
    module_wait_for_stable_page = getattr(module, "wait_for_stable_page", None)

    if callable(module_level_handle) and not getattr(module_level_handle, "_codex_browser_patch", False):
        async def patched_module_handle(*args, **kwargs):
            self = args[0] if args else None
            page = _get_raw_page(getattr(self, "page", None))
            current_url = str(getattr(page, "url", ""))
            if current_url.startswith("https://chatgpt.com/"):
                if await _page_looks_authenticated(page):
                    await _move_authenticated_page_out_of_login_route(page)
                    logger.warning(
                        "Short-circuiting browser.handler.handle on authenticated ChatGPT page."
                    )
                    return True
                logger.warning(
                    "Short-circuiting browser.handler.handle on ChatGPT home and deferring auth to request time."
                )
                return True
            return await module_level_handle(*args, **kwargs)

        patched_module_handle._codex_browser_patch = True
        module.handle = patched_module_handle
        patched_any = True

    if callable(module_wait_for_stable_page) and not getattr(
        module_wait_for_stable_page, "_codex_browser_patch", False
    ):
        async def patched_wait_for_stable_page(*args, **kwargs):
            page = None
            if args:
                candidate = args[0]
                page = _get_raw_page(getattr(candidate, "page", candidate))
            if page is not None and await _page_looks_authenticated(page):
                await _move_authenticated_page_out_of_login_route(page)
                logger.warning(
                    "Short-circuiting browser.handler.wait_for_stable_page on authenticated ChatGPT page."
                )
                return True
            return await module_wait_for_stable_page(*args, **kwargs)

        patched_wait_for_stable_page._codex_browser_patch = True
        module.wait_for_stable_page = patched_wait_for_stable_page
        patched_any = True

    def wrap_method(method, class_name: str, method_name: str = "handle"):
        async def patched(self, *args, **kwargs):
            page = _get_raw_page(getattr(self, "page", None))
            current_url = str(getattr(page, "url", ""))
            if page is not None and await _page_looks_authenticated(page):
                await _move_authenticated_page_out_of_login_route(page)
                logger.warning(
                    "Short-circuiting %s.%s on authenticated ChatGPT page.",
                    class_name,
                    method_name,
                )
                return True
            if current_url.startswith("https://chatgpt.com/") and method_name == "handle":
                logger.warning(
                    "Short-circuiting %s.handle on ChatGPT home and deferring auth to request time.",
                    class_name,
                )
                return True
            return await method(self, *args, **kwargs)

        return patched

    for name, value in vars(module).items():
        if not isinstance(value, type):
            continue
        if getattr(value, "_codex_browser_handle_patch", False):
            continue
        patched_class_any = False
        for method_name in ("handle", "wait_for_stable_page"):
            method = getattr(value, method_name, None)
            if method is None or not callable(method):
                continue
            setattr(value, method_name, wrap_method(method, name, method_name))
            patched_class_any = True
        if patched_class_any:
            value._codex_browser_handle_patch = True
            patched_any = True

    module._codex_browser_handler_patch = True
    if patched_any:
        logger.info("Applied browser handler short-circuit patch.")


def _message_get(message, key, default=None):
    if message is None:
        return default
    if isinstance(message, dict):
        return message.get(key, default)
    return getattr(message, key, default)


def _request_get(request_data, key, default=None):
    if request_data is None:
        return default
    if isinstance(request_data, dict):
        return request_data.get(key, default)

    try:
        value = getattr(request_data, key)
        if value is not None:
            return value
    except Exception:
        pass

    for extra_name in ("model_extra", "__pydantic_extra__", "__dict__"):
        try:
            extra = getattr(request_data, extra_name, None)
        except Exception:
            extra = None
        if isinstance(extra, dict) and key in extra:
            return extra.get(key, default)

    try:
        meta = getattr(request_data, "meta", None)
    except Exception:
        meta = None
    if meta is not None:
        try:
            value = getattr(meta, key)
            if value is not None:
                return value
        except Exception:
            pass
        if isinstance(meta, dict):
            return meta.get(key, default)

    return default


def _normalize_text_content(content) -> str:
    if content is None:
        return ""

    if isinstance(content, str):
        return content.strip()

    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                text = item.strip()
                if text:
                    parts.append(text)
                continue

            item_type = _message_get(item, "type", "")
            if item_type in {"text", "input_text"}:
                text = (
                    _message_get(item, "text")
                    or _message_get(item, "content")
                    or _message_get(item, "value")
                    or ""
                )
                text = str(text).strip()
                if text:
                    parts.append(text)

        return "\n".join(parts).strip()

    return str(content).strip()


def _extract_image_refs_from_message(message) -> list[str]:
    refs: list[str] = []

    images = _message_get(message, "images", None) or []
    for image in images:
        if isinstance(image, str) and image.strip():
            refs.append(image.strip())

    content = _message_get(message, "content", None)
    if isinstance(content, list):
        for item in content:
            item_type = _message_get(item, "type", "")
            if item_type not in {"image_url", "input_image"}:
                continue

            image_url = _message_get(item, "image_url") or _message_get(item, "url")
            if isinstance(image_url, dict):
                image_url = image_url.get("url")
            if isinstance(image_url, str) and image_url.strip():
                refs.append(image_url.strip())

    deduped: list[str] = []
    seen: set[str] = set()
    for ref in refs:
        if ref not in seen:
            seen.add(ref)
            deduped.append(ref)
    return deduped


def _build_prompt_and_images(request_data) -> tuple[str, list[str]]:
    messages = list(getattr(request_data, "messages", []) or [])
    if not messages:
        return "", []

    if len(messages) == 1:
        message = messages[0]
        return (
            _normalize_text_content(_message_get(message, "content", "")),
            _extract_image_refs_from_message(message),
        )

    prompt_parts: list[str] = []
    latest_user_images: list[str] = []

    for message in messages:
        role = str(_message_get(message, "role", "user") or "user").strip().lower()
        content = _normalize_text_content(_message_get(message, "content", ""))
        if content:
            prompt_parts.append(f"{role.capitalize()}:\n{content}")
        if role == "user":
            latest_user_images = _extract_image_refs_from_message(message)

    prompt_parts.append("Assistant:\nAnswer the last user message.")
    return "\n\n".join(part for part in prompt_parts if part).strip(), latest_user_images


def _collapse_whitespace(text) -> str:
    return " ".join(str(text or "").split()).strip()


def _normalize_match_text(text) -> str:
    return _collapse_whitespace(text).lower()


def _build_request_markers(request_data, submitted_prompt: str = "") -> list[str]:
    markers: list[str] = []
    messages = list(getattr(request_data, "messages", []) or [])

    for message in reversed(messages):
        role = str(_message_get(message, "role", "user") or "user").strip().lower()
        if role != "user":
            continue
        content = _collapse_whitespace(_normalize_text_content(_message_get(message, "content", "")))
        if content:
            markers.append(content)
            break

    submitted_prompt = _collapse_whitespace(submitted_prompt)
    if submitted_prompt:
        markers.append(submitted_prompt)

    deduped: list[str] = []
    seen: set[str] = set()
    for marker in markers:
        normalized = _normalize_match_text(marker)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        deduped.append(marker)
    return deduped


def _normalize_chat_name(value) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return " ".join(text.split())[:120]


def _normalize_chat_session_marker(value) -> str:
    text = _collapse_whitespace(value)
    if not text:
        return ""
    return text[:400]


def _coerce_bool(value, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off", ""}:
        return False
    return default


def _coerce_int(value, default: int, minimum: int | None = None, maximum: int | None = None) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError):
        result = default
    if minimum is not None:
        result = max(minimum, result)
    if maximum is not None:
        result = min(maximum, result)
    return result


def _load_chat_sessions() -> dict[str, dict[str, str]]:
    try:
        if not _CHAT_SESSION_STORE_PATH.exists():
            return {}
        data = json.loads(_CHAT_SESSION_STORE_PATH.read_text())
        if not isinstance(data, dict):
            return {}
        normalized: dict[str, dict[str, str]] = {}
        for key, value in data.items():
            name = _normalize_chat_name(key)
            if not name:
                continue

            if isinstance(value, str):
                url = str(value or "").strip()
                marker = ""
            elif isinstance(value, dict):
                url = str(value.get("url", "") or "").strip()
                marker = _normalize_chat_session_marker(value.get("marker", ""))
            else:
                continue

            if url.startswith("https://chatgpt.com/c/"):
                normalized[name] = {
                    "url": url,
                    "marker": marker,
                }
        return normalized
    except Exception as exc:
        logger.warning("Failed to load chat session store: %s", exc)
        return {}


def _save_chat_sessions(sessions: dict[str, dict[str, str]]) -> None:
    try:
        _CHAT_SESSION_STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _CHAT_SESSION_STORE_PATH.write_text(
            json.dumps(dict(sorted(sessions.items())), ensure_ascii=True, indent=2)
        )
    except Exception as exc:
        logger.warning("Failed to save chat session store: %s", exc)


def _resolve_chat_request_options(request_data) -> dict[str, object]:
    chat_mode = str(_request_get(request_data, "chat_mode", "new") or "new").strip().lower()
    if chat_mode not in {"new", "current", "named"}:
        chat_mode = "new"

    chat_name = _normalize_chat_name(_request_get(request_data, "chat_name", ""))
    create_if_missing = _request_get(request_data, "create_if_missing", None)
    if create_if_missing is None:
        create_if_missing = chat_mode == "named"
    else:
        create_if_missing = _coerce_bool(create_if_missing, default=(chat_mode == "named"))

    return {
        "chat_mode": chat_mode,
        "chat_name": chat_name,
        "create_if_missing": create_if_missing,
    }


def _current_chat_url(page) -> str:
    current_url = str(getattr(page, "url", "") or "").strip()
    if current_url.startswith("https://chatgpt.com/c/"):
        return current_url
    return ""


def _persist_named_chat_session(chat_context: dict[str, object] | None) -> None:
    if not isinstance(chat_context, dict):
        return

    if chat_context.get("chat_mode") != "named":
        return

    chat_name = _normalize_chat_name(chat_context.get("chat_name"))
    chat_url = str(chat_context.get("chat_url", "") or "").strip()
    chat_marker = _normalize_chat_session_marker(chat_context.get("request_marker", ""))
    if not chat_name or not chat_url.startswith("https://chatgpt.com/c/"):
        return

    sessions = _load_chat_sessions()
    session_entry = sessions.get(chat_name, {})
    if (
        session_entry.get("url") == chat_url
        and session_entry.get("marker", "") == chat_marker
    ):
        return

    sessions[chat_name] = {
        "url": chat_url,
        "marker": chat_marker,
    }
    _save_chat_sessions(sessions)
    logger.info("Saved named chat session '%s' -> %s.", chat_name, chat_url)


def _decode_data_url_to_file(data_url: str) -> str:
    header, encoded = data_url.split(",", 1)
    mime = header[5:].split(";", 1)[0] if header.startswith("data:") else "application/octet-stream"
    ext = mimetypes.guess_extension(mime) or ".bin"
    if ext == ".jpe":
        ext = ".jpg"

    path = Path("/tmp") / f"llm-web-api-upload-{uuid.uuid4().hex}{ext}"
    path.write_bytes(base64.b64decode(encoded))
    return str(path)


def _prepare_upload_files(image_refs: list[str]) -> list[str]:
    files: list[str] = []
    for ref in image_refs:
        if not ref:
            continue
        if ref.startswith("data:"):
            try:
                files.append(_decode_data_url_to_file(ref))
            except Exception as exc:
                logger.warning("Skipping invalid data URL image payload: %s", exc)
            continue
        if os.path.exists(ref):
            files.append(ref)
    return files


def _get_raw_page(page_or_wrapper):
    if page_or_wrapper is None:
        return None
    return getattr(page_or_wrapper, "raw_page", page_or_wrapper)


async def _page_looks_authenticated(page) -> bool:
    page = _get_raw_page(page)
    if page is None:
        return False

    login_button = page.locator('[data-testid="login-button"]').first
    try:
        login_button_count = await login_button.count()
    except Exception:
        login_button_count = 0

    composer = page.locator("#prompt-textarea").first
    composer_visible = False
    try:
        composer_visible = await composer.is_visible(timeout=1000)
    except Exception:
        pass
    new_chat = page.locator('[data-testid="create-new-chat-button"]').first
    new_chat_visible = False
    try:
        new_chat_visible = await new_chat.is_visible(timeout=1000)
    except Exception:
        pass
    conversation_turn = page.locator('[data-message-author-role="assistant"]').first
    conversation_visible = False
    try:
        conversation_visible = await conversation_turn.is_visible(timeout=1000)
    except Exception:
        pass
    logger.warning(
        "Authenticated-page probe: composer_visible=%s new_chat_visible=%s conversation_visible=%s login_button_count=%s url=%s",
        composer_visible,
        new_chat_visible,
        conversation_visible,
        login_button_count,
        getattr(page, "url", ""),
    )

    if login_button_count:
        return False
    if composer_visible:
        return True
    if new_chat_visible:
        return True
    if conversation_visible:
        return True

    return False


async def _move_authenticated_page_out_of_login_route(page) -> None:
    page = _get_raw_page(page)
    if page is None:
        return
    current_url = str(getattr(page, "url", ""))
    if not current_url.startswith("https://chatgpt.com/"):
        return
    try:
        await page.evaluate(
            """() => {
                const current = new URL(location.href);
                if (current.pathname === "/") {
                    history.replaceState({}, "", "/c/codex-ready");
                }
                return location.href;
            }"""
        )
    except Exception:
        pass


def _is_navigation_race(exc: Exception) -> bool:
    return "Execution context was destroyed" in str(exc)


def _is_missing_mfa_code_error(exc: Exception) -> bool:
    return "MFA challenge requires OPENAI_LOGIN_OTP_CODE" in str(exc)


def _message_matches_request(text: str, request_markers: list[str]) -> bool:
    candidate = _normalize_match_text(text)
    if not candidate:
        return False

    for marker in request_markers:
        normalized_marker = _normalize_match_text(marker)
        if not normalized_marker:
            continue
        shortest_length = min(len(candidate), len(normalized_marker))
        if shortest_length < 12:
            if candidate == normalized_marker:
                return True
            continue
        if (
            candidate == normalized_marker
            or candidate in normalized_marker
            or normalized_marker in candidate
        ):
            return True
    return False


def _annotate_turn_ordinals(turns: list[dict]) -> list[dict]:
    user_count = 0
    assistant_count = 0
    annotated: list[dict] = []

    for turn in turns:
        role = str(turn.get("role", "") or "").strip().lower()
        text = str(turn.get("text", "") or "").strip()
        annotated_turn = {
            "role": role,
            "text": text,
            "message_id": turn.get("message_id"),
        }
        if role == "user":
            annotated_turn["role_ordinal"] = user_count
            user_count += 1
        elif role == "assistant":
            annotated_turn["role_ordinal"] = assistant_count
            assistant_count += 1
        else:
            annotated_turn["role_ordinal"] = None
        annotated.append(annotated_turn)

    return annotated


async def _extract_turns_from_page(page) -> list[dict]:
    page = _get_raw_page(page)
    if page is None:
        return []

    try:
        turns = await page.evaluate(
            """() => {
                return Array.from(document.querySelectorAll('[data-message-author-role]'))
                    .map((node) => {
                        const role = node.getAttribute('data-message-author-role') || '';
                        const text = (node.innerText || node.textContent || '').trim();
                        const turn = node.closest('[data-turn]');
                        const messageId =
                            node.getAttribute('data-message-id')
                            || (turn ? turn.getAttribute('data-message-id') : null);
                        return { role, text, message_id: messageId };
                    })
                    .filter((turn) => turn.role === 'user' || turn.role === 'assistant');
            }"""
        )
    except Exception:
        return []

    if not isinstance(turns, list):
        return []
    return _annotate_turn_ordinals([turn for turn in turns if isinstance(turn, dict)])


async def _install_chatgpt_capture_hook(page) -> None:
    page = _get_raw_page(page)
    if page is None:
        return

    try:
        await page.evaluate(
            """
            () => {
              if (window.__codexChatGPTCaptureInstalled) return true;
              window.__codexChatGPTCaptureInstalled = true;
              window.__codexChatGPTCapture = window.__codexChatGPTCapture || {
                records: [],
                maxRecords: 40,
                maxResponseChars: 8000000
              };

              const store = window.__codexChatGPTCapture;
              const originalFetch = window.__codexOriginalFetch || window.fetch;
              window.__codexOriginalFetch = originalFetch;

              const toUrl = input => {
                try {
                  if (typeof input === "string") return input;
                  if (input && typeof input.url === "string") return input.url;
                } catch (_) {}
                return "";
              };

              const shouldCapture = url => {
                if (!url || typeof url !== "string") return false;
                return (
                  url.includes("/backend-api/conversation")
                  || url.includes("/backend-api/f/conversation")
                  || url.includes("/backend-api/files")
                  || url.includes("/backend-api/attachments")
                  || url.includes("/backend-api/download")
                  || url.includes("/backend-api/content")
                );
              };

              const safeBody = (input, init) => {
                try {
                  if (init && typeof init.body === "string") return init.body.slice(0, 200000);
                  if (input && typeof input === "object" && typeof input.body === "string") {
                    return input.body.slice(0, 200000);
                  }
                } catch (_) {}
                return "";
              };

              window.fetch = function(input, init) {
                const url = toUrl(input);
                const method = (
                  init && init.method
                  || input && typeof input === "object" && input.method
                  || "GET"
                );
                const capture = shouldCapture(url);
                const requestBody = capture ? safeBody(input, init) : "";

                return originalFetch.call(this, input, init).then(async response => {
                  if (!capture) return response;

                  const record = {
                    url,
                    method,
                    status: response.status,
                    ok: response.ok,
                    timestamp: Date.now(),
                    requestBody,
                    responseText: "",
                    responseHeaders: {}
                  };

                  try {
                    response.headers.forEach((value, key) => {
                      record.responseHeaders[key] = value;
                    });
                  } catch (_) {}

                  try {
                    const text = await response.clone().text();
                    record.responseText = String(text || "").slice(0, store.maxResponseChars || 8000000);
                  } catch (error) {
                    record.error = String(error && error.message || error);
                  }

                  store.records.push(record);
                  const maxRecords = store.maxRecords || 40;
                  if (store.records.length > maxRecords) {
                    store.records.splice(0, store.records.length - maxRecords);
                  }
                  return response;
                });
              };

              window.__codexGetChatGPTCapture = () => {
                const records = Array.isArray(store.records) ? store.records : [];
                return records.map(record => ({...record}));
              };

              window.__codexClearChatGPTCapture = () => {
                const count = Array.isArray(store.records) ? store.records.length : 0;
                store.records = [];
                return count;
              };

              return true;
            }
            """
        )
    except Exception as exc:
        logger.warning("Failed to install ChatGPT fetch capture hook: %s", exc)


async def _clear_chatgpt_capture_records(page) -> None:
    page = _get_raw_page(page)
    if page is None:
        return
    try:
        await page.evaluate(
            """() => {
              if (typeof window.__codexClearChatGPTCapture === "function") {
                window.__codexClearChatGPTCapture();
              }
            }"""
        )
    except Exception:
        pass


async def _get_chatgpt_capture_records(page) -> list[dict]:
    page = _get_raw_page(page)
    if page is None:
        return []
    try:
        records = await page.evaluate(
            """() => {
              if (typeof window.__codexGetChatGPTCapture === "function") {
                return window.__codexGetChatGPTCapture();
              }
              return [];
            }"""
        )
    except Exception as exc:
        logger.warning("Failed to read ChatGPT capture records: %s", exc)
        return []
    if not isinstance(records, list):
        return []
    return [record for record in records if isinstance(record, dict)]


def _iter_json_values_from_text(text: str):
    if not text:
        return

    stripped = text.strip()
    if not stripped:
        return

    try:
        yield json.loads(stripped)
        return
    except Exception:
        pass

    for line in stripped.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("data:"):
            line = line[5:].strip()
        if not line or line == "[DONE]":
            continue
        try:
            yield json.loads(line)
        except Exception:
            continue


def _coerce_chatgpt_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            text = _coerce_chatgpt_text(item)
            if text:
                parts.append(text)
        return "\n".join(parts).strip()
    if not isinstance(value, dict):
        return ""

    content_type = str(value.get("content_type", "") or "")
    if content_type in {"text", "multimodal_text"} and isinstance(value.get("parts"), list):
        return _coerce_chatgpt_text(value.get("parts"))

    for key in ("text", "content", "markdown", "value"):
        if key in value:
            text = _coerce_chatgpt_text(value.get(key))
            if text:
                return text

    if isinstance(value.get("parts"), list):
        return _coerce_chatgpt_text(value.get("parts"))

    return ""


def _append_chatgpt_file(files: list[dict[str, object]], item: dict) -> None:
    url = ""
    for key in ("download_url", "url", "href", "file_url"):
        candidate = item.get(key)
        if isinstance(candidate, str) and candidate.strip():
            url = candidate.strip()
            break

    has_file_shape = any(
        key in item
        for key in (
            "file_id",
            "file_name",
            "filename",
            "mime_type",
            "mimeType",
            "download_url",
            "file_url",
        )
    )
    file_id = str(item.get("file_id") or (item.get("id") if has_file_shape else "") or "").strip()
    if not url and file_id:
        url = f"https://chatgpt.com/backend-api/files/{file_id}/download"

    name = ""
    for key in ("name", "filename", "file_name", "title"):
        candidate = item.get(key)
        if isinstance(candidate, str) and candidate.strip():
            name = candidate.strip()
            break

    mime = str(item.get("mime_type") or item.get("mimeType") or item.get("content_type") or "").strip()
    size = item.get("size") or item.get("bytes")

    looks_like_file = bool(url) and (
        "backend-api/files" in url
        or "backend-api/attachments" in url
        or "download" in url
        or bool(name)
        or bool(mime)
    )
    if not looks_like_file:
        return

    record: dict[str, object] = {"url": url}
    if name:
        record["name"] = name
    if mime:
        record["mime_type"] = mime
    if file_id:
        record["id"] = file_id
    if isinstance(size, (int, float)):
        record["size"] = size
    files.append(record)


def _collect_chatgpt_messages_and_files(value) -> tuple[list[dict], list[dict[str, object]]]:
    turns: list[dict] = []
    files: list[dict[str, object]] = []
    seen_messages: set[str] = set()
    seen_files: set[str] = set()

    def visit(node):
        if isinstance(node, list):
            for item in node:
                visit(item)
            return
        if not isinstance(node, dict):
            return

        if "message" in node and isinstance(node.get("message"), dict):
            visit(node.get("message"))

        author = node.get("author")
        role = ""
        if isinstance(author, dict):
            role = str(author.get("role", "") or "").strip().lower()
        elif isinstance(node.get("role"), str):
            role = str(node.get("role", "") or "").strip().lower()

        content = node.get("content")
        text = _coerce_chatgpt_text(content if content is not None else node.get("text"))
        if role in {"user", "assistant"} and text:
            message_id = str(node.get("id") or node.get("message_id") or "").strip() or None
            marker = f"{role}:{message_id or ''}:{text}"
            if marker not in seen_messages:
                seen_messages.add(marker)
                turns.append(
                    {
                        "role": role,
                        "text": text.strip(),
                        "message_id": message_id,
                    }
                )

        _append_chatgpt_file(files, node)

        for key in ("attachments", "files", "assets", "parts", "metadata", "recipient"):
            child = node.get(key)
            if isinstance(child, (dict, list)):
                visit(child)

        if isinstance(node.get("mapping"), dict):
            for child in node["mapping"].values():
                visit(child)

        for key, child in node.items():
            if key in {
                "message",
                "content",
                "attachments",
                "files",
                "assets",
                "parts",
                "metadata",
                "recipient",
                "mapping",
            }:
                continue
            if isinstance(child, (dict, list)):
                visit(child)

    visit(value)

    deduped_files: list[dict[str, object]] = []
    for item in files:
        marker = str(item.get("url") or item.get("id") or item.get("name") or "")
        if not marker or marker in seen_files:
            continue
        seen_files.add(marker)
        deduped_files.append(item)

    return _annotate_turn_ordinals(turns), deduped_files


def _extract_chatgpt_stream_delta(value) -> str:
    if not isinstance(value, dict):
        return ""

    delta = value.get("v")
    if isinstance(delta, str):
        event_type = str(value.get("type", "") or value.get("event", "") or "").lower()
        if not event_type or any(token in event_type for token in ("delta", "append", "content")):
            return delta

    for key in ("delta", "text_delta", "content_delta"):
        candidate = value.get(key)
        if isinstance(candidate, str):
            return candidate
        if isinstance(candidate, dict):
            text = _coerce_chatgpt_text(candidate)
            if text:
                return text

    return ""


def _extract_turns_and_files_from_capture_records(
    records: list[dict],
) -> tuple[list[dict], list[dict[str, object]]]:
    all_turns: list[dict] = []
    all_files: list[dict[str, object]] = []

    for record in records:
        response_text = str(record.get("responseText", "") or "")
        request_body = str(record.get("requestBody", "") or "")
        for source_name, text in (("request", request_body), ("response", response_text)):
            stream_parts: list[str] = []
            for value in _iter_json_values_from_text(text):
                turns, files = _collect_chatgpt_messages_and_files(value)
                all_turns.extend(turns)
                all_files.extend(files)
                if source_name == "response":
                    delta = _extract_chatgpt_stream_delta(value)
                    if delta:
                        stream_parts.append(delta)
            if stream_parts:
                all_turns.append(
                    {
                        "role": "assistant",
                        "text": "".join(stream_parts).strip(),
                        "message_id": None,
                    }
                )

    return _annotate_turn_ordinals(all_turns), all_files


async def _extract_request_specific_assistant_capture_from_page(
    page,
    request_markers: list[str],
) -> tuple[str, str | None, list[dict[str, object]]]:
    records = await _get_chatgpt_capture_records(page)
    turns, files = _extract_turns_and_files_from_capture_records(records)
    text, message_id = _select_assistant_turn_for_request(
        turns,
        request_markers,
        previous_user_count=None,
        previous_assistant_count=None,
    )
    if not text:
        for turn in reversed(turns):
            if turn.get("role") == "assistant" and turn.get("text"):
                return turn.get("text", ""), turn.get("message_id"), files
    return text, message_id, files


async def _wait_for_request_specific_assistant_capture_from_page(
    page,
    request_markers: list[str],
    timeout_ms: int = 12000,
) -> tuple[str, str | None, list[dict[str, object]]]:
    deadline = time.monotonic() + (timeout_ms / 1000)
    last_result: tuple[str, str | None, list[dict[str, object]]] = ("", None, [])
    while time.monotonic() < deadline:
        result = await _extract_request_specific_assistant_capture_from_page(
            page,
            request_markers,
        )
        text, _message_id, files = result
        if text and not _looks_like_placeholder_reply(text):
            return result
        if files:
            last_result = result
        await page.wait_for_timeout(500)
    return last_result


def _extract_turns_from_html_file(html_file: Path) -> list[dict]:
    soup = BeautifulSoup(html_file.read_text(errors="ignore"), "html.parser")
    turns: list[dict] = []
    for node in soup.select("[data-message-author-role]"):
        role = str(node.attrs.get("data-message-author-role", "") or "").strip().lower()
        if role not in {"user", "assistant"}:
            continue
        turn = node.find_parent(attrs={"data-turn": True})
        turns.append(
            {
                "role": role,
                "text": node.get_text("\n", strip=True).strip(),
                "message_id": node.attrs.get("data-message-id")
                or (turn.attrs.get("data-message-id") if turn else None),
            }
        )
    return _annotate_turn_ordinals(turns)


def _select_assistant_turn_for_request(
    turns: list[dict],
    request_markers: list[str],
    previous_user_count: int | None = None,
    previous_assistant_count: int | None = None,
) -> tuple[str, str | None]:
    if not turns:
        return "", None

    user_indices: list[int] = []
    matched_user_indices: list[int] = []
    new_user_indices: list[int] = []

    for index, turn in enumerate(turns):
        if turn.get("role") != "user":
            continue
        user_indices.append(index)
        ordinal = turn.get("role_ordinal")
        is_new_user_turn = previous_user_count is None or (
            isinstance(ordinal, int) and ordinal >= previous_user_count
        )
        if is_new_user_turn:
            new_user_indices.append(index)
        if is_new_user_turn and _message_matches_request(turn.get("text", ""), request_markers):
            matched_user_indices.append(index)

    candidate_user_indices = matched_user_indices
    if not candidate_user_indices and previous_user_count is not None:
        candidate_user_indices = new_user_indices[-1:]

    for user_index in reversed(candidate_user_indices):
        next_user_index = len(turns)
        for future_user_index in user_indices:
            if future_user_index > user_index:
                next_user_index = future_user_index
                break

        assistant_candidates: list[dict] = []
        for turn in turns[user_index + 1 : next_user_index]:
            if turn.get("role") != "assistant":
                continue
            text = turn.get("text", "")
            if not text:
                continue
            ordinal = turn.get("role_ordinal")
            if previous_assistant_count is not None and (
                not isinstance(ordinal, int) or ordinal < previous_assistant_count
            ):
                continue
            assistant_candidates.append(turn)

        if assistant_candidates:
            selected = assistant_candidates[-1]
            return selected.get("text", ""), selected.get("message_id")

    return "", None


async def _extract_request_specific_assistant_text_from_page(
    page,
    request_markers: list[str],
    previous_user_count: int | None = None,
    previous_assistant_count: int | None = None,
) -> tuple[str, str | None]:
    turns = await _extract_turns_from_page(page)
    return _select_assistant_turn_for_request(
        turns,
        request_markers,
        previous_user_count=previous_user_count,
        previous_assistant_count=previous_assistant_count,
    )


async def _capture_turn_counts(page) -> tuple[int | None, int | None]:
    turns = await _extract_turns_from_page(page)
    if not turns:
        return None, None
    user_count = sum(1 for turn in turns if turn.get("role") == "user")
    assistant_count = sum(1 for turn in turns if turn.get("role") == "assistant")
    return user_count, assistant_count


def _extract_request_specific_assistant_text_from_error_html(
    request_markers: list[str],
    previous_user_count: int | None = None,
    previous_assistant_count: int | None = None,
) -> tuple[str, str | None]:
    error_dir = Path("/app/data/error")
    if not error_dir.exists():
        return "", None

    html_files = sorted(error_dir.glob("*.html"), key=lambda p: p.stat().st_mtime, reverse=True)
    for html_file in html_files[:5]:
        try:
            turns = _extract_turns_from_html_file(html_file)
            text, message_id = _select_assistant_turn_for_request(
                turns,
                request_markers,
                previous_user_count=previous_user_count,
                previous_assistant_count=previous_assistant_count,
            )
            if text:
                return text, message_id
        except Exception:
            continue
    return "", None


def _resolve_openai_otp_code() -> str:
    for env_name in ("OPENAI_LOGIN_OTP_CODE", "OPENAI_LOGIN_MFA_CODE"):
        value = os.getenv(env_name, "").strip()
        if value:
            return value

    otp_file = os.getenv("OPENAI_LOGIN_OTP_FILE", "").strip()
    candidate_files = [Path(otp_file)] if otp_file else []
    candidate_files.append(Path("/app/data/openai_otp_code.txt"))
    for otp_path in candidate_files:
        try:
            if otp_path.exists():
                value = otp_path.read_text().strip()
                if value:
                    return value
        except Exception:
            continue

    for env_name in ("OPENAI_LOGIN_OTP_SECRET", "OPENAI_LOGIN_MFA_SECRET"):
        secret = os.getenv(env_name, "").strip()
        if secret:
            return pyotp.TOTP(secret).now()

    raise RuntimeError(
        "MFA challenge requires OPENAI_LOGIN_OTP_CODE / OPENAI_LOGIN_MFA_CODE "
        "or OPENAI_LOGIN_OTP_SECRET / OPENAI_LOGIN_MFA_SECRET."
    )


async def _dismiss_cookie_banner(page) -> None:
    for label in ("Reject non-essential", "Accept all", "Close"):
        try:
            button = page.get_by_role("button", name=label).first
            await button.click(timeout=1500)
            await page.wait_for_timeout(300)
        except Exception:
            continue


async def _complete_auth_login(page) -> None:
    login_type = os.getenv("OPENAI_LOGIN_TYPE", "").strip().lower()
    login_email = os.getenv("OPENAI_LOGIN_EMAIL", "").strip()
    login_password = os.getenv("OPENAI_LOGIN_PASSWORD", "").strip()
    if login_type != "email" or not login_email or not login_password:
        raise RuntimeError("OPENAI email login credentials are not configured.")
    password_submitted = False
    mfa_submitted = False
    deadline = time.monotonic() + 180

    while time.monotonic() < deadline:
        current_url = str(getattr(page, "url", ""))

        if current_url.startswith("https://chatgpt.com/") and password_submitted:
            return

        if await _submit_login_email_step(page):
            continue

        password_input = page.locator('input[type="password"][name="current-password"]').first
        if await password_input.count() and not password_submitted:
            logger.info("Direct page path reached auth password page at %s; submitting password.", current_url)
            await password_input.fill(login_password)
            await page.locator('button[type="submit"]').first.click()
            password_submitted = True
            await page.wait_for_timeout(2500)
            continue

        mfa_input = page.locator('input[name="code"]').first
        if await mfa_input.count() and not mfa_submitted:
            await _submit_mfa_challenge(page)
            mfa_submitted = True
            continue

        otp_ready_selectors = (
            'input[autocomplete="one-time-code"]',
            'input[inputmode="numeric"]',
            'input[type="tel"]',
        )
        if not mfa_submitted:
            for selector in otp_ready_selectors:
                candidate = page.locator(selector).first
                try:
                    if await candidate.count():
                        await _submit_mfa_challenge(page)
                        mfa_submitted = True
                        break
                except Exception:
                    continue
            if mfa_submitted:
                continue

        if "device-notification" in current_url:
            switched = await _switch_from_device_notification_to_totp(page)
            if switched:
                continue
            approved = await _wait_for_device_notification_approval(page, timeout_seconds=180)
            if approved:
                await page.wait_for_timeout(1500)
                continue
            raise TimeoutError(
                f"Timed out waiting for device-notification approval on {current_url}"
            )

        if password_submitted and "auth.openai.com" not in current_url:
            return

        if current_url.startswith("chrome-error://chromewebdata/"):
            logger.warning("Encountered chrome-error during auth completion; retrying ChatGPT.")
            await page.goto("https://chatgpt.com/", wait_until="domcontentloaded")
            await page.wait_for_timeout(1500)
            continue

        await page.wait_for_timeout(1000)

    raise TimeoutError(f"Timed out completing auth flow on {getattr(page, 'url', '')}")


async def _submit_login_email_step(page) -> bool:
    login_email = os.getenv("OPENAI_LOGIN_EMAIL", "").strip()
    if not login_email:
        return False

    selectors = (
        (
            page.locator('[data-testid="modal-no-auth-login"] input[type="email"][name="email"]').first,
            page.locator('[data-testid="modal-no-auth-login"] button[type="submit"]').first,
        ),
        (
            page.locator('input[type="email"][name="email"]').first,
            page.locator('button[type="submit"]').first,
        ),
    )

    for email_input, continue_button in selectors:
        try:
            if not await email_input.count():
                continue

            try:
                current_value = await email_input.input_value()
            except Exception:
                current_value = ""
            try:
                is_disabled = await email_input.is_disabled()
            except Exception:
                is_disabled = False

            if not is_disabled and current_value != login_email:
                await email_input.fill(login_email)

            await continue_button.click(force=True)
            await page.wait_for_timeout(1500)
            logger.warning("Submitted login email step on %s.", getattr(page, "url", ""))
            return True
        except Exception:
            continue

    return False


async def _ensure_authenticated_session(page, force: bool = False) -> None:
    if not force and await _page_looks_authenticated(page):
        return

    login_button = page.locator('[data-testid="login-button"]').first
    if not force and not await login_button.count():
        return

    logger.warning("Login button still visible on ChatGPT; forcing authenticated session.")
    if force:
        await page.goto(
            "https://auth.openai.com/log-in-or-create-account",
            wait_until="domcontentloaded",
        )
        await page.wait_for_timeout(1500)
    elif await login_button.count():
        await login_button.click(force=True)
        await page.wait_for_timeout(1000)
    else:
        await page.goto(
            "https://auth.openai.com/log-in-or-create-account",
            wait_until="domcontentloaded",
        )
        await page.wait_for_timeout(1500)
    await _complete_auth_login(page)
    await page.goto("https://chatgpt.com/", wait_until="domcontentloaded")
    await page.wait_for_load_state("domcontentloaded")
    await page.wait_for_timeout(2000)


async def _submit_mfa_challenge(page) -> None:
    code = _resolve_openai_otp_code()

    input_selectors = (
        'input[name="code"]',
        'input[autocomplete="one-time-code"]',
        'input[inputmode="numeric"]',
        'input[type="tel"]',
        'input[type="text"]',
    )

    code_input = None
    for selector in input_selectors:
        candidate = page.locator(selector).first
        try:
            if await candidate.count():
                code_input = candidate
                break
        except Exception:
            continue

    if code_input is None:
        return

    logger.warning("Submitting MFA code on %s.", getattr(page, "url", ""))
    try:
        await code_input.fill(code)
    except Exception:
        try:
            await code_input.click()
            await page.keyboard.insert_text(code)
        except Exception:
            raise

    submit_candidates = (
        page.locator('button[type="submit"]').first,
        page.get_by_role("button", name="Continue").first,
        page.get_by_role("button", name="Verify").first,
    )
    for button in submit_candidates:
        try:
            if await button.count():
                await button.click(force=True)
                await page.wait_for_timeout(2500)
                return
        except Exception:
            continue


async def _switch_from_device_notification_to_totp(page) -> bool:
    controls = (
        page.get_by_role("link", name="Try another method").first,
        page.get_by_role("button", name="Try another method").first,
    )
    for control in controls:
        try:
            if await control.count():
                logger.warning("Switching OpenAI MFA flow from device notification to another method.")
                await control.click(force=True)
                await page.wait_for_timeout(1500)
                return True
        except Exception:
            continue
    return False


async def _wait_for_device_notification_approval(page, timeout_seconds: int = 180) -> bool:
    deadline = time.monotonic() + timeout_seconds
    logger.warning(
        "Waiting for OpenAI device-notification approval on %s for up to %ss.",
        getattr(page, "url", ""),
        timeout_seconds,
    )

    while time.monotonic() < deadline:
        current_url = str(getattr(page, "url", ""))
        if "device-notification" not in current_url:
            return True
        await page.wait_for_timeout(2000)

    return False


async def _ensure_chat_ready(page) -> None:
    last_error = None

    for attempt in range(3):
        try:
            logger.info("Preparing ChatGPT page for direct completion, attempt %s.", attempt + 1)
            if not str(getattr(page, "url", "")).startswith("https://chatgpt.com/"):
                logger.info("Navigating to ChatGPT from %s.", getattr(page, "url", ""))
                await page.goto("https://chatgpt.com/", wait_until="domcontentloaded")
            await page.wait_for_load_state("domcontentloaded")
            await page.wait_for_timeout(1500)

            await _dismiss_cookie_banner(page)

            composer = page.locator("#prompt-textarea").first
            if await composer.count():
                logger.info("Chat composer is available on %s.", getattr(page, "url", ""))
                await composer.wait_for(state="visible", timeout=10000)
                return

            if "auth.openai.com" in str(getattr(page, "url", "")):
                await _complete_auth_login(page)
                await page.goto("https://chatgpt.com/", wait_until="domcontentloaded")
                await page.wait_for_load_state("domcontentloaded")
                await page.wait_for_timeout(2000)
                await _dismiss_cookie_banner(page)
                if await composer.count():
                    await composer.wait_for(state="visible", timeout=10000)
                    return

            last_error = RuntimeError(f"Composer not ready on {getattr(page, 'url', '')}")
        except Exception as exc:
            last_error = exc
            if _is_navigation_race(exc):
                logger.info("Chat page preparation raced with navigation on attempt %s.", attempt + 1)
            else:
                logger.warning("Chat page preparation attempt %s failed: %s", attempt + 1, exc)

    if last_error is not None:
        raise last_error
    raise RuntimeError("Failed to prepare ChatGPT page.")


async def _open_fresh_chat(
    page,
    require_authenticated: bool = False,
    force_reauthentication: bool = False,
) -> None:
    await _ensure_chat_ready(page)

    if require_authenticated or force_reauthentication:
        await _ensure_authenticated_session(page, force=force_reauthentication)
        await page.locator("#prompt-textarea").first.wait_for(state="visible", timeout=30000)

    try:
        new_chat = page.locator('[data-testid="create-new-chat-button"]').first
        logger.info("Opening a fresh chat from %s.", getattr(page, "url", ""))
        await new_chat.click(timeout=3000)
        await page.wait_for_load_state("domcontentloaded")
        await page.wait_for_timeout(1000)
        if "auth.openai.com" in str(getattr(page, "url", "")):
            logger.info("New chat redirected to auth; completing login before continuing.")
            await _complete_auth_login(page)
            await page.goto("https://chatgpt.com/", wait_until="domcontentloaded")
            await page.wait_for_load_state("domcontentloaded")
            await page.wait_for_timeout(1500)
    except Exception:
        pass

    await _dismiss_cookie_banner(page)
    await page.locator("#prompt-textarea").first.wait_for(state="visible", timeout=30000)


async def _prepare_chat_for_request(
    page,
    request_data,
    require_authenticated: bool = False,
    force_reauthentication: bool = False,
) -> dict[str, object]:
    chat_context = _resolve_chat_request_options(request_data)
    chat_mode = str(chat_context.get("chat_mode", "new"))
    chat_name = str(chat_context.get("chat_name", ""))
    create_if_missing = bool(chat_context.get("create_if_missing", False))

    if chat_mode == "new":
        await _open_fresh_chat(
            page,
            require_authenticated=require_authenticated,
            force_reauthentication=force_reauthentication,
        )
        chat_context["chat_url"] = _current_chat_url(page)
        chat_context["used_existing"] = False
        return chat_context

    await _ensure_chat_ready(page)

    if require_authenticated or force_reauthentication or chat_mode in {"current", "named"}:
        await _ensure_authenticated_session(page, force=force_reauthentication)
        await page.locator("#prompt-textarea").first.wait_for(state="visible", timeout=30000)

    if chat_mode == "current":
        logger.info("Reusing current chat on %s.", getattr(page, "url", ""))
        await _dismiss_cookie_banner(page)
        await page.locator("#prompt-textarea").first.wait_for(state="visible", timeout=30000)
        chat_context["chat_url"] = _current_chat_url(page)
        chat_context["used_existing"] = bool(chat_context["chat_url"])
        return chat_context

    if not chat_name:
        raise RuntimeError("chat_name is required when chat_mode is 'named'.")

    sessions = _load_chat_sessions()
    session_entry = sessions.get(chat_name, {})
    target_url = str(session_entry.get("url", "") or "").strip()
    target_marker = _normalize_chat_session_marker(session_entry.get("marker", ""))
    if target_url:
        logger.info("Opening named chat '%s' from %s.", chat_name, target_url)
        if _current_chat_url(page) != target_url:
            await page.goto(target_url, wait_until="domcontentloaded")
            await page.wait_for_load_state("domcontentloaded")
            await page.wait_for_timeout(1500)
        await _dismiss_cookie_banner(page)
        await _ensure_chat_ready(page)

        resolved_url = _current_chat_url(page)
        if resolved_url == target_url:
            marker_loaded = await _wait_for_named_chat_marker(page, target_marker)
            if target_marker and not marker_loaded:
                logger.warning(
                    "Named chat '%s' reopened URL %s but did not load stored marker yet.",
                    chat_name,
                    target_url,
                )
                await page.reload(wait_until="domcontentloaded")
                await page.wait_for_load_state("domcontentloaded")
                await page.wait_for_timeout(2000)
                await _dismiss_cookie_banner(page)
                await _ensure_chat_ready(page)
                marker_loaded = await _wait_for_named_chat_marker(page, target_marker)
                resolved_url = _current_chat_url(page)

            if target_marker and not marker_loaded:
                raise RuntimeError(
                    f"Named chat '{chat_name}' did not finish loading its stored history."
                )

            chat_context["chat_url"] = resolved_url
            chat_context["loaded_marker"] = target_marker
            chat_context["used_existing"] = True
            return chat_context

        logger.warning(
            "Named chat '%s' did not reopen stored URL. expected=%s current=%s",
            chat_name,
            target_url,
            resolved_url or getattr(page, "url", ""),
        )

    if not create_if_missing:
        raise RuntimeError(f"Named chat '{chat_name}' was not found.")

    logger.info("Creating a new named chat '%s'.", chat_name)
    await _open_fresh_chat(
        page,
        require_authenticated=require_authenticated,
        force_reauthentication=force_reauthentication,
    )
    chat_context["chat_url"] = _current_chat_url(page)
    chat_context["used_existing"] = False
    return chat_context


async def _page_contains_user_marker(page, marker: str) -> bool:
    marker_norm = _normalize_match_text(marker)
    if not marker_norm:
        return True

    locator = page.locator('[data-turn="user"] [data-message-author-role="user"]')
    try:
        count = await locator.count()
    except Exception as exc:
        if _is_navigation_race(exc):
            return False
        raise

    start_index = max(0, count - 12)
    for index in range(start_index, count):
        try:
            text = await locator.nth(index).inner_text(timeout=1000)
        except Exception:
            continue
        text_norm = _normalize_match_text(text)
        if marker_norm in text_norm:
            return True

    return False


async def _wait_for_named_chat_marker(page, marker: str, timeout_ms: int = 15000) -> bool:
    marker = _normalize_chat_session_marker(marker)
    if not marker:
        return True

    deadline = time.monotonic() + (timeout_ms / 1000)
    while time.monotonic() < deadline:
        if await _page_contains_user_marker(page, marker):
            return True
        await page.wait_for_timeout(500)
    return False


async def _wait_for_new_user_turn(page, previous_count: int, timeout_ms: int = 8000) -> bool:
    locator = page.locator('[data-turn="user"] [data-message-author-role="user"]')
    deadline = time.monotonic() + (timeout_ms / 1000)

    while time.monotonic() < deadline:
        try:
            count = await locator.count()
        except Exception as exc:
            if _is_navigation_race(exc):
                await page.wait_for_timeout(300)
                continue
            raise
        if count > previous_count:
            return True
        await page.wait_for_timeout(300)

    return False


async def _send_prompt(page, prompt: str, image_paths: list[str], previous_user_count: int) -> None:
    logger.info("Submitting prompt via direct page path on %s.", getattr(page, "url", ""))
    await _ensure_chat_ready(page)
    composer = page.locator("#prompt-textarea").first
    await composer.wait_for(state="visible", timeout=30000)
    await composer.click()

    try:
        await page.keyboard.press("Control+A")
        await page.keyboard.press("Backspace")
    except Exception:
        pass

    if image_paths:
        logger.info("Uploading %s image(s) before prompt submission.", len(image_paths))
        upload_input = page.locator("input#upload-files").first
        await upload_input.set_input_files(image_paths, timeout=30000)
        await page.wait_for_timeout(1500)

    if prompt:
        await page.keyboard.insert_text(prompt)
    else:
        await page.keyboard.insert_text("Please describe the uploaded image.")

    await page.wait_for_timeout(400)

    submit_selectors = (
        '[data-testid="send-button"]',
        'button[aria-label="Send prompt"]',
        'button[aria-label="Send message"]',
        'button[aria-label="Send"]',
    )

    for selector in submit_selectors:
        try:
            button = page.locator(selector).first
            if await button.count():
                logger.info("Trying submit button selector %s.", selector)
                await button.click(timeout=3000)
                if await _wait_for_new_user_turn(page, previous_user_count):
                    logger.info("Prompt submission created a new user turn via %s.", selector)
                    return
        except Exception:
            continue

    for send_action in (composer.press("Enter"), page.keyboard.press("Enter")):
        try:
            await send_action
            if await _wait_for_new_user_turn(page, previous_user_count):
                logger.info("Prompt submission created a new user turn via Enter key.")
                return
        except Exception:
            continue

    raise TimeoutError("Prompt submission did not create a new user turn.")


async def _extract_assistant_image_outputs(node) -> list[dict[str, object]]:
    try:
        images = await node.locator("img").evaluate_all(
            """
            async elements => {
              const readBlobAsDataUrl = blob => new Promise((resolve, reject) => {
                const reader = new FileReader();
                reader.onload = () => resolve(reader.result);
                reader.onerror = () => reject(reader.error);
                reader.readAsDataURL(blob);
              });

              const out = [];
              for (const img of elements) {
                const src = img.currentSrc || img.src || "";
                if (!src) continue;

                const item = {
                  url: src,
                  alt: img.alt || "",
                  width: img.naturalWidth || img.width || 0,
                  height: img.naturalHeight || img.height || 0
                };

                if (src.startsWith("blob:") || src.includes("/backend-api/estuary/content")) {
                  try {
                    const response = await fetch(src);
                    const blob = await response.blob();
                    item.data_url = await readBlobAsDataUrl(blob);
                    item.mime = blob.type || "image/png";
                  } catch (error) {
                    item.error = String(error && error.message || error);
                  }
                }

                out.push(item);
              }
              return out;
            }
            """
        )
    except Exception as exc:
        logger.warning("Failed to extract assistant image outputs: %s", exc)
        return []

    deduped: list[dict[str, object]] = []
    seen: set[str] = set()
    for item in images:
        if not isinstance(item, dict):
            continue
        marker = str(item.get("data_url") or item.get("url") or "")
        if not marker or marker in seen:
            continue
        seen.add(marker)
        deduped.append(item)
    return deduped


async def _extract_recent_assistant_image_outputs(page, max_turns: int = 8) -> list[dict[str, object]]:
    locator = page.locator('[data-turn="assistant"] [data-message-author-role="assistant"]')
    try:
        count = await locator.count()
    except Exception as exc:
        if _is_navigation_race(exc):
            return []
        raise

    for index in range(count - 1, max(-1, count - max_turns - 1), -1):
        try:
            images = await _extract_assistant_image_outputs(locator.nth(index))
        except Exception:
            images = []
        if images:
            return images
    return []


async def _extract_page_image_outputs(page) -> list[dict[str, object]]:
    try:
        images = await page.evaluate(
            """
            async () => {
              const readBlobAsDataUrl = blob => new Promise((resolve, reject) => {
                const reader = new FileReader();
                reader.onload = () => resolve(reader.result);
                reader.onerror = () => reject(reader.error);
                reader.readAsDataURL(blob);
              });

              const out = [];
              const push = async (url, element, source) => {
                if (!url || typeof url !== "string") return;
                const rect = element && element.getBoundingClientRect ? element.getBoundingClientRect() : {width: 0, height: 0};
                const naturalWidth = element && (element.naturalWidth || element.width) || 0;
                const naturalHeight = element && (element.naturalHeight || element.height) || 0;
                const width = Math.max(Math.round(rect.width || 0), naturalWidth || 0);
                const height = Math.max(Math.round(rect.height || 0), naturalHeight || 0);
                if (width && height && (width < 64 || height < 64)) return;

                const item = {url, source, alt: element && element.alt || "", width, height};
                if (url.startsWith("blob:") || url.includes("/backend-api/estuary/content")) {
                  try {
                    const response = await fetch(url);
                    const blob = await response.blob();
                    item.data_url = await readBlobAsDataUrl(blob);
                    item.mime = blob.type || "image/png";
                  } catch (error) {
                    item.error = String(error && error.message || error);
                  }
                }
                out.push(item);
              };

              for (const img of Array.from(document.images || [])) {
                await push(img.currentSrc || img.src, img, "img");
              }

              for (const link of Array.from(document.querySelectorAll("a[href]"))) {
                const href = link.href || "";
                if (/\\.(png|jpe?g|webp|gif)(\\?|#|$)/i.test(href) || href.startsWith("blob:")) {
                  await push(href, link, "link");
                }
              }

              for (const el of Array.from(document.querySelectorAll("*"))) {
                const bg = getComputedStyle(el).backgroundImage || "";
                for (const match of bg.matchAll(/url\\([\"']?([^\"')]+)[\"']?\\)/g)) {
                  await push(match[1], el, "background");
                }
              }

              return out;
            }
            """
        )
    except Exception as exc:
        logger.warning("Failed to extract page image outputs: %s", exc)
        return []

    deduped: list[dict[str, object]] = []
    seen: set[str] = set()
    for item in images:
        if not isinstance(item, dict):
            continue
        marker = str(item.get("data_url") or item.get("url") or "")
        if not marker or marker in seen:
            continue
        seen.add(marker)
        deduped.append(item)
    return deduped


async def _extract_page_file_outputs(page) -> list[dict[str, object]]:
    try:
        files = await page.evaluate(
            """
            () => {
              const out = [];
              const filePattern = /\\.(pdf|docx?|pptx?|xlsx?|csv|zip|txt|md)(\\?|#|$)/i;
              for (const link of Array.from(document.querySelectorAll("a[href]"))) {
                const href = link.href || "";
                const text = (link.innerText || link.textContent || "").trim();
                const download = link.getAttribute("download") || "";
                if (
                  filePattern.test(href)
                  || href.includes("/backend-api/files")
                  || href.includes("/backend-api/attachments")
                  || href.includes("/download")
                  || download
                ) {
                  out.push({
                    url: href,
                    name: download || text || "",
                    mime_type: link.type || ""
                  });
                }
              }
              return out;
            }
            """
        )
    except Exception as exc:
        logger.warning("Failed to extract page file outputs: %s", exc)
        return []

    deduped: list[dict[str, object]] = []
    seen: set[str] = set()
    for item in files:
        if not isinstance(item, dict):
            continue
        marker = str(item.get("url") or "")
        if not marker or marker in seen:
            continue
        seen.add(marker)
        deduped.append(item)
    return deduped


def _looks_like_placeholder_reply(text: str) -> bool:
    normalized = _collapse_whitespace(text).strip().lower()
    return normalized in {
        "thinking",
        "reasoning",
        "思考中",
        "正在思考",
    }


def _merge_file_outputs(*groups: list[dict[str, object]] | None) -> list[dict[str, object]]:
    merged: list[dict[str, object]] = []
    seen: set[str] = set()
    for group in groups:
        if not group:
            continue
        for item in group:
            if not isinstance(item, dict):
                continue
            marker = str(item.get("url") or item.get("id") or item.get("name") or "")
            if not marker or marker in seen:
                continue
            seen.add(marker)
            merged.append(item)
    return merged


async def _is_assistant_response_in_progress(page) -> bool:
    selectors = (
        '[data-testid="stop-button"]',
        'button[aria-label="Stop streaming"]',
        'button[aria-label="Stop generating"]',
        'button[aria-label="Stop response"]',
        'button[aria-label="停止生成"]',
        'button:has-text("Stop")',
        'button:has-text("停止")',
    )
    for selector in selectors:
        try:
            locator = page.locator(selector).first
            if await locator.count() and await locator.is_visible(timeout=250):
                return True
        except Exception:
            continue
    return False


async def _is_assistant_turn_finalized(node) -> bool:
    try:
        finalized = await node.evaluate(
            """element => {
              const root = element.closest('[data-testid^="conversation-turn"], [data-turn]') || element.parentElement || element;
              const selectors = [
                '[data-testid="copy-turn-action-button"]',
                '[data-testid="good-response-turn-action-button"]',
                '[data-testid="bad-response-turn-action-button"]',
                'button[aria-label="Copy"]',
                'button[aria-label="复制"]'
              ];
              for (const selector of selectors) {
                const found = root.querySelector(selector);
                if (!found) continue;
                const style = getComputedStyle(found);
                const rect = found.getBoundingClientRect();
                if (style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 0 && rect.height > 0) {
                  return true;
                }
              }
              return false;
            }"""
        )
        if finalized:
            return True
    except Exception:
        pass

    selectors = (
        '[data-testid="copy-turn-action-button"]',
        '[data-testid="good-response-turn-action-button"]',
        '[data-testid="bad-response-turn-action-button"]',
        'button[aria-label="Copy"]',
        'button[aria-label="复制"]',
        'button:has-text("Copy")',
        'button:has-text("复制")',
    )
    for selector in selectors:
        try:
            locator = node.locator(selector).first
            if await locator.count() and await locator.is_visible(timeout=250):
                return True
        except Exception:
            continue
    return False


async def _wait_for_assistant_reply(
    page,
    previous_count: int,
    timeout_ms: int = 120000,
    collect_images: bool = True,
):
    locator = page.locator('[data-turn="assistant"] [data-message-author-role="assistant"]')
    deadline = time.monotonic() + (timeout_ms / 1000)
    last_text = ""
    last_message_id = None
    last_images: list[dict[str, object]] = []
    last_image_marker = ""
    stable_rounds = 0
    last_change_at = time.monotonic()
    first_content_at = 0.0

    while time.monotonic() < deadline:
        response_in_progress = await _is_assistant_response_in_progress(page)
        try:
            count = await locator.count()
        except Exception as exc:
            if _is_navigation_race(exc):
                await page.wait_for_timeout(500)
                continue
            raise
        if count > previous_count:
            node = locator.nth(count - 1)
            turn_finalized = await _is_assistant_turn_finalized(node)
            text = ""
            try:
                text = (await node.inner_text(timeout=1000)).strip()
            except Exception:
                text = ""
            if _looks_like_placeholder_reply(text):
                text = ""

            if text and text != last_text:
                last_text = text
                stable_rounds = 0
                last_change_at = time.monotonic()
                if not first_content_at:
                    first_content_at = last_change_at
                try:
                    last_message_id = await node.get_attribute("data-message-id")
                except Exception:
                    last_message_id = None
            elif text:
                stable_rounds += 1

            if collect_images:
                images = await _extract_assistant_image_outputs(node)
                if not images:
                    images = await _extract_page_image_outputs(page)
                image_marker = json.dumps(images, sort_keys=True) if images else ""
                if image_marker and image_marker != last_image_marker:
                    last_images = images
                    last_image_marker = image_marker
                    stable_rounds = 0
                    last_change_at = time.monotonic()
                    if not first_content_at:
                        first_content_at = last_change_at
                    try:
                        last_message_id = await node.get_attribute("data-message-id")
                    except Exception:
                        last_message_id = None
                elif image_marker:
                    stable_rounds += 1

                if (
                    last_images
                    and (turn_finalized or not response_in_progress)
                    and stable_rounds >= 4
                    and (time.monotonic() - last_change_at) >= 4
                ):
                    logger.info("Assistant reply stabilized with %s generated image(s).", len(last_images))
                    return last_text or "Image generated.", last_message_id, last_images

            if (
                last_text
                and (turn_finalized or not response_in_progress)
                and stable_rounds >= (8 if turn_finalized else 45)
                and (time.monotonic() - last_change_at) >= (8 if turn_finalized else 45)
                and first_content_at
                and (time.monotonic() - first_content_at) >= (12 if turn_finalized else 120)
            ):
                logger.info("Assistant reply stabilized with %s characters.", len(last_text))
                return last_text, last_message_id, last_images
        else:
            if collect_images:
                images = await _extract_recent_assistant_image_outputs(page)
                if not images:
                    images = await _extract_page_image_outputs(page)
                image_marker = json.dumps(images, sort_keys=True) if images else ""
                if image_marker and image_marker != last_image_marker:
                    last_images = images
                    last_image_marker = image_marker
                    stable_rounds = 0
                    last_change_at = time.monotonic()
                    if not first_content_at:
                        first_content_at = last_change_at
                elif image_marker:
                    stable_rounds += 1

                if (
                    last_images
                    and not response_in_progress
                    and stable_rounds >= 4
                    and (time.monotonic() - last_change_at) >= 4
                ):
                    logger.info("Recovered %s generated image(s) from recent assistant turns.", len(last_images))
                    return "Image generated.", last_message_id, last_images

        await page.wait_for_timeout(1000)

    if last_text:
        return last_text, last_message_id, last_images
    if last_images:
        return "Image generated.", last_message_id, last_images

    raise TimeoutError("Timed out waiting for assistant reply from ChatGPT page.")


async def _create_completion_via_page(
    self,
    request_data,
    ensure_authenticated: bool = False,
    force_reauthentication: bool = False,
):
    page = _get_raw_page(getattr(self, "page", None))
    if page is None:
        raise RuntimeError("OpenAI browser page is unavailable.")

    prompt, image_refs = _build_prompt_and_images(request_data)
    if not prompt and not image_refs:
        raise RuntimeError("No prompt content found in request.")
    is_image_generation = _coerce_bool(
        _request_get(request_data, "image_generation", None),
        default=False,
    )
    submitted_prompt = prompt or "Please describe the uploaded image."
    request_markers = _build_request_markers(request_data, submitted_prompt)

    logger.info(
        "Direct page completion path started. prompt_chars=%s image_count=%s ensure_authenticated=%s force_reauthentication=%s current_url=%s",
        len(prompt),
        len(image_refs),
        ensure_authenticated,
        force_reauthentication,
        getattr(page, "url", ""),
    )
    chat_context = await _prepare_chat_for_request(
        page,
        request_data,
        require_authenticated=(ensure_authenticated or bool(image_refs)),
        force_reauthentication=force_reauthentication,
    )
    chat_context["request_marker"] = _normalize_chat_session_marker(submitted_prompt)
    await _install_chatgpt_capture_hook(page)
    await _clear_chatgpt_capture_records(page)
    assistant_locator = page.locator('[data-turn="assistant"] [data-message-author-role="assistant"]')
    user_locator = page.locator('[data-turn="user"] [data-message-author-role="user"]')
    try:
        previous_count = await assistant_locator.count()
    except Exception as exc:
        if _is_navigation_race(exc):
            previous_count = 0
        else:
            raise
    try:
        previous_user_count = await user_locator.count()
    except Exception as exc:
        if _is_navigation_race(exc):
            previous_user_count = 0
        else:
            raise
    image_paths = _prepare_upload_files(image_refs)
    await _send_prompt(page, prompt, image_paths, previous_user_count)
    image_outputs: list[dict[str, object]] = []
    timeout_ms = _coerce_int(
        _request_get(request_data, "response_timeout_ms", None),
        120000,
        minimum=30000,
        maximum=900000,
    )
    try:
        content, message_id, image_outputs = await _wait_for_assistant_reply(
            page,
            previous_count,
            timeout_ms=timeout_ms,
            collect_images=is_image_generation,
        )
        captured_content, captured_message_id, captured_files = (
            await _wait_for_request_specific_assistant_capture_from_page(
                page,
                request_markers,
            )
        )
        if captured_content and len(captured_content) >= max(20, len(content) // 2):
            content = captured_content
            if captured_message_id:
                message_id = captured_message_id
        elif captured_content and not content:
            content = captured_content
            message_id = captured_message_id
        captured_files = _merge_file_outputs(
            captured_files,
            await _extract_page_file_outputs(page),
        )
    except Exception:
        captured_files = []
        if is_image_generation:
            image_outputs = await _extract_recent_assistant_image_outputs(page)
            if not image_outputs:
                image_outputs = await _extract_page_image_outputs(page)
            if image_outputs:
                logger.warning(
                    "Recovered image generation response from recent assistant images after wait failure."
                )
                content = "Image generated."
                message_id = None
                chat_context["chat_url"] = _current_chat_url(page)
                _persist_named_chat_session(chat_context)
                return _build_fallback_chat_response(
                    request_data,
                    content,
                    message_id,
                    chat_context=chat_context,
                    image_outputs=image_outputs,
                )
        captured_content, captured_message_id, captured_files = (
            await _wait_for_request_specific_assistant_capture_from_page(
                page,
                request_markers,
            )
        )
        if captured_content:
            content = captured_content
            message_id = captured_message_id
            captured_files = _merge_file_outputs(
                captured_files,
                await _extract_page_file_outputs(page),
            )
            logger.warning(
                "Recovered direct page completion from captured ChatGPT backend response after wait failure."
            )
        else:
            content, message_id = await _extract_request_specific_assistant_text_from_page(
                page,
                request_markers,
                previous_user_count=previous_user_count,
                previous_assistant_count=previous_count,
            )
            if not content:
                raise
            logger.warning(
                "Recovered direct page completion from request-matched assistant text after wait failure."
            )
            captured_files = await _extract_page_file_outputs(page)
    chat_context["chat_url"] = _current_chat_url(page)
    _persist_named_chat_session(chat_context)
    return _build_fallback_chat_response(
        request_data,
        content,
        message_id,
        chat_context=chat_context,
        image_outputs=image_outputs,
        file_outputs=captured_files,
    )


def _build_fallback_chat_response(
    request_data,
    content: str,
    message_id: str | None = None,
    chat_context: dict[str, object] | None = None,
    image_outputs: list[dict[str, object]] | None = None,
    file_outputs: list[dict[str, object]] | None = None,
):
    completion_tokens = max(1, len(content) // 4)
    prompt_text = "\n".join(
        _normalize_text_content(_message_get(message, "content", ""))
        for message in getattr(request_data, "messages", [])
    )
    prompt_tokens = max(1, len(prompt_text) // 4) if prompt_text else 0

    response = {
        "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": getattr(request_data, "model", "auto"),
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": content,
                },
                "logprobs": None,
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }

    if image_outputs:
        response["images"] = image_outputs
    if file_outputs:
        response["files"] = file_outputs

    meta = getattr(request_data, "meta", None)
    if getattr(meta, "enable", False) or image_outputs or file_outputs:
        response_meta = {}
        if message_id:
            response_meta["message_id"] = message_id
            response_meta["conversation_id"] = None
        if isinstance(chat_context, dict):
            chat_mode = chat_context.get("chat_mode")
            chat_name = chat_context.get("chat_name")
            chat_url = chat_context.get("chat_url")
            if chat_mode:
                response_meta["chat_mode"] = chat_mode
            if chat_name:
                response_meta["chat_name"] = chat_name
            if chat_url:
                response_meta["chat_url"] = chat_url
        if image_outputs:
            response_meta["images"] = image_outputs
        if file_outputs:
            response_meta["files"] = file_outputs
        if response_meta:
            response["meta"] = response_meta

    return response


def _patch_openai_client(module) -> None:
    client_cls = getattr(module, "OpenAIClient", None)
    if client_cls is None or getattr(client_cls, "_codex_completion_fallback_patch", False):
        return

    original_post_init = getattr(client_cls, "post_init", None)
    original_create_completion = client_cls.create_completion

    async def patched_post_init(self, *args, **kwargs):
        page = _get_raw_page(getattr(self, "page", None))
        if original_post_init is None:
            return True

        try:
            return await original_post_init(self, *args, **kwargs)
        except Exception as exc:
            current_url = str(getattr(page, "url", "")) if page is not None else ""
            if page is not None and current_url.startswith("https://chatgpt.com/") and _has_openai_login_credentials():
                logger.warning(
                    "OpenAIClient.post_init failed on ChatGPT home with %s; ensuring authenticated session directly.",
                    type(exc).__name__,
                )
                await _ensure_authenticated_session(page, force=False)
                await _ensure_chat_ready(page)
                if not getattr(self, "current_model", None):
                    self.current_model = "auto"
                if not getattr(self, "support_models", None):
                    self.support_models = ["gpt-5-mini", "gpt-5-3"]
                return True
            raise

    async def patched_create_completion(self, request_data, **kwargs):
        prompt, image_refs = _build_prompt_and_images(request_data)
        is_image_generation = _coerce_bool(
            _request_get(request_data, "image_generation", None),
            default=False,
        )
        prefer_authenticated_path = bool(image_refs) or is_image_generation or _has_openai_login_credentials()
        chat_context = _resolve_chat_request_options(request_data)

        if not getattr(request_data, "stream", False):
            try:
                return await _create_completion_via_page(
                    self,
                    request_data,
                    ensure_authenticated=prefer_authenticated_path,
                )
            except Exception as exc:
                if is_image_generation:
                    page = _get_raw_page(getattr(self, "page", None))
                    if page is not None:
                        image_outputs = await _extract_recent_assistant_image_outputs(page)
                        if not image_outputs:
                            image_outputs = await _extract_page_image_outputs(page)
                        if image_outputs:
                            logger.warning(
                                "Recovered image generation response from recent assistant images after direct path failure."
                            )
                            chat_context["chat_url"] = _current_chat_url(page)
                            return _build_fallback_chat_response(
                                request_data,
                                "Image generated.",
                                None,
                                chat_context=chat_context,
                                image_outputs=image_outputs,
                            )
                logger.warning(
                    "Direct page completion path failed with %s: %s; falling back to upstream client.",
                    type(exc).__name__,
                    exc,
                )
                try:
                    logger.warning(
                        "Retrying direct page completion after forcing reauthentication."
                    )
                    return await _create_completion_via_page(
                        self,
                        request_data,
                        ensure_authenticated=True,
                        force_reauthentication=True,
                    )
                except Exception as auth_exc:
                    logger.warning(
                        "Authenticated direct page retry failed with %s: %s; falling back to upstream client.",
                        type(auth_exc).__name__,
                        auth_exc,
                    )

        page = _get_raw_page(getattr(self, "page", None))
        submitted_prompt = prompt or (
            "Please describe the uploaded image." if image_refs else ""
        )
        request_markers = _build_request_markers(request_data, submitted_prompt)
        chat_context["request_marker"] = _normalize_chat_session_marker(submitted_prompt)
        previous_user_count = None
        previous_assistant_count = None
        if page is not None:
            try:
                await _install_chatgpt_capture_hook(page)
                await _clear_chatgpt_capture_records(page)
                previous_user_count, previous_assistant_count = await _capture_turn_counts(page)
            except Exception as snapshot_exc:
                logger.warning("Failed to snapshot page turns before upstream completion: %s", snapshot_exc)

        try:
            response = await original_create_completion(self, request_data, **kwargs)
            if page is not None:
                chat_context["chat_url"] = _current_chat_url(page)
                _persist_named_chat_session(chat_context)
            return response
        except Exception as exc:
            if getattr(request_data, "stream", False):
                raise

            content = ""
            message_id = None
            file_outputs: list[dict[str, object]] = []
            if request_markers:
                content, message_id, file_outputs = (
                    await _wait_for_request_specific_assistant_capture_from_page(
                        page,
                        request_markers,
                    )
                )
                if not content:
                    content, message_id = await _extract_request_specific_assistant_text_from_page(
                        page,
                        request_markers,
                        previous_user_count=previous_user_count,
                        previous_assistant_count=previous_assistant_count,
                    )
                if page is not None:
                    file_outputs = _merge_file_outputs(
                        file_outputs,
                        await _extract_page_file_outputs(page),
                    )
            if not content:
                content, message_id = _extract_request_specific_assistant_text_from_error_html(
                    request_markers,
                    previous_user_count=previous_user_count,
                    previous_assistant_count=previous_assistant_count,
                )

            if content:
                if page is not None:
                    chat_context["chat_url"] = _current_chat_url(page)
                    _persist_named_chat_session(chat_context)
                logger.warning(
                    "OpenAI completion raised %s after a request-matched page reply was found; returning scraped assistant text.",
                    type(exc).__name__,
                )
                return _build_fallback_chat_response(
                    request_data,
                    content,
                    message_id,
                    chat_context=chat_context,
                    file_outputs=file_outputs,
                )
            raise

    if original_post_init is not None:
        client_cls.post_init = patched_post_init
    client_cls.create_completion = patched_create_completion
    client_cls._codex_completion_fallback_patch = True
    logger.info("Applied OpenAI completion timeout fallback patch.")


def _patch_openai_provider(module) -> None:
    provider_cls = getattr(module, "OpenAIProvider", None)
    if provider_cls is None or getattr(provider_cls, "_codex_start_timeout_patch", False):
        return

    original_start = getattr(provider_cls, "start", None)
    if original_start is None:
        return

    async def _close_maybe(value) -> None:
        if value is None:
            return
        close = getattr(value, "close", None)
        if not callable(close):
            return
        try:
            result = close()
            if inspect.isawaitable(result):
                await result
        except Exception:
            pass

    async def _reset_provider_state(provider) -> None:
        owners = [
            provider,
            getattr(provider, "client", None),
            getattr(provider, "openai_client", None),
        ]
        attr_names = (
            "page",
            "raw_page",
            "browser",
            "context",
            "browser_context",
            "playwright_context",
        )
        for owner in owners:
            if owner is None:
                continue
            for attr_name in attr_names:
                if not hasattr(owner, attr_name):
                    continue
                try:
                    await _close_maybe(getattr(owner, attr_name))
                except Exception:
                    pass
                try:
                    setattr(owner, attr_name, None)
                except Exception:
                    pass

    async def patched_start(self, *args, **kwargs):
        last_error = None

        for attempt in range(3):
            _cleanup_stale_browser_profile_locks()
            await _reset_provider_state(self)
            try:
                return await asyncio.wait_for(
                    original_start(self, *args, **kwargs),
                    timeout=180,
                )
            except Exception as exc:
                last_error = exc
                message = str(exc)
                client = getattr(self, "client", None) or getattr(self, "openai_client", None)
                page = _get_raw_page(getattr(client, "page", None)) if client is not None else None
                current_model = getattr(client, "current_model", None) if client is not None else None
                support_models = getattr(client, "support_models", None) if client is not None else None
                if client is not None and page is not None:
                    try:
                        if current_model and support_models and await _page_looks_authenticated(page):
                            logger.warning(
                                "OpenAIProvider.start timed out after session became usable; treating startup as successful."
                            )
                            return None
                    except Exception:
                        pass
                if client is not None and (current_model or support_models):
                    logger.warning(
                        "OpenAIProvider.start hit %s after client metadata became available; treating startup as successful.",
                        type(exc).__name__,
                    )
                    return None
                retryable = (
                    isinstance(exc, TimeoutError)
                    or "ECONNREFUSED" in message
                    or "connect_over_cdp" in message
                    or "playwright_context" in message
                )
                if attempt < 2 and retryable:
                    logger.warning(
                        "Retrying OpenAIProvider.start after retryable startup failure: %s",
                        exc,
                    )
                    await asyncio.sleep(3)
                    continue
                raise

        if last_error is not None:
            raise last_error
        return None

    provider_cls.start = patched_start
    provider_cls._codex_start_timeout_patch = True
    logger.info("Applied OpenAI provider startup timeout patch.")


def _patch_provider_manager(module) -> None:
    if module is None or getattr(module, "_codex_provider_manager_patch", False):
        return

    manager = getattr(module, "provider_manager", None)
    manager_cls = getattr(manager, "__class__", None) if manager is not None else None
    original_start_all = getattr(manager_cls, "start_all", None) if manager_cls is not None else None
    if original_start_all is None or getattr(manager_cls, "_codex_start_all_patch", False):
        return

    async def patched_start_all(self, *args, **kwargs):
        task = asyncio.create_task(original_start_all(self, *args, **kwargs))

        def _consume_result(finished_task):
            try:
                finished_task.result()
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                logger.warning(
                    "Background provider_manager.start_all task ended with %s: %s",
                    type(exc).__name__,
                    exc,
                )

        task.add_done_callback(_consume_result)
        done, _ = await asyncio.wait({task}, timeout=90)
        if task in done:
            return await task

        logger.warning(
            "provider_manager.start_all exceeded startup timeout; cancelling background initialization and using direct page routes."
        )
        task.cancel()
        return None

    manager_cls.start_all = patched_start_all
    manager_cls._codex_start_all_patch = True
    module._codex_provider_manager_patch = True
    logger.info("Applied provider_manager startup timeout patch.")


def _patch_chat_api(module) -> None:
    if module is None or getattr(module, "_codex_chat_api_patch", False):
        return

    form_cls = getattr(module, "ChatCompletionForm", None)
    if form_cls is None:
        return

    try:
        model_config = dict(getattr(form_cls, "model_config", {}) or {})
        if model_config.get("extra") != "allow":
            model_config["extra"] = "allow"
            form_cls.model_config = model_config
            rebuild = getattr(form_cls, "model_rebuild", None)
            if callable(rebuild):
                rebuild(force=True)
        module._codex_chat_api_patch = True
        logger.info("Applied ChatCompletionForm extra-field patch.")
    except Exception as exc:
        logger.warning("Failed to patch ChatCompletionForm extra-field handling: %s", exc)


def _find_active_openai_page():
    try:
        import llm.provider_manager as provider_manager_module
    except Exception as exc:
        logger.warning("Current image route could not import provider manager: %s", exc)
        return None

    manager = getattr(provider_manager_module, "provider_manager", None)
    providers = []
    provider_dict = getattr(manager, "provider_dict", None)
    if isinstance(provider_dict, dict):
        providers.extend(provider_dict.values())
    try:
        providers.extend(manager.get_all_providers())
    except Exception:
        pass

    for provider in providers:
        for owner in (
            provider,
            getattr(provider, "client", None),
            getattr(provider, "openai_client", None),
        ):
            if owner is None:
                continue
            for attr_name in ("page", "raw_page"):
                page = _get_raw_page(getattr(owner, attr_name, None))
                if page is not None:
                    try:
                        if callable(getattr(page, "is_closed", None)) and page.is_closed():
                            continue
                    except Exception:
                        continue
                    return page
    return None


def _configured_model_ids() -> list[str]:
    candidates = [
        os.getenv("OPENAI_CHAT_MODEL", "").strip(),
        "gpt-5-3",
        "gpt-5-mini",
        "gpt-4o",
        "gpt-4o-mini",
    ]
    models: list[str] = []
    seen: set[str] = set()
    for model in candidates:
        if not model or model in seen:
            continue
        seen.add(model)
        models.append(model)
    return models


def _authorization_is_valid(request) -> bool:
    expected = os.getenv("OPENAI_API_TOKEN", "").strip()
    if not expected:
        return True
    header = str(request.headers.get("authorization", "") or "").strip()
    if header.lower().startswith("bearer "):
        return header[7:].strip() == expected
    return header == expected


def _promote_latest_route(app, path: str, methods: set[str]) -> None:
    routes = getattr(getattr(app, "router", None), "routes", None)
    if not isinstance(routes, list) or not routes:
        return

    for index in range(len(routes) - 1, -1, -1):
        route = routes[index]
        route_path = getattr(route, "path", None)
        route_methods = set(getattr(route, "methods", set()) or set())
        if route_path == path and methods.issubset(route_methods):
            routes.insert(0, routes.pop(index))
            return


def _image_data_url_to_b64_json(data_url: str) -> str:
    if not data_url.startswith("data:") or "," not in data_url:
        return data_url
    return data_url.split(",", 1)[1]


def _rank_generated_image(item: dict[str, object]) -> int:
    url = str(item.get("url") or "")
    width = int(item.get("width") or 0)
    height = int(item.get("height") or 0)
    score = width * height
    if item.get("data_url"):
        score += 20_000_000
    if "/backend-api/estuary/content" in url:
        score += 10_000_000
    if "public_content" in url or "thumbnail" in url:
        score -= 10_000_000
    if width and height and min(width, height) < 256:
        score -= 5_000_000
    return score


def _filter_generated_images(images: list[dict[str, object]], limit: int) -> list[dict[str, object]]:
    candidates: list[dict[str, object]] = []
    for item in images:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "")
        width = int(item.get("width") or 0)
        height = int(item.get("height") or 0)
        if ("public_content" in url or "thumbnail" in url) and not item.get("data_url"):
            continue
        if width and height and min(width, height) < 256:
            continue
        if item.get("data_url") or url:
            candidates.append(item)

    if not candidates:
        candidates = [item for item in images if isinstance(item, dict)]

    deduped: list[dict[str, object]] = []
    seen: set[str] = set()
    for item in sorted(candidates, key=_rank_generated_image, reverse=True):
        marker = str(item.get("data_url") or item.get("url") or "")
        if not marker or marker in seen:
            continue
        seen.add(marker)
        deduped.append(item)
        if len(deduped) >= limit:
            break
    return deduped


def _build_openai_image_response(
    images: list[dict[str, object]],
    response_format: str,
    limit: int,
) -> dict[str, object]:
    selected = _filter_generated_images(images, limit)
    data: list[dict[str, object]] = []
    wants_b64 = response_format in {"b64_json", "base64", "data_url"}

    for item in selected:
        output: dict[str, object] = {}
        data_url = str(item.get("data_url") or "")
        url = str(item.get("url") or "")
        if wants_b64 and data_url:
            output["b64_json"] = _image_data_url_to_b64_json(data_url)
        elif wants_b64 and str(item.get("b64_json") or ""):
            output["b64_json"] = str(item.get("b64_json"))
        elif data_url and response_format == "data_url":
            output["url"] = data_url
        elif url:
            output["url"] = url
        elif data_url:
            output["b64_json"] = _image_data_url_to_b64_json(data_url)

        if item.get("alt"):
            output["revised_prompt"] = str(item.get("alt"))
        if output:
            data.append(output)

    return {
        "created": int(time.time()),
        "data": data,
    }


def _patch_main_api_routes() -> None:
    try:
        import main as main_module
    except Exception as exc:
        logger.warning("Failed to import main for current image route patch: %s", exc)
        return

    main = main_module
    if getattr(main, "_codex_current_images_route_patch", False):
        return

    original_create_api = main.create_api

    def patched_create_api(app, *args, **kwargs):
        result = original_create_api(app, *args, **kwargs)

        if not getattr(app, "_codex_current_images_route_added", False):
            from fastapi import Request
            from fastapi.responses import JSONResponse

            @app.get("/v1/models")
            async def codex_models():
                return {
                    "object": "list",
                    "data": [
                        {
                            "id": model,
                            "object": "model",
                            "created": 0,
                            "owned_by": "chatgpt-web",
                        }
                        for model in _configured_model_ids()
                    ],
                }

            @app.post("/v1/chat/completions")
            async def codex_chat_completions(request: Request):
                if not _authorization_is_valid(request):
                    return JSONResponse(
                        status_code=401,
                        content={
                            "error": {
                                "message": "Invalid API token.",
                                "type": "invalid_request_error",
                                "code": "invalid_api_key",
                            }
                        },
                    )

                try:
                    data = await request.json()
                    import llm.api.chat as chat_api

                    request_data = chat_api.ChatCompletionForm(**data)
                    page = _find_active_openai_page()
                    if page is None:
                        raise RuntimeError("OpenAI browser page is unavailable.")
                    return await _create_completion_via_page(
                        SimpleNamespace(page=page),
                        request_data,
                        ensure_authenticated=True,
                    )
                except Exception as exc:
                    logger.warning("Direct ChatGPT route failed: %s", exc)
                    return JSONResponse(
                        status_code=500,
                        content={
                            "error": {
                                "message": str(exc),
                                "type": type(exc).__name__,
                                "code": "chatgpt_page_error",
                            }
                        },
                    )

            @app.post("/v1/images/generations")
            async def codex_image_generations(request: Request):
                if not _authorization_is_valid(request):
                    return JSONResponse(
                        status_code=401,
                        content={
                            "error": {
                                "message": "Invalid API token.",
                                "type": "invalid_request_error",
                                "code": "invalid_api_key",
                            }
                        },
                    )

                try:
                    payload = await request.json()
                    prompt = str(payload.get("prompt") or "").strip()
                    if not prompt:
                        return JSONResponse(
                            status_code=400,
                            content={
                                "error": {
                                    "message": "prompt is required.",
                                    "type": "invalid_request_error",
                                    "param": "prompt",
                                    "code": "missing_prompt",
                                }
                            },
                        )

                    n = _coerce_int(payload.get("n"), 1, minimum=1, maximum=4)
                    size = str(payload.get("size") or "").strip()
                    output_format = str(
                        payload.get("output_format")
                        or payload.get("response_format")
                        or "url"
                    ).strip().lower()
                    if output_format not in {"url", "b64_json", "base64", "data_url"}:
                        output_format = "url"

                    prompt_parts = [f"Generate {n} image(s) from this prompt:", prompt]
                    if size:
                        prompt_parts.append(f"Image size: {size}.")
                    prompt_parts.append("Return the generated image in the conversation, not a description.")

                    request_data = SimpleNamespace(
                        model=str(
                            payload.get("chat_model")
                            or os.getenv("OPENAI_CHAT_MODEL")
                            or "gpt-5-3"
                        ),
                        messages=[
                            {
                                "role": "user",
                                "content": "\n".join(prompt_parts),
                            }
                        ],
                        stream=False,
                        meta=SimpleNamespace(enable=True),
                        chat_mode=str(payload.get("chat_mode") or "new"),
                        chat_name=payload.get("chat_name"),
                        create_if_missing=payload.get("create_if_missing", True),
                        image_generation=True,
                        response_timeout_ms=_coerce_int(
                            payload.get("response_timeout_ms"),
                            600000,
                            minimum=30000,
                            maximum=900000,
                        ),
                    )

                    page = _find_active_openai_page()
                    if page is None:
                        raise RuntimeError("OpenAI browser page is unavailable.")

                    chat_response = await _create_completion_via_page(
                        SimpleNamespace(page=page),
                        request_data,
                        ensure_authenticated=True,
                    )
                    images = []
                    if isinstance(chat_response, dict):
                        images.extend(chat_response.get("images") or [])
                        meta = chat_response.get("meta") or {}
                        if isinstance(meta, dict):
                            images.extend(meta.get("images") or [])
                    response = _build_openai_image_response(images, output_format, n)
                    if not response["data"]:
                        raise RuntimeError("ChatGPT did not return a generated image.")
                    return response
                except Exception as exc:
                    logger.warning("Direct ChatGPT image route failed: %s", exc)
                    return JSONResponse(
                        status_code=500,
                        content={
                            "error": {
                                "message": str(exc),
                                "type": type(exc).__name__,
                                "code": "image_generation_error",
                            }
                        },
                    )

            _promote_latest_route(app, "/v1/models", {"GET"})
            _promote_latest_route(app, "/v1/chat/completions", {"POST"})
            _promote_latest_route(app, "/v1/images/generations", {"POST"})

            @app.get("/v1/images/current")
            async def codex_current_images():
                page = _find_active_openai_page()
                if page is None:
                    return {
                        "images": [],
                        "url": "",
                        "error": "OpenAI browser page is unavailable.",
                    }
                images = await _extract_recent_assistant_image_outputs(page, max_turns=20)
                if not images:
                    images = await _extract_page_image_outputs(page)
                return {
                    "images": images,
                    "url": _current_chat_url(page) or str(getattr(page, "url", "") or ""),
                }

            app._codex_current_images_route_added = True
            logger.info("Added /v1/images/current route for current ChatGPT page image extraction.")

        return result

    main.create_api = patched_create_api
    main._codex_current_images_route_patch = True


def _install_import_hook() -> None:
    original_import = builtins.__import__

    def patched_import(name, globals=None, locals=None, fromlist=(), level=0):
        module = original_import(name, globals, locals, fromlist, level)
        try:
            target = sys.modules.get("llm.provider.openai.login")
            if target is not None:
                _patch_openai_login_handler(target)
            browser_target = sys.modules.get("llm.browser.handler")
            if browser_target is not None:
                _patch_browser_handler(browser_target)
            client_target = sys.modules.get("llm.provider.openai.client")
            if client_target is not None:
                _patch_openai_client(client_target)
            core_target = sys.modules.get("llm.provider.openai.core")
            if core_target is not None:
                _patch_openai_provider(core_target)
            provider_manager_target = sys.modules.get("llm.provider_manager")
            if provider_manager_target is not None:
                _patch_provider_manager(provider_manager_target)
            chat_api_target = sys.modules.get("llm.api.chat")
            if chat_api_target is not None:
                _patch_chat_api(chat_api_target)
        except Exception as exc:
            logger.warning("Login handler import patch failed: %s", exc)
        return module

    builtins.__import__ = patched_import


def _patch_asyncio_wait_for() -> None:
    original_wait_for = asyncio.wait_for

    async def patched_wait_for(awaitable, timeout, *args, **kwargs):
        adjusted_timeout = timeout
        if timeout == 30:
            for frame in inspect.stack():
                filename = frame.filename.replace("\\", "/")
                if filename.endswith("/llm/provider/openai/client.py"):
                    adjusted_timeout = 90
                    logger.info(
                        "Extending OpenAI client wait_for timeout from 30s to 90s."
                    )
                    break
        return await original_wait_for(awaitable, adjusted_timeout, *args, **kwargs)

    asyncio.wait_for = patched_wait_for


_patch_login_button_selector()
_patch_connect_over_cdp()
_patch_main_api_routes()
_install_import_hook()
_patch_asyncio_wait_for()
