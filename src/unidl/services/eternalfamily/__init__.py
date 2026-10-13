"""Eternal Family Android TV service implementation.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterator
from typing import Any

from ...core.attachments import Attachment, normalize_attachments
from ...core.cdm import CdmError
from ...core.chapters import Chapter
from ...core.flow import Ask, Back, Choice, FlowContext
from ...core.playback import DrmInfo, Playback
from ...core.qr import QrPresentation
from ...core.service import AuthStatus, Capabilities, Service, ServiceContext, registry
from ...core.settings import Option, Setting
from ...core.titles import Title, TitleKind
from . import api

_QUALITY = Setting(
    "video_quality",
    "Preferred stream quality",
    kind="choice",
    options=[
        Option("auto", "Automatic (highest available)"),
        Option("1080p", "1080p Full HD"),
        Option("720p", "720p HD"),
        Option("480p", "480p SD"),
    ],
    default="auto",
    help="Quality ladder preference for Mux HLS stream delivery.",
    resets_session=False,
)


@registry.register
class EternalFamily(Service):
    """Eternal Family Android TV 2.7.0 / 2.11.0 (tv.uscreen.eternalfamily2)."""

    ID = "eternalfamily"
    NAME = "Eternal Family"
    TAG = "EFAM"
    ALIASES = (
        "eternal",
        "eternaltv",
        "eternal_family",
        "tv.uscreen.eternalfamily2",
    )
    TITLE_RE = (
        r"(?:https?://)?(?:www\.)?eternal\.tv/(?:programs|catalog)/[^/?#]+|"
        r"^eternalfamily:(?:programs/)?[^/?#]+"
    )
    GEOFENCE = ("US", "CA")
    DESCRIPTION = (
        "Eternal Family Android TV: TV-code pairing, Mux HLS streaming, "
        "catalog, search, 24/7 live channels, chapters, and attachments."
    )
    MEDIA_TYPES = ("video",)
    USES = Capabilities()
    SETTINGS = [_QUALITY]

    SUPPORTS_URL = True
    SUPPORTS_SEARCH = True
    SUPPORTS_LIVE = True
    SUPPORTS_LIBRARY = True
    USES_COOKIES = False
    TOKEN_FILE = api.TOKEN_FILE

    def __init__(self, ctx: ServiceContext) -> None:
        super().__init__(ctx)
        self._api: api.EternalFamilyApi | None = None
        self._auth_error = ""

    def token_name(self) -> str:
        return self.TOKEN_FILE

    def _cached_session(self) -> api.Session:
        cached = self.ctx.tokens.read(self.token_name())
        return api.Session.from_cache(cached)

    def _save_session(self, state: dict[str, Any]) -> None:
        path = self.ctx.tokens.write(self.token_name(), state)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass

    def _runtime_session(self):
        self.ctx.refresh_proxy()
        session = self.ctx.session(user_agent=api.USER_AGENT, cookies=False)
        return session

    def _new_client(self) -> api.EternalFamilyApi:
        return api.EternalFamilyApi(
            self._runtime_session(),
            state=self._cached_session(),
            on_save=self._save_session,
        )

    def client(self) -> api.EternalFamilyApi:
        current = self._api
        if current is None:
            current = self._new_client()
            self._api = current
        return current

    # =========================================================================
    # Authentication & TV Code Pairing
    # =========================================================================

    def auth_status(self) -> AuthStatus:
        """Cheap, non-network report of current login state."""
        if self._auth_error:
            return AuthStatus(
                False,
                self._auth_error,
                detail="cached session unavailable",
                anonymous_ok=True,
                logout_available=True,
            )
        session = self._cached_session()
        if not session.signed_in:
            return AuthStatus(
                False,
                "anonymous",
                detail="TV code activates subscriber catalog",
                anonymous_ok=True,
            )
        return AuthStatus(
            True,
            f"signed in · {session.label()}",
            detail="Eternal Family TV session",
            anonymous_ok=True,
            logout_available=True,
        )

    def home(self, ctx: FlowContext) -> Iterator[Ask]:
        session = self._cached_session()
        if session.signed_in:
            try:
                ctx.status("Validating Eternal Family session")
                client = self.client()
                client.ensure_session(force=False)
            except api.EternalFamilyAuthError as exc:
                self._auth_error = f"session expired · {exc}"
                ctx.warn(self._auth_error)
            except Exception:
                pass
        yield from super().home(ctx)

    def login(self, ctx: FlowContext) -> Iterator[Ask]:
        """Sign in using Android TV pairing code (aligned with APK QRLoginForm)."""
        client = self._new_client()
        ctx.status("Requesting TV pairing code from Eternal Family")
        try:
            challenge = client.start_tv_code()
        except api.EternalFamilyError as exc:
            ctx.error(str(exc))
            return

        def poll() -> api.Session | None:
            return client.poll_tv_code(challenge)

        try:
            session = yield ctx.wait_for(
                "Activate Eternal Family on another device",
                [
                    ("Code", challenge.code),
                    ("Open", challenge.url),
                    f"Visit {challenge.url} and enter code {challenge.code}",
                ],
                poll=poll,
                timeout=challenge.expires_in,
                interval=api.TV_POLL_INTERVAL,
                hint="Enter the code on your phone or computer, then wait for confirmation.",
                qr=QrPresentation(
                    payload=challenge.qr_url or f"{challenge.url}?code={challenge.code}",
                    fallback_url=challenge.url,
                    alt="Eternal Family TV activation",
                ),
            )
        except api.EternalFamilyCodeExpired as exc:
            ctx.error(str(exc))
            return
        except api.EternalFamilyError as exc:
            ctx.error(str(exc))
            return
        except Back:
            return

        if not isinstance(session, api.Session) or not session.signed_in:
            ctx.error("Pairing completed without a verified session")
            return

        self._api = client
        self._auth_error = ""
        self._save_session(session.as_dict())
        ctx.log(f"TV login confirmed · {session.label()}", "ok")

    def logout(self) -> None:
        client = self.client()
        client.logout()
        self.ctx.tokens.write(self.token_name(), {})
        self._auth_error = ""
        super().logout()

    # =========================================================================
    # Browsing & Navigation
    # =========================================================================

    def open_url(self, ctx: FlowContext, target: str) -> Iterator[Ask]:
        permalink = api.extract_permalink(target)
        if not permalink:
            ctx.error(f"Cannot identify an Eternal Family title or channel from: {target}")
            return

        client = self.client()
        ctx.status(f"Loading {permalink}")
        try:
            item = client.program_detail(permalink)
        except api.EternalFamilyError as exc:
            ctx.error(str(exc))
            return

        yield from self._emit_item(ctx, item)

    def search(self, ctx: FlowContext, query: str) -> Iterator[Ask]:
        client = self.client()
        ctx.status(f"Searching Eternal Family for '{query}'")
        try:
            hits = client.search(query)
        except api.EternalFamilyError as exc:
            ctx.error(str(exc))
            return

        if not hits:
            ctx.warn(f"No results found for '{query}'")
            return

        choices = [
            Choice(
                hit.title,
                hit,
                detail=hit.duration or hit.short_description or "",
                tags=("Live",) if hit.content_type == "live_event" else (),
            )
            for hit in hits
        ]

        picked = yield ctx.pick(f"Results for '{query}'", choices)
        if picked is not None and isinstance(picked, api.ProgramItem):
            yield from self._emit_item(ctx, picked)

    def live(self, ctx: FlowContext) -> Iterator[Ask]:
        """Browse and watch 24/7 live channels."""
        client = self.client()
        ctx.status("Loading live channels")
        try:
            channels = client.live_channels()
        except api.EternalFamilyError as exc:
            ctx.error(str(exc))
            return

        if not channels:
            ctx.warn("No active live channels available")
            return

        columns = ["Channel", "Details", "Status"]
        rows = [
            (
                ch.title,
                ch.short_description or ch.description[:60] or "24/7 Channel",
                "ON AIR" if ch.active_stream else "OFFLINE",
            )
            for ch in channels
        ]

        chosen = yield ctx.table(
            "Eternal Family Live TV",
            columns,
            rows,
            channels,
        )
        if chosen is not None and isinstance(chosen, api.ProgramItem):
            yield from self._emit_item(ctx, chosen)

    def library(self, ctx: FlowContext) -> Iterator[Ask]:
        """Browse catalog categories."""
        client = self.client()
        ctx.status("Loading categories")
        try:
            cats = client.categories()
        except api.EternalFamilyError as exc:
            ctx.error(str(exc))
            return

        choices = [
            Choice(
                cat.title,
                cat,
                detail=f"Category #{cat.id}",
                tags=("24/7 Live",) if cat.id == api.LIVE_CATEGORY_ID else (),
            )
            for cat in cats
        ]

        chosen_cat = yield ctx.pick("Eternal Family Catalog", choices)
        if chosen_cat is None or not isinstance(chosen_cat, api.CategoryItem):
            return

        if chosen_cat.id == api.LIVE_CATEGORY_ID:
            yield from self.live(ctx)
            return

        ctx.status(f"Loading {chosen_cat.title}")
        try:
            items = client.category_contents(chosen_cat.id)
        except api.EternalFamilyError as exc:
            ctx.error(str(exc))
            return

        if not items:
            ctx.warn(f"Category '{chosen_cat.title}' is currently empty")
            return

        item_choices = [
            Choice(
                item.title,
                item,
                detail=item.duration or item.short_description or "",
                tags=("Live",) if item.content_type == "live_event" else (),
            )
            for item in items
        ]

        picked = yield ctx.pick(
            f"Eternal Family · {chosen_cat.title}",
            item_choices,
            multi=True,
            hint="space to tick, enter to confirm",
        )
        selected_items = [p for p in (picked or []) if isinstance(p, api.ProgramItem)]
        if len(selected_items) > 1:
            ctx.batch(len(selected_items))
        for item in selected_items:
            yield from self._emit_item(ctx, item)

    # =========================================================================
    # Playback & Media Handover
    # =========================================================================

    def _emit_item(self, ctx: FlowContext, item: api.ProgramItem) -> Iterator[Ask]:
        ctx.status(f"Resolving playback for {item.title}")
        title = self._title_from_item(item)
        try:
            playback = self.get_playback(title, item=item)
        except api.EternalFamilyAccessDenied as exc:
            ctx.error(str(exc))
            return
        except api.EternalFamilyError as exc:
            ctx.error(f"Playback error for {item.title}: {exc}")
            return

        yield ctx.emit(playback)

    def _title_from_item(self, item: api.ProgramItem) -> Title:
        kind = TitleKind.CHANNEL if item.content_type == "live_event" else TitleKind.MOVIE

        # Extract potential year from title, e.g. "Dance Or Die (1987)" -> "1987"
        year: str | None = None
        m_year = re.search(r"\((\d{4})\)", item.title)
        if m_year:
            year = m_year.group(1)

        return Title(
            id=str(item.id),
            kind=kind,
            name=item.title,
            year=year,
            duration=item.duration_seconds,
            synopsis=item.description or item.short_description or None,
            cover_url=item.main_poster or None,
            service=self.ID,
            data={"item": item},
        )

    def get_playback(self, title: Title, *, item: api.ProgramItem | None = None) -> Playback:
        """Construct Playback with Mux HLS manifest, chapters, and attachments."""
        client = self.client()
        prog = item
        if prog is None:
            raw_item = (title.data or {}).get("item") if isinstance(title.data, dict) else None
            if isinstance(raw_item, api.ProgramItem):
                prog = raw_item
            else:
                prog = client.program_detail(title.id)

        info = client.resolve_playback_info(prog)
        manifest_url = info.manifest_url
        if not manifest_url:
            raise api.EternalFamilyError(f"No stream manifest found for {title.name}")

        # Build Chapters from timecodes if enabled
        chapters: list[Chapter] = []
        if self.fetch_chapters_enabled() and prog.timecodes:
            for tc in prog.timecodes:
                if isinstance(tc, dict):
                    tc_title = str(tc.get("title") or tc.get("name") or "Chapter").strip()
                    secs = tc.get("seconds")
                    if secs is not None:
                        try:
                            start_ms = int(float(secs) * 1000)
                            chapters.append(Chapter(title=tc_title, start_ms=start_ms))
                        except (ValueError, TypeError):
                            pass

        # Build Attachments
        attachments: list[Attachment] = []
        for att in prog.attachments:
            if isinstance(att, dict):
                att_name = str(att.get("name") or att.get("title") or "Attachment").strip()
                att_url = str(att.get("url") or att.get("src") or "").strip()
                if att_url:
                    attachments.append(Attachment(name=att_name, url=att_url))

        # Add poster image as attachment if present
        if prog.main_poster:
            attachments.append(
                Attachment(
                    name=f"{title.name} Poster",
                    url=prog.main_poster,
                    kind="poster",
                )
            )

        headers = {
            "User-Agent": api.USER_AGENT,
            "X-Store-Token": api.STORE_TOKEN,
            "X-App-Platform": "android_tv",
            "X-App-Version": api.APP_VERSION,
        }
        if client.state.access_token:
            headers["Authorization"] = f"Bearer {client.state.access_token}"

        is_live = bool(info.is_live or prog.content_type == "live_event" or title.kind == TitleKind.CHANNEL)

        drm = (
            DrmInfo(system=info.drm_system, license_url=info.license_url)
            if (info.drm_system and info.license_url)
            else DrmInfo(clear=True)
        )

        return Playback(
            title=title,
            save_name=self.save_name(title),
            manifest_url=manifest_url,
            headers=headers,
            proxy=self.ctx.proxy,
            is_live=is_live,
            drm=drm,
            chapters=chapters,
            attachments=normalize_attachments(attachments),
            video_quality_hint=str(self.settings.get("video_quality") or "auto"),
            note=f"Eternal Family · {'Live Channel' if is_live else 'Mux HLS'}",
        )

    def get_license(self, challenge: bytes, drm: DrmInfo) -> bytes:
        """Acquire Widevine license from Mux license server."""
        if not drm.license_url:
            raise CdmError("Eternal Family playback returned no Widevine license URL")
        try:
            return self.client().widevine_license(drm.license_url, challenge)
        except api.EternalFamilyError as exc:
            raise CdmError(str(exc)) from exc
