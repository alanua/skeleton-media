"""Pure, opt-in provenance adapter; this module does not integrate any HTTP route.

The caller must verify that jobs belong to the catalog item and that each job's
``sources`` list contains actual source records, not arbitrary catalog metadata.
Jobs are ordered ``(mtime, job)`` rows; each job has ``job_id`` and sources have
``source_id`` (and optionally the same ``job_id``). IDs are nonempty strings.
Alternatively ``verified_source`` is a caller-attested source record, accepted
without group membership; its job page fallback still requires both IDs.

No URL inspection can prove a remote resource is HTML without fetching it. The
validator therefore accepts only syntactically public webpage candidates from
these provenance fields, conservatively rejecting media paths and most queries.
It performs no DNS, I/O, or mutations. Callers retain responsibility for provenance
and for any network policy applied when a user later opens a link.
"""

from collections.abc import Mapping
import ipaddress
import re
from urllib.parse import parse_qsl, unquote, urlsplit

_MEDIA = re.compile(
    r"\.(?:m3u8?|mp4|mpd|m4[av]|mp[23]|webm|mkv|avi|mov|ts|aac|ogg|wav|"
    r"flac|jpg|jpeg|png|gif|webp|svg|pdf|apk)(?:$|[/;])", re.I
)
_FIELDS = ("discovery_page_url", "source_page_url", "history_page_url")
_MAX_SITES = 16


def validated_page_url(value):
    """Return an unchanged safe webpage candidate, or None (never resolve DNS).

    Queries allow only numeric episode/season/page IDs, plus YouTube's canonical
    /watch?v=<11-character video ID>. Fragments, ports other than 80/443,
    escaped URL delimiters, credentials, and ambiguous host spellings fail closed.
    """
    if not isinstance(value, str) or not value or len(value) > 2048:
        return None
    decoded = unquote(value)
    if any(ord(c) <= 32 or ord(c) >= 127 for c in decoded) or "\\" in decoded:
        return None
    if re.search(r"%(?![0-9a-fA-F]{2})", value) or "%" in decoded:
        return None
    try:
        parts = urlsplit(value)
        host = parts.hostname
        if (parts.scheme not in ("http", "https") or not host or parts.username
                or parts.password or "@" in parts.netloc or parts.fragment
                or parts.port not in (None, 80, 443) or "%" in parts.netloc):
            return None
        # Require ordinary DNS names; IP literals are deliberately all rejected.
        try:
            ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            return None
        labels = host.split(".")
        if (len(labels) < 2 or not re.fullmatch(r"[a-zA-Z]{2,63}", labels[-1])
                or any(not re.fullmatch(r"[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?", x)
                       for x in labels)
                or labels[-1].lower() in {"localhost", "local", "internal", "lan", "home", "test", "invalid", "example", "onion"}
                or host.lower() == "localhost" or host.lower().endswith(".localhost")):
            return None
        path = unquote(parts.path)
        if _MEDIA.search(path) or re.search(r"%(?:2f|3f|23|40|3a)", value, re.I):
            return None
        if parts.query:
            query = parse_qsl(parts.query, keep_blank_values=True, strict_parsing=True)
            if host.lower() in {"youtube.com", "www.youtube.com", "m.youtube.com"}:
                if not (path == "/watch" and len(query) == 1 and query[0][0] == "v"
                        and re.fullmatch(r"[A-Za-z0-9_-]{11}", query[0][1])):
                    return None
            elif (not query or len({key for key, _ in query}) != len(query)
                  or any(key not in {"id", "page", "episode", "season"}
                         or not re.fullmatch(r"[0-9]{1,10}", val) for key, val in query)):
                return None
    except (ValueError, UnicodeError):
        return None
    return value


def _identity(value):
    return value if isinstance(value, str) and value.strip() == value and value else None


def _records(group):
    if not isinstance(group, Mapping) or not isinstance(group.get("jobs"), (list, tuple)):
        return
    for row in group["jobs"]:
        if not isinstance(row, (tuple, list)) or len(row) != 2 or not isinstance(row[1], Mapping):
            continue
        job = row[1]
        job_id = _identity(job.get("job_id"))
        sources = job.get("sources")
        if not job_id or not isinstance(sources, (list, tuple)):
            continue
        for source in sources:
            if (isinstance(source, Mapping) and _identity(source.get("source_id"))
                    and source.get("job_id", job_id) == job_id):
                yield job_id, source, job


def _pages(source, job):
    pages = []
    for key in _FIELDS:
        page = validated_page_url(source.get(key))
        if page:
            pages.append(page)
            break
    if not pages and job is not None:
        page = validated_page_url(job.get("page_url"))
        if page:
            pages.append(page)
    # Legacy lists are accepted only on a verified source, never on the group.
    sites = source.get("source_sites")
    if isinstance(sites, (list, tuple)):
        for site in sites:
            page = validated_page_url(site.get("source_page_url") if isinstance(site, Mapping) else site)
            if page and page not in pages:
                pages.append(page)
            if len(pages) >= _MAX_SITES:
                break
    return pages


def source_page_provenance(group, *, job_id=None, source_id=None,
                           verified_source=None, include_source_sites=False,
                           all_verified_sites=False, max_sites=8):
    """Return additive Android-compatible fields, or {} without a selected page.

    Selection requires BOTH stable IDs, matched exactly in the SAME job/source.
    Conflicting duplicate matches fail closed. ``verified_source`` instead means
    the caller has independently verified this exact selected source; do not pass
    a preferred/catalog object merely because it has a URL. When IDs are also
    supplied they must agree with that object.

    ``source_sites`` is omitted by default. Opt in with ``include_source_sites``:
    the default includes only selected-source pages; ``all_verified_sites=True``
    includes all mapped sources in original job/source order, not selected-first.
    Lists are URL-deduplicated and bounded to ``max_sites`` (1..16, default 8).
    Missing selection never exposes other sources. No title/episode matching.
    """
    records = list(_records(group))
    if verified_source is not None:
        if not isinstance(verified_source, Mapping):
            return {}
        if any(value is not None and (_identity(value) is None or verified_source.get(key) != value)
               for key, value in (("job_id", job_id), ("source_id", source_id))):
            return {}
        selected = verified_source
        jid = _identity(selected.get("job_id"))
        sid = _identity(selected.get("source_id"))
        jobs = [job for rid, src, job in records if jid and sid and rid == jid and src["source_id"] == sid]
        # Historical duplicates may fall back only when their original page agrees.
        fallback = jobs[0] if jobs and len({validated_page_url(j.get("page_url")) for j in jobs}) == 1 else None
    else:
        if not _identity(job_id) or not _identity(source_id):
            return {}
        matches = [(src, job) for jid, src, job in records if jid == job_id and src["source_id"] == source_id]
        if not matches:
            return {}
        candidates = [_pages(src, job) for src, job in matches]
        if any(pages != candidates[0] for pages in candidates[1:]):
            return {}
        selected, fallback = matches[0]
    pages = _pages(selected, fallback)
    if not pages:
        return {}
    result = {"source_page_url": pages[0]}
    if include_source_sites:
        limit = max(1, min(max_sites, _MAX_SITES)) if type(max_sites) is int else 8
        known = [page for _, src, job in records for page in _pages(src, job)] if all_verified_sites else pages
        # A standalone attested selection may be absent from group records.
        known = list(dict.fromkeys(known + pages))[:limit]
        result["source_sites"] = [{"source_page_url": page} for page in known]
    return result
