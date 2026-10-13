"""DAFilms Android TV: TV-code sign-in, token refresh, catalogue, search, clear streaming, attachments, and chapters."""

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

_REGION = Setting(
    "region",
    "Storefront region",
    kind="choice",
    options=[
        Option("global", "Global / International (dafilms.com)"),
        Option("cz", "Czech Republic (dafilms.cz)"),
        Option("sk", "Slovakia (dafilms.sk)"),
    ],
    default="global",
    help="Storefront and catalog regional storefront. dafilms.com is default.",
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
        Option("SD", "SD (480p/576p)"),
    ],
    default="auto",
    help="Preferred quality ladder profile from the DAFilms player stream list.",
    resets_session=False,
)


@registry.register
class DAFilms(Service):
    ID = "dafilms"
    NAME = "DAFilms"
    TAG = "DAF"
    ALIASES = (
        "dafilms",
        "dafilms.com",
        "dafilms.cz",
        "dafilms.sk",
        "dafilms.net",
        "com.abuco.dafilms",
    )
    TITLE_RE = (
        r"(?:https?://)?(?:www\.)?dafilms\.(?:com|cz|sk)/(?:film|collection|film-selection|program|selection|special)/[^/?#]+|"
        r"^dafilms:(?:(?:film|collection|film-selection|program|selection|special)/)?\d+"
    )
    GEOFENCE = ()
    DESCRIPTION = (
        "DAFilms Android TV 1.18 (com.abuco.dafilms): TV-code sign-in, token refresh, "
        "catalogue, search, clear streaming, attachments, and chapters."
    )
    MEDIA_TYPES = ("video",)
    USES = Capabilities()
    SETTINGS = [_REGION, _QUALITY]
    SUPPORTS_URL = True
    SUPPORTS_SEARCH = True
    SUPPORTS_LIVE = False
    SUPPORTS_LIBRARY = True
    TOKEN_FILE = api.TOKEN_FILE

    def __init__(self, ctx: ServiceContext) -> None:
        super().__init__(ctx)
        self._api: api.DAFilmsApi | None = None
        self._auth_error = ""

    def region(self) -> str:
        return api.normalize_region(self.settings.get("region") or api.DEFAULT_REGION)

    def token_name(self) -> str:
        return self.TOKEN_FILE

    def _cached_session(self) -> api.Session:
        return api.Session.from_cache(
            self.ctx.tokens.read(self.token_name()),
            region=self.region(),
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

    def _new_client(self) -> api.DAFilmsApi:
        return api.DAFilmsApi(
            self._runtime_session(),
            region=self.region(),
            state=self._cached_session(),
            on_save=self._save_session,
        )

    def client(self) -> api.DAFilmsApi:
        wanted_region = self.region()
        current = self._api
        if current is None or current.region != wanted_region:
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
            detail="DAFilms Android TV session",
            anonymous_ok=True,
            logout_available=True,
        )

    def home(self, ctx: FlowContext) -> Iterator[Ask]:
        session = self._cached_session()
        if session.signed_in:
            try:
                ctx.status("Checking DAFilms session")
                client = self.client()
                client.ensure_session(force=False)
                ctx.log(f"DAFilms session active · {client.state.label()}", "ok")
            except api.DAFilmsError as exc:
                self._auth_error = f"cached session unavailable · {exc}"
                ctx.warn(self._auth_error)
        yield from super().home(ctx)

    def login(self, ctx: FlowContext) -> Iterator[Ask]:
        client = self._new_client()
        if client.state.signed_in:
            try:
                ctx.status("Validating cached DAFilms session")
                client.ensure_session(force=True)
            except api.DAFilmsError:
                pass
            else:
                if client.state.signed_in:
                    self._api = client
                    self._auth_error = ""
                    ctx.log(f"DAFilms session active · {client.state.label()}", "ok")
                    return

        try:
            ctx.status("Requesting DAFilms TV code")
            challenge = client.start_tv_code()
        except api.DAFilmsError as exc:
            ctx.error(str(exc))
            return

        def poll() -> api.Session | None:
            return client.poll_tv_code(challenge)

        try:
            session = yield ctx.wait_for(
                "Confirm DAFilms on another device",
                [
                    ("Code", challenge.access_code),
                    ("Open", challenge.qr_link),
                    f"Visit {challenge.qr_link} or dafilms.com/tvlogin and enter code {challenge.access_code}",
                ],
                poll,
                timeout=challenge.timeout,
                interval=api.TV_POLL_INTERVAL,
                hint="Enter the code on the web page, then return here.",
                qr=QrPresentation(
                    payload=challenge.qr_link,
                    fallback_url=challenge.qr_link,
                    alt="DAFilms TV login",
                ),
            )
        except api.DAFilmsCodeExpired as exc:
            ctx.error(str(exc))
            return
        except api.DAFilmsError as exc:
            ctx.error(str(exc))
            return
        except Back:
            return

        if not isinstance(session, api.Session) or not session.signed_in:
            ctx.error("DAFilms TV login completed without a usable session")
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
            ctx.status(f"Searching DAFilms for {query}")
            hits, _ = client.search(query)
        except api.DAFilmsError as exc:
            ctx.error(str(exc))
            return

        if not hits:
            ctx.warn(f"DAFilms found nothing for {query}")
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

        chosen_id = yield ctx.pick(f"DAFilms · {query}", choices)
        if chosen_id is not None:
            yield from self._emit_film(ctx, chosen_id)

    def library(self, ctx: FlowContext) -> Iterator[Ask]:
        client = self.client()
        category = yield ctx.pick(
            "DAFilms Library",
            [
                Choice("Watch for Free", "free", detail="Free catalog (collection 72)"),
                Choice("All Films", "films", detail="Browse all available films"),
                Choice("Collections & Festivals", "collections", detail="Curated selections and festival retrospectives"),
            ],
        )
        if category == "free":
            yield from self.open_url(ctx, "dafilms:collection/72")
        elif category == "films":
            try:
                ctx.status("Loading DAFilms catalog")
                items, _ = client.films(page=1, page_size=30)
            except api.DAFilmsError as exc:
                ctx.error(str(exc))
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
            picked = yield ctx.pick("DAFilms · All Films", choices)
            if picked is not None:
                yield from self._emit_film(ctx, picked)
        elif category == "collections":
            try:
                ctx.status("Loading collections")
                home_data = client.home()
                cols = home_data.get("collections") or []
            except api.DAFilmsError as exc:
                ctx.error(str(exc))
                return
            if not cols:
                ctx.warn("No collections found")
                return
            col_choices = [
                Choice(
                    col.get("name") or "Collection",
                    col.get("id"),
                    detail=col.get("description") or "",
                )
                for col in cols
                if col.get("id") is not None
            ]
            picked_col = yield ctx.pick("DAFilms · Collections", col_choices)
            if picked_col is not None:
                yield from self.open_url(ctx, f"dafilms:collection/{picked_col}")

    def live(self, ctx: FlowContext) -> Iterator[Ask]:
        ctx.warn("DAFilms does not provide live broadcasts or live channels.")
        return
        yield  # Make it a generator

    def open_url(self, ctx: FlowContext, target: str) -> Iterator[Ask]:
        collection_id = api.extract_collection_id(target)
        if collection_id is not None:
            is_program = any(
                k in target.lower()
                for k in ("program", "film-selection", "selection", "special")
            )
            yield from self._emit_collection(ctx, collection_id, prefer_selection=is_program)
            return

        film_id = api.extract_film_id(target)
        if film_id is not None:
            yield from self._emit_film(ctx, film_id)
            return

        ctx.error(f"Cannot identify a DAFilms film or collection from: {target}")

    def _emit_collection(
        self,
        ctx: FlowContext,
        collection_id: int | str,
        *,
        prefer_selection: bool = False,
    ) -> Iterator[Ask]:
        client = self.client()
        try:
            ctx.status(f"Loading collection {collection_id}")
            col_data = client.collection(collection_id, prefer_selection=prefer_selection)
        except api.DAFilmsError as exc:
            ctx.error(str(exc))
            return

        # Collection response contains a list of items or list of collections with items
        items_raw: list[dict] = []
        col_title = "Collection"
        if isinstance(col_data, list):
            for c in col_data:
                if isinstance(c, dict):
                    col_title = c.get("name") or col_title
                    items_raw.extend(c.get("items") or [])
        elif isinstance(col_data, dict):
            col_title = col_data.get("name") or col_title
            items_raw = col_data.get("items") or []

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
            f"DAFilms · {col_title}",
            choices,
            multi=True,
            hint="space to tick, enter to confirm",
        )
        selected_ids = list(picked or [])
        if len(selected_ids) > 1:
            ctx.batch(len(selected_ids))
        for film_id in selected_ids:
            yield from self._emit_film(ctx, film_id)

    def _emit_film(self, ctx: FlowContext, film_id: int | str) -> Iterator[Ask]:
        client = self.client()
        try:
            ctx.status(f"Loading DAFilms #{film_id}")
            detail = client.detail(film_id)
        except api.DAFilmsError as exc:
            ctx.error(str(exc))
            return

        title = self._title_from_detail(film_id, detail)
        try:
            playback = self.get_playback(title)
        except api.DAFilmsAccessDenied as exc:
            ctx.error(str(exc))
            return
        except api.DAFilmsError as exc:
            ctx.error(f"Playback error for {title.name}: {exc}")
            return

        yield ctx.emit(playback)

    def get_playback(self, title: Title) -> Playback:
        detail = (title.data or {}).get("detail") if isinstance(title.data, dict) else None
        if not detail:
            detail = self.client().detail(title.id)
        return self._playback(title, detail)

    def _title_from_detail(self, film_id: int | str, detail: dict) -> Title:
        video = detail.get("video") or {}
        name = str(video.get("title") or f"DAFilms {film_id}").strip()
        synopsis = str(video.get("description") or video.get("shortDescription") or "").strip()

        duration = None
        if video.get("duration"):
            try:
                duration = round(float(video["duration"]) / 1000.0)
            except (ValueError, TypeError):
                pass

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

        directors = video.get("directors")
        director_str = ", ".join(str(d).strip() for d in directors) if isinstance(directors, list) else None
        cover = video.get("image") or detail.get("film_qrcode")

        return Title(
            id=str(video.get("id") or film_id),
            kind=TitleKind.MOVIE,
            name=name,
            year=str(year) if year is not None else None,
            duration=duration,
            synopsis=synopsis or None,
            cover_url=str(cover) if cover else None,
            service=self.ID,
            data={"detail": detail, "directors": director_str},
        )

    def _playback(self, title: Title, detail: dict) -> Playback:
        client = self.client()
        info = client.player(title.id)

        if not info.playlist:
            raise api.DAFilmsError(f"DAFilms returned no video streams for {title.name}")

        requested_quality = str(self.settings.get("video_quality") or "auto").strip()
        chosen: api.PlaybackSource | None = None
        if requested_quality != "auto":
            for s in info.playlist:
                if requested_quality.lower() in s.size.lower():
                    chosen = s
                    break

        if chosen is None:
            # Prefer Full HD > HD > SD > first available
            for preferred in ("full hd", "hd", "sd"):
                for s in info.playlist:
                    if preferred in s.size.lower():
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

        # Build Attachments
        attachments: list[Attachment] = list(info.attachments)
        cover = detail.get("video", {}).get("image")
        if cover and not any(a.url == cover for a in attachments):
            attachments.append(Attachment(url=str(cover), name=f"{title.name} Poster", kind="poster"))

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
            note=f"DAFilms · {chosen.size}",
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
