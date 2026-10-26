"""Opt-in public web primitives for full LIFT, without ambient credentials/proxies."""

import asyncio
import hashlib
import http.client
import ipaddress
import json
import socket
import ssl
import time
import uuid
import zlib
from dataclasses import replace
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit, urlunsplit

from darwinagent.agent import PublicToolError, ToolSpec
from darwinagent.contracts import plain
from darwinagent.runtime.artifacts import atomic_json

from .data import sha
from .local_tools import LocalTools
from .text_edits import EditFailure, read_text, replace_text

PROVIDER = "duckduckgo-lite-v1"
ENDPOINT = "https://lite.duckduckgo.com/lite/"
DOH_HOST = "cloudflare-dns.com"
DOH_ADDRESS = "1.1.1.1"
DOH_MAX_BODY = 16_384
FAKE_DNS_RANGE = ipaddress.ip_network("198.18.0.0/15")
TIMEOUT = 20
MAX_BODY = 2_000_000
MAX_TEXT = 40_000
MAX_REDIRECTS = 4


class TableFailure(PublicToolError):
    """Actionable local input failures without host exception text."""

    public_codes = frozenset(
        {
            "invalid_table_reference",
            "table_file_not_found",
            "invalid_workbook",
            "table_sheet_not_supported",
            "table_sheet_not_found",
            "unsupported_table_format",
        }
    )


class WebFailure(PublicToolError):
    """Fixed, credential-free failure code; external exception messages are never exposed."""

    public_codes = frozenset(
        {
            "network_timeout",
            "dns_resolver_failure",
            "dns_response_limit",
            "dns_response_mismatch",
            "nonpublic_destination",
            "dns_no_addresses",
            "unsafe_url",
            "unsafe_port",
            "invalid_url_or_dns_failure",
            "redirect_limit_or_missing_location",
            "provider_challenge",
            "provider_blocked",
            "http_failure",
            "unsupported_content_encoding",
            "body_limit",
            "decoded_body_limit",
            "invalid_compressed_body",
            "network_failure",
            "redirect_limit",
            "invalid_search_response",
            "frozen_full_tools_changed",
            "invalid_search_arguments",
            "invalid_read_arguments",
            "unsupported_content_type",
            "output_limit",
            "tool_failure",
        }
    )


class TextHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.title_parts, self.hidden = [], [], 0
        self.in_title = False

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "noscript", "template"}:
            self.hidden += 1
        if tag == "title":
            self.in_title = True
        if tag in {"p", "div", "br", "li", "h1", "h2", "h3", "tr"}:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style", "noscript", "template"} and self.hidden:
            self.hidden -= 1
        if tag == "title":
            self.in_title = False

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)
            if self.in_title:
                self.title_parts.append(data)

    def text(self):
        return "\n".join(
            " ".join(line.split()) for line in "".join(self.parts).splitlines() if line.strip()
        )


def public_ip(address):
    ip = ipaddress.ip_address(address)
    return ip.is_global and not ip.is_multicast and not ip.is_reserved


def doh_addresses(host, deadline):
    """Fixed public bootstrap, verified TLS host, no redirects/proxy or DNS bootstrap."""
    addresses = []
    for record_type in ("A", "AAAA"):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise WebFailure("network_timeout")
        connection = PinnedHTTP(DOH_HOST, 443, DOH_ADDRESS, remaining, True)
        try:
            connection.request(
                "GET",
                "/dns-query?" + urlencode({"name": host, "type": record_type}),
                headers={"Accept": "application/dns-json", "Accept-Encoding": "identity"},
            )
            response_socket = connection.sock
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise WebFailure("network_timeout")
            response_socket.settimeout(remaining)
            response = connection.getresponse()
            if response.status != 200:
                raise WebFailure("dns_resolver_failure")
            body = bytearray()
            while len(body) <= DOH_MAX_BODY:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise WebFailure("network_timeout")
                response_socket.settimeout(remaining)
                chunk = response.read1(min(4096, DOH_MAX_BODY + 1 - len(body)))
                if not chunk:
                    break
                body.extend(chunk)
            if len(body) > DOH_MAX_BODY:
                raise WebFailure("dns_response_limit")
            data = json.loads(body)
            questions = data.get("Question", [])
            if (
                data.get("Status") != 0
                or len(questions) != 1
                or questions[0].get("name", "").rstrip(".").lower() != host.rstrip(".").lower()
                or questions[0].get("type") != (1 if record_type == "A" else 28)
            ):
                raise WebFailure("dns_response_mismatch")
            for answer in data.get("Answer", []):
                if answer.get("type") in {1, 28}:
                    address = answer.get("data", "")
                    if not public_ip(address):
                        raise WebFailure("nonpublic_destination")
                    addresses.append(address)
        except WebFailure:
            raise
        except TimeoutError:
            raise WebFailure("network_timeout") from None
        except Exception:
            raise WebFailure("dns_resolver_failure") from None
        finally:
            connection.close()
    if not addresses:
        raise WebFailure("dns_no_addresses")
    return addresses


def public_destination(url, deadline=None):
    """Validate all DNS answers, then pin the connection to an approved address."""
    try:
        parsed = urlsplit(url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or len(url) > 4096
        ):
            raise WebFailure("unsafe_url")
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        if port not in {80, 443}:
            raise WebFailure("unsafe_port")
        addresses = [
            a[4][0] for a in socket.getaddrinfo(parsed.hostname, port, type=socket.SOCK_STREAM)
        ]
        # Literal private/reserved destinations always reject. Only domain fake-IP DNS
        # receives the fixed public DoH fallback; private LAN DNS never does.
        try:
            ipaddress.ip_address(parsed.hostname)
            literal = True
        except ValueError:
            literal = False
        if (
            not literal
            and addresses
            and all(ipaddress.ip_address(a) in FAKE_DNS_RANGE for a in addresses)
        ):
            addresses = doh_addresses(parsed.hostname, deadline or time.monotonic() + TIMEOUT)
        if not addresses or any(not public_ip(a) for a in addresses):
            raise WebFailure("nonpublic_destination")
        return parsed, port, addresses[0]
    except WebFailure:
        raise
    except Exception:
        raise WebFailure("invalid_url_or_dns_failure") from None


class PinnedHTTP(http.client.HTTPConnection):
    def __init__(self, host, port, address, timeout, secure):
        super().__init__(host, port, timeout=timeout)
        self.address, self.secure = address, secure

    def connect(self):
        self.sock = socket.create_connection((self.address, self.port), self.timeout)
        if self.secure:
            self.sock = ssl.create_default_context().wrap_socket(
                self.sock, server_hostname=self.host
            )


def fetch_public(url):
    """No urllib proxy/cookie handling, automatic redirect, or second DNS resolution."""
    deadline = time.monotonic() + TIMEOUT
    for redirect in range(MAX_REDIRECTS + 1):
        parsed, port, address = public_destination(url, deadline)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise WebFailure("network_timeout")
        connection = PinnedHTTP(parsed.hostname, port, address, remaining, parsed.scheme == "https")
        try:
            path = urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
            connection.request(
                "GET",
                path,
                headers={
                    "User-Agent": "DarwinAgent-LIFT-PublicWeb/1.0",
                    "Accept-Encoding": "identity",
                },
            )
            response_socket = connection.sock
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise WebFailure("network_timeout")
            response_socket.settimeout(remaining)
            response = connection.getresponse()
            if response.status in {301, 302, 303, 307, 308}:
                location = response.getheader("Location")
                if not location or redirect == MAX_REDIRECTS:
                    raise WebFailure("redirect_limit_or_missing_location")
                url = urljoin(url, location)
                continue
            if response.status == 202:
                raise WebFailure("provider_challenge")
            if response.status != 200:
                raise WebFailure(
                    "provider_blocked" if response.status in {401, 403, 429} else "http_failure"
                )
            encoding = response.getheader("Content-Encoding", "identity").lower()
            if encoding not in {"identity", "gzip"}:
                raise WebFailure("unsupported_content_encoding")
            size = response.getheader("Content-Length")
            if size and int(size) > MAX_BODY:
                raise WebFailure("body_limit")
            body = bytearray()
            while len(body) <= MAX_BODY:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise WebFailure("network_timeout")
                response_socket.settimeout(remaining)
                chunk = response.read1(min(65536, MAX_BODY + 1 - len(body)))
                if not chunk:
                    break
                body.extend(chunk)
            if len(body) > MAX_BODY:
                raise WebFailure("body_limit")
            response_hash = hashlib.sha256(body).hexdigest()
            if encoding == "gzip":
                decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
                body = decoder.decompress(bytes(body), MAX_BODY + 1)
                if len(body) > MAX_BODY or decoder.unconsumed_tail:
                    raise WebFailure("decoded_body_limit")
                if not decoder.eof or decoder.unused_data:
                    raise WebFailure("invalid_compressed_body")
            content_type = response.getheader("Content-Type", "").lower()
            charset = response.headers.get_content_charset() or "utf-8"
            return {
                "url": url,
                "content_type": content_type,
                "body": bytes(body).decode(charset, errors="replace"),
                "response_sha256": response_hash,
            }
        except WebFailure:
            raise
        except TimeoutError:
            raise WebFailure("network_timeout") from None
        except Exception:
            raise WebFailure("network_failure") from None
        finally:
            connection.close()
    raise WebFailure("redirect_limit_or_missing_location")


def challenge(body):
    lowered = body.lower()
    return any(
        token in lowered
        for token in (
            "anomaly.js",
            "anomaly-modal",
            "verify you are human",
            "g-recaptcha",
            "unusual traffic",
        )
    )


class LiteSearchHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.results = []
        self.title = self.snippet = False
        self.page_title = False
        self.page_title_parts = []
        self.input_query = None

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "title":
            self.page_title = True
        if tag == "input" and attributes.get("name") == "q":
            self.input_query = attributes.get("value")
        if tag == "a" and "result-link" in attributes.get("class", "").split():
            url = urljoin(ENDPOINT, attributes.get("href", ""))
            parsed = urlsplit(url)
            if (
                parsed.hostname in {"duckduckgo.com", "lite.duckduckgo.com"}
                and parsed.path == "/l/"
            ):
                url = parse_qs(parsed.query).get("uddg", [""])[0]
            parsed = urlsplit(url)
            if parsed.scheme in {"http", "https"} and parsed.hostname and not parsed.username:
                self.results.append({"title": "", "url": url[:4096], "snippet": ""})
                self.title = True
        if tag == "td" and "result-snippet" in attributes.get("class", "").split():
            self.snippet = True

    def handle_endtag(self, tag):
        if tag == "a":
            self.title = False
        if tag == "td":
            self.snippet = False
        if tag == "title":
            self.page_title = False

    def handle_data(self, data):
        if self.page_title:
            self.page_title_parts.append(data)
        if self.results and self.title:
            self.results[-1]["title"] += data
        if self.results and self.snippet:
            self.results[-1]["snippet"] += data


def parse_search(page, query, max_results):
    if challenge(page["body"]):
        raise WebFailure("provider_challenge")
    parser = LiteSearchHTML()
    parser.feed(page["body"])
    title = "".join(parser.page_title_parts)
    if "DuckDuckGo" not in title or parser.input_query != query:
        raise WebFailure("invalid_search_response")
    results = parser.results[:max_results]
    if not results and "no results" not in page["body"].lower():
        raise WebFailure("invalid_search_response")
    for result in results:
        result["title"] = " ".join(result["title"].split())[:500]
        result["snippet"] = " ".join(result["snippet"].split())[:2000]
    return {
        "provider": PROVIDER,
        "query": query,
        "provider_query": parser.input_query,
        "source_url": page["url"],
        "response_sha256": page.get("response_sha256")
        or hashlib.sha256(page["body"].encode()).hexdigest(),
        "status": "results" if results else "no_results",
        "results": results,
        "retrieved_at": time.time(),
    }


class FullLocalTools(LocalTools):
    """Separate opt-in toolbox; legacy declarations and defaults remain unchanged."""

    def __init__(self, workspace, readonly=False):
        super().__init__(workspace, readonly)
        self.full_source = Path(__file__)
        self.full_hash = sha(self.full_source)
        self.edit_source = Path(__file__).with_name("text_edits.py")
        self.edit_hash = sha(self.edit_source)
        self.audit_path = Path(self.workspace).parent / (Path(self.workspace).name + "-tool-audit")

    def identity(self):
        return {
            **super().identity(),
            "full_tools_sha256": sha(Path(__file__)),
            "local_tools_sha256": sha(Path(__file__).with_name("local_tools.py")),
            "text_edits_sha256": sha(Path(__file__).with_name("text_edits.py")),
            "provider": PROVIDER,
            "endpoint": ENDPOINT,
            "network_timeout_seconds": TIMEOUT,
            "max_body_bytes": MAX_BODY,
            "max_text_chars": MAX_TEXT,
            "max_redirects": MAX_REDIRECTS,
            "network_policy": "public-dns-pinned-no-proxy-v2",
            "resolver_policy": "system-public-or-fakeip-only-cloudflare-doh-v1",
            "doh_host": DOH_HOST,
            "doh_address": DOH_ADDRESS,
            "doh_max_body_bytes": DOH_MAX_BODY,
        }

    async def invoke(self, name, params):
        started = time.time()
        audit_path = self.audit_path
        output = {"ok": False, "error_type": "WebFailure", "message": "tool_failure"}
        try:
            if sha(self.full_source) != self.full_hash:
                raise WebFailure("frozen_full_tools_changed")
            if sha(self.edit_source) != self.edit_hash:
                raise WebFailure("frozen_full_tools_changed")
            if name == "replace_text" and self.readonly:
                raise PermissionError("Judge has read-only artifacts")
            if name in {"replace_text", "read_text"}:
                result = (replace_text if name == "replace_text" else read_text)(
                    self.workspace, plain(params)
                )
            elif name not in {"search_web", "read_web"}:
                self.audit_path = None
                try:
                    result = await super().invoke(name, params)
                finally:
                    self.audit_path = audit_path
            else:
                args = plain(params)
                if name == "search_web":
                    query, limit = args.get("query"), args.get("max_results", 5)
                    if (
                        not isinstance(query, str)
                        or not query.strip()
                        or len(query) > 2000
                        or type(limit) is not int
                        or not 1 <= limit <= 10
                    ):
                        raise WebFailure("invalid_search_arguments")
                    url = ENDPOINT + "?" + urlencode({"q": query})
                else:
                    url = args.get("url")
                    if not isinstance(url, str):
                        raise WebFailure("invalid_read_arguments")
                try:
                    page = await asyncio.wait_for(asyncio.to_thread(fetch_public, url), TIMEOUT + 1)
                except TimeoutError:
                    raise WebFailure("network_timeout") from None
                if name == "search_web":
                    result = parse_search(page, query, limit)
                else:
                    if challenge(page["body"]):
                        raise WebFailure("provider_challenge")
                    if "html" in page["content_type"]:
                        parser = TextHTML()
                        parser.feed(page["body"])
                        text, title = parser.text(), " ".join(parser.title_parts).strip()[:500]
                    elif page["content_type"].startswith("text/plain"):
                        text, title = page["body"], ""
                    else:
                        raise WebFailure("unsupported_content_type")
                    result = {
                        "url": page["url"],
                        "response_sha256": page.get("response_sha256")
                        or hashlib.sha256(page["body"].encode()).hexdigest(),
                        "title": title,
                        "text": text[:MAX_TEXT],
                        "truncated": len(text) > MAX_TEXT,
                        "retrieved_at": time.time(),
                    }
            if len(json.dumps(result, ensure_ascii=False).encode()) > 180000:
                raise WebFailure("output_limit")
            output = {"ok": True, "result": result}
            return result
        except Exception as exc:
            code = getattr(exc, "public_code", None)
            if name in {"read_table", "transform_tables"} and code in TableFailure.public_codes:
                output = {"ok": False, "error_type": "TableFailure", "message": code}
                raise TableFailure(code) from None
            # Legacy errors also use fixed messages here to avoid leaking host paths.
            message = str(exc) if isinstance(exc, (WebFailure, EditFailure)) else "tool_failure"
            output = {"ok": False, "error_type": type(exc).__name__, "message": message}
            if isinstance(exc, (WebFailure, EditFailure, PermissionError)):
                raise
            raise WebFailure(message) from None
        finally:
            if audit_path is not None:
                receipt = uuid.uuid4().hex
                # Inputs are hashed: rejected userinfo/URLs must not copy credentials to audit.
                atomic_json(
                    Path(audit_path) / (receipt + ".json"),
                    {
                        "id": receipt,
                        "name": name,
                        "params_sha256": hashlib.sha256(
                            json.dumps(plain(params), sort_keys=True, default=str).encode()
                        ).hexdigest(),
                        "output": output,
                        "started": started,
                        "finished": time.time(),
                    },
                )

    async def search_web(self, params):
        control = getattr(self, "retrieval_control", None)
        if control is not None:
            control.before_tool("search_web")
        return await self.invoke("search_web", params)

    async def read_web(self, params):
        control = getattr(self, "retrieval_control", None)
        if control is not None:
            control.before_tool("read_web")
        return await self.invoke("read_web", params)

    async def replace_text(self, params):
        return await self.invoke("replace_text", params)

    def specs(self):
        edits = (
            ()
            if self.readonly
            else (
                ToolSpec(
                    "replace_text",
                    self.replace_text,
                    {
                        "type": "object",
                        "properties": {
                            key: {"type": "string"}
                            for key in ("path", "expected_sha256", "old", "new")
                        },
                        "required": ["path", "expected_sha256", "old", "new"],
                        "additionalProperties": False,
                    },
                    description="Edit an existing UTF-8 file without rewriting it: replace exactly one nonempty old fragment with new; expected_sha256 is required from the latest read_text/write receipt. Stale files and nonunique matches reject without writing. Prefer small edits for large HTML; every edit is one counted write.",
                    effect="write",
                    identity=self.identity(),
                ),
            )
        )
        return (
            tuple(
                replace(
                    tool,
                    input_contract={
                        "type": "object",
                        "properties": {
                            "path": {"type": "string"},
                            "offset": {"type": "integer"},
                            "limit": {"type": "integer"},
                            "expected_sha256": {"type": "string"},
                        },
                        "required": ["path"],
                        "additionalProperties": False,
                    },
                    description="Read UTF-8 text. Large files return a bounded page: follow next_offset until null, keeping expected_sha256 equal to the first page sha256. Offsets count characters; optional limit is 1..128000. sha256 binds the complete file, content_sha256 binds this page. Small default reads retain the full-text response.",
                )
                if tool.name == "read_text"
                else tool
                for tool in super().specs()
            )
            + edits
            + (
                ToolSpec(
                    "search_web",
                    self.search_web,
                    {
                        "type": "object",
                        "properties": {
                            "query": {"type": "string"},
                            "max_results": {"type": "integer"},
                        },
                        "required": ["query"],
                        "additionalProperties": False,
                    },
                    description="Search live public web using DuckDuckGo Lite; returns title, URL, snippet and retrieval time; provider failure is explicit",
                    identity=self.identity(),
                ),
                ToolSpec(
                    "read_web",
                    self.read_web,
                    {
                        "type": "object",
                        "properties": {"url": {"type": "string"}},
                        "required": ["url"],
                        "additionalProperties": False,
                    },
                    description="Read public HTTP(S) HTML/plain text, bounded to 40000 characters; reports title, final URL and truncation",
                    identity=self.identity(),
                ),
            )
        )
