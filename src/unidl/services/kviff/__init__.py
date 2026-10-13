"""KVIFF.TV Android TV: TV-code sign-in, token refresh, catalogue, search, clear streaming, attachments, and chapters."""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from pathlib import Path

from ...core.attachments import Attachment, normalize_attachments
from ...core.chapters import Chapter
from ...core.flow import Ask, Back, Choice, FlowContext
from ...core.playback import DrmInfo, ExternalTrack, Playback, SubtitleReference
from ...core.qr import QrPresentation
from ...core.secureio import atomic_write_bytes
from ...core.service import AuthStatus, Capabilities, Service, ServiceContext, registry
from ...core.settings import Option, Setting
from ...core.titles import Title, TitleKind
from . import api

_LANGUAGE = Setting(
    "language",
    "Storefront language",
    kind="choice",
    options=[
        Option("cs", "Čeština (Czech)"),
        Option("en", "English"),
    ],
    default="cs",
    help="Storefront and API language interface. 'cs' is default.",
    resets_session=False,
)

_QUALITY = Setting(
    "video_quality",
    "Preferred video quality",
    kind="choice",
    options=[
        Option("auto", "Automatic (highest available)"),
        Option("Full HD", "Full HD (1080p)"),
        Option("HD", "HD (720p)"),
        Option("SD", "SD (480p/360p)"),
    ],
    default="auto",
    help="Preferred quality ladder profile from KVIFF.TV player stream list.",
    resets_session=False,
)


@registry.register
class Kviff(Service):
    ID = "kviff"
    NAME = "KVIFF.TV"
    TAG = "KVIFF"
    ALIASES = (
        "kviff",
        "kviff.tv",
        "com.abuco.kviff",
        "kvifftv",
    )
    TITLE_RE = (
        r"(?:https?://)?(?:www\.)?kviff\.tv/.*|"
        r"^kviff:(?:(?:film|video|collection|channel)/)?[^/?#]+"
    )
    GEOFENCE = ()
    DESCRIPTION = (
        "KVIFF.TV Android TV 1.17 (com.abuco.kviff): TV-code sign-in, token refresh, "
        "catalogue, search, clear streaming, attachments, and chapters."
    )
    MEDIA_TYPES = ("video",)
    USES = Capabilities()
    SETTINGS = [_LANGUAGE, _QUALITY]
    SUPPORTS_URL = True
    SUPPORTS_SEARCH = True
    SUPPORTS_LIVE = True
    SUPPORTS_LIBRARY = True
    TOKEN_FILE = api.TOKEN_FILE

    def __init__(self, ctx: ServiceContext) -> None:
        super().__init__(ctx)
        self._api: api.KviffApi | None = None
        self._auth_error = ""

    def language(self) -> str:
        return api.normalize_language(self.settings.get("language") or api.DEFAULT_LANGUAGE)

    def token_name(self) -> str:
        return self.TOKEN_FILE

    def _cached_session(self) -> api.Session:
        return api.Session.from_cache(
            self.ctx.tokens.read(self.token_name()),
            language=self.language(),
        )

    def _save_session(self, state: dict) -> None:
        path = self.ctx.tokens.write(self.token_name(), state)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass

    def _runtime_session(self):
        self.ctx.refresh_proxy()
        session = self.ctx.session(user_agent=api.USER_AGENT, cookies=False)
        session.trust_env = False
        return session

    def _new_client(self) -> api.KviffApi:
        return api.KviffApi(
            self._runtime_session(),
            language=self.language(),
            state=self._cached_session(),
            on_save=self._save_session,
        )

    def client(self) -> api.KviffApi:
        wanted_lang = self.language()
        current = self._api
        if current is None or current.language != wanted_lang:
            current = self._new_client()
        self._api = current
        return current

    def auth_status(self) -> AuthStatus:
        if self._auth_error:
            return AuthStatus(
                False,
                self._auth_error,
                detail="cached token retained",
                anonymous_ok=True,
                logout_available=True,
            )
        session = self._cached_session()
        if not session.signed_in:
            return AuthStatus(
                False,
                "anonymous",
                detail="TV code unlocks subscribed/rented films",
                anonymous_ok=True,
            )
        return AuthStatus(
            True,
            f"signed in · {session.label()}",
            detail="KVIFF.TV Android TV session",
            anonymous_ok=True,
            logout_available=True,
        )

    def home(self, ctx: FlowContext) -> Iterator[Ask]:
        session = self._cached_session()
        if session.signed_in:
            try:
                ctx.status("Checking KVIFF.TV session")
                client = self.client()
                client.ensure_session(force=False)
                ctx.log(f"KVIFF.TV session active · {client.state.label()}", "ok")
            except api.KviffError as exc:
                self._auth_error = f"cached session unavailable · {exc}"
                ctx.warn(self._auth_error)
        yield from super().home(ctx)

    def login(self, ctx: FlowContext) -> Iterator[Ask]:
        client = self._new_client()
        if client.state.signed_in:
            try:
                ctx.status("Validating cached KVIFF.TV session")
                client.ensure_session(force=True)
            except api.KviffError:
                pass
            else:
                if client.state.signed_in:
                    self._api = client
                    self._auth_error = ""
                    ctx.log(f"KVIFF.TV session active · {client.state.label()}", "ok")
                    return

        try:
            ctx.status("Requesting KVIFF.TV code")
            challenge = client.start_tv_code()
        except api.KviffError as exc:
            ctx.error(str(exc))
            return

        def poll() -> api.Session | None:
            return client.poll_tv_code(challenge)

        try:
            session = yield ctx.wait_for(
                "Confirm KVIFF.TV on another device",
                [
                    ("Code", challenge.access_code),
                    ("Open", challenge.qr_link),
                    f"Open {challenge.qr_link} to pair, or visit kviff.tv/pair and enter code {challenge.access_code}",
                ],
                poll=poll,
                timeout=challenge.timeout,
                interval=api.TV_POLL_INTERVAL,
                hint="Enter the code on the web page, then return here.",
                qr=QrPresentation(
                    payload=challenge.qr_link,
                    fallback_url=challenge.qr_link,
                    alt="KVIFF.TV TV login",
                ),
            )
        except api.KviffCodeExpired as exc:
            ctx.error(str(exc))
            return
        except api.KviffError as exc:
            ctx.error(str(exc))
            return
        except Back:
            client.cancel_tv_code(challenge)
            return

        if not isinstance(session, api.Session) or not session.signed_in:
            ctx.error("KVIFF.TV TV login completed without a usable session")
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

    def search(self, ctx: FlowContext, query: str) -> Iterator[Ask]:
        client = self.client()
        try:
            ctx.status(f"Searching KVIFF.TV for {query}")
            hits, _ = client.search(query)
        except api.KviffError as exc:
            ctx.error(str(exc))
            return

        if not hits:
            ctx.warn(f"KVIFF.TV found nothing for {query}")
            return

        choices: list[Choice] = []
        for hit in hits:
            details: list[str] = []
            if hit.year:
                details.append(str(hit.year))
            if hit.directors:
                details.append(", ".join(hit.directors))
            if hit.duration_ms:
                mins = round(hit.duration_ms / 60000)
                details.append(f"{mins}m")
            tags: list[str] = []
            if hit.fvod:
                tags.append("Free")
            if hit.svod:
                tags.append("SVOD")
            if hit.price:
                tags.append(hit.price)
            choices.append(
                Choice(
                    hit.title,
                    hit.id,
                    detail=" · ".join(details) if details else "",
                    tags=tuple(tags),
                )
            )

        chosen_id = yield ctx.pick(f"KVIFF.TV · {query}", choices)
        if chosen_id is not None:
            yield from self._emit_film(ctx, chosen_id)

    def library(self, ctx: FlowContext) -> Iterator[Ask]:
        client = self.client()
        category = yield ctx.pick(
            "KVIFF.TV Library",
            [
                Choice("All Films (Alphabet)", "alphabet", detail="Browse catalog from A to Z"),
                Choice("Collections", "collections", detail="Curated festival selections and themes"),
                Choice("Featured Home", "home", detail="Top watched and recommended films"),
            ],
        )
        if category == "alphabet":
            try:
                ctx.status("Loading KVIFF.TV alphabetical catalog")
                items, _ = client.alphabet(page=1, page_size=30)
            except api.KviffError as exc:
                ctx.error(str(exc))
                return
            if not items:
                ctx.warn("No films found in alphabetical catalog")
                return
            choices = [
                Choice(
                    item.title,
                    item.id,
                    detail=" · ".join(
                        part
                        for part in (
                            str(item.year) if item.year else "",
                            ", ".join(item.directors) if item.directors else "",
                            f"{round(item.duration_ms / 60000)}m" if item.duration_ms else "",
                        )
                        if part
                    ),
                    tags=tuple(["Free"] if item.fvod else (["SVOD"] if item.svod else [])),
                )
                for item in items
            ]
            picked = yield ctx.pick("KVIFF.TV · All Films", choices)
            if picked is not None:
                yield from self._emit_film(ctx, picked)
        elif category == "collections":
            try:
                ctx.status("Loading KVIFF.TV collections")
                cols = client.collections()
            except api.KviffError as exc:
                ctx.error(str(exc))
                return
            if not cols:
                ctx.warn("No collections found")
                return
            col_choices = [
                Choice(
                    str(col.get("name") or col.get("title") or "Collection"),
                    str(col.get("id") or col.get("_id")),
                    detail=str(col.get("synopsis") or col.get("description") or ""),
                )
                for col in cols
                if col.get("id") or col.get("_id")
            ]
            picked_col = yield ctx.pick("KVIFF.TV · Collections", col_choices)
            if picked_col is not None:
                yield from self._emit_collection(ctx, picked_col)
        elif category == "home":
            try:
                ctx.status("Loading KVIFF.TV home catalog")
                home_data = client.home()
            except api.KviffError as exc:
                ctx.error(str(exc))
                return
            items_raw = (
                home_data.get("recommended")
                or home_data.get("most-watched")
                or home_data.get("recent")
                or []
            )
            items = [api.FilmItem.from_dict(item) for item in items_raw if isinstance(item, dict)]
            if not items:
                ctx.warn("No featured items found on home screen")
                return
            choices = [
                Choice(
                    item.title,
                    item.id,
                    detail=" · ".join(
                        part
                        for part in (
                            str(item.year) if item.year else "",
                            ", ".join(item.directors) if item.directors else "",
                        )
                        if part
                    ),
                )
                for item in items
            ]
            picked = yield ctx.pick("KVIFF.TV · Featured", choices)
            if picked is not None:
                yield from self._emit_film(ctx, picked)

    def live(self, ctx: FlowContext) -> Iterator[Ask]:
        client = self.client()
        try:
            ctx.status("Checking for KVIFF.TV live channels and broadcasts")
            channels = client.live_channels()
        except Exception as exc:
            ctx.error(f"Failed to check live channels: {exc}")
            return

        if not channels:
            ctx.warn("No active live broadcasts or live channels currently on KVIFF.TV.")
            return

        choices = [
            Choice(
                str(ch.get("name") or ch.get("title") or "Live Channel"),
                str(ch.get("id") or ch.get("_id") or idx),
                detail=str(ch.get("description") or "Live stream"),
            )
            for idx, ch in enumerate(channels)
        ]
        picked_channel = yield ctx.pick("KVIFF.TV · Live", choices)
        if picked_channel is not None:
            yield from self._emit_channel(ctx, picked_channel)

    def open_url(self, ctx: FlowContext, target: str) -> Iterator[Ask]:
        film_id = api.extract_film_id(target)
        if film_id is not None:
            yield from self._emit_film(ctx, film_id)
            return

        channel_id = api.extract_channel_id(target)
        if channel_id is not None:
            yield from self._emit_channel(ctx, channel_id)
            return

        collection_id = api.extract_collection_id(target)
        if collection_id is not None:
            yield from self._emit_collection(ctx, collection_id)
            return

        ctx.error(f"Cannot identify a KVIFF.TV film, collection, or channel from: {target}")

    def _emit_collection(self, ctx: FlowContext, collection_id: str) -> Iterator[Ask]:
        client = self.client()
        try:
            ctx.status(f"Loading collection {collection_id}")
            col_data = client.collection(collection_id)
        except api.KviffError as exc:
            ctx.error(str(exc))
            return

        items_raw: list[dict] = []
        col_title = str(col_data.get("name") or col_data.get("title") or "Collection")
        raw_items = col_data.get("items")
        if isinstance(raw_items, list):
            items_raw = raw_items
        elif isinstance(raw_items, dict):
            items_raw = list(raw_items.values())

        items = [api.FilmItem.from_dict(item) for item in items_raw if isinstance(item, dict)]
        if not items:
            ctx.warn(f"Collection {collection_id} contains no films")
            return

        choices = [
            Choice(
                item.title,
                item.id,
                detail=" · ".join(
                    part
                    for part in (
                        str(item.year) if item.year else "",
                        ", ".join(item.directors) if item.directors else "",
                        f"{round(item.duration_ms / 60000)}m" if item.duration_ms else "",
                    )
                    if part
                ),
                tags=tuple(["Free"] if item.fvod else (["SVOD"] if item.svod else [])),
            )
            for item in items
        ]

        picked = yield ctx.pick(
            f"KVIFF.TV · {col_title}",
            choices,
            multi=True,
            hint="space to tick, enter to confirm",
        )
        selected_ids = list(picked or [])
        if len(selected_ids) > 1:
            ctx.batch(len(selected_ids))
        for film_id in selected_ids:
            yield from self._emit_film(ctx, film_id)

    def _emit_film(self, ctx: FlowContext, film_id: str) -> Iterator[Ask]:
        client = self.client()
        try:
            ctx.status(f"Loading KVIFF.TV film #{film_id}")
            detail = client.detail(film_id)
        except api.KviffError as exc:
            ctx.error(str(exc))
            return

        title = self._title_from_detail(film_id, detail)
        try:
            playback = self.get_playback(title)
        except api.KviffAccessDenied as exc:
            ctx.error(str(exc))
            return
        except api.KviffError as exc:
            ctx.error(f"Playback error for {title.name}: {exc}")
            return

        yield ctx.emit(playback)

    def _emit_channel(self, ctx: FlowContext, channel_id: str) -> Iterator[Ask]:
        title = Title(
            id=str(channel_id),
            kind=TitleKind.CHANNEL,
            name=f"KVIFF.TV Channel {channel_id}",
            service=self.ID,
            channel="KVIFF.TV",
        )
        try:
            playback = self.get_playback(title)
        except api.KviffError as exc:
            ctx.error(f"Live playback error: {exc}")
            return
        yield ctx.emit(playback)

    def get_playback(self, title: Title) -> Playback:
        detail = (title.data or {}).get("detail") if isinstance(title.data, dict) else None
        if not detail:
            detail = self.client().detail(title.id)
        return self._playback(title, detail)

    def _title_from_detail(self, film_id: str, detail: dict) -> Title:
        video = detail.get("video") if isinstance(detail.get("video"), dict) else {}
        name = str(
            video.get("title")
            or detail.get("title")
            or detail.get("name")
            or f"KVIFF.TV {film_id}"
        ).strip()
        synopsis = str(
            video.get("description")
            or video.get("shortDescription")
            or detail.get("description")
            or detail.get("synopsis")
            or ""
        ).strip()

        duration = None
        duration_val = video.get("duration") or detail.get("duration")
        if duration_val:
            try:
                duration = round(float(duration_val) / 1000.0)
            except (ValueError, TypeError):
                pass
        elif detail.get("duration_sec"):
            duration = float(detail["duration_sec"])

        year = None
        attrs = detail.get("attributes") or {}
        if isinstance(attrs, dict) and "country_with_year" in attrs:
            cwy = attrs["country_with_year"]
            if isinstance(cwy, list) and len(cwy) > 1 and str(cwy[1]).isdigit():
                year = int(cwy[1])
        if year is None:
            tags = detail.get("tags") or {}
            if isinstance(tags, dict) and "country_with_year" in tags:
                cwy = tags["country_with_year"]
                if isinstance(cwy, list) and len(cwy) > 1 and str(cwy[1]).isdigit():
                    year = int(cwy[1])
        if year is None and detail.get("year"):
            try:
                year = int(detail["year"])
            except (ValueError, TypeError):
                pass
        if year is None and detail.get("datePublished"):
            import re
            m = re.search(r"\b(19\d\d|20\d\d)\b", str(detail["datePublished"]))
            if m:
                year = int(m.group(1))

        directors = video.get("directors") or detail.get("directors")
        director_str = None
        if isinstance(directors, list):
            director_str = ", ".join(str(d).strip() for d in directors if str(d).strip())

        cover = video.get("image") or detail.get("image") or detail.get("poster")
        thumb = video.get("thumb") or detail.get("thumb") or detail.get("thumbnail")

        kind = TitleKind.CHANNEL if detail.get("type") == "channel" else TitleKind.MOVIE

        return Title(
            id=str(video.get("id") or detail.get("id") or film_id),
            kind=kind,
            name=name,
            year=str(year) if year is not None else None,
            duration=duration,
            synopsis=synopsis or None,
            cover_url=str(cover) if cover else None,
            service=self.ID,
            data={"detail": detail, "directors": director_str, "thumb": thumb},
        )

    def _playback(self, title: Title, detail: dict) -> Playback:
        client = self.client()
        info = client.player(title.id)

        if not info.playlist:
            raise api.KviffError(f"KVIFF.TV returned no video streams for {title.name}")

        requested_quality = str(self.settings.get("video_quality") or "auto").strip()
        chosen: api.PlaybackSource | None = None
        if requested_quality != "auto":
            req_norm = requested_quality.lower().strip()
            # Try exact match first
            for s in info.playlist:
                if s.size.lower().strip() == req_norm:
                    chosen = s
                    break
            # Substring match without matching 'full hd' when 'hd' is wanted
            if chosen is None:
                for s in info.playlist:
                    s_lower = s.size.lower().strip()
                    if req_norm == "hd" and "full" in s_lower:
                        continue
                    if req_norm in s_lower:
                        chosen = s
                        break

        if chosen is None:
            # Prefer Full HD > HD > SD > first available
            for preferred in ("full hd", "hd", "sd"):
                for s in info.playlist:
                    s_lower = s.size.lower().strip()
                    if preferred == "hd" and "full" in s_lower:
                        continue
                    if preferred in s_lower:
                        chosen = s
                        break
                if chosen is not None:
                    break

        if chosen is None:
            chosen = info.playlist[0]

        alternates = tuple(s.src for s in info.playlist if s.src != chosen.src)

        # Build SubtitleReferences
        sub_setting = str(self.settings.get("sub_langs") or "all")
        references: list[SubtitleReference] = []
        for track in info.tracks:
            selected = api.subtitle_selected(sub_setting, track.srclang)
            references.append(
                SubtitleReference(
                    url=track.src,
                    language=track.srclang or "und",
                    name=track.label or track.srclang,
                    original=track.default,
                    selected=selected,
                )
            )

        # Build Attachments (posters & thumbnails)
        attachments: list[Attachment] = list(info.attachments)
        cover = detail.get("image") or detail.get("poster")
        if cover and not any(a.url == cover for a in attachments):
            attachments.append(Attachment(url=str(cover), name=f"{title.name} Poster", kind="poster"))
        thumb = detail.get("thumb") or detail.get("thumbnail")
        if thumb and not any(a.url == thumb for a in attachments):
            attachments.append(Attachment(url=str(thumb), name=f"{title.name} Thumbnail", kind="thumbnail"))

        # Build Chapters
        chapters: list[Chapter] = []
        if self.fetch_chapters_enabled():
            chapters = list(info.chapters)

        headers: dict[str, str] = {}
        if client.state.signed_in:
            headers["Authorization"] = client._auth_header_value()

        return Playback(
            title=title,
            save_name=self.save_name(title),
            manifest_url=chosen.src,
            alternate_manifest_urls=alternates,
            headers=headers,
            proxy=self.ctx.proxy,
            drm=DrmInfo(clear=True),
            subtitle_references=references,
            attachments=normalize_attachments(attachments),
            chapters=chapters,
            video_quality_hint=chosen.size,
            note=f"KVIFF.TV · {chosen.size}",
        )

    def prepare_download(self, playback: Playback, log: Callable[[str], None]) -> None:
        if not playback.subtitle_references:
            return
        staged = {Path(item.path).resolve() for item in playback.mux_imports if item.path}
        for reference in playback.subtitle_references:
            if not reference.selected:
                continue
            track = self._stage_subtitle(playback.save_name, reference, log)
            if track is None:
                continue
            path = Path(track.path).resolve()
            if path in staged:
                continue
            staged.add(path)
            playback.mux_imports.append(track)

    def _stage_subtitle(
        self,
        save_name: str,
        reference: SubtitleReference,
        log: Callable[[str], None],
    ) -> ExternalTrack | None:
        try:
            session = self._runtime_session()
            resp = session.get(reference.url, timeout=30.0)
            resp.raise_for_status()
            data = resp.content
            directory = self.ctx.subtitle_dir()
            directory.mkdir(parents=True, exist_ok=True)
            ext = ".vtt" if b"WEBVTT" in data[:100] else ".srt"
            path = directory / f"{save_name}.{reference.language}{ext}"
            atomic_write_bytes(path, data)
            if log:
                log(f"Staged subtitle [{reference.language}] {reference.name}")
            return ExternalTrack(path=str(path), language=reference.language, name=reference.name)
        except Exception as exc:
            if log:
                log(f"Failed to fetch subtitle {reference.url}: {exc}")
            return None
