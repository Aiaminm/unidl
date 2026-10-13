"""Eternal Family Android TV API client.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

import requests

APP_PACKAGE = "tv.uscreen.eternalfamily2"
APP_NAME = "Eternal Family"
APP_VERSION = "2.11.0"
VERSION_CODE = 21110006
STORE_ID = 8334
STORE_TOKEN = "r4hyaRv7kwXy7g=="
ORIGIN = "eternaltv"

API_BASE = "https://api.uscreen.io/api/v2"
STOREFRONT_BASE = "https://eternal.tv"
LIVE_CATEGORY_ID = 269934
TOKEN_FILE = "eternalfamily_token.json"

USER_AGENT = "Dart/3.12 (dart:io)"
TV_USER_AGENT = "tv.uscreen.eternalfamily2/2.11.0 (Android TV; en)"

TV_POLL_INTERVAL = 3.0
TV_POLL_TIMEOUT = 600.0


class EternalFamilyError(RuntimeError):
    """Base error for Eternal Family service."""


class EternalFamilyAuthError(EternalFamilyError):
    """Authentication or authorization error."""


class EternalFamilyCodeExpired(EternalFamilyAuthError):
    """Pairing code expired before activation."""


class EternalFamilyAccessDenied(EternalFamilyError):
    """Content requires an active subscription or purchase."""


@dataclass
class TvChallenge:
    code: str
    code_id: str
    url: str = f"{STOREFRONT_BASE}/connect"
    qr_url: str = ""
    expires_in: float = TV_POLL_TIMEOUT


@dataclass
class PlaybackInfo:
    manifest_url: str
    is_live: bool = False
    drm_system: str | None = None
    license_url: str | None = None


@dataclass
class Session:
    access_token: str = ""
    refresh_token: str = ""
    email: str = ""
    name: str = ""
    user_id: int | str = ""
    roles: list[str] = field(default_factory=list)
    subscribed: bool = False
    logged_in: bool = False
    last_verified: float = 0.0

    @classmethod
    def from_cache(cls, data: dict[str, Any] | None) -> Session:
        if not isinstance(data, dict):
            return cls()
        return cls(
            access_token=str(data.get("access_token") or ""),
            refresh_token=str(data.get("refresh_token") or ""),
            email=str(data.get("email") or ""),
            name=str(data.get("name") or ""),
            user_id=data.get("user_id") or "",
            roles=list(data.get("roles") or []),
            subscribed=bool(data.get("subscribed", False)),
            logged_in=bool(data.get("logged_in") or data.get("access_token")),
            last_verified=float(data.get("last_verified") or 0.0),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "email": self.email,
            "name": self.name,
            "user_id": self.user_id,
            "roles": self.roles,
            "subscribed": self.subscribed,
            "logged_in": self.logged_in,
            "last_verified": self.last_verified,
        }

    @property
    def signed_in(self) -> bool:
        return bool(self.logged_in or self.access_token)

    def label(self) -> str:
        if self.email:
            return self.email
        if self.name:
            return self.name
        if self.access_token:
            return f"token · {self.access_token[:8]}…"
        return "signed in"


@dataclass
class ProgramItem:
    id: int | str
    title: str
    description: str = ""
    short_description: str = ""
    duration: str = ""
    duration_seconds: int | None = None
    content_type: str = "video"
    active_stream: bool = False
    main_poster: str = ""
    permalink: str = ""
    url: str = ""
    hls_url: str = ""
    categories: list[int] = field(default_factory=list)
    timecodes: list[dict[str, Any]] = field(default_factory=list)
    attachments: list[dict[str, Any]] = field(default_factory=list)

    @property
    def is_live(self) -> bool:
        return (
            self.content_type == "live_event"
            or self.permalink.startswith("channel")
            or LIVE_CATEGORY_ID in self.categories
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ProgramItem:
        hls = str(data.get("slider_video_hls") or data.get("hls") or "")
        raw_dur = data.get("duration")
        dur_str = str(raw_dur) if raw_dur is not None else ""
        dur_secs = data.get("duration_in_seconds")
        if dur_secs is None and isinstance(raw_dur, (int, float)):
            dur_secs = int(raw_dur)

        return cls(
            id=data.get("id") or "",
            title=str(data.get("title") or "Untitled").strip(),
            description=clean_html(str(data.get("description") or "")),
            short_description=str(data.get("short_description") or "").strip(),
            duration=dur_str,
            duration_seconds=dur_secs,
            content_type=str(data.get("content_type") or "video"),
            active_stream=bool(data.get("active_stream", False)),
            main_poster=str(
                data.get("preview")
                or data.get("main_poster_featured")
                or data.get("main_poster")
                or data.get("player_poster_image")
                or ""
            ),
            permalink=str(data.get("permalink") or ""),
            url=str(data.get("url") or ""),
            hls_url=hls,
            categories=list(data.get("categories") or []),
            timecodes=list(data.get("timecodes") or data.get("chapters") or []),
            attachments=list(data.get("attachments") or data.get("files") or data.get("downloadable_resources") or []),
        )


@dataclass
class CategoryItem:
    id: int
    title: str
    permalink: str = ""
    position: int = 0

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CategoryItem:
        return cls(
            id=int(data.get("id") or 0),
            title=str(data.get("title") or "").strip(),
            permalink=str(data.get("permalink") or ""),
            position=int(data.get("position") or 0),
        )


def clean_html(raw: str) -> str:
    """Strip basic HTML markup into readable text."""
    if not raw:
        return ""
    text = re.sub(r"<br\s*/?>", "\n", raw, flags=re.IGNORECASE)
    text = re.sub(r"</p>", "\n\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def extract_permalink(target: str) -> str | None:
    """Extract program permalink / slug from target URL or ID."""
    clean = (target or "").strip()
    if not clean:
        return None

    # Handle custom scheme e.g. eternalfamily:programs/channel1 or eternalfamily:dance-or-die
    if clean.startswith("eternalfamily:"):
        rest = clean.split(":", 1)[1].strip().lstrip("/")
        if rest.startswith("programs/"):
            rest = rest.split("programs/", 1)[1]
        elif rest.startswith("catalog/"):
            rest = rest.split("catalog/", 1)[1]
        return rest.split("/")[0].split("?")[0]

    # Handle full HTTP URL
    if "eternal.tv" in clean.lower():
        parsed = urlparse(clean)
        path = parsed.path.strip("/")
        parts = path.split("/")
        if len(parts) >= 2 and parts[0] in ("programs", "catalog"):
            return parts[1]
        if len(parts) == 1 and parts[0]:
            return parts[0]

    # Handle bare permalink or numeric ID string
    if re.match(r"^[a-zA-Z0-9_-]+$", clean):
        return clean

    return None


class EternalFamilyApi:
    """Network client for Eternal Family Android TV platform."""

    def __init__(
        self,
        session: requests.Session | None = None,
        state: Session | None = None,
        on_save: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.http = session or requests.Session()
        self.state = state or Session()
        self._on_save = on_save

    def _save(self, state: Session) -> None:
        self.state = state
        if self._on_save:
            self._on_save(state.as_dict())

    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers = {
            "X-Store-Token": STORE_TOKEN,
            "X-App-Platform": "android_tv",
            "X-App-Version": APP_VERSION,
            "X-Locale": "en",
            "X-Features-Supported": "show_coming_soon_videos_in_catalog",
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        }
        if self.state.access_token:
            headers["Authorization"] = f"Bearer {self.state.access_token}"
        if extra:
            headers.update(extra)
        return headers

    # -------------------------------------------------------------------------
    # Authentication & TV Code Pairing (Android TV APK Aligned)
    # -------------------------------------------------------------------------

    def start_tv_code(self) -> TvChallenge:
        """Start a TV code pairing session.

        Requests an authentic pairing code from api.uscreen.io/api/v2/sessions/code.
        Never generates synthetic codes.
        """
        try:
            resp = self.http.post(
                f"{API_BASE}/sessions/code",
                headers=self._headers(),
                timeout=15.0,
            )
            if resp.status_code in (200, 201):
                data = resp.json() or {}
                code = str(data.get("code") or "").strip()
                if code:
                    activate_url = str(data.get("activate_url") or f"{STOREFRONT_BASE}/connect")
                    qr_url = str(data.get("qr_url") or f"{activate_url}?code={code}")
                    return TvChallenge(
                        code=code,
                        code_id=str(data.get("code_id") or code),
                        url=activate_url,
                        qr_url=qr_url,
                        expires_in=TV_POLL_TIMEOUT,
                    )
            raise EternalFamilyAuthError(
                f"Failed to obtain TV code from server: HTTP {resp.status_code} {resp.text}"
            )
        except EternalFamilyError:
            raise
        except Exception as exc:
            raise EternalFamilyAuthError(f"Network error requesting TV code: {exc}") from exc

    def poll_tv_code(self, challenge: TvChallenge) -> Session | None:
        """Check whether the TV pairing code has been authorized on the web."""
        try:
            resp = self.http.post(
                f"{API_BASE}/sessions/code_access",
                headers=self._headers({"Content-Type": "application/json"}),
                json={"code": challenge.code, "device": "Android TV"},
                timeout=10.0,
            )
            # 422 means authorization is pending user confirmation on web
            if resp.status_code == 422:
                return None
            if resp.status_code == 200:
                data = resp.json() or {}
                user_data = data.get("user") or {}
                sub_data = data.get("subscription") or {}
                auth_data = data.get("auth") or {}
                token = str(auth_data.get("access_token") or data.get("access_token") or "")
                if token:
                    session = Session(
                        access_token=token,
                        refresh_token=str(auth_data.get("refresh_token") or data.get("refresh_token") or ""),
                        email=str(user_data.get("email") or ""),
                        name=str(user_data.get("name") or ""),
                        user_id=user_data.get("id") or "",
                        roles=[user_data.get("role")] if user_data.get("role") else [],
                        subscribed=bool(user_data.get("subscribed") or sub_data.get("active")),
                        logged_in=True,
                        last_verified=time.time(),
                    )
                    self._save(session)
                    return session
        except Exception:
            pass
        return None

    def ensure_session(self, force: bool = False) -> Session:
        """Validate cached session or refresh token if needed."""
        now = time.time()
        if not force and self.state.signed_in and (now - self.state.last_verified < 3600):
            return self.state

        if not self.state.signed_in:
            return self.state

        try:
            resp = self.http.get(
                f"{API_BASE}/sessions/_",
                headers=self._headers(),
                timeout=10.0,
            )
            if resp.status_code == 200:
                data = resp.json() or {}
                user_data = data.get("user") or {}
                sub_data = data.get("subscription") or {}
                self.state.logged_in = True
                self.state.email = str(user_data.get("email") or self.state.email)
                self.state.name = str(user_data.get("name") or self.state.name)
                self.state.user_id = user_data.get("id") or self.state.user_id
                self.state.subscribed = bool(user_data.get("subscribed") or sub_data.get("active"))
                self.state.last_verified = now
                self._save(self.state)
                return self.state
        except Exception:
            pass

        # Try refresh token if available
        if self.state.refresh_token:
            return self.refresh_tokens()

        return self.state

    def refresh_tokens(self) -> Session:
        """Exchange refresh token for a new access token."""
        if not self.state.refresh_token:
            raise EternalFamilyAuthError("No refresh token available")

        try:
            resp = self.http.post(
                f"{API_BASE}/sessions/refresh",
                headers=self._headers({"Content-Type": "application/json"}),
                json={"refresh_token": self.state.refresh_token},
                timeout=10.0,
            )
            if resp.status_code == 200:
                data = resp.json() or {}
                auth_data = data.get("auth") or data
                self.state.access_token = str(auth_data.get("access_token") or self.state.access_token)
                self.state.refresh_token = str(auth_data.get("refresh_token") or self.state.refresh_token)
                self.state.last_verified = time.time()
                self._save(self.state)
                return self.state
            raise EternalFamilyAuthError(f"Token refresh was denied by server (HTTP {resp.status_code})")
        except EternalFamilyError:
            raise
        except Exception as exc:
            raise EternalFamilyAuthError(f"Failed to refresh session token: {exc}") from exc

    def logout(self) -> None:
        """Sign out and invalidate current session."""
        try:
            self.http.post(f"{API_BASE}/sessions/logout", headers=self._headers(), timeout=5.0)
        except Exception:
            pass
        self.state = Session()
        if self._on_save:
            self._on_save({})

    # -------------------------------------------------------------------------
    # Catalog, Programs & Search
    # -------------------------------------------------------------------------

    def categories(self) -> list[CategoryItem]:
        """Fetch platform categories."""
        try:
            resp = self.http.get(
                f"{API_BASE}/categories",
                headers=self._headers(),
                params={"page": 1, "per_page": 50},
                timeout=15.0,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            raise EternalFamilyError(f"Failed to fetch categories: {exc}") from exc

        raw_list = data if isinstance(data, list) else data.get("categories", [])
        if not isinstance(raw_list, list):
            return []
        items = [CategoryItem.from_dict(item) for item in raw_list if isinstance(item, dict)]
        return sorted(items, key=lambda c: c.position)

    def category_contents(self, category_id: int, page: int = 1) -> list[ProgramItem]:
        """Fetch programs in a category."""
        try:
            resp = self.http.get(
                f"{API_BASE}/contents/search",
                headers=self._headers(),
                params={"category_id": category_id, "page": page},
                timeout=15.0,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            raise EternalFamilyError(f"Failed to fetch category {category_id} contents: {exc}") from exc

        raw_list = data if isinstance(data, list) else data.get("contents", [])
        if not isinstance(raw_list, list):
            return []
        return [ProgramItem.from_dict(item) for item in raw_list if isinstance(item, dict)]

    def search(self, query: str, page: int = 1) -> list[ProgramItem]:
        """Search contents by keyword."""
        clean_q = (query or "").strip()
        try:
            resp = self.http.get(
                f"{API_BASE}/contents/search",
                headers=self._headers(),
                params={"query": clean_q, "page": page},
                timeout=15.0,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            raise EternalFamilyError(f"Failed to search for '{query}': {exc}") from exc

        raw_list = data if isinstance(data, list) else data.get("contents", [])
        if not isinstance(raw_list, list):
            return []
        return [ProgramItem.from_dict(item) for item in raw_list if isinstance(item, dict)]

    def program_detail(self, permalink: str) -> ProgramItem:
        """Fetch full program details by permalink / slug or numeric id."""
        clean_slug = (permalink or "").strip()
        if not clean_slug:
            raise EternalFamilyError("A valid program permalink or slug is required")

        try:
            resp = self.http.get(
                f"{API_BASE}/contents/{clean_slug}",
                headers=self._headers(),
                timeout=15.0,
            )
            if resp.status_code == 404:
                # Try finding in search
                hits = self.search(clean_slug)
                for h in hits:
                    if h.permalink == clean_slug or str(h.id) == clean_slug:
                        return h
                raise EternalFamilyError(f"Program not found: {clean_slug}")
            resp.raise_for_status()
            data = resp.json()
        except EternalFamilyError:
            raise
        except Exception as exc:
            raise EternalFamilyError(f"Failed to fetch program {clean_slug}: {exc}") from exc

        if not isinstance(data, dict):
            raise EternalFamilyError(f"Invalid response for program {clean_slug}")

        content_dict = data.get("content") if isinstance(data.get("content"), dict) else data
        item = ProgramItem.from_dict(content_dict)
        if "files" in data and isinstance(data["files"], list) and data["files"]:
            item.attachments = list(data["files"])
        return item

    def live_channels(self) -> list[ProgramItem]:
        """Fetch 24/7 live channels."""
        return self.category_contents(LIVE_CATEGORY_ID)

    def resolve_playback_info(self, item: ProgramItem) -> PlaybackInfo:
        """Resolve Mux HLS stream manifest URL and DRM info for a program or live channel."""
        # 1. Direct stream already on the item
        if item.hls_url:
            is_live = bool(item.content_type == "live_event")
            return PlaybackInfo(manifest_url=item.hls_url, is_live=is_live)

        # 2. Ensure numeric content ID is resolved without recursion
        content_id = item.id
        if not content_id and item.permalink:
            try:
                detail = self.program_detail(item.permalink)
                if detail.hls_url:
                    is_live = bool(detail.content_type == "live_event")
                    return PlaybackInfo(manifest_url=detail.hls_url, is_live=is_live)
                if detail.id:
                    content_id = detail.id
            except Exception:
                pass

        if not content_id:
            raise EternalFamilyError(f"Cannot determine numeric content ID for '{item.title}'")

        # 3. Call authentic playlist endpoint: /contents/{id}/playlist?inject_closed_captions=1
        try:
            resp = self.http.get(
                f"{API_BASE}/contents/{content_id}/playlist",
                headers=self._headers(),
                params={"inject_closed_captions": 1},
                timeout=15.0,
            )
        except Exception as exc:
            raise EternalFamilyError(f"Failed to fetch playlist for '{item.title}': {exc}") from exc

        if resp.status_code == 200:
            playlist = resp.json()
            if isinstance(playlist, list) and playlist:
                first = playlist[0]
                if isinstance(first, dict):
                    hls = first.get("hls")
                    preview = first.get("preview") or {}
                    preview_hls = preview.get("hls_url") if isinstance(preview, dict) else None
                    manifest = str(hls or preview_hls or "")

                    if manifest:
                        drm_options = first.get("drm_options") or {}
                        license_url = None
                        drm_system = None
                        if isinstance(drm_options, dict):
                            wv = drm_options.get("com.widevine.alpha")
                            if wv:
                                drm_system = "widevine"
                                license_url = str(wv)

                        is_live = bool(item.content_type == "live_event")
                        return PlaybackInfo(
                            manifest_url=manifest,
                            is_live=is_live,
                            drm_system=drm_system,
                            license_url=license_url,
                        )

            raise EternalFamilyAccessDenied(
                f"No video stream found in playlist response for '{item.title}'."
            )

        if resp.status_code == 403:
            try:
                err_data = resp.json()
                if isinstance(err_data, dict):
                    code = str(err_data.get("code") or "").lower()
                    msg = str(err_data.get("message") or "")
                    if code == "geo" or "country" in msg.lower():
                        raise EternalFamilyAccessDenied(
                            f"'{item.title}' is geoblocked in your region (HTTP 403 geo). "
                            "Allowed countries: US, CA. Please configure a US or CA proxy in UniDL."
                        )
            except (ValueError, KeyError):
                pass
            raise EternalFamilyAccessDenied(
                f"Access denied for '{item.title}' (HTTP 403). "
                "An active Eternal Family subscription or TV sign-in may be required."
            )

        if resp.status_code == 401:
            raise EternalFamilyAccessDenied(
                f"Authentication required for '{item.title}' (HTTP 401). "
                "Please sign in via TV code."
            )

        if resp.status_code == 404:
            raise EternalFamilyError(
                f"Content '{item.title}' (ID {content_id}) not found on server (HTTP 404)."
            )

        raise EternalFamilyError(
            f"Failed to resolve playback for '{item.title}' (HTTP {resp.status_code})"
        )

    def resolve_stream(self, item: ProgramItem) -> str:
        """Resolve Mux HLS stream manifest URL for a program or live channel."""
        return self.resolve_playback_info(item).manifest_url

    def widevine_license(self, license_url: str, challenge: bytes) -> bytes:
        """Exchange Widevine challenge for license with Mux license server."""
        try:
            resp = self.http.post(
                license_url,
                data=challenge,
                headers={"Content-Type": "application/octet-stream"},
                timeout=15.0,
            )
            resp.raise_for_status()
            return resp.content
        except Exception as exc:
            raise EternalFamilyError(f"Failed to acquire Widevine license: {exc}") from exc
