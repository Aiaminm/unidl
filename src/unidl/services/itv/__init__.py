"""ITVX - one native service for the Widevine and PlayReady scripts."""

from __future__ import annotations

import hashlib
import re
import time
from collections.abc import Iterator

from ...core.cdm import CdmError
from ...core.credentials import CredentialSlot
from ...core.flow import Ask, Choice, FlowContext
from ...core.playback import DrmInfo, ExternalTrack, Playback, SubtitleReference
from ...core.secureio import atomic_write_bytes, private_directory
from ...core.service import AuthStatus, Capabilities, Service, registry
from ...core.titles import Title, TitleKind
from ...downloader.models import StreamInfo
from ...downloader.subtitles import SubtitleConversionError, convert_subtitle_file
from . import api

_PLAYREADY_SOAP_ACTION = (
    '"http://schemas.microsoft.com/DRM/2007/03/protocols/AcquireLicense"'
)


class ITV(Service):
    ID = "itv"
    NAME = "ITVX"
    TAG = "ITV"
    ALIASES = ("itvx", "itvhub")
    TITLE_RE = r"(?:www\.)?itv\.com/(?:watch|hub|live)/"
    GEOFENCE = ("GB",)
    DESCRIPTION = "ITVX catalogue, live channels and start-again; Widevine or PlayReady."
    USES = Capabilities()
    DRM_SYSTEMS = ("widevine", "playready")
    CREDENTIALS = [CredentialSlot("default", "ITVX account")]
    SUPPORTS_URL = True
    SUPPORTS_SEARCH = True
    SUPPORTS_LIVE = True

    TOKEN_FILE = "itv_token.json"

    def __init__(self, ctx):
        super().__init__(ctx)
        self._api: api.ItvApi | None = None

    # ------------------------------------------------------------------- auth
    def auth_status(self) -> AuthStatus:
        state = self.ctx.tokens.read(self.TOKEN_FILE) or {}
        now = time.time()
        if state.get("access_token") and int(state.get("token_expires") or 0) > now + 300:
            return AuthStatus(True, "ITVX session active", detail="cache")
        if state.get("refresh_token") and int(state.get("refresh_token_expires") or 0) > now:
            return AuthStatus(True, "ITVX session ready to refresh", detail="cache")
        cred = self.ctx.credential("default")
        if cred.complete:
            return AuthStatus(False, "credentials ready", detail="will sign in on demand")
        return AuthStatus(False, "no ITVX account configured")

    def login(self, ctx: FlowContext) -> Iterator[Ask]:
        cred = self.ctx.credential("default")
        username = cred.username or str(
            (yield ctx.text("ITVX email", placeholder="name@example.com")) or ""
        ).strip()
        if not username:
            ctx.warn("no email entered")
            return
        password = cred.password or str((yield ctx.text("ITVX password", password=True)) or "")
        if not password:
            ctx.warn("no password entered")
            return
        client = api.ItvApi(
            session=self.ctx.session(user_agent=api.USER_AGENT),
            drm_system=self.drm_system(),
        )
        try:
            ctx.status("Signing in to ITVX")
            state = client.authenticate(username, password)
        except api.ItvError as exc:
            ctx.error(str(exc))
            return
        self.ctx.tokens.write(self.TOKEN_FILE, state)
        self._api = client
        ctx.log("ITVX session saved", "ok")

    def logout(self) -> None:
        self.ctx.tokens.remove(self.TOKEN_FILE)
        self._api = None

    def client(self, ctx: FlowContext | None = None) -> api.ItvApi:
        chosen = self.drm_system()
        if self._api is not None and self._api.drm_system == chosen:
            return self._api
        state = self.ctx.tokens.read(self.TOKEN_FILE) or {}
        client = api.ItvApi(
            session=self.ctx.session(user_agent=api.USER_AGENT),
            state=state,
            drm_system=chosen,
        )
        cred = self.ctx.credential("default")
        if ctx:
            ctx.status("Opening ITVX session")
        fresh = client.authenticate(cred.username, cred.password)
        if fresh is not state or fresh.get("saved_at") != state.get("saved_at"):
            self.ctx.tokens.write(self.TOKEN_FILE, fresh)
            if ctx:
                ctx.log("ITVX token refreshed", "ok")
        self._api = client
        return client

    # ---------------------------------------------------------------- browsing
    def open_url(self, ctx: FlowContext, target: str) -> Iterator[Ask]:
        parsed = api.parse_input(target)
        if parsed is None:
            ctx.error(
                f"That does not look like an ITVX link or content id: {target}\n"
                "Expected an itv.com/watch or /live URL, or an ITV legacy id."
            )
            return
        try:
            client = self.client(ctx)
            if parsed.kind == "live":
                yield from self._emit_live(ctx, client, client.channel(parsed.value))
            else:
                yield from self._show(
                    ctx,
                    client,
                    client.show(
                        parsed.value,
                        shortform_category=parsed.shortform_category,
                    ),
                )
        except api.ItvError as exc:
            ctx.error(str(exc))

    def search(self, ctx: FlowContext, query: str) -> Iterator[Ask]:
        try:
            client = self.client(ctx)
            ctx.status(f"Searching ITVX for {query}")
            hits = [hit for hit in client.search(query) if hit.route in {"vod", "live"}]
        except api.ItvError as exc:
            ctx.error(str(exc))
            return
        if not hits:
            ctx.warn(f"ITVX found nothing for {query}")
            return
        chosen = yield ctx.pick(
            f"ITVX  ·  {query}",
            [
                Choice(
                    hit.title,
                    hit,
                    detail=hit.synopsis[:90],
                    tags=tuple(
                        value
                        for value in (
                            hit.kind,
                            hit.genre,
                            f"{hit.episode_count} episodes" if hit.episode_count else "",
                        )
                        if value
                    ),
                )
                for hit in hits
            ],
        )
        if chosen is None:
            return
        try:
            if chosen.route == "live":
                yield from self._emit_live(ctx, client, client.channel(chosen.channel_id))
            else:
                yield from self._show(ctx, client, client.show(chosen.content_id))
        except api.ItvError as exc:
            ctx.error(str(exc))

    def _show(self, ctx: FlowContext, client: api.ItvApi, show: api.Show) -> Iterator[Ask]:
        if show.films:
            picked = show.films if len(show.films) == 1 else (
                yield ctx.pick(
                    show.title,
                    [Choice(item.label, item, detail=item.synopsis[:90]) for item in show.films],
                    multi=True,
                )
            )
            yield from self._emit_many(ctx, client, list(picked or []))
            return
        if not show.seasons:
            ctx.warn(f"ITVX returned no episodes for {show.title}")
            return
        season = show.seasons[0] if len(show.seasons) == 1 else (
            yield ctx.pick(
                f"{show.title}  ·  series",
                [Choice(entry.label, entry) for entry in show.seasons],
            )
        )
        if season is None:
            return
        picked = yield ctx.pick(
            f"{show.title}  ·  {season.label}",
            [Choice(item.label, item, detail=item.synopsis[:90]) for item in season.episodes],
            multi=True,
            hint="space to tick, enter to confirm",
        )
        yield from self._emit_many(ctx, client, list(picked or []))

    def _emit_many(self, ctx: FlowContext, client: api.ItvApi, items: list[api.Episode]) -> Iterator[Ask]:
        if len(items) > 1:
            ctx.batch(len(items))
        for item in items:
            yield from self._emit(ctx, client, item)

    # ------------------------------------------------------------------- live
    def live(self, ctx: FlowContext) -> Iterator[Ask]:
        try:
            client = self.client(ctx)
            ctx.status("Loading ITVX live channels")
            channels = client.channels()
        except api.ItvError as exc:
            ctx.error(str(exc))
            return
        chosen = yield ctx.table(
            f"ITVX  ·  {len(channels)} live channels",
            ["Channel", "Now", "Next"],
            [(channel.name, channel.now, channel.next) for channel in channels],
            channels,
        )
        if chosen is not None:
            yield from self._emit_live(ctx, client, chosen)

    def _emit_live(self, ctx: FlowContext, client: api.ItvApi, channel: api.Channel) -> Iterator[Ask]:
        try:
            ctx.status(f"Resolving {channel.name}")
            source = client.live_source(channel, self.drm_system())
        except api.ItvError as exc:
            ctx.error(str(exc))
            return
        restart = False
        title = Title(
            id=channel.id,
            kind=TitleKind.CHANNEL,
            name=channel.name,
            channel=channel.name,
            episode_name=channel.now or None,
            synopsis=channel.synopsis or None,
            service=self.ID,
        )
        if source.start_again:
            mode = yield ctx.pick(
                f"{channel.name}  ·  {channel.now or 'Live'}",
                [Choice("Watch live", "live"), Choice("Restart current programme", "restart")],
            )
            if mode is None:
                return
            restart = mode == "restart"
            if restart:
                source = api.Source(
                    manifest=source.start_again,
                    license_url=source.license_url,
                    encrypted=source.encrypted,
                    protocol=source.protocol,
                    subtitles=source.subtitles,
                )
        yield ctx.emit(
            self._playback(
                title,
                source,
                is_live=True,
                restart=restart,
            )
        )

    # --------------------------------------------------------------- playback
    def _emit(self, ctx: FlowContext, client: api.ItvApi, item: api.Episode) -> Iterator[Ask]:
        try:
            ctx.status(f"Resolving {item.label}")
            source = client.source(item, self.drm_system())
        except api.ItvError as exc:
            ctx.error(f"{item.label}: {exc}")
            return
        title = Title(
            id=item.id,
            kind=TitleKind.MOVIE if item.film else TitleKind.EPISODE,
            name=(
                item.title
                if item.film or (item.season is None and item.number is None)
                else item.show_title
            ),
            year=item.year or None,
            season=None if item.film else item.season,
            episode=None if item.film else item.number,
            episode_name=None,
            genre=item.genre or None,
            synopsis=item.synopsis or None,
            service=self.ID,
        )
        yield ctx.emit(self._playback(title, source))

    def augment_tracks(self, playback: Playback, tracks, log):
        del tracks, log
        return [
            StreamInfo(
                manifest_type="service",
                media_type="subtitle",
                url=reference.url,
                original_url=reference.url,
                id=f"itv-subtitle-{index}",
                group_id=reference.kind,
                name=reference.name or reference.language,
                language=reference.language,
                codecs="webvtt",
                extension="vtt",
                extra={
                    "service_sidecar": "subtitle",
                    "subtitle_reference_url": reference.url,
                    "subtitle_kind": reference.kind,
                },
            )
            for index, reference in enumerate(playback.subtitle_references, 1)
        ]

    def prepare_download(self, playback: Playback, log) -> None:
        selected = [reference for reference in playback.subtitle_references if reference.selected]
        if not selected:
            return
        client = self.client()
        directory = private_directory(self.ctx.subtitle_dir())
        safe_name = _safe_component(playback.save_name) or "ITVX"
        output_format = str(self.settings.get("sub_format", "srt") or "srt")
        auto_fix = bool(self.settings.get("auto_subtitle_fix", True))
        for reference in selected:
            subtitle = api.Subtitle(
                url=reference.url,
                language=reference.language.split("-", 1)[0],
                label=reference.name or reference.language,
                locale=reference.language,
            )
            locale = _safe_component(subtitle.locale.replace("-", "_")) or "und"
            try:
                data = client.subtitle_bytes(subtitle)
                digest = hashlib.sha256(subtitle.url.encode() + b"\0" + data).hexdigest()[:12]
                original = directory / f"{safe_name}_{locale}_{digest}.vtt"
                atomic_write_bytes(original, data)
            except (api.ItvError, OSError) as exc:
                log(f"could not stage {subtitle.label} subtitles: {exc}", "warning")
                continue
            try:
                converted = convert_subtitle_file(
                    original,
                    output_format=output_format,
                    output_path=original.with_suffix(f".{output_format}"),
                    auto_fix=auto_fix,
                )
                if not converted.is_file() or converted.stat().st_size == 0:
                    raise SubtitleConversionError("no subtitle output was produced")
            except (SubtitleConversionError, OSError) as exc:
                log(f"could not convert {subtitle.label} subtitles: {exc}", "warning")
                continue
            log(f"subtitle prepared · {subtitle.label}", "ok")
            if not any(track.path == str(converted) for track in playback.mux_imports):
                playback.mux_imports.append(
                    ExternalTrack(
                        path=str(converted),
                        language=subtitle.language or "und",
                        name=subtitle.label,
                    )
                )

    def _playback(
        self,
        title: Title,
        source: api.Source,
        *,
        is_live: bool = False,
        restart: bool = False,
    ) -> Playback:
        playback = Playback(
            title=title,
            save_name=self.save_name(title),
            manifest_url=source.manifest,
            headers={"User-Agent": api.USER_AGENT, "Origin": api.WWW, "Referer": f"{api.WWW}/"},
            proxy=self.ctx.proxy,
            is_live=is_live,
            note=f"{source.line()}{' · restart' if restart else ''}",
            subtitle_references=[
                SubtitleReference(
                    url=subtitle.url,
                    language=subtitle.locale,
                    name=subtitle.label,
                )
                for subtitle in source.subtitles
            ],
        )
        playback.drm = (
            DrmInfo(system=self.drm_system(), license_url=source.license_url)
            if source.encrypted
            else DrmInfo(clear=True)
        )
        return playback

    def get_license(self, challenge: bytes, drm: DrmInfo) -> bytes:
        client = self._api
        if client is None:
            raise CdmError("ITVX session is not open")
        try:
            response = client.session.post(
                drm.license_url,
                data=challenge,
                headers={"Content-Type": "application/octet-stream", "User-Agent": api.USER_AGENT},
                timeout=30,
            )
            response.raise_for_status()
        except Exception as exc:
            raise CdmError(f"ITVX Widevine licence failed: {exc}") from exc
        return response.content

    def get_license_soap(self, challenge: str, drm: DrmInfo) -> str:
        client = self._api
        if client is None:
            raise CdmError("ITVX session is not open")

        try:
            response = client.session.post(
                drm.license_url,
                data=challenge,
                headers={
                    "Content-Type": "text/xml; charset=UTF-8",
                    "SOAPAction": _PLAYREADY_SOAP_ACTION,
                    "User-Agent": api.USER_AGENT,
                },
                timeout=30,
            )
            response.raise_for_status()
        except Exception as exc:
            raise CdmError(f"ITVX PlayReady licence failed: {exc}") from exc
        return response.text


def _safe_component(value: str) -> str:
    return re.sub(r"[^\w.-]+", "_", value or "").strip("._")


registry.register(ITV)

__all__ = ["ITV", "api"]
