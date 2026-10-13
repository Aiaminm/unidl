"""DAFilms Android TV client."""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlparse

import requests

from ...core.attachments import Attachment
from ...core.chapters import Chapter

APP_PACKAGE = "com.abuco.dafilms"
APP_NAME = "DAFilms"
APP_VERSION = "1.18"
VERSION_CODE = 18
WEBAPP_VERSION = "0.0.1"

AZURE_HOST = "icy-river-00ecf4903.5.azurestaticapps.net"
AZURE_STATIC_URL = f"https://{AZURE_HOST}/indexAndroidTV.html?3"

DEFAULT_REGION = "global"

REGION_DOMAINS: dict[str, str] = {
    "global": "dafilms.com",
    "cz": "dafilms.cz",
    "sk": "dafilms.sk",
}

API_BASE_URLS: dict[str, str] = {
    "global": "https://dafilms.com/api/",
    "cz": "https://dafilms.cz/api/",
    "sk": "https://dafilms.sk/api/",
}

API_LANGUAGES: dict[str, str] = {
    "global": "en",
    "cz": "cs",
    "sk": "sk",
}

USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 10; BRAVIA 4K Build/QTG3.200305.006.S284; wv) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/124.0.6367.82 "
    f"Safari/537.36 {APP_PACKAGE}/{APP_VERSION}"
)

TOKEN_FILE = "dafilms_token.json"
TV_POLL_INTERVAL = 2.0  # 2000ms aligned with APK loginTimer
TV_POLL_TIMEOUT = 900.0  # 15 minutes aligned with APK refreshTimer


class DAFilmsError(Exception):
    """Base error for DAFilms."""


class DAFilmsAuthError(DAFilmsError):
    """Authentication or token error."""


class DAFilmsCodeExpired(DAFilmsError):
    """TV code login challenge expired."""


class DAFilmsAccessDenied(DAFilmsError):
    """User lacks entitlement or subscription for content."""


class DAFilmsNotFound(DAFilmsError):
    """Requested content was not found."""


def normalize_region(value: object) -> str:
    key = str(value or "").strip().lower()
    if key in ("cz", "cs", "czech", "dafilms.cz", "https://dafilms.cz"):
        return "cz"
    if key in ("sk", "slovak", "slovakia", "dafilms.sk", "https://dafilms.sk"):
        return "sk"
    return "global"


def domain_for(region: str) -> str:
    return REGION_DOMAINS.get(normalize_region(region), "dafilms.com")


def api_base_for(region: str) -> str:
    return API_BASE_URLS.get(normalize_region(region), "https://dafilms.com/api/")


def language_for(region: str) -> str:
    return API_LANGUAGES.get(normalize_region(region), "en")


def extract_film_id(target: str) -> int | None:
    text = str(target or "").strip()
    if not text:
        return None
    if text.isdigit():
        return int(text)
    m = re.match(r"^dafilms:(?:film/)?(\d+)$", text, re.I)
    if m:
        return int(m.group(1))
    parsed = urlparse(text)
    path = parsed.path.strip("/")
    m = re.match(r"^film/(\d+)(?:-[^/?#]+)?$", path, re.I)
    if m:
        return int(m.group(1))
    # Query parameters fallback e.g. ?id=19101
    query = parse_qs(parsed.query)
    if "id" in query and query["id"][0].isdigit():
        return int(query["id"][0])
    return None


def extract_collection_id(target: str) -> int | None:
    text = str(target or "").strip()
    if not text:
        return None
    m = re.match(
        r"^dafilms:(?:collection|film-selection|program|selection|special)/(\d+)$",
        text,
        re.I,
    )
    if m:
        return int(m.group(1))
    parsed = urlparse(text)
    path = parsed.path.strip("/")
    m = re.match(
        r"^(?:collection|film-selection|program|selection|special)/(\d+)(?:-[^/?#]+)?$",
        path,
        re.I,
    )
    if m:
        return int(m.group(1))
    return None


def subtitle_selected(setting: str, language: str) -> bool:
    requested = [s.strip().lower() for s in (setting or "all").split(",") if s.strip()]
    if not requested or "all" in requested:
        return True
    if "none" in requested:
        return False
    lang = (language or "").lower()
    base = lang.split("-", 1)[0]
    return lang in requested or base in requested


@dataclass(frozen=True, slots=True)
class TvChallenge:
    access_code: str
    access_code_hash: str
    qr_link: str
    expiration_minutes: int = 15
    timeout: float = TV_POLL_TIMEOUT
    device_id: str = ""


@dataclass
class Session:
    token: str = ""
    language: str = "en"
    name: str = ""
    subscriber: bool = False
    expiration: str = ""
    login_status: bool = False
    region: str = "global"
    last_verified: float = 0.0

    @property
    def signed_in(self) -> bool:
        return bool(self.token.strip())

    def label(self) -> str:
        parts: list[str] = []
        if self.name:
            parts.append(self.name)
        if self.subscriber:
            parts.append("subscriber")
        elif self.signed_in:
            parts.append("signed in")
        return " · ".join(parts) if parts else "signed in"

    def as_dict(self) -> dict[str, Any]:
        return {
            "token": self.token,
            "language": self.language,
            "name": self.name,
            "subscriber": self.subscriber,
            "expiration": self.expiration,
            "login_status": self.login_status,
            "region": self.region,
            "last_verified": self.last_verified,
        }

    @classmethod
    def from_cache(cls, data: dict[str, Any] | None, region: str = "global") -> Session:
        if not isinstance(data, dict):
            return cls(region=normalize_region(region))
        return cls(
            token=str(data.get("token") or "").strip(),
            language=str(data.get("language") or language_for(region)).strip(),
            name=str(data.get("name") or "").strip(),
            subscriber=bool(data.get("subscriber")),
            expiration=str(data.get("expiration") or "").strip(),
            login_status=bool(data.get("login_status", bool(data.get("token")))),
            region=normalize_region(data.get("region") or region),
            last_verified=float(data.get("last_verified") or 0.0),
        )


@dataclass(frozen=True, slots=True)
class FilmItem:
    id: int
    title: str
    short_description: str = ""
    description: str = ""
    duration_ms: int = 0
    url: str = ""
    image: str = ""
    directors: tuple[str, ...] = ()
    price: str = ""
    svod: bool = False
    tvod: bool = False
    fvod: bool = False
    public: bool = True
    year: int | None = None
    country: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FilmItem:
        directors = data.get("directors")
        if isinstance(directors, list):
            dirs = tuple(str(d).strip() for d in directors if str(d).strip())
        elif isinstance(directors, str):
            dirs = (directors.strip(),)
        else:
            dirs = ()

        year: int | None = None
        country: str = ""
        attrs = data.get("attributes") or {}
        if isinstance(attrs, dict) and "country_with_year" in attrs:
            cwy = attrs["country_with_year"]
            if isinstance(cwy, list):
                if len(cwy) > 0 and cwy[0]:
                    country = str(cwy[0]).strip()
                if len(cwy) > 1 and str(cwy[1]).isdigit():
                    year = int(cwy[1])
        tags = data.get("tags") or {}
        if year is None and isinstance(tags, dict) and "country_with_year" in tags:
            cwy = tags["country_with_year"]
            if isinstance(cwy, list):
                if len(cwy) > 0 and cwy[0] and not country:
                    country = str(cwy[0]).strip()
                if len(cwy) > 1 and str(cwy[1]).isdigit():
                    year = int(cwy[1])

        return cls(
            id=int(data.get("id") or 0),
            title=str(data.get("title") or "").strip(),
            short_description=str(data.get("shortDescription") or "").strip(),
            description=str(data.get("description") or "").strip(),
            duration_ms=int(data.get("duration") or 0),
            url=str(data.get("url") or "").strip(),
            image=str(data.get("image") or "").strip(),
            directors=dirs,
            price=str(data.get("price") or "").strip(),
            svod=bool(data.get("svod")),
            tvod=bool(data.get("tvod")),
            fvod=bool(data.get("fvod")),
            public=bool(data.get("public", True)),
            year=year,
            country=country,
        )


@dataclass(frozen=True, slots=True)
class PlaybackSource:
    size: str
    src: str


@dataclass(frozen=True, slots=True)
class SubtitleTrack:
    label: str
    srclang: str
    src: str
    default: bool = False
    kind: str = "subtitles"


@dataclass(slots=True)
class PlaybackInfo:
    film: dict[str, Any]
    playlist: list[PlaybackSource]
    tracks: list[SubtitleTrack]
    chapters: list[Chapter] = field(default_factory=list)
    attachments: list[Attachment] = field(default_factory=list)


class DAFilmsApi:
    """DAFilms HTTP API wrapper."""

    def __init__(
        self,
        session: requests.Session,
        region: str = DEFAULT_REGION,
        state: Session | None = None,
        on_save: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.session = session
        self.region = normalize_region(region)
        self.state = state or Session(region=self.region)
        self.on_save = on_save

    @property
    def api_base(self) -> str:
        return api_base_for(self.region)

    @property
    def language(self) -> str:
        return language_for(self.region)

    def _auth_header_value(self) -> str:
        """APK format: 'Bearer Bearer ' + token."""
        token = self.state.token.strip()
        if not token:
            return ""
        if token.startswith("Bearer "):
            return f"Bearer {token}"
        return f"Bearer Bearer {token}"

    def _headers(self, auth: bool = True) -> dict[str, str]:
        headers: dict[str, str] = {
            "User-Agent": USER_AGENT,
            "Accept": "application/json, text/plain, */*",
            "Access-Control-Allow-Origin": "*",
            "Referer": AZURE_STATIC_URL,
            "Origin": f"https://{AZURE_HOST}",
        }
        if auth and self.state.signed_in:
            headers["Authorization"] = self._auth_header_value()
        return headers

    def _request(
        self,
        method: str,
        endpoint: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        auth: bool = True,
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        url = self.api_base + endpoint.lstrip("/")
        req_params: dict[str, Any] = {"language": self.language}
        if params:
            req_params.update(params)

        try:
            resp = self.session.request(
                method,
                url,
                params=req_params,
                json=json,
                headers=self._headers(auth=auth),
                timeout=timeout,
            )
        except requests.RequestException as exc:
            raise DAFilmsError(f"HTTP request to {url} failed: {exc}") from exc

        if resp.status_code == 404:
            raise DAFilmsNotFound(f"Resource {endpoint} not found (404)")

        try:
            data = resp.json()
        except ValueError as exc:
            raise DAFilmsError(f"Invalid JSON response from {url}: {resp.text[:200]}") from exc

        return data

    def start_tv_code(self) -> TvChallenge:
        """Initiate TV code login aligned with APK Code.js."""
        data = self._request("GET", "code", auth=False)
        payload = data.get("data") or {}
        access_code = payload.get("accessCode")

        # In APK Code.js: if accessCode is null, call code?action=refresh
        if not access_code:
            data = self._request("GET", "code", params={"action": "refresh"}, auth=False)
            payload = data.get("data") or {}
            access_code = payload.get("accessCode")

        if not payload or not payload.get("accessCodeHash"):
            err = (data.get("error") or {}).get("errorMessage") or "Unknown error"
            raise DAFilmsError(f"Failed to obtain DAFilms TV code: {err}")

        exp_raw = payload.get("accessCodeExpiration") or "15"
        try:
            exp_minutes = int(exp_raw)
        except (ValueError, TypeError):
            exp_minutes = 15
        timeout = float(exp_minutes * 60)

        return TvChallenge(
            access_code=str(access_code or "").strip(),
            access_code_hash=str(payload.get("accessCodeHash") or "").strip(),
            qr_link=str(payload.get("qrLink") or "").strip(),
            expiration_minutes=exp_minutes,
            timeout=timeout,
            device_id=str(payload.get("deviceId") or "").strip(),
        )

    def poll_tv_code(self, challenge: TvChallenge) -> Session | None:
        """Poll TV code status aligned with APK Code.js."""
        endpoint = f"auth/devices/code/login?code={challenge.access_code_hash}"
        data = self._request("POST", endpoint, auth=False)
        status = bool(data.get("status"))

        if not status:
            error = data.get("error") or {}
            msg = str(error.get("errorMessage") or error.get("errorUserText") or "")
            if "not authorized" in msg.lower():
                return None
            if "expired" in msg.lower():
                raise DAFilmsCodeExpired("The TV pairing code has expired.")
            return None

        user_data = data.get("data") or {}
        token = str(user_data.get("token") or "").strip()
        if not token:
            return None

        session = Session(
            token=token,
            language=str(user_data.get("language") or self.language).strip(),
            name=str(user_data.get("name") or "").strip(),
            subscriber=bool(user_data.get("subscriber")),
            expiration=str(user_data.get("expiration") or "").strip(),
            login_status=True,
            region=self.region,
            last_verified=time.time(),
        )
        self.state = session
        if self.on_save:
            self.on_save(session.as_dict())
        return session

    def ensure_session(self, force: bool = False) -> Session | None:
        """Validate and refresh active user session."""
        if not self.state.signed_in:
            return None

        now = time.time()
        if not force and (now - self.state.last_verified) < 300:
            return self.state

        try:
            data = self._request("GET", "user/account", auth=True)
        except Exception:
            return self.state

        if not data.get("status"):
            error = data.get("error") or {}
            err_msg = str(error.get("errorMessage") or "")
            if "User not found" in err_msg:
                self.state = Session(region=self.region)
                if self.on_save:
                    self.on_save(self.state.as_dict())
                raise DAFilmsAuthError("Session expired or user not found.")
            return self.state

        user_info = data.get("data") or {}
        refreshed_token = str(user_info.get("token") or "").strip()
        if refreshed_token:
            self.state.token = refreshed_token
        self.state.name = str(user_info.get("name") or self.state.name).strip()
        self.state.subscriber = bool(user_info.get("subscriber", self.state.subscriber))
        self.state.expiration = str(user_info.get("expiration") or self.state.expiration).strip()
        self.state.login_status = bool(user_info.get("loginStatus", True))
        self.state.last_verified = now

        if self.on_save:
            self.on_save(self.state.as_dict())
        return self.state

    def logout(self) -> None:
        """Log out and invalidate session."""
        if self.state.signed_in:
            try:
                self._request("GET", "logout", auth=True)
            except Exception:
                pass
        self.state = Session(region=self.region)
        if self.on_save:
            self.on_save(self.state.as_dict())

    def detail(self, film_id: int | str) -> dict[str, Any]:
        """Fetch full program details for film_id."""
        data = self._request("GET", f"detail/{film_id}", auth=self.state.signed_in)
        if not data.get("status"):
            err = (data.get("error") or {}).get("errorMessage") or "Film detail not available"
            raise DAFilmsNotFound(f"Film {film_id} not found: {err}")
        return data.get("data") or {}

    def player(self, film_id: int | str) -> PlaybackInfo:
        """Fetch playback streaming information for film_id."""
        data = self._request("GET", f"player/{film_id}", auth=self.state.signed_in)
        if not data.get("status"):
            err = data.get("error") or {}
            user_text = err.get("errorUserText") or err.get("errorMessage") or "Access denied"
            raise DAFilmsAccessDenied(f"DAFilms playback denied for {film_id}: {user_text}")

        payload = data.get("data") or {}
        film = payload.get("film") or {}

        # Parse playlist streams
        playlist_raw = payload.get("playlist") or []
        playlist: list[PlaybackSource] = []
        for item in playlist_raw:
            if isinstance(item, dict) and item.get("src"):
                size = str(item.get("size") or "Auto").strip()
                playlist.append(PlaybackSource(size=size, src=str(item["src"]).strip()))

        # Parse subtitle tracks
        tracks_raw = payload.get("tracks") or []
        tracks: list[SubtitleTrack] = []
        for track in tracks_raw:
            if isinstance(track, dict) and track.get("src"):
                raw_lang = str(track.get("srclang") or track.get("label") or "und").strip().lower()
                raw_label = str(track.get("label") or track.get("srclang") or "Subtitles").strip()
                tracks.append(
                    SubtitleTrack(
                        label=raw_label,
                        srclang=raw_lang,
                        src=str(track["src"]).strip(),
                        default=bool(track.get("default")),
                        kind=str(track.get("kind") or "subtitles").strip(),
                    )
                )

        # Parse any chapters if present in payload or film
        chapters: list[Chapter] = []
        chapters_raw = payload.get("chapters") or film.get("chapters") or []
        if isinstance(chapters_raw, list):
            for ch in chapters_raw:
                if isinstance(ch, dict) and "start_ms" in ch:
                    try:
                        chapters.append(
                            Chapter(
                                start_ms=int(ch["start_ms"]),
                                title=str(ch.get("title") or "Chapter"),
                                end_ms=int(ch["end_ms"]) if "end_ms" in ch else None,
                            )
                        )
                    except (ValueError, TypeError):
                        pass

        # Parse attachments (cover poster, etc.)
        attachments: list[Attachment] = []
        cover = film.get("image") or payload.get("image")
        if cover:
            title_name = film.get("title") or str(film_id)
            attachments.append(Attachment(url=str(cover), name=f"{title_name} Poster", kind="poster"))

        return PlaybackInfo(
            film=film,
            playlist=playlist,
            tracks=tracks,
            chapters=chapters,
            attachments=attachments,
        )

    def search(
        self,
        query: str,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[FilmItem], int]:
        """Search catalog by text query."""
        params = {
            "query": query,
            "p": page,
            "pageSize": page_size,
        }
        data = self._request("GET", "search", params=params, auth=self.state.signed_in)
        if not data.get("status"):
            return [], 0

        payload = data.get("data") or {}
        items_raw = payload.get("items") or []
        items = [FilmItem.from_dict(item) for item in items_raw if isinstance(item, dict)]

        paging = payload.get("paging") or {}
        page_count = int(paging.get("pageCount") or 1)
        return items, page_count

    def films(
        self,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[FilmItem], int]:
        """Browse all films in catalog."""
        params = {"p": page, "pageSize": page_size}
        data = self._request("GET", "films", params=params, auth=self.state.signed_in)
        if not data.get("status"):
            return [], 0

        payload = data.get("data") or {}
        items_raw = payload.get("items") or []
        items = [FilmItem.from_dict(item) for item in items_raw if isinstance(item, dict)]

        paging = payload.get("paging") or {}
        page_count = int(paging.get("pageCount") or 1)
        return items, page_count

    def collection(
        self,
        collection_id: int | str,
        page: int = 1,
        page_size: int = 50,
        *,
        prefer_selection: bool = False,
    ) -> list[dict] | dict[str, Any]:
        """Retrieve items in a collection or festival selection."""
        params = {"p": page, "pageSize": page_size}
        endpoints = (
            f"film-selection/{collection_id}",
            f"collection/{collection_id}",
        ) if prefer_selection else (
            f"collection/{collection_id}",
            f"film-selection/{collection_id}",
        )

        last_error = ""
        for ep in endpoints:
            try:
                data = self._request("GET", ep, params=params, auth=self.state.signed_in)
                if data.get("status") and data.get("data"):
                    return data["data"]
                if not data.get("status"):
                    err = (data.get("error") or {}).get("errorMessage") or "Not found"
                    last_error = err
            except DAFilmsNotFound as exc:
                last_error = str(exc)
            except DAFilmsError as exc:
                last_error = str(exc)

        raise DAFilmsNotFound(f"Collection/program {collection_id} not found ({last_error})")

    def home(self) -> dict[str, Any]:
        """Fetch home page channels and featured tiles."""
        data = self._request("GET", "home", auth=self.state.signed_in)
        if not data.get("status"):
            return {}
        return data.get("data") or {}
