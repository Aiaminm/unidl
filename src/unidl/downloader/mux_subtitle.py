"""Choose the default subtitle without changing the selected subtitle set."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from .models import StreamInfo
from .selection import _LANGUAGE_ALIASES, _extra_bool, _subtitle_kinds, _subtitle_sort_key
from .utils import compact_join, pretty_codec


def normalize_default_subtitle(value: object) -> str:
    """Normalize one subtitle preference, never a subtitle filter."""
    value = str(value or "auto").strip().replace("_", "-").casefold()
    if not re.fullmatch(r"[a-z]{2,8}(?:-[a-z0-9]{1,8})*", value):
        raise ValueError("Use auto, original, or one language tag, for example en or fr-CA.")
    return value


def _language(value: object) -> str:
    tag = str(value or "und").strip().replace("_", "-").casefold()
    primary, separator, suffix = tag.partition("-")
    return _LANGUAGE_ALIASES.get(primary, primary) + (separator + suffix if separator else "")


def _matches(language: object, wanted: str) -> bool:
    actual, target = _language(language), _language(wanted)
    return actual == target or ("-" not in target and actual.partition("-")[0] == target)


def _role(stream: StreamInfo) -> str:
    return " ".join(
        str(value or "")
        for value in (stream.role, stream.name, stream.group_id, stream.extra.get("subtitle_kind"))
    ).casefold().replace("_", " ").replace("-", " ")


def original_subtitle(stream: StreamInfo) -> bool:
    """Whether a provider explicitly identifies this subtitle as original."""
    extra = stream.extra if isinstance(stream.extra, dict) else {}
    if any(
        _extra_bool(stream, key)
        for key in ("original", "is_original", "isOriginal", "subtitle_original")
    ):
        return True
    original_language = extra.get("original_language")
    if isinstance(original_language, str) and original_language.strip() and original_language.casefold() not in {
        "true", "false", "yes", "no", "und", "unknown"
    }:
        if _language(original_language) == _language(stream.language):
            return True
    return bool(re.search(r"\boriginal\b", _role(stream)))


def source_default_subtitle(stream: StreamInfo) -> bool:
    """Read a source default marker without treating it as a user preference."""
    raw = stream.extra.get("raw")
    if isinstance(raw, dict) and any(
        _extra_bool(StreamInfo("file", extra=raw), key)
        for key in ("default", "is_default", "default_track", "isDefault")
    ):
        return True
    return any(
        _extra_bool(stream, key)
        for key in ("default", "is_default", "default_track", "isDefault")
    ) or bool(re.search(r"\bdefault\b", _role(stream)))


def _ordinary(stream: StreamInfo) -> bool:
    kinds = _subtitle_kinds(stream)
    return not kinds.intersection({"forced", "sdh", "commentary", "audio_description"})


def _best(candidates: list[StreamInfo]) -> StreamInfo:
    # Keep the shared priority order, while preserving selected-list order on
    # exact ties instead of using a provider title as a hidden tie-breaker.
    best = candidates[0]
    for candidate in candidates[1:]:
        if _subtitle_sort_key(candidate)[:2] > _subtitle_sort_key(best)[:2]:
            best = candidate
    return best


@dataclass(frozen=True)
class DefaultSubtitleChoice:
    stream: StreamInfo | None
    preference: str = "auto"
    fallback: str = ""  # missing_language | missing_original | no_default
    supported: bool = True

    @property
    def label(self) -> str:
        if self.stream is None:
            return ""
        stream = self.stream
        codec = pretty_codec(stream.codecs, "subtitle") or stream.codecs
        kinds = ", ".join(sorted(_subtitle_kinds(stream)))
        return compact_join([stream.language or "und", codec, kinds if kinds != "normal" else None])


def resolve_default_subtitle(
    selected: Sequence[StreamInfo],
    preference: object = "auto",
) -> DefaultSubtitleChoice:
    """Resolve one default among the final selected subtitle tracks only."""
    wanted = normalize_default_subtitle(preference)
    subtitles = [
        stream
        for stream in selected
        if stream.media_type in {"subtitle", "subtitles", "text"}
    ]
    if not subtitles:
        return DefaultSubtitleChoice(None, wanted)

    fallback = ""
    candidates = subtitles
    if wanted not in {"auto", "original"}:
        candidates = [stream for stream in subtitles if _matches(stream.language, wanted)]
        if not candidates:
            fallback = "missing_language"
            candidates = [stream for stream in subtitles if original_subtitle(stream)]
            if not candidates:
                return DefaultSubtitleChoice(None, wanted, fallback="no_default")
    elif wanted == "original":
        candidates = [stream for stream in subtitles if original_subtitle(stream)]
        if not candidates:
            return DefaultSubtitleChoice(None, wanted, fallback="missing_original")
    else:
        marked = [stream for stream in candidates if source_default_subtitle(stream)]
        if marked:
            # An explicit provider default wins over role heuristics. Within
            # several marked rows, keep ordinary subtitles ahead of SDH/forced.
            ordinary_marked = [stream for stream in marked if _ordinary(stream)]
            candidates = ordinary_marked or marked
        else:
            original = [stream for stream in candidates if original_subtitle(stream)]
            if original:
                candidates = original
            else:
                return DefaultSubtitleChoice(None, wanted, fallback="no_default")

    ordinary = [stream for stream in candidates if _ordinary(stream)]
    candidates = ordinary or candidates
    return DefaultSubtitleChoice(_best(candidates), wanted, fallback)
