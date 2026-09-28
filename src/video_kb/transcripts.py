"""Read explicit, source-bound public transcripts without guessing video speech.

The only supported embedded transcript contract is schema.org VideoObject's
transcript property. A description/caption/title is never promoted to transcript.
No referenced files, scripts or media URLs are fetched or executed here.
"""
from __future__ import annotations

import json

from .network import SourceError, normalize_source_url


def embedded_transcript(scripts: list[str], source_url: str) -> str:
    canonical = normalize_source_url(source_url)
    visited = 0
    for script in scripts:
        try:
            value = json.loads(script)
        except (ValueError, RecursionError):
            continue
        stack = [(value, False, 0)]
        while stack:
            item, schema_context, depth = stack.pop()
            visited += 1
            if visited > 3000:
                return ""
            if depth > 12:
                continue
            if isinstance(item, list):
                stack.extend((entry, schema_context, depth + 1) for entry in item)
                continue
            if not isinstance(item, dict):
                continue
            context = item.get("@context")
            if context is not None:
                schema_context = context in ("https://schema.org", "http://schema.org", "https://schema.org/", "http://schema.org/") if isinstance(context, str) else False
            kind = item.get("@type")
            is_video = kind == "VideoObject" or (isinstance(kind, list) and "VideoObject" in kind)
            if schema_context and is_video and isinstance(item.get("transcript"), str) and item["transcript"].strip():
                try:
                    same_source = isinstance(item.get("url"), str) and normalize_source_url(item["url"]) == canonical
                except SourceError:
                    same_source = False
                if same_source:
                    return item["transcript"][:50001]
            stack.extend((child, schema_context, depth + 1) for child in item.values() if isinstance(child, (dict, list)))
    return ""
