from __future__ import annotations

import importlib
import runpy
import sys
import subprocess
from urllib.parse import urlparse

_TARGET = "skeleton_media.cast.resolver"

if __name__ == "__main__":
    runpy.run_module(_TARGET, run_name="__main__")
else:
    _impl = importlib.import_module(_TARGET)

    _original_discover = _impl.discover

    def _discover_anitube_with_mirror_cooldown(page_url: str):
        page_url, host = _impl.site_registry.validate_public_url(page_url)
        host = host.lower()
        if host != "anitube.in.ua" and not host.endswith(".anitube.in.ua"):
            return _original_discover(page_url)

        parsed_page = urlparse(page_url)
        if parsed_page.path and not parsed_page.path.lower().endswith(".html"):
            canonical_path = parsed_page.path.rstrip("/") + ".html"
            page_url = parsed_page._replace(path=canonical_path).geturl()

        title = "Відео"
        poster = None
        last_document = None
        browser_error = None
        cooldown_remaining = _impl._anitube_cooldown_remaining()

        def parse_candidate(text: str):
            nonlocal title, poster, last_document
            document = _impl.lxml_html.fromstring(text)
            last_document = document
            title = _impl._page_title(document) or title
            poster = poster or _impl._page_poster(document, page_url)
            return _impl._playlist_targets(page_url, text, document)

        if not cooldown_remaining:
            try:
                playlist = parse_candidate(_impl._curl_text(page_url, headers=_impl._browser_headers(page_url)))
                if playlist:
                    return playlist, title, poster
            except _impl.OriginProtectedError:
                raise
            except _impl.BrowserChallengeError as exc:
                browser_error = exc
            except (RuntimeError, ValueError, OSError, subprocess.TimeoutExpired):
                pass

            try:
                playlist = parse_candidate(_impl._chrome_text(page_url, timeout=65))
                if playlist:
                    return playlist, title, poster
            except _impl.OriginProtectedError:
                raise
            except _impl.BrowserChallengeError as exc:
                # A normal JS challenge is not the same as a hard origin block.
                # Preserve the mirror path instead of starting a cooldown.
                browser_error = exc
            except (ValueError, OSError, subprocess.TimeoutExpired):
                pass

        try:
            playlist = parse_candidate(_impl._public_html_mirror(page_url, timeout=55))
            if playlist:
                return playlist, title, poster
        except _impl.OriginProtectedError:
            raise
        except (RuntimeError, ValueError, OSError, subprocess.TimeoutExpired):
            pass

        if last_document is not None:
            found = _impl._generic_embed_scan(last_document, page_url)
            if found:
                return found, title, poster
        if cooldown_remaining:
            raise _impl.OriginProtectedError(url=page_url, cooldown_remaining_seconds=cooldown_remaining)
        if browser_error is not None:
            raise browser_error
        raise RuntimeError("AniTube не повернув доступних відеопотоків.")

    _impl.discover = _discover_anitube_with_mirror_cooldown
    sys.modules[__name__] = _impl
