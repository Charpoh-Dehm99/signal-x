"""Universe construction from Binance data-dump (S3) listings and exchangeInfo.

The S3 listing is the source of truth for which symbols have data dumps: it
still contains delisted symbols that the live exchangeInfo endpoint has
forgotten. That matters directly for G0 validity (survivorship bias).

Scope of this module:
- Build whitelisted S3 ListObjects URLs and fetch size-capped XML/JSON.
- Parse S3 listing pages strictly (paginate by marker).
- Normalize dump directory names deterministically (Binance publishes some
  delisted symbol history under "<SYM>SETTLED" directories).
- Classify directories into the crypto-perp study universe vs exclusions
  (quarterlies, non-USDT quotes, TradFi perps), recording a reason for every
  exclusion (see signalx/validation.py policy note).

Security posture (fail loud, never silent):
- URLs are built only from whitelisted hosts/prefixes; callers never supply
  URL fragments. Markers are validated and quoted.
- Listing responses are capped in size and parsed with a strict expected
  schema; anything unexpected raises UniverseError instead of being skipped.
- Symbol strings pass validate_symbol before they can enter the universe.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING
from urllib.parse import quote

from defusedxml.ElementTree import ParseError, fromstring

from .validation import ValidationError, validate_symbol

if TYPE_CHECKING:  # pragma: no cover - annotations only
    from xml.etree.ElementTree import Element  # nosec B405 - annotations only; parsing uses defusedxml

S3_HOST = "https://s3-ap-northeast-1.amazonaws.com"
S3_BUCKET = "data.binance.vision"
FAPI_HOST = "https://fapi.binance.com"
EXCHANGE_INFO_URL = f"{FAPI_HOST}/fapi/v1/exchangeInfo"

MONTHLY_KLINES_PREFIX = "data/futures/um/monthly/klines/"
MONTHLY_FUNDING_PREFIX = "data/futures/um/monthly/fundingRate/"
_PREFIX_ROOTS = (MONTHLY_KLINES_PREFIX, MONTHLY_FUNDING_PREFIX)

MAX_LISTING_BYTES = 8 << 20     # listings are ~100 KB; anything near 8 MiB is wrong
MAX_PAGE_ITEMS = 1000           # S3 MaxKeys cap; more than this means schema drift
MAX_PAGES = 64
MAX_PREFIX_LEN = 256
MAX_DIR_LEN = 64

_SAFE_PATH_RE = re.compile(r"^[A-Za-z0-9/_.-]+\Z")
_QUARTERLY_RE = re.compile(r"^[A-Z0-9]{2,15}USDT_\d{6}\Z")
_MONTH_RE = re.compile(r"^\d{4}-(?:0[1-9]|1[0-2])\Z")
_KEY_RE = re.compile(r"^.+-(?P<label>[A-Za-z0-9]+)-(?P<period>\d{4}-\d{2})\.zip\Z")

TRADIFI_CONTRACTS = frozenset({"TRADIFI_PERPETUAL"})


class UniverseError(RuntimeError):
    """Any universe/listing failure that must be surfaced, never swallowed."""


# ------------------------------------------------------------------- HTTP


def listing_url(prefix: str, marker: str = "") -> str:
    """Build a whitelisted S3 ListObjects (v1) URL for a dump prefix."""
    if not isinstance(prefix, str) or not prefix.endswith("/"):
        raise UniverseError(f"listing prefix must end with '/': {prefix!r}")
    if len(prefix) > MAX_PREFIX_LEN or not _SAFE_PATH_RE.match(prefix):
        raise UniverseError(f"listing prefix rejected: {prefix!r}")
    if not any(prefix.startswith(root) for root in _PREFIX_ROOTS):
        raise UniverseError(f"listing prefix outside dump roots: {prefix!r}")
    if marker and (len(marker) > MAX_PREFIX_LEN or not _SAFE_PATH_RE.match(marker)):
        raise UniverseError(f"listing marker rejected: {marker!r}")
    url = f"{S3_HOST}/{S3_BUCKET}?delimiter=/&prefix={quote(prefix, safe='')}"
    if marker:
        url += f"&marker={quote(marker, safe='')}"
    return url


class _RetryableListingError(UniverseError):
    """Transient listing failure (timeout, 429, 5xx) — safe to retry."""


def _fetch_text_once(url: str, *, session, timeout: float, max_bytes: int) -> str:
    resp = session.get(url, stream=True, timeout=timeout)
    try:
        status = resp.status_code
        if status != 200:
            if status == 429 or 500 <= status < 600:
                raise _RetryableListingError(f"HTTP {status} for {url}")
            raise UniverseError(f"HTTP {status} for {url}")
        total = 0
        chunks: list[bytes] = []
        for chunk in resp.iter_content(chunk_size=1 << 20):
            total += len(chunk)
            if total > max_bytes:
                raise UniverseError(f"listing exceeds {max_bytes} bytes: {url}")
            chunks.append(chunk)
    finally:
        close = getattr(resp, "close", None)
        if callable(close):
            close()
    try:
        return b"".join(chunks).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise UniverseError(f"listing is not valid UTF-8: {url}") from exc


def fetch_text(
    url: str,
    *,
    session,
    timeout: float = 60.0,
    max_bytes: int = MAX_LISTING_BYTES,
    retries: int = 2,
) -> str:
    """GET a whitelisted S3 listing URL; strict status/size/decode handling.

    Retries transient failures (network errors, 429, 5xx) up to `retries`
    times; any other failure raises immediately.
    """
    import requests  # local import keeps module import cheap for tests

    if not isinstance(url, str) or not url.startswith(S3_HOST + "/"):
        raise UniverseError(f"refusing non-whitelisted URL: {url!r}")
    last_exc: Exception | None = None
    for _ in range(retries + 1):
        try:
            return _fetch_text_once(url, session=session, timeout=timeout, max_bytes=max_bytes)
        except _RetryableListingError as exc:
            last_exc = exc
        except (OSError, requests.RequestException) as exc:
            last_exc = exc
    raise UniverseError(f"listing fetch failed after {retries + 1} attempts for {url}: {last_exc}")


def fetch_exchange_info(*, session, timeout: float = 60.0) -> tuple[dict[str, dict], list[str]]:
    """GET the USDⓈ-M futures exchangeInfo payload (strict parse).

    Returns (info_map, skipped_nonwhitelist_symbols); see parse_exchange_info.
    """
    resp = session.get(EXCHANGE_INFO_URL, timeout=timeout)
    if resp.status_code != 200:
        raise UniverseError(f"HTTP {resp.status_code} for exchangeInfo")
    text = resp.text
    if not isinstance(text, str) or len(text.encode("utf-8")) > MAX_LISTING_BYTES:
        raise UniverseError("exchangeInfo payload missing or oversized")
    return parse_exchange_info(text)


# ----------------------------------------------------------------- parsing


def _xml_root(text: str) -> Element:
    if not isinstance(text, str) or not text.lstrip().startswith("<"):
        raise UniverseError("listing response is not XML text")
    try:
        root = fromstring(text)
    except ParseError as exc:
        raise UniverseError(f"listing XML parse failure: {exc}") from exc
    if not root.tag.endswith("ListBucketResult"):
        raise UniverseError(f"unexpected listing root tag: {root.tag!r}")
    return root


def _child(root: Element, suffix: str) -> Element | None:
    for el in root:
        if el.tag.endswith(suffix):
            return el
    return None


def _truncated(root: Element) -> bool:
    el = _child(root, "IsTruncated")
    if el is None or el.text is None or el.text.strip().lower() not in {"true", "false"}:
        raise UniverseError("listing missing/invalid IsTruncated")
    return el.text.strip().lower() == "true"


def _next_marker(root: Element) -> str:
    el = _child(root, "NextMarker")
    marker = el.text.strip() if el is not None and el.text else ""
    if marker and (len(marker) > MAX_PREFIX_LEN or not _SAFE_PATH_RE.match(marker)):
        raise UniverseError(f"listing returned unsafe NextMarker: {marker!r}")
    return marker


def parse_common_prefixes(text: str, base_prefix: str) -> tuple[list[str], str, bool]:
    """Parse a delimiter listing into (dir names, next_marker, is_truncated).

    Returns the immediate subdirectory names under `base_prefix` only; nested
    or foreign prefixes raise (schema drift).
    """
    root = _xml_root(text)
    dirs: list[str] = []
    for el in root:
        if not el.tag.endswith("CommonPrefixes"):
            continue
        for p in el:
            if not p.tag.endswith("Prefix"):
                continue
            value = (p.text or "").strip()
            if not value.startswith(base_prefix) or not value.endswith("/"):
                raise UniverseError(f"unexpected prefix in listing: {value!r}")
            name = value[len(base_prefix):-1]
            if not name or "/" in name or len(name) > MAX_DIR_LEN:
                raise UniverseError(f"unexpected nested prefix: {value!r}")
            dirs.append(name)
    if len(dirs) > MAX_PAGE_ITEMS:
        raise UniverseError(f"listing page has {len(dirs)} entries (> {MAX_PAGE_ITEMS})")
    return dirs, _next_marker(root), _truncated(root)


def parse_key_page(text: str, dump_dir: str, label: str) -> tuple[list[str], str, bool]:
    """Parse a key listing into (months, next_marker, is_truncated).

    Keys must match "<dump_dir>-<label>-YYYY-MM.zip" (basename). `.CHECKSUM`
    companions are skipped; any other shape raises (schema drift).
    """
    if not dump_dir or len(dump_dir) > MAX_DIR_LEN or not _SAFE_PATH_RE.match(dump_dir):
        raise UniverseError(f"bad dump dir: {dump_dir!r}")
    if not label or len(label) > MAX_DIR_LEN or not _SAFE_PATH_RE.match(label):
        raise UniverseError(f"bad dump label: {label!r}")
    root = _xml_root(text)
    months: set[str] = set()
    for el in root:
        if not el.tag.endswith("Contents"):
            continue
        key_el = None
        for k in el:
            if k.tag.endswith("Key"):
                key_el = k
                break
        if key_el is None or not key_el.text:
            raise UniverseError("listing Contents entry missing Key")
        key = key_el.text.strip()
        if len(key) > MAX_PREFIX_LEN or not _SAFE_PATH_RE.match(key):
            raise UniverseError(f"unsafe listing key: {key!r}")
        basename = key.rsplit("/", 1)[-1]
        if basename.endswith(".CHECKSUM"):
            continue
        m = _KEY_RE.match(basename)
        if m is None or not basename.startswith(f"{dump_dir}-") or m.group("label") != label:
            raise UniverseError(f"unexpected listing key: {key!r}")
        period = m.group("period")
        if not _MONTH_RE.match(period):
            raise UniverseError(f"bad month in listing key: {key!r}")
        months.add(period)
    if len(months) > MAX_PAGE_ITEMS:
        raise UniverseError(f"listing page has {len(months)} months (> {MAX_PAGE_ITEMS})")
    return sorted(months), _next_marker(root), _truncated(root)


def parse_exchange_info(text: str) -> tuple[dict[str, dict], list[str]]:
    """Parse exchangeInfo into ({symbol: {contract_type, status, quote_asset}}, skipped).

    Symbols that fail the pipeline whitelist (e.g. unicode/meme tickers that
    exist on the exchange but cannot enter any downstream request path) are
    skipped and *reported* — never silently dropped. Any other malformation
    (missing/mis-typed fields, duplicate symbol) fails loud.
    """
    try:
        payload = json.loads(text)
    except (TypeError, ValueError) as exc:
        raise UniverseError(f"exchangeInfo is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("symbols"), list):
        raise UniverseError("exchangeInfo missing 'symbols' list")
    out: dict[str, dict] = {}
    skipped: list[str] = []
    for entry in payload["symbols"]:
        if not isinstance(entry, dict) or "symbol" not in entry:
            raise UniverseError("exchangeInfo entry missing symbol field")
        raw_symbol = entry["symbol"]
        try:
            sym = validate_symbol(raw_symbol)
        except ValidationError:
            if not isinstance(raw_symbol, str) or len(raw_symbol) > MAX_DIR_LEN:
                raise UniverseError(f"exchangeInfo entry with unusable symbol: {raw_symbol!r}") from None
            skipped.append(raw_symbol)
            continue
        try:
            contract = entry["contractType"]
            status = entry["status"]
            quote_asset = entry["quoteAsset"]
        except KeyError as exc:
            raise UniverseError(f"exchangeInfo entry {sym} missing {exc}") from exc
        for name, value in (("contractType", contract), ("status", status), ("quoteAsset", quote_asset)):
            if not isinstance(value, str) or not value or len(value) > 32:
                raise UniverseError(f"exchangeInfo {sym}: bad {name}: {value!r}")
        if sym in out:
            raise UniverseError(f"exchangeInfo duplicate symbol: {sym}")
        out[sym] = {"contract_type": contract, "status": status, "quote_asset": quote_asset}
    return out, skipped


# ------------------------------------------------------------- pagination


def paginate(parse_page, fetch_page, *, max_pages: int = MAX_PAGES) -> list[str]:
    """Drive a marker-based listing to completion; fail loud on loops/overflow.

    parse_page(text) -> (items, next_marker, is_truncated); fetch_page(marker)
    returns the listing text for that marker ("" for the first page).
    """
    items: list[str] = []
    marker = ""
    for _ in range(max_pages):
        page_items, next_marker, truncated = parse_page(fetch_page(marker))
        items.extend(page_items)
        if not truncated:
            return items
        if not next_marker or next_marker == marker:
            raise UniverseError("listing pagination is stuck (no progress)")
        marker = next_marker
    raise UniverseError(f"listing exceeded {max_pages} pages")


# ------------------------------------------------------------- normalize


def normalize_symbol_dir(raw: str) -> str:
    """Normalize a dump dir name: strip trailing "SETTLED"/"_" markers.

    Binance publishes some delisted perps' history under "<SYM>SETTLED"
    (occasionally doubled) and at least one "<SYM>_" directory. Deterministic
    rule: strip suffix markers, then require a valid symbol — else fail loud.
    """
    if not isinstance(raw, str) or not raw or len(raw) > MAX_DIR_LEN:
        raise UniverseError(f"bad dump dir name: {raw!r}")
    name = raw
    while name.endswith("SETTLED"):
        name = name[:-len("SETTLED")]
    if name.endswith("_"):
        name = name[:-1]
    try:
        return validate_symbol(name)
    except ValidationError as exc:
        raise UniverseError(f"cannot normalize dump dir {raw!r}: {exc}") from exc


def build_universe(
    raw_dirs: list[str],
    info_map: dict[str, dict] | None = None,
) -> tuple[dict[str, dict], list[tuple[str, str]]]:
    """Classify dump dirs into the crypto-perp universe + reasoned exclusions.

    Returns (universe, excluded):
      universe: {base_symbol: {"dump_dir": raw_dir, "variants": [raw, ...]}}
        - plain dirs (name == base symbol) are preferred over SETTLED variants;
          ties break lexicographically for determinism.
      excluded: [(raw_dir, reason)] with reason in
        {"quarterly", "non-usdt", "invalid", "tradifi", "non-perpetual"}.
    """
    if not isinstance(raw_dirs, list):
        raise UniverseError("build_universe: raw_dirs must be a list")
    info_map = info_map or {}
    variants: dict[str, list[str]] = {}
    excluded: list[tuple[str, str]] = []
    for raw in raw_dirs:
        if not isinstance(raw, str):
            raise UniverseError(f"build_universe: non-str dir {raw!r}")
        if _QUARTERLY_RE.match(raw):
            excluded.append((raw, "quarterly"))
            continue
        try:
            base = normalize_symbol_dir(raw)
        except UniverseError:
            excluded.append((raw, "invalid"))
            continue
        if not base.endswith("USDT"):
            excluded.append((raw, "non-usdt"))
            continue
        info = info_map.get(base)
        if info is not None:
            if info.get("contract_type") in TRADIFI_CONTRACTS:
                excluded.append((raw, "tradifi"))
                continue
            if info.get("contract_type") != "PERPETUAL":
                excluded.append((raw, "non-perpetual"))
                continue
        variants.setdefault(base, []).append(raw)

    universe: dict[str, dict] = {}
    for base, raws in variants.items():
        raws = sorted(raws)
        preferred = next((r for r in raws if r == base), raws[0])
        universe[base] = {"dump_dir": preferred, "variants": raws}
    return universe, excluded


def fetch_server_universe(*, session, max_pages: int = MAX_PAGES) -> list[str]:
    """All symbol-level dump dirs under the monthly klines prefix (sorted)."""

    def parse(page: str):
        return parse_common_prefixes(page, MONTHLY_KLINES_PREFIX)

    def fetch(marker: str) -> str:
        return fetch_text(listing_url(MONTHLY_KLINES_PREFIX, marker), session=session)

    return sorted(set(paginate(parse, fetch, max_pages=max_pages)))


def fetch_available_months(symbol_dir: str, *, interval: str | None, session, max_pages: int = MAX_PAGES) -> list[str]:
    """Available YYYY-MM dump months for a dir/interval (interval None => funding)."""
    if interval is None:
        prefix, label = f"{MONTHLY_FUNDING_PREFIX}{symbol_dir}/", "fundingRate"
    else:
        if interval not in {"1m", "1h"}:
            raise UniverseError(f"interval not whitelisted for listings: {interval!r}")
        prefix, label = f"{MONTHLY_KLINES_PREFIX}{symbol_dir}/{interval}/", interval

    def parse(page: str):
        return parse_key_page(page, symbol_dir, label)

    def fetch(marker: str) -> str:
        return fetch_text(listing_url(prefix, marker), session=session)

    return paginate(parse, fetch, max_pages=max_pages)
