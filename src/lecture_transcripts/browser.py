import math
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qs, urlsplit


class MediaDiscoveryError(Exception):
    pass


@dataclass(frozen=True)
class BrowserSource:
    url: str = field(repr=False)
    page_url: str = field(repr=False)


def signed_master_url(value: object, now: float | None = None) -> str | None:
    if not isinstance(value, str) or any(character.isspace() for character in value):
        return None
    try:
        parsed = urlsplit(value)
        parameters = parse_qs(parsed.query)
        expires_values = parameters.get("rvctokenendtime", [])
        hash_values = parameters.get("rvctokenhash", [])
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or not parsed.hostname.lower().endswith(".ust.hk")
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port not in (None, 443)
            or "/rvcsecured/" not in parsed.path
            or not parsed.path.endswith("/playlist.m3u8")
            or len(expires_values) != 1
            or len(hash_values) != 1
            or not hash_values[0]
        ):
            return None
        expires = float(expires_values[0])
    except ValueError:
        return None
    if not math.isfinite(expires) or expires <= (time.time() if now is None else now):
        return None
    return value


PLAYER_SOURCES = """() => {
    const sources = [];
    const add = value => {
        if (typeof value !== 'string' || !value) return;
        try { sources.push(new URL(value, location.href).href); } catch {}
    };
    try {
        if (typeof window.jwplayer === 'function') {
            for (const item of window.jwplayer().getPlaylist() || []) {
                add(item.file);
                for (const source of item.sources || []) add(source.file);
            }
        }
    } catch {}
    for (const video of document.querySelectorAll('video, video source')) {
        add(video.currentSrc || video.src);
    }
    return sources;
}"""


def default_profile_dir() -> Path:
    state_home = Path(
        os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")
    )
    return state_home / "lecture-transcripts" / "browser"


def wait_for_source(
    context,
    page,
    timeout: int | None = None,
    start_url: str | None = None,
) -> BrowserSource:
    owned_pages = [page]
    responses = []
    deadline = None if timeout is None else time.monotonic() + timeout

    def track_popup(popup):
        owned_pages.append(popup)
        popup.on("popup", track_popup)

    def observe_response(response):
        try:
            owner = response.request.frame.page
            if response.status == 200 and owner in owned_pages:
                candidate = signed_master_url(response.url)
                if candidate is not None:
                    responses.append(BrowserSource(candidate, owner.url))
        except Exception:
            pass

    page.on("popup", track_popup)
    context.on("response", observe_response)
    try:
        if start_url is not None:
            try:
                page.goto(start_url, wait_until="domcontentloaded", timeout=30000)
            except Exception:
                print(
                    "The initial navigation is still loading or failed; you can navigate to the lecture manually.",
                    flush=True,
                )
            page.bring_to_front()
        while True:
            open_pages = [owned for owned in owned_pages if not owned.is_closed()]
            if not open_pages:
                raise MediaDiscoveryError(
                    "The browser was closed before a lecture source was found."
                )
            for owned in open_pages:
                for frame in owned.frames:
                    try:
                        candidates = frame.evaluate(PLAYER_SOURCES)
                    except Exception:
                        continue
                    if not isinstance(candidates, list):
                        continue
                    for value in candidates:
                        candidate = signed_master_url(value)
                        if candidate is not None:
                            return BrowserSource(candidate, owned.url)
            for source in reversed(responses):
                if signed_master_url(source.url) is not None:
                    return source
            if deadline is not None and time.monotonic() >= deadline:
                raise MediaDiscoveryError(
                    "Timed out waiting for a lecture source. Complete SSO/MFA and open the recording."
                )
            try:
                open_pages[0].wait_for_timeout(250)
            except Exception:
                if all(owned.is_closed() for owned in owned_pages):
                    raise MediaDiscoveryError(
                        "The browser was closed before a lecture source was found."
                    ) from None
                raise MediaDiscoveryError(
                    "The helper browser connection was lost. Cancel and try again."
                ) from None
    finally:
        context.remove_listener("response", observe_response)


def get_browser_source(
    start_url: str = "https://canvas.ust.hk/",
    profile_dir: Path | None = None,
    timeout: int | None = None,
    *,
    sync_playwright_factory=None,
) -> BrowserSource:
    if sync_playwright_factory is None:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise MediaDiscoveryError(
                "Browser support is optional. Install it with `uv pip install --python .venv/bin/python -e '.[browser]'`, "
                "then run `.venv/bin/python -m playwright install chromium`."
            ) from None
        sync_playwright_factory = sync_playwright
    profile = (profile_dir or default_profile_dir()).expanduser().resolve()
    profile.mkdir(parents=True, exist_ok=True, mode=0o700)
    profile.chmod(0o700)
    print(
        "Opening a dedicated browser. Complete Microsoft SSO/MFA there, then open the lecture and press Play if needed.",
        flush=True,
    )
    print(
        "Waiting for an unexpired HKUST RVC source; no password or cookie export is required. Close the browser or press Ctrl+C to cancel.",
        flush=True,
    )
    try:
        with sync_playwright_factory() as playwright:
            context = playwright.chromium.launch_persistent_context(
                user_data_dir=str(profile),
                headless=False,
            )
            try:
                page = context.new_page()
                source = wait_for_source(context, page, timeout, start_url)
                print(
                    "Lecture source captured. Closing the helper browser and continuing the clip.",
                    flush=True,
                )
                return source
            finally:
                context.close()
    except MediaDiscoveryError:
        raise
    except Exception:
        raise MediaDiscoveryError(
            "Could not use the helper browser. Install Chromium with `.venv/bin/python -m playwright install chromium`, "
            "check that a graphical session is available, and close other helpers using this profile."
        ) from None
