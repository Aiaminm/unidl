"""KVIFF.TV Android TV client."""

from __future__ import annotations

import base64
import datetime
import html
import json
import random
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlparse

import requests

from ...core.attachments import Attachment
from ...core.chapters import Chapter

APP_PACKAGE = "com.abuco.kviff"
APP_NAME = "KVIFF.TV"
APP_VERSION = "1.17"
VERSION_CODE = 17
WEBAPP_VERSION = "0.0.1"

AZURE_HOST = "nice-coast-029238203.2.azurestaticapps.net"
AZURE_STATIC_URL = f"https://{AZURE_HOST}/indexAndroidTV.html?7"

API_BASE_URL = "https://kviff.tv/api/"
WEB_URL = "https://kviff.tv"
PAIR_URL = "https://kviff.tv/pair"

# Tivio Studio Firebase / Firestore integration constants for kviff.tv
TIVIO_API_KEY = "AIzaSyB02udgMkNLADkLJ_w5YNBMR2VR1WHfusI"
TIVIO_PROJECT_ID = "tivio-production"
KVIFF_ORG_ID = "t9MsEI24hFFMzEjOSrNB"
KVIFF_ORG_PATH = f"projects/{TIVIO_PROJECT_ID}/databases/(default)/documents/organizations/{KVIFF_ORG_ID}"
KVIFF_TENANT_ID = "X9MsEI24hFFMzEjOSrNB-oehrc"
FIRESTORE_REST_BASE = f"https://firestore.googleapis.com/v1/projects/{TIVIO_PROJECT_ID}/databases/(default)/documents"
FIREBASE_AUTH_SIGN_IN_URL = f"https://identitytoolkit.googleapis.com/v1/accounts:signInWithCustomToken?key={TIVIO_API_KEY}"
FIREBASE_TOKEN_REFRESH_URL = f"https://securetoken.googleapis.com/v1/token?key={TIVIO_API_KEY}"
FIREBASE_ACCOUNT_INFO_URL = f"https://identitytoolkit.googleapis.com/v1/accounts:lookup?key={TIVIO_API_KEY}"
TIVIO_GET_SOURCE_URL = "https://europe-west3-tivio-production.cloudfunctions.net/getSourceUrl"

# Algolia search integration constants for kviff.tv
ALGOLIA_APP_ID = "OL4UZ1QNHS"
ALGOLIA_API_KEY = "NTdiZTE4MWI4NGYzYWU0ZGE1ZDVlNWVmZWM2MGFkYWE4NWI2ODNhMTVmMTkxMTg2YWIwMzQwNmQzYzEzMDE2MHJlc3RyaWN0SW5kaWNlcz0lNUIlMjJ2aWRlb3MlMjIlMkMlMjJ2aWRlb3NfY3JlYXRlZF9kZXNjJTIyJTJDJTIydGFncyUyMiUyQyUyMnR2Q2hhbm5lbHMlMjIlNUQ="
ALGOLIA_BASE_URL = f"https://{ALGOLIA_APP_ID}-dsn.algolia.net/1/indexes"
ALGOLIA_INDEX_VIDEOS = "videos"
ALGOLIA_ORG_PATH = f"organizations/{KVIFF_ORG_ID}"

DEFAULT_LANGUAGE = "cs"
SUPPORTED_LANGUAGES = ("cs", "en")

USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 10; BRAVIA 4K Build/QTG3.200305.006.S284; wv) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/124.0.6367.82 "
    f"Safari/537.36 {APP_PACKAGE}/{APP_VERSION}"
)

TOKEN_FILE = "kviff_token.json"
TV_POLL_INTERVAL = 2.0  # 2000ms aligned with APK loginTimer
TV_POLL_TIMEOUT = 900.0  # 15 minutes aligned with APK refreshTimer
REQUEST_TIMEOUT = 60.0  # 60000ms aligned with APK Axios.Timeout


class KviffError(Exception):
    """Base error for KVIFF.TV."""


class KviffAuthError(KviffError):
    """Authentication or session token error."""


class KviffCodeExpired(KviffError):
    """TV code login challenge expired."""


class KviffAccessDenied(KviffError):
    """User lacks entitlement or subscription for content."""


class KviffNotFound(KviffError):
    """Requested content or resource was not found."""


def normalize_language(value: object) -> str:
    key = str(value or "").strip().lower()
    if key in ("en", "eng", "english"):
        return "en"
    return "cs"


def extract_film_id(target: str) -> str | None:
    text = str(target or "").strip()
    if not text:
        return None
    # 1. kviff:URI scheme
    m = re.match(r"^kviff:(?:(?:film|video|videos)/)?([^/?#]+)$", text, re.I)
    if m:
        return m.group(1)
    # 2. Standalone digits or alphanumeric IDs / slugs (without url slashes or queries)
    if "/" not in text and not text.startswith("http") and "?" not in text:
        return text
    # 3. Web URLs
    parsed = urlparse(text)
    query = parse_qs(parsed.query)
    for q_key in ("video", "videoId", "id", "v"):
        if q_key in query and query[q_key]:
            val = str(query[q_key][0]).strip()
            if val:
                return val
    path = parsed.path.strip("/")
    # /videos/{id_or_slug} or /film/{id} or /player/{id} or /detail/{id}
    m = re.match(r"^(?:videos|film|player|detail)/([^/?#]+)$", path, re.I)
    if m:
        return m.group(1)
    return None


def extract_collection_id(target: str) -> str | None:
    text = str(target or "").strip()
    if not text:
        return None
    parsed = urlparse(text)
    query = parse_qs(parsed.query)
    for q_key in ("video", "videoId", "v"):
        if q_key in query and query[q_key]:
            return None
    m = re.match(r"^kviff:(?:collection|collections|tag|tags)/([^/?#]+)$", text, re.I)
    if m:
        return m.group(1)
    for q_key in ("collection", "collectionId", "row", "rowId", "tag", "tagId"):
        if q_key in query and query[q_key]:
            val = str(query[q_key][0]).strip()
            if val:
                return val
    path = parsed.path.strip("/")
    m = re.match(r"^(?:collection|collections)/([^/?#]+)$", path, re.I)
    if m:
        return m.group(1)
    return None


def extract_channel_id(target: str) -> str | None:
    text = str(target or "").strip()
    if not text:
        return None
    parsed = urlparse(text)
    query = parse_qs(parsed.query)
    for q_key in ("video", "videoId", "v"):
        if q_key in query and query[q_key]:
            return None
    m = re.match(r"^kviff:(?:channel|tvChannels)/([^/?#]+)$", text, re.I)
    if m:
        return m.group(1)
    for q_key in ("channel", "channelId", "tvChannel", "tvChannelId"):
        if q_key in query and query[q_key]:
            val = str(query[q_key][0]).strip()
            if val:
                return val
    path = parsed.path.strip("/")
    m = re.match(r"^(?:channel|tvChannels)/([^/?#]+)$", path, re.I)
    if m:
        return m.group(1)
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


def parse_iso_duration(duration_str: str) -> int:
    """Parse ISO 8601 duration e.g. PT1H17M47S to total seconds."""
    if not duration_str or not duration_str.startswith("PT"):
        return 0
    m = re.match(r"^PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?$", duration_str)
    if not m:
        return 0
    hours = int(m.group(1) or 0)
    minutes = int(m.group(2) or 0)
    seconds = float(m.group(3) or 0.0)
    return int(round(hours * 3600 + minutes * 60 + seconds))


def extract_jwt_claims(token: str) -> dict[str, Any]:
    """Decode unverified JWT payload claims without third-party dependencies."""
    if not token or "." not in token:
        return {}
    parts = token.split(".")
    if len(parts) < 2:
        return {}
    payload = parts[1]
    rem = len(payload) % 4
    if rem > 0:
        payload += "=" * (4 - rem)
    try:
        data = base64.urlsafe_b64decode(payload.encode("ascii"))
        claims = json.loads(data.decode("utf-8"))
        if isinstance(claims, dict):
            return claims
    except Exception:
        pass
    return {}


@dataclass(frozen=True, slots=True)
class TvChallenge:
    access_code: str
    access_code_hash: str
    qr_link: str
    expiration_minutes: int = 15
    timeout: float = TV_POLL_TIMEOUT
    device_id: str = ""
    auth_type: str = "auto"


@dataclass
class Session:
    token: str = ""
    refresh_token: str = ""
    language: str = DEFAULT_LANGUAGE
    name: str = ""
    email: str = ""
    user_id: str = ""
    subscriber: bool = False
    expiration: str = ""
    expires_at: float = 0.0
    login_status: bool = False
    last_verified: float = 0.0
    auth_type: str = "auto"
    tenant_id: str = ""

    @property
    def signed_in(self) -> bool:
        return bool(self.token.strip())

    @property
    def is_tivio(self) -> bool:
        if self.auth_type == "tivio":
            return True
        if self.auth_type == "abuco":
            return False
        return bool(self.refresh_token.strip())

    def label(self) -> str:
        parts: list[str] = []
        if self.name:
            parts.append(self.name)
        elif self.email:
            parts.append(self.email)
        if self.subscriber:
            parts.append("subscriber")
        elif self.signed_in:
            parts.append("signed in")
        return " · ".join(parts) if parts else "signed in"

    def as_dict(self) -> dict[str, Any]:
        return {
            "token": self.token,
            "refresh_token": self.refresh_token,
            "language": self.language,
            "name": self.name,
            "email": self.email,
            "user_id": self.user_id,
            "subscriber": self.subscriber,
            "expiration": self.expiration,
            "expires_at": self.expires_at,
            "login_status": self.login_status,
            "last_verified": self.last_verified,
            "auth_type": self.auth_type,
            "tenant_id": self.tenant_id,
        }

    @classmethod
    def from_cache(cls, data: dict[str, Any] | None, language: str = DEFAULT_LANGUAGE) -> Session:
        if not isinstance(data, dict):
            return cls(language=normalize_language(language))
        return cls(
            token=str(data.get("token") or "").strip(),
            refresh_token=str(data.get("refresh_token") or "").strip(),
            language=str(data.get("language") or language).strip(),
            name=str(data.get("name") or "").strip(),
            email=str(data.get("email") or "").strip(),
            user_id=str(data.get("user_id") or "").strip(),
            subscriber=bool(data.get("subscriber")),
            expiration=str(data.get("expiration") or "").strip(),
            expires_at=float(data.get("expires_at") or 0.0),
            login_status=bool(data.get("login_status", bool(data.get("token")))),
            last_verified=float(data.get("last_verified") or 0.0),
            auth_type=str(data.get("auth_type") or ("tivio" if data.get("refresh_token") else "abuco")),
            tenant_id=str(data.get("tenant_id") or "").strip(),
        )


@dataclass(frozen=True, slots=True)
class FilmItem:
    id: str
    title: str
    short_description: str = ""
    description: str = ""
    duration_ms: int = 0
    url: str = ""
    image: str = ""
    thumb: str = ""
    directors: tuple[str, ...] = ()
    price: str = ""
    svod: bool = False
    tvod: bool = False
    fvod: bool = False
    public: bool = True
    year: int | None = None
    country: str = ""
    kind: str = "movie"

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FilmItem:
        directors = data.get("directors") or data.get("director")
        if isinstance(directors, list):
            dirs: list[str] = []
            for d in directors:
                if isinstance(d, dict) and "name" in d:
                    dirs.append(str(d["name"]).strip())
                elif str(d).strip():
                    dirs.append(str(d).strip())
            dirs_tuple = tuple(dirs)
        elif isinstance(directors, str):
            dirs_tuple = (directors.strip(),)
        else:
            dirs_tuple = ()

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

        if year is None and data.get("year"):
            try:
                year = int(data["year"])
            except (ValueError, TypeError):
                pass
        if year is None and data.get("datePublished"):
            m = re.search(r"\b(19\d\d|20\d\d)\b", str(data["datePublished"]))
            if m:
                year = int(m.group(1))

        duration_ms = 0
        if data.get("duration"):
            try:
                duration_ms = int(data["duration"])
            except (ValueError, TypeError):
                pass
        elif data.get("runtime"):
            try:
                duration_ms = int(data["runtime"]) * 60000
            except (ValueError, TypeError):
                pass

        item_id = str(data.get("id") or data.get("_id") or "").strip()
        title = str(data.get("title") or data.get("name") or "").strip()

        return cls(
            id=item_id,
            title=title,
            short_description=str(data.get("shortDescription") or data.get("synopsis") or "").strip(),
            description=str(data.get("description") or "").strip(),
            duration_ms=duration_ms,
            url=str(data.get("url") or "").strip(),
            image=str(data.get("image") or data.get("poster") or "").strip(),
            thumb=str(data.get("thumb") or data.get("thumbnail") or "").strip(),
            directors=dirs_tuple,
            price=str(data.get("price") or "").strip(),
            svod=bool(data.get("svod")),
            tvod=bool(data.get("tvod")),
            fvod=bool(data.get("fvod")),
            public=bool(data.get("public", True)),
            year=year,
            country=country,
            kind=str(data.get("type") or "movie").strip(),
        )


@dataclass(frozen=True, slots=True)
class PlaybackSource:
    size: str
    src: str

    @property
    def url(self) -> str:
        return self.src


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
    is_live: bool = False


class KviffApi:
    """KVIFF.TV HTTP API wrapper aligned with com.abuco.kviff."""

    def __init__(
        self,
        session: requests.Session,
        language: str = DEFAULT_LANGUAGE,
        state: Session | None = None,
        on_save: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.session = session
        self.language = normalize_language(language)
        self.state = state or Session(language=self.language)
        self.on_save = on_save

    @property
    def api_base(self) -> str:
        return API_BASE_URL

    def _auth_header_value(self) -> str:
        """Construct authorization header based on session type."""
        token = self.state.token.strip()
        if not token:
            return ""
        if self.state.auth_type == "tivio" or token.startswith("eyJ"):
            if token.startswith("Bearer "):
                return token
            return f"Bearer {token}"
        if token.startswith("Bearer Bearer "):
            return token
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
        json_data: dict[str, Any] | None = None,
        auth: bool = True,
        timeout: float = REQUEST_TIMEOUT,
    ) -> dict[str, Any]:
        if endpoint.startswith("http://") or endpoint.startswith("https://"):
            url = endpoint
        else:
            url = self.api_base + endpoint.lstrip("/")

        req_params: dict[str, Any] = {"language": self.language}
        if params:
            req_params.update(params)

        try:
            resp = self.session.request(
                method,
                url,
                params=req_params,
                json=json_data,
                headers=self._headers(auth=auth),
                timeout=timeout,
            )
        except requests.RequestException as exc:
            raise KviffError(f"HTTP request to {url} failed: {exc}") from exc

        if resp.status_code == 404:
            raise KviffNotFound(f"Resource {endpoint} not found (404)")

        try:
            data = resp.json()
        except ValueError as exc:
            raise KviffError(f"Invalid JSON response from {url}: {resp.text[:200]}") from exc

        return data

    def start_tv_code(self) -> TvChallenge:
        """Initiate TV code login.

        First attempts legacy Abuco endpoint aligned with APK Code.vue.
        When Abuco returns 404 (as on the current live kviff.tv service),
        automatically falls back to modern Tivio Studio Firestore invite
        pairing (kviff.tv/pair).
        """
        try:
            return self._start_abuco_tv_code()
        except KviffNotFound:
            return self._start_tivio_tv_code()

    def _start_abuco_tv_code(self) -> TvChallenge:
        """Initiate TV code login aligned with APK Code.vue."""
        data: dict[str, Any]
        try:
            data = self._request("GET", "auth/devices/code", auth=False)
        except KviffNotFound:
            data = self._request("GET", "code", auth=False)

        payload = data.get("data") or {}
        access_code = payload.get("accessCode")

        # In APK Code.vue: if accessCode is null, call code?action=refresh
        if not access_code:
            try:
                data = self._request("GET", "auth/devices/code", params={"action": "refresh"}, auth=False)
            except KviffNotFound:
                data = self._request("GET", "code", params={"action": "refresh"}, auth=False)
            payload = data.get("data") or {}
            access_code = payload.get("accessCode")

        if not payload or not payload.get("accessCodeHash"):
            err = (data.get("error") or {}).get("errorMessage") or "Unknown error"
            raise KviffError(f"Failed to obtain KVIFF.TV code: {err}")

        exp_raw = payload.get("accessCodeExpiration") or "15"
        try:
            exp_minutes = int(exp_raw)
        except (ValueError, TypeError):
            exp_minutes = 15
        timeout = float(exp_minutes * 60)

        qr_link = str(payload.get("qrLink") or f"{PAIR_URL}?code={access_code}").strip()

        return TvChallenge(
            access_code=str(access_code or "").strip(),
            access_code_hash=str(payload.get("accessCodeHash") or "").strip(),
            qr_link=qr_link,
            expiration_minutes=exp_minutes,
            timeout=timeout,
            device_id=str(payload.get("deviceId") or "").strip(),
            auth_type="abuco",
        )

    def _start_tivio_tv_code(self) -> TvChallenge:
        """Initiate Tivio Studio Firestore invite pairing (kviff.tv/pair)."""
        code_digits = "".join(str(random.randint(0, 9)) for _ in range(8))
        access_code = f"{code_digits[:4]}-{code_digits[4:]}"
        now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()

        doc_body = {
            "fields": {
                "code": {"stringValue": code_digits},
                "created": {"timestampValue": now_iso},
                "organizationRef": {
                    "referenceValue": KVIFF_ORG_PATH,
                },
                "state": {"stringValue": "NEW"},
                "type": {"stringValue": "SINGLE_SIGN_ON"},
            }
        }
        url = f"{FIRESTORE_REST_BASE}/invites?key={TIVIO_API_KEY}"
        try:
            resp = self.session.post(
                url,
                json=doc_body,
                headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
                timeout=REQUEST_TIMEOUT,
            )
        except requests.RequestException as exc:
            raise KviffError(f"Failed to create Tivio invite: {exc}") from exc

        if resp.status_code != 200:
            raise KviffError(f"Failed to create Tivio invite: HTTP {resp.status_code} {resp.text[:200]}")

        data = resp.json()
        doc_name = data.get("name") or ""
        doc_id = doc_name.split("/")[-1] if doc_name else ""
        if not doc_id:
            raise KviffError("Tivio invite response did not include document ID")

        qr_link = f"{PAIR_URL}?code={code_digits}"
        return TvChallenge(
            access_code=access_code,
            access_code_hash=doc_id,
            qr_link=qr_link,
            expiration_minutes=15,
            timeout=TV_POLL_TIMEOUT,
            device_id=code_digits,
            auth_type="tivio",
        )

    def poll_tv_code(self, challenge: TvChallenge) -> Session | None:
        """Poll TV code status supporting both Tivio and legacy Abuco."""
        if challenge.auth_type == "tivio":
            return self._poll_tivio_tv_code(challenge)
        if challenge.auth_type == "abuco":
            return self._poll_abuco_tv_code(challenge)
        if challenge.access_code_hash.startswith("hash") or "_" in challenge.access_code_hash:
            return self._poll_abuco_tv_code(challenge)
        try:
            return self._poll_tivio_tv_code(challenge)
        except (KviffNotFound, KviffError):
            return self._poll_abuco_tv_code(challenge)

    def _poll_abuco_tv_code(self, challenge: TvChallenge) -> Session | None:
        """Poll legacy Abuco TV code status aligned with APK Code.vue."""
        endpoint = f"auth/devices/code/login?code={challenge.access_code_hash}"
        data: dict[str, Any]
        try:
            data = self._request("POST", endpoint, auth=False)
        except KviffNotFound:
            data = self._request("POST", "code/login", params={"code": challenge.access_code_hash}, auth=False)

        status = bool(data.get("status"))
        if not status:
            error = data.get("error") or {}
            msg = str(error.get("errorMessage") or error.get("errorUserText") or "")
            if "not authorized" in msg.lower():
                return None
            if "expired" in msg.lower():
                raise KviffCodeExpired("The TV pairing code has expired.")
            return None

        user_data = data.get("data") or {}
        token = str(user_data.get("token") or "").strip()
        if not token:
            return None

        session = Session(
            token=token,
            language=str(user_data.get("language") or self.language).strip(),
            name=str(user_data.get("name") or "").strip(),
            email=str(user_data.get("email") or "").strip(),
            subscriber=bool(user_data.get("subscriber")),
            expiration=str(user_data.get("expiration") or "").strip(),
            login_status=True,
            last_verified=time.time(),
            auth_type="abuco",
        )
        self.state = session
        if self.on_save:
            self.on_save(session.as_dict())
        return session

    def _poll_tivio_tv_code(self, challenge: TvChallenge) -> Session | None:
        """Poll Tivio Studio Firestore invite pairing and exchange custom token."""
        url = f"{FIRESTORE_REST_BASE}/invites/{challenge.access_code_hash}?key={TIVIO_API_KEY}"
        try:
            resp = self.session.get(
                url,
                headers={"Accept": "application/json", "User-Agent": USER_AGENT},
                timeout=REQUEST_TIMEOUT,
            )
        except requests.RequestException as exc:
            raise KviffError(f"HTTP error polling Tivio invite: {exc}") from exc

        if resp.status_code == 404:
            raise KviffNotFound("TV code challenge document not found.")
        if resp.status_code != 200:
            return None

        data = resp.json()
        fields = data.get("fields") or {}
        state = (fields.get("state") or {}).get("stringValue") or ""
        if state == "NEW":
            return None
        if state in ("CANCELLED", "EXPIRED"):
            raise KviffCodeExpired("The TV pairing code has expired or was cancelled.")
        if state != "CLOSED":
            return None

        # Extract custom token from payload
        payload_field = fields.get("payload") or {}
        custom_token = ""
        if "mapValue" in payload_field:
            custom_token = (
                payload_field["mapValue"]
                .get("fields", {})
                .get("token", {})
                .get("stringValue", "")
            )
        elif "stringValue" in payload_field:
            custom_token = payload_field["stringValue"]

        if not custom_token:
            return None

        # Extract tenant from custom token claims or default to KVIFF_TENANT_ID
        custom_claims = extract_jwt_claims(custom_token)
        tenant_id = str(
            custom_claims.get("tenant_id")
            or custom_claims.get("tenantId")
            or KVIFF_TENANT_ID
        ).strip()

        # Exchange custom token with Firebase Auth / Identity Platform
        sign_in_body: dict[str, Any] = {
            "token": custom_token,
            "returnSecureToken": True,
        }
        if tenant_id:
            sign_in_body["tenantId"] = tenant_id

        auth_resp = self.session.post(
            FIREBASE_AUTH_SIGN_IN_URL,
            json=sign_in_body,
            headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
            timeout=REQUEST_TIMEOUT,
        )

        # Resilient fallback if tenant mismatched
        if auth_resp.status_code != 200 and "TENANT_ID_MISMATCH" in auth_resp.text:
            alt_tenant = None if "tenantId" in sign_in_body else KVIFF_TENANT_ID
            alt_body: dict[str, Any] = {"token": custom_token, "returnSecureToken": True}
            if alt_tenant:
                alt_body["tenantId"] = alt_tenant
            auth_resp = self.session.post(
                FIREBASE_AUTH_SIGN_IN_URL,
                json=alt_body,
                headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
                timeout=REQUEST_TIMEOUT,
            )

        if auth_resp.status_code != 200:
            raise KviffAuthError(f"Failed to exchange custom token: {auth_resp.text[:200]}")
        auth_data = auth_resp.json()
        id_token = str(auth_data.get("idToken") or "").strip()
        refresh_token = str(auth_data.get("refreshToken") or "").strip()
        expires_in = int(auth_data.get("expiresIn") or 3600)
        local_id = str(auth_data.get("localId") or "").strip()

        # Extract user profile from ID token claims
        id_claims = extract_jwt_claims(id_token)
        email = str(id_claims.get("email") or "").strip()
        display_name = str(id_claims.get("name") or "").strip()
        if not local_id:
            local_id = str(id_claims.get("user_id") or id_claims.get("sub") or "").strip()

        # Lookup user profile in Identity Platform
        try:
            lookup_body: dict[str, Any] = {"idToken": id_token}
            if tenant_id:
                lookup_body["tenantId"] = tenant_id
            lookup_resp = self.session.post(
                FIREBASE_ACCOUNT_INFO_URL,
                json=lookup_body,
                headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
                timeout=REQUEST_TIMEOUT,
            )
            if lookup_resp.status_code == 200:
                users = lookup_resp.json().get("users") or []
                if users:
                    u = users[0]
                    email = str(u.get("email") or email).strip()
                    display_name = str(u.get("displayName") or display_name).strip()
                    local_id = str(u.get("localId") or local_id).strip()
        except Exception:
            pass

        session = Session(
            token=id_token,
            refresh_token=refresh_token,
            language=self.language,
            name=display_name or (email.split("@")[0] if email else "KVIFF.TV User"),
            email=email,
            user_id=local_id,
            subscriber=True,
            expires_at=time.time() + expires_in,
            login_status=True,
            last_verified=time.time(),
            auth_type="tivio",
            tenant_id=tenant_id,
        )
        self.state = session
        if self.on_save:
            self.on_save(session.as_dict())

        # Cleanup invite document
        self.cancel_tv_code(challenge)
        return session

    def cancel_tv_code(self, challenge: TvChallenge) -> None:
        """Cancel or finalize the TV pairing invite document in Firestore."""
        if challenge.auth_type == "tivio" and challenge.access_code_hash:
            url = f"{FIRESTORE_REST_BASE}/invites/{challenge.access_code_hash}?updateMask.fieldPaths=state&key={TIVIO_API_KEY}"
            try:
                self.session.patch(
                    url,
                    json={"fields": {"state": {"stringValue": "CANCELLED"}}},
                    headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
                    timeout=10.0,
                )
            except Exception:
                pass

    def refresh_tivio_token(self) -> Session:
        """Refresh Firebase ID token using refresh_token."""
        if not self.state.refresh_token:
            raise KviffAuthError("Cannot refresh Tivio session without refresh_token")

        resp = self.session.post(
            FIREBASE_TOKEN_REFRESH_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": self.state.refresh_token,
            },
            headers={"User-Agent": USER_AGENT},
            timeout=REQUEST_TIMEOUT,
        )
        if resp.status_code != 200:
            self.state = Session(language=self.language)
            if self.on_save:
                self.on_save(self.state.as_dict())
            raise KviffAuthError(f"Failed to refresh KVIFF.TV session: HTTP {resp.status_code}")

        data = resp.json()
        new_id_token = str(data.get("id_token") or data.get("access_token") or "").strip()
        new_refresh = str(data.get("refresh_token") or self.state.refresh_token).strip()
        expires_in = int(data.get("expires_in") or 3600)
        user_id = str(data.get("user_id") or self.state.user_id).strip()

        self.state.token = new_id_token
        self.state.refresh_token = new_refresh
        self.state.user_id = user_id
        self.state.expires_at = time.time() + expires_in
        self.state.last_verified = time.time()
        self.state.login_status = True

        if self.on_save:
            self.on_save(self.state.as_dict())
        return self.state

    def ensure_session(self, force: bool = False) -> Session | None:
        """Validate and refresh active user session."""
        if not self.state.signed_in:
            return None

        now = time.time()
        # Tivio token refresh
        if self.state.is_tivio:
            if not force and self.state.expires_at and (now < self.state.expires_at - 300):
                return self.state
            return self.refresh_tivio_token()

        # Legacy Abuco auth validation
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
                self.state = Session(language=self.language, auth_type="abuco")
                if self.on_save:
                    self.on_save(self.state.as_dict())
                raise KviffAuthError("Session expired or user not found.")
            return self.state

        user_info = data.get("data") or {}
        refreshed_token = str(user_info.get("token") or "").strip()
        if refreshed_token:
            self.state.token = refreshed_token
        self.state.name = str(user_info.get("name") or self.state.name).strip()
        self.state.email = str(user_info.get("email") or self.state.email).strip()
        self.state.subscriber = bool(user_info.get("subscriber", self.state.subscriber))
        self.state.expiration = str(user_info.get("expiration") or self.state.expiration).strip()
        self.state.login_status = bool(user_info.get("loginStatus", True))
        self.state.last_verified = now

        if self.on_save:
            self.on_save(self.state.as_dict())
        return self.state

    def logout(self) -> None:
        """Log out and invalidate session."""
        if self.state.signed_in and not self.state.is_tivio:
            try:
                self._request("GET", "logout", auth=True)
            except Exception:
                pass
        self.state = Session(language=self.language)
        if self.on_save:
            self.on_save(self.state.as_dict())

    def init(self) -> dict[str, Any]:
        """Fetch app init configuration."""
        data = self._request("GET", "init", auth=False)
        return data.get("data") or {}

    def home(self) -> dict[str, Any]:
        """Fetch home page sections and featured carousel."""
        data = self._request("GET", "home", auth=self.state.signed_in)
        if not data.get("status"):
            return {}
        return data.get("data") or {}

    def collections(self) -> list[dict[str, Any]]:
        """Fetch catalog collections."""
        data = self._request("GET", "collections", auth=self.state.signed_in)
        if not data.get("status"):
            return []
        res = data.get("data")
        return res if isinstance(res, list) else []

    def collection(
        self,
        collection_id_or_url: str,
        page: int = 1,
        page_size: int = 20,
    ) -> dict[str, Any]:
        """Retrieve items in a collection."""
        params = {"p": page, "pageSize": page_size}
        if collection_id_or_url.startswith("http://") or collection_id_or_url.startswith("https://"):
            data = self._request("GET", collection_id_or_url, params=params, auth=self.state.signed_in)
        else:
            data = self._request("GET", f"collection/{collection_id_or_url}", params=params, auth=self.state.signed_in)

        if not data.get("status"):
            err = (data.get("error") or {}).get("errorMessage") or "Collection not found"
            raise KviffNotFound(f"Collection {collection_id_or_url} not found: {err}")

        payload = data.get("data")
        if isinstance(payload, list) and payload:
            return payload[0]
        if isinstance(payload, dict):
            return payload
        return {}

    def alphabet(
        self,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[FilmItem], int]:
        """Fetch films sorted alphabetically (aligned with APK 'channels/alphabet')."""
        params = {"p": page, "pageSize": page_size}
        data = self._request("GET", "channels/alphabet", params=params, auth=self.state.signed_in)
        if not data.get("status"):
            return [], 0

        payload = data.get("data") or {}
        items: list[FilmItem] = []
        for key, value in payload.items():
            if key != "paging" and isinstance(value, list):
                for item in value:
                    if isinstance(item, dict):
                        items.append(FilmItem.from_dict(item))

        paging = payload.get("paging") or {}
        page_count = int(paging.get("pageCount") or 1)
        return items, page_count

    def _film_item_from_algolia(self, hit: dict[str, Any]) -> FilmItem:
        """Construct a FilmItem from an Algolia search hit."""
        oid = str(hit.get("objectID") or hit.get("id") or "").strip()
        name_val = hit.get("name") or {}
        title = ""
        if isinstance(name_val, dict):
            title = str(
                name_val.get(self.language)
                or name_val.get("cs")
                or name_val.get("en")
                or next(iter(name_val.values()), "")
            ).strip()
        elif isinstance(name_val, str):
            title = name_val.strip()
        if not title:
            title = str(hit.get("defaultName") or oid).strip()

        desc_val = hit.get("description") or {}
        description = ""
        if isinstance(desc_val, dict):
            description = str(
                desc_val.get(self.language)
                or desc_val.get("cs")
                or desc_val.get("en")
                or next(iter(desc_val.values()), "")
            ).strip()
        elif isinstance(desc_val, str):
            description = desc_val.strip()

        assets = hit.get("assets") or {}
        cover_url = ""
        thumb_url = ""
        if isinstance(assets, dict):
            cov = assets.get("cover") or {}
            if isinstance(cov, dict):
                cover_url = str(
                    (cov.get("@2") or {}).get("background")
                    or (cov.get("@1") or {}).get("background")
                    or ""
                ).strip()
            vdet = assets.get("video_detail") or {}
            if isinstance(vdet, dict):
                thumb_url = str(
                    (vdet.get("@1") or {}).get("background")
                    or (vdet.get("@2") or {}).get("background")
                    or ""
                ).strip()

        year = None
        if hit.get("year"):
            try:
                year = int(hit["year"])
            except (ValueError, TypeError):
                pass
        if year is None and hit.get("created"):
            m = re.search(r"\b(19\d\d|20\d\d)\b", str(hit["created"]))
            if m:
                year = int(m.group(1))

        monetization = hit.get("monetizationAccessIds")
        fvod = monetization is False
        svod = bool(monetization)

        return FilmItem(
            id=oid,
            title=title,
            short_description=description[:200] if description else "",
            description=description,
            duration_ms=0,
            url=f"{WEB_URL}/videos/{oid}",
            image=cover_url,
            thumb=thumb_url,
            fvod=fvod,
            svod=svod,
            year=year,
            kind=str(hit.get("type") or "movie").lower().strip(),
        )

    def _search_algolia(
        self,
        query: str,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[FilmItem], int]:
        """Search KVIFF.TV catalog using Algolia."""
        url = f"{ALGOLIA_BASE_URL}/{ALGOLIA_INDEX_VIDEOS}/query"
        headers = {
            "x-algolia-application-id": ALGOLIA_APP_ID,
            "x-algolia-api-key": ALGOLIA_API_KEY,
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        }
        algolia_page = max(0, page - 1)
        base_filter = f"organizationPath:{ALGOLIA_ORG_PATH}"
        strict_filter = f"{base_filter} AND isDraft:false AND isDuplicate:false AND hide:false"

        payload = {
            "query": query,
            "filters": strict_filter,
            "page": algolia_page,
            "hitsPerPage": page_size,
        }
        try:
            resp = self.session.post(url, json=payload, headers=headers, timeout=REQUEST_TIMEOUT)
            if resp.status_code == 200:
                data = resp.json()
                hits = data.get("hits") or []
                if not hits and query:
                    payload["filters"] = base_filter
                    resp_relaxed = self.session.post(url, json=payload, headers=headers, timeout=REQUEST_TIMEOUT)
                    if resp_relaxed.status_code == 200:
                        data = resp_relaxed.json()
                        hits = data.get("hits") or []

                items = [self._film_item_from_algolia(h) for h in hits if isinstance(h, dict)]
                nb_pages = int(data.get("nbPages") or 1)
                return items, nb_pages
        except Exception:
            pass

        return [], 0

    def _detail_to_film_item(self, detail: dict[str, Any]) -> FilmItem | None:
        """Convert a film detail dict to FilmItem."""
        if not detail or not isinstance(detail, dict):
            return None
        video = detail.get("video") if isinstance(detail.get("video"), dict) else {}
        item_id = str(detail.get("id") or detail.get("_id") or video.get("id") or "").strip()
        title = str(
            detail.get("title")
            or detail.get("name")
            or video.get("title")
            or ""
        ).strip()
        if not item_id or not title:
            return None

        duration_ms = 0
        dur = detail.get("duration") or video.get("duration")
        if dur:
            try:
                duration_ms = int(dur)
            except (ValueError, TypeError):
                pass

        year = None
        y = detail.get("year") or video.get("year")
        if y:
            try:
                year = int(y)
            except (ValueError, TypeError):
                pass

        dirs: tuple[str, ...] = ()
        d = detail.get("directors") or video.get("directors")
        if isinstance(d, list):
            dirs = tuple(str(x) for x in d if str(x).strip())
        elif isinstance(d, str):
            dirs = (d.strip(),)

        return FilmItem(
            id=item_id,
            title=title,
            short_description=str(detail.get("shortDescription") or detail.get("synopsis") or "").strip(),
            description=str(detail.get("description") or "").strip(),
            duration_ms=duration_ms,
            url=f"{WEB_URL}/videos/{item_id}",
            image=str(detail.get("image") or detail.get("poster") or "").strip(),
            thumb=str(detail.get("thumb") or detail.get("thumbnail") or "").strip(),
            directors=dirs,
            svod=bool(detail.get("svod")),
            tvod=bool(detail.get("tvod")),
            fvod=bool(detail.get("fvod")),
            year=year,
        )

    def search(
        self,
        query: str,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[FilmItem], int]:
        """Search catalog by keyword using Algolia (modern) and Abuco (legacy) fallback."""
        query_str = query.strip()
        if not query_str:
            return [], 0

        # 1. Try legacy Abuco endpoint first (preserves compatibility with legacy mocks & endpoints)
        try:
            params = {
                "keyword": query_str,
                "query": query_str,
                "p": page,
                "pageSize": page_size,
            }
            data = self._request("GET", "search", params=params, auth=self.state.signed_in)
            if data.get("status"):
                payload = data.get("data") or {}
                items_raw = payload.get("items") or []
                items = [FilmItem.from_dict(item) for item in items_raw if isinstance(item, dict)]
                paging = payload.get("paging") or {}
                page_count = int(paging.get("pageCount") or 1)
                if items:
                    return items, page_count
        except (KviffNotFound, KviffError):
            pass

        # 2. Modern Algolia search
        algolia_hits, algolia_pages = self._search_algolia(query_str, page=page, page_size=page_size)
        if algolia_hits:
            return algolia_hits, algolia_pages

        # 3. Direct ID / slug resolution fallback if user searched for specific document ID or slug
        if len(query_str) >= 3:
            try:
                doc = self.detail(query_str)
                if doc:
                    item = self._detail_to_film_item(doc)
                    if item:
                        return [item], 1
            except Exception:
                pass

        return [], 0

    def detail(self, film_id: str) -> dict[str, Any]:
        """Fetch full program details for film_id, with Tivio and web fallbacks."""
        try:
            data = self._request("GET", f"detail/{film_id}", auth=self.state.signed_in)
            if data.get("status"):
                payload = data.get("data")
                if isinstance(payload, list) and payload:
                    return payload[0]
                if isinstance(payload, dict):
                    return payload
        except (KviffNotFound, KviffError):
            pass

        # Tivio Firestore integration
        tivio_doc = self._get_tivio_video_doc(film_id)
        if tivio_doc:
            return self._parse_tivio_doc_to_detail(tivio_doc)

        # Web fallback: fetch web video page and parse JSON-LD
        web_detail = self.parse_web_video_page(film_id)
        if web_detail:
            return web_detail

        raise KviffNotFound(f"Film {film_id} not found")

    def _get_tivio_video_doc(self, target: str) -> dict[str, Any] | None:
        """Fetch video document from Tivio Firestore either by docId or slug."""
        text = str(target or "").strip()
        if not text:
            return None

        # 1. Direct document ID lookup (alphanumeric 20 chars)
        if len(text) == 20 and re.match(r"^[A-Za-z0-9_-]{20}$", text):
            url = f"{FIRESTORE_REST_BASE}/videos/{text}?key={TIVIO_API_KEY}"
            try:
                resp = self.session.get(url, headers={"User-Agent": USER_AGENT}, timeout=REQUEST_TIMEOUT)
                if resp.status_code == 200:
                    return resp.json()
            except Exception:
                pass

        # 2. Query by urlName.{lang}
        query_url = f"{FIRESTORE_REST_BASE}:runQuery?key={TIVIO_API_KEY}"
        languages_to_try = [self.language]
        if "cs" not in languages_to_try:
            languages_to_try.append("cs")
        if "en" not in languages_to_try:
            languages_to_try.append("en")

        for lang in languages_to_try:
            q = {
                "structuredQuery": {
                    "from": [{"collectionId": "videos"}],
                    "where": {
                        "fieldFilter": {
                            "field": {"fieldPath": f"urlName.{lang}"},
                            "op": "ARRAY_CONTAINS",
                            "value": {"stringValue": text},
                        }
                    },
                    "limit": 1,
                }
            }
            try:
                resp = self.session.post(
                    query_url,
                    json=q,
                    headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
                    timeout=REQUEST_TIMEOUT,
                )
                if resp.status_code == 200:
                    res = resp.json()
                    if isinstance(res, list) and res and "document" in res[0]:
                        return res[0]["document"]
            except Exception:
                pass

        # 3. Query by defaultName
        q_default = {
            "structuredQuery": {
                "from": [{"collectionId": "videos"}],
                "where": {
                    "fieldFilter": {
                        "field": {"fieldPath": "defaultName"},
                        "op": "EQUAL",
                        "value": {"stringValue": text},
                    }
                },
                "limit": 1,
            }
        }
        try:
            resp = self.session.post(
                query_url,
                json=q_default,
                headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
                timeout=REQUEST_TIMEOUT,
            )
            if resp.status_code == 200:
                res = resp.json()
                if isinstance(res, list) and res and "document" in res[0]:
                    return res[0]["document"]
        except Exception:
            pass

        return None

    def _parse_tivio_doc_to_detail(self, doc: dict[str, Any]) -> dict[str, Any]:
        """Convert Firestore video document to standard program detail dict."""
        fields = doc.get("fields") or {}
        doc_name = doc.get("name") or ""
        doc_id = doc_name.split("/")[-1]

        name_map = (fields.get("name") or {}).get("mapValue", {}).get("fields", {})
        title = (
            (name_map.get(self.language) or {}).get("stringValue")
            or (name_map.get("cs") or {}).get("stringValue")
            or (name_map.get("en") or {}).get("stringValue")
            or (fields.get("defaultName") or {}).get("stringValue")
            or doc_id
        )

        desc_map = (fields.get("description") or {}).get("mapValue", {}).get("fields", {})
        desc = (
            (desc_map.get(self.language) or {}).get("stringValue")
            or (desc_map.get("cs") or {}).get("stringValue")
            or (desc_map.get("en") or {}).get("stringValue")
            or ""
        )

        year_val = (fields.get("year") or {}).get("integerValue")
        year = int(year_val) if year_val else None

        duration_ms = 0
        profiles = (fields.get("profiles") or {}).get("mapValue", {}).get("fields", {})
        for p_val in profiles.values():
            dur = (p_val.get("mapValue") or {}).get("fields", {}).get("duration", {}).get("integerValue")
            if dur:
                duration_ms = int(dur)
                break

        assets = (fields.get("assets") or {}).get("mapValue", {}).get("fields", {})
        cover_val = (assets.get("cover") or {}).get("mapValue", {}).get("fields", {})
        detail_val = (assets.get("video_detail") or {}).get("mapValue", {}).get("fields", {})

        cover = None
        for k in ("@2", "@1"):
            if k in cover_val:
                cover = (cover_val[k].get("mapValue") or {}).get("fields", {}).get("background", {}).get("stringValue")
                if cover:
                    break

        thumb = None
        for k in ("@1", "@2"):
            if k in detail_val:
                thumb = (detail_val[k].get("mapValue") or {}).get("fields", {}).get("background", {}).get("stringValue")
                if thumb:
                    break

        linked: list[dict[str, str]] = []
        linked_vals = (fields.get("linkedVideos") or {}).get("arrayValue", {}).get("values", [])
        for lv in linked_vals:
            lv_fields = (lv.get("mapValue") or {}).get("fields", {})
            vref = (lv_fields.get("videoRef") or {}).get("referenceValue", "")
            vtype = (lv_fields.get("type") or {}).get("stringValue", "")
            if vref:
                linked.append({"id": vref.split("/")[-1], "type": vtype})

        return {
            "id": doc_id,
            "title": title,
            "description": desc,
            "synopsis": desc,
            "duration": duration_ms,
            "year": year,
            "image": cover or thumb or "",
            "thumb": thumb or cover or "",
            "linkedVideos": linked,
            "pgRating": (fields.get("pgRating") or {}).get("stringValue") or "",
            "tivio_doc": doc,
        }

    def _player_from_tivio(self, detail: dict[str, Any], kind: str = "film") -> PlaybackInfo | None:
        """Request streaming source URL via Tivio Cloud Functions."""
        doc_id = str(detail.get("id") or "").strip()
        if not doc_id:
            return None

        target_id = doc_id
        if kind in ("trailer", "teaser"):
            trailer_id = next(
                (lv["id"] for lv in detail.get("linkedVideos", []) if lv.get("type") == "TRAILER"),
                None,
            )
            if trailer_id:
                target_id = trailer_id

        body = {
            "data": {
                "id": target_id,
                "documentType": "video",
                "capabilities": [
                    {"codec": "h264", "protocol": "hls", "encryption": "none"},
                    {"codec": "h264", "protocol": "dash", "encryption": "none"},
                ],
            }
        }
        headers = {"Content-Type": "application/json", "User-Agent": USER_AGENT}
        if self.state.signed_in and self.state.token:
            headers["Authorization"] = f"Bearer {self.state.token}"

        try:
            resp = self.session.post(
                TIVIO_GET_SOURCE_URL,
                json=body,
                headers=headers,
                timeout=REQUEST_TIMEOUT,
            )
        except requests.RequestException as exc:
            raise KviffError(f"HTTP error requesting Tivio stream: {exc}") from exc

        if resp.status_code == 200:
            result = resp.json().get("result") or {}
            stream_url = str(result.get("url") or "").strip()
            if stream_url:
                title_name = str(detail.get("title") or doc_id)
                playlist = [PlaybackSource(size="Full HD", src=stream_url)]
                attachments: list[Attachment] = []
                if detail.get("image"):
                    attachments.append(Attachment(url=str(detail["image"]), name=f"{title_name} Poster", kind="poster"))
                if detail.get("thumb") and detail["thumb"] != detail.get("image"):
                    attachments.append(Attachment(url=str(detail["thumb"]), name=f"{title_name} Thumbnail", kind="thumbnail"))

                # Check if document has subtitles metadata
                tracks: list[SubtitleTrack] = []
                tivio_doc = detail.get("tivio_doc") or {}
                enc_meta = (tivio_doc.get("fields", {}).get("encodingMetadata") or {}).get("mapValue", {}).get("fields", {})
                subs_vals = (enc_meta.get("subtitles") or {}).get("arrayValue", {}).get("values", [])
                for sub in subs_vals:
                    sub_f = (sub.get("mapValue") or {}).get("fields", {})
                    sub_lang = (sub_f.get("language") or {}).get("stringValue", "")
                    sub_url = (sub_f.get("url") or {}).get("stringValue", "")
                    sub_default = bool((sub_f.get("isDefault") or {}).get("booleanValue", False))
                    if sub_url and (sub_url.startswith("http://") or sub_url.startswith("https://")):
                        tracks.append(
                            SubtitleTrack(
                                label=sub_lang.upper(),
                                srclang=sub_lang,
                                src=sub_url,
                                default=sub_default,
                            )
                        )

                return PlaybackInfo(
                    film=detail,
                    playlist=playlist,
                    tracks=tracks,
                    chapters=[],
                    attachments=attachments,
                )

        err_data = {}
        try:
            err_data = resp.json().get("error") or {}
        except Exception:
            pass
        err_msg = str(err_data.get("message") or resp.text[:200])

        if resp.status_code == 401 or "not logged in" in err_msg.lower():
            trailer_id = next(
                (lv["id"] for lv in detail.get("linkedVideos", []) if lv.get("type") == "TRAILER"),
                None,
            )
            trailer_hint = f" (Trailer is available at kviff:{trailer_id})" if trailer_id else ""
            raise KviffAccessDenied(
                f"KVIFF.TV stream for '{detail.get('title')}' requires being signed in. "
                f"Please run login first.{trailer_hint}"
            )

        raise KviffAccessDenied(f"KVIFF.TV playback denied for '{detail.get('title')}': {err_msg}")

    def player(self, film_id: str, kind: str = "film") -> PlaybackInfo:
        """Fetch playback streaming information for film_id with Tivio and web fallbacks."""
        if kind in ("trailer", "feature", "video"):
            endpoint = f"player/{film_id}/?type=video"
        else:
            endpoint = f"player/{film_id}"

        try:
            data = self._request("GET", endpoint, auth=self.state.signed_in)
        except (KviffNotFound, KviffError):
            data = {}

        if data.get("status"):
            payload = data.get("data") or {}
            film = payload.get("film") or {}
            playlist_raw = payload.get("playlist") or []
            playlist: list[PlaybackSource] = []

            for p_item in playlist_raw:
                if not isinstance(p_item, dict):
                    continue
                # May contain nested 'sources'
                sources = p_item.get("sources")
                if isinstance(sources, list):
                    for s in sources:
                        if isinstance(s, dict) and s.get("url"):
                            size = str(s.get("size") or "Full HD").strip()
                            playlist.append(PlaybackSource(size=size, src=str(s["url"]).strip()))
                elif p_item.get("src") or p_item.get("url"):
                    size = str(p_item.get("size") or "Full HD").strip()
                    src = str(p_item.get("src") or p_item.get("url")).strip()
                    playlist.append(PlaybackSource(size=size, src=src))

            # Parse subtitle tracks
            tracks_raw = payload.get("tracks") or []
            tracks: list[SubtitleTrack] = []
            for track in tracks_raw:
                if isinstance(track, dict) and (track.get("src") or track.get("url")):
                    tracks.append(
                        SubtitleTrack(
                            label=str(track.get("label") or track.get("srclang") or "Subtitles").strip(),
                            srclang=str(track.get("srclang") or "und").strip(),
                            src=str(track.get("src") or track.get("url")).strip(),
                            default=bool(track.get("default")),
                            kind=str(track.get("kind") or "subtitles").strip(),
                        )
                    )

            # Parse chapters or markers
            chapters: list[Chapter] = []
            chapters_raw = payload.get("chapters") or film.get("chapters") or payload.get("markers") or []
            if isinstance(chapters_raw, list):
                for ch in chapters_raw:
                    if not isinstance(ch, dict):
                        continue
                    # Format 1: start_ms, end_ms, title
                    if "start_ms" in ch:
                        try:
                            chapters.append(
                                Chapter(
                                    start_ms=int(ch["start_ms"]),
                                    title=str(ch.get("title") or "Chapter"),
                                    end_ms=int(ch["end_ms"]) if "end_ms" in ch else None,
                                    kind=str(ch.get("kind") or "").strip(),
                                )
                            )
                        except (ValueError, TypeError):
                            pass
                    # Format 2: relativeFromMs, relativeToMs, type, title
                    elif "relativeFromMs" in ch:
                        try:
                            start_ms = int(ch["relativeFromMs"])
                            end_ms = int(ch["relativeToMs"]) if "relativeToMs" in ch else None
                            ch_type = str(ch.get("type") or "").strip().lower()
                            title = str(ch.get("title") or ch.get("name") or ch_type or "Chapter").strip()
                            chapters.append(Chapter(start_ms=start_ms, title=title, end_ms=end_ms, kind=ch_type))
                        except (ValueError, TypeError):
                            pass

            # Parse attachments (cover, thumbnail)
            attachments: list[Attachment] = []
            cover = film.get("image") or payload.get("image")
            title_name = film.get("title") or str(film_id)
            if cover:
                attachments.append(Attachment(url=str(cover), name=f"{title_name} Poster", kind="poster"))
            thumb = film.get("thumb") or payload.get("thumb")
            if thumb and thumb != cover:
                attachments.append(Attachment(url=str(thumb), name=f"{title_name} Thumbnail", kind="thumbnail"))

            if playlist:
                return PlaybackInfo(
                    film=film,
                    playlist=playlist,
                    tracks=tracks,
                    chapters=chapters,
                    attachments=attachments,
                )

        # Tivio Cloud Function getSourceUrl fallback
        try:
            detail = self.detail(film_id)
            if detail.get("tivio_doc"):
                tivio_playback = self._player_from_tivio(detail, kind=kind)
                if tivio_playback:
                    return tivio_playback
        except (KviffNotFound, KviffError):
            pass

        # Fallback to web video page
        web_info = self.parse_web_video_page(film_id)
        content_url = web_info.get("contentUrl")
        if content_url:
            playlist = [PlaybackSource(size="Full HD", src=content_url)]
            attachments = []
            title_name = web_info.get("title") or str(film_id)
            if web_info.get("image"):
                attachments.append(Attachment(url=str(web_info["image"]), name=f"{title_name} Poster", kind="poster"))
            if web_info.get("thumb") and web_info.get("thumb") != web_info.get("image"):
                attachments.append(Attachment(url=str(web_info["thumb"]), name=f"{title_name} Thumbnail", kind="thumbnail"))

            return PlaybackInfo(
                film=web_info,
                playlist=playlist,
                tracks=[],
                chapters=[],
                attachments=attachments,
            )

        err_msg = (data.get("error") or {}).get("errorUserText") or (data.get("error") or {}).get("errorMessage") or "Access denied or stream not found"
        raise KviffAccessDenied(f"KVIFF.TV playback denied for {film_id}: {err_msg}")

    def parse_web_video_page(self, target: str) -> dict[str, Any]:
        """Scrape metadata and content stream from kviff.tv web video page."""
        if target.startswith("http://") or target.startswith("https://"):
            url = target
        else:
            url = f"{WEB_URL}/videos/{target}"

        try:
            # kviff.tv serves full SSR JSON-LD graph to bot/crawler UAs
            headers = {"User-Agent": "Googlebot/2.1 (+http://www.google.com/bot.html)"}
            resp = self.session.get(url, headers=headers, timeout=15.0)
            if resp.status_code != 200:
                return {}
            resp.encoding = "utf-8"
            text = resp.text
        except Exception:
            return {}

        jld_match = re.search(r'<script type="application/ld\+json">(.*?)</script>', text, re.DOTALL)
        if not jld_match:
            return {}

        try:
            data = json.loads(jld_match.group(1))
        except Exception:
            return {}

        graph = data.get("@graph", [])
        movie_obj = next((x for x in graph if x.get("@type") == "Movie"), {})
        video_obj = next((x for x in graph if x.get("@type") == "VideoObject"), {})

        name = movie_obj.get("name") or video_obj.get("name") or ""
        desc = movie_obj.get("description") or video_obj.get("description") or ""
        image = movie_obj.get("image") or ""
        thumb = video_obj.get("thumbnailUrl") or image
        duration_sec = parse_iso_duration(video_obj.get("duration") or "")
        content_url = video_obj.get("contentUrl") or ""
        embed_url = video_obj.get("embedUrl") or ""
        published = movie_obj.get("datePublished") or ""

        dirs: list[str] = []
        for d in movie_obj.get("director") or []:
            if isinstance(d, dict) and "name" in d:
                dirs.append(d["name"])
            elif isinstance(d, str):
                dirs.append(d)

        actors: list[str] = []
        for a in movie_obj.get("actor") or []:
            if isinstance(a, dict) and "name" in a:
                actors.append(a["name"])
            elif isinstance(a, str):
                actors.append(a)

        return {
            "id": target,
            "title": html.unescape(name),
            "description": html.unescape(desc),
            "image": image,
            "thumb": thumb,
            "duration": duration_sec * 1000 if duration_sec else 0,
            "duration_sec": duration_sec,
            "contentUrl": content_url,
            "embedUrl": embed_url,
            "datePublished": published,
            "directors": dirs,
            "actors": actors,
            "genres": movie_obj.get("genre") or [],
        }

    def live_channels(self) -> list[dict[str, Any]]:
        """Retrieve live streams / TV channels if published."""
        channels: list[dict[str, Any]] = []
        try:
            home_data = self.home()
            for ch in home_data.get("channels") or []:
                if isinstance(ch, dict) and (ch.get("isLive") or ch.get("target") == "channel"):
                    channels.append(ch)
        except Exception:
            pass
        return channels
