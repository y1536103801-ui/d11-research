#!/usr/bin/env python3
"""Anonymous public product-page / original-file capture. Python 3.10+, POSIX; Pillow for image."""
from __future__ import annotations

import argparse
import codecs
import hashlib
import http.client
import io
import ipaddress
import json
import os
import queue
import re
import secrets
import socket
import ssl
import stat
import struct
import sys
import threading
import time
import zlib
from dataclasses import dataclass
from datetime import datetime, timezone
from email.message import Message
from html.parser import HTMLParser
from urllib.parse import quote, unquote, urljoin, urlsplit, urlunsplit
from urllib.request import getproxies, proxy_bypass

PAGE_BYTES = 3 * 1024 * 1024
IMAGE_BYTES = 16 * 1024 * 1024
MAX_PIXELS = 24_000_000
MAX_EDGE = 16384
TOTAL_SECONDS = 30
IO_SECONDS = 8
MAX_REDIRECTS = 4


class CaptureError(Exception):
    def __init__(self, code, detail):
        super().__init__(detail)
        self.code, self.detail = code, detail


def fail(code, detail):
    raise CaptureError(code, detail)


_SPECIAL = tuple(ipaddress.ip_network(x) for x in (
    "100.64.0.0/10", "192.0.0.0/24", "192.88.99.0/24", "198.18.0.0/15",
    "::/96", "::ffff:0:0/96", "::ffff:0:0:0/96", "64:ff9b::/96", "64:ff9b:1::/48",
    "100::/64", "2001::/32", "2001:db8::/32", "2002::/16", "3fff::/20", "fec0::/10",
))


def public_ip(value):
    ip = ipaddress.ip_address(value)
    return (ip.is_global and not (ip.is_loopback or ip.is_link_local or ip.is_multicast
            or ip.is_unspecified or ip.is_reserved) and
            not any(ip.version == net.version and ip in net for net in _SPECIAL))


# Only public selectors / transformations. Unknown query fields are never forwarded or printed.
_SELECTOR = re.compile(r"(?:id|sku(?:id)?|spu(?:id)?|goods_?id|product_?id|item_?id|variant(?:_?id)?|pid|cat(?:egory)?|lang|locale|color|size|page|w|h|width|height|q|quality|format|fmt|fit|dpr|auto|v|ver|version|t|utm_(?:source|medium|campaign|term|content))", re.I)
_CDN = re.compile(r"(?:image/(?:resize|format|quality|crop|rotate|auto-orient|interlace|circle|rounded-corners)(?:,[a-z0-9_.!%-]+)*(?:/(?:resize|format|quality|crop|rotate|auto-orient|interlace|circle|rounded-corners)(?:,[a-z0-9_.!%-]+)*)*)", re.I)
_CREDENTIAL = re.compile(r"(?:^|[/;?&#=])(?:access[_-]?token|auth[_-]?(?:token|key)|api[_-]?key|bearer|refresh[_-]?token|token|session(?:id)?|jsessionid|passw(?:or)?d|secret|signature|sig|credential|x-amz-signature|x-oss-signature)(?:[/;:=?#&]|$)", re.I)
_BAD_HOST = (".localhost", ".local", ".internal", ".lan", ".home", ".corp", ".test", ".invalid", ".onion", ".arpa")


def public_url(raw):
    if not isinstance(raw, str) or not raw or len(raw) > 4096 or re.search(r"[\s\\\x00-\x1f\x7f]", raw):
        fail("unsafe_url", "Invalid URL characters or length.")
    try:
        p = urlsplit(raw)
        host, port = p.hostname, p.port
        if p.scheme not in ("http", "https") or not host or "@" in p.netloc or "%" in p.netloc:
            fail("unsafe_url", "Only anonymous absolute HTTP(S) URLs are accepted.")
        if port is not None and port != (443 if p.scheme == "https" else 80):
            fail("unsafe_url", "Only the standard port for this scheme is accepted.")
        host = host.rstrip(".").encode("idna").decode("ascii").lower()
        try:
            lit = ipaddress.ip_address(host)
        except ValueError:
            if (len(host) > 253 or "." not in host or host.endswith(_BAD_HOST) or
                not all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", part)
                        for part in host.split(".")) or
                re.fullmatch(r"(?:0x[0-9a-f]+|[0-9]+)(?:\.(?:0x[0-9a-f]+|[0-9]+))*", host)):
                fail("unsafe_url", "Invalid, local or ambiguous host.")
        else:
            if not public_ip(lit):
                fail("unsafe_url", "Destination address is not public unicast.")
            host = "[" + str(lit) + "]" if lit.version == 6 else str(lit)
        # Detect encoded controls and private path markers, without interpreting token values.
        path = p.path
        for _ in range(3):
            if re.search(r"[\\\x00-\x1f\x7f]", path) or _CREDENTIAL.search(path):
                fail("unsafe_url", "Unsafe path or credential marker.")
            decoded = unquote(path)
            if decoded == path:
                break
            path = decoded
        else:
            fail("unsafe_url", "Excessive nested encoding.")
        if p.fragment and (re.search(r"[=&?]", unquote(p.fragment)) or _CREDENTIAL.search(unquote(p.fragment))):
            fail("unsafe_url", "Credential-bearing fragment.")
        if p.query:
            for pair in p.query.split("&"):
                k, eq, v = pair.partition("=")
                if eq and _SELECTOR.fullmatch(k) and re.fullmatch(r"[A-Za-z0-9._~-]{1,96}", v):
                    continue
                if eq and k.lower() == "x-oss-process" and _CDN.fullmatch(unquote(v)):
                    continue
                if not eq and re.fullmatch(r"imageView2/[0-5](?:/(?:w|h|q|format)/[a-z0-9]+)+", pair, re.I):
                    continue
                fail("unsafe_url", "Unrecognized or credential-bearing query; URL withheld.")
        return urlunsplit((p.scheme, host, quote(p.path or "/", safe="/:@!$&'()*+,;=-._~%"), p.query, ""))
    except (UnicodeError, ValueError):
        fail("unsafe_url", "Malformed URL or host.")


class Deadline:
    def __init__(self, seconds):
        self.end = time.monotonic() + seconds

    def left(self):
        remaining = self.end - time.monotonic()
        if remaining <= 0:
            fail("timeout", "Total network deadline exceeded.")
        return remaining


def resolve(host, port, deadline, *, public):
    """Resolve once per hop, check the complete answer, then connect only to numeric addresses."""
    answer = queue.Queue(maxsize=1)

    def lookup():
        try:
            rows = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP)
            answer.put([(family, addr) for family, _, _, _, addr in rows])
        except OSError:
            answer.put(None)

    threading.Thread(target=lookup, daemon=True).start()
    try:
        rows = answer.get(timeout=min(IO_SECONDS, deadline.left()))
    except queue.Empty:
        fail("timeout", "DNS lookup timed out.")
    if not rows:
        fail("dns_error", "Name resolution failed.")
    if public and any(f not in (socket.AF_INET, socket.AF_INET6) or "%" in a[0] or not public_ip(a[0])
                      for f, a in rows):
        fail("unsafe_url", "DNS returned a non-public destination; the entire hop was rejected.")
    return list(dict.fromkeys((f, a) for f, a in rows if f in (socket.AF_INET, socket.AF_INET6)))


def configured_proxy(p):
    settings = getproxies()
    if not settings or proxy_bypass(p.hostname):
        return None
    raw = settings.get(p.scheme) or settings.get("all")
    if not raw:
        return None
    try:
        proxy = urlsplit(raw if "://" in raw else "http://" + raw)
        if (proxy.scheme != "http" or not proxy.hostname or "@" in proxy.netloc or
            proxy.path not in ("", "/") or proxy.query or proxy.fragment):
            fail("proxy_unsupported", "Configured proxy must be an HTTP CONNECT proxy without credentials.")
        return proxy.hostname, proxy.port or 80
    except ValueError:
        fail("proxy_unsupported", "Invalid configured proxy.")


def tcp_connect(rows, deadline):
    for family, addr in rows:
        sock = socket.socket(family, socket.SOCK_STREAM)
        try:
            sock.settimeout(min(IO_SECONDS, deadline.left()))
            sock.connect(addr)
            return sock
        except (OSError, CaptureError):
            sock.close()
            deadline.left()
    fail("connect_error", "Could not connect to the selected route.")


def connect_tunnel(sock, address, port):
    numeric = f"[{address}]" if ":" in address else address
    target = f"{numeric}:{port}"
    sock.sendall(f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\n".encode("ascii"))
    # Before TLS / GET: a CONNECT peer cannot legitimately send origin response bytes yet.
    with sock.makefile("rb") as stream:
        status = stream.readline(4097)
        if not re.fullmatch(rb"HTTP/1\.[01] 200(?: [^\r\n]*)?\r\n", status):
            fail("proxy_error", "Configured proxy refused the pinned destination.")
        total = len(status)
        while total < 32768:
            line = stream.readline(4097)
            if line == b"\r\n":
                return
            if not line.endswith(b"\r\n") or len(line) > 4096:
                break
            total += len(line)
    fail("proxy_error", "Invalid proxy response.")


def tls_context():
    # On macOS use the OS-maintained CA bundle, including with a standalone Python install.
    ca = "/etc/ssl/cert.pem" if sys.platform == "darwin" and os.path.isfile("/etc/ssl/cert.pem") else None
    return ssl.create_default_context(cafile=ca)


def read_entity(res, limit, deadline):
    lengths = res.headers.get_all("Content-Length", [])
    if len(lengths) > 1 or (lengths and (not re.fullmatch(r"[0-9]{1,18}", lengths[0]) or
                                        res.getheader("Transfer-Encoding"))):
        fail("bad_response", "Invalid or ambiguous HTTP body length.")
    size = int(lengths[0]) if lengths else None
    if size is not None and size > limit:
        fail("too_large", "Body exceeds the byte limit.")
    buf = bytearray()
    while True:
        deadline.left()
        chunk = res.read1(min(65536, limit + 1 - len(buf)))
        if not chunk:
            break
        buf.extend(chunk)
        if len(buf) > limit:
            fail("too_large", "Body exceeds the byte limit.")
    if size is not None and len(buf) != size:
        fail("bad_response", "Truncated HTTP body.")
    encoding = (res.getheader("Content-Encoding") or "identity").strip().lower()
    if encoding == "identity":
        return bytes(buf)
    if encoding not in ("gzip", "deflate"):
        fail("unsupported_encoding", "Unsupported HTTP content encoding.")
    try:
        decoder = zlib.decompressobj(31 if encoding == "gzip" else 15)
        data = decoder.decompress(buf, limit + 1)
    except zlib.error:
        fail("bad_response", "Invalid compressed body.")
    if len(data) > limit or decoder.unconsumed_tail:
        fail("too_large", "Inflated body exceeds the byte limit.")
    if not decoder.eof or decoder.unused_data:
        fail("bad_response", "Truncated or concatenated compressed body.")
    return data


@dataclass
class Hop:
    status: int
    headers: Message
    body: bytes
    route: str


def get_hop(url, kind, deadline, *, referer=None):
    p = urlsplit(public_url(url))
    port = 443 if p.scheme == "https" else 80
    rows = resolve(p.hostname, port, deadline, public=True)
    proxy = configured_proxy(p)
    sock = tcp_connect(resolve(*proxy, deadline, public=False) if proxy else rows, deadline)
    active = [sock]

    def interrupt():
        try:
            active[0].shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    timer = threading.Timer(max(0.001, deadline.end - time.monotonic()), interrupt)
    timer.daemon = True
    timer.start()
    conn = None
    try:
        sock.settimeout(min(IO_SECONDS, deadline.left()))
        if proxy:
            connect_tunnel(sock, rows[0][1][0], port)
        if p.scheme == "https":
            sock = tls_context().wrap_socket(sock, server_hostname=p.hostname, do_handshake_on_connect=False)
            active[0] = sock
            sock.settimeout(min(IO_SECONDS, deadline.left()))
            sock.do_handshake()
        conn = http.client.HTTPConnection(p.hostname, port)
        conn.auto_open = False  # Never reconnect or re-resolve the validated hostname.
        conn.sock = sock
        target = p.path + ("?" + p.query if p.query else "")
        headers = {
            "Host": p.netloc, "User-Agent": "PublicProductCapture/1.0", "Connection": "close",
            "Accept-Encoding": "identity", "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.5",
            "Accept": "text/html,application/xhtml+xml" if kind == "page" else "image/*,application/octet-stream;q=0.5",
        }
        if referer:
            rp = urlsplit(public_url(referer))
            if not (rp.scheme == "https" and p.scheme == "http"):
                headers["Referer"] = f"{rp.scheme}://{rp.netloc}/"
        conn.request("GET", target, headers=headers)
        with conn.getresponse() as res:
            redirect = res.status in (301, 302, 303, 307, 308)
            data = b"" if redirect or (kind == "image" and res.status != 200) else read_entity(
                res, PAGE_BYTES if kind == "page" else IMAGE_BYTES, deadline)
            deadline.left()
            return Hop(res.status, res.headers, data, "http_connect" if proxy else "direct")
    except ssl.SSLCertVerificationError:
        fail("tls_error", "TLS certificate chain or hostname verification failed.")
    except ssl.SSLError:
        deadline.left()
        fail("tls_error", "TLS handshake or TLS transport failed.")
    except TimeoutError:
        fail("timeout", "Connection or read timed out.")
    except (OSError, http.client.HTTPException):
        deadline.left()
        fail("network_error", "Connection failed or HTTP response was incomplete.")
    finally:
        timer.cancel()
        if conn is not None:
            conn.close()
        sock.close()


def fetch(raw, kind, *, request=None, referer=None, before_hop=None):
    if kind not in ("page", "image"):
        raise ValueError("unknown capture kind")
    url = public_url(raw)
    hops = []
    seen = set()
    deadline = Deadline(TOTAL_SECONDS)
    while True:
        if before_hop is not None:
            before_hop(url)
        if url in seen:
            fail("redirect_loop", "Redirect loop.")
        seen.add(url)
        hop = (request or get_hop)(url, kind, deadline, **({"referer": referer} if referer and not hops else {}))
        hops.append({"url": url, "http_status": hop.status, "route": hop.route})
        if hop.status not in (301, 302, 303, 307, 308):
            return url, hop, hops
        if len(hops) > MAX_REDIRECTS:
            fail("redirect_limit", "Too many redirects.")
        loc = hop.headers.get_all("Location", [])
        if len(loc) != 1 or not loc[0] or re.search(r"[\\\x00-\x1f\x7f]", loc[0]):
            fail("bad_redirect", "Missing or ambiguous redirect location.")
        try:
            dest = public_url(urljoin(url, loc[0]))
        except ValueError:
            fail("bad_redirect", "Malformed redirect location.")
        if url.startswith("https:") and dest.startswith("http:"):
            fail("unsafe_redirect", "HTTPS downgrade rejected.")
        url = dest


def clean(s):
    s = re.sub(r"[\ud800-\udfff\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", s)
    return re.sub(r"\s+", " ", s).strip()


def decode_html(body, headers):
    head = body[:8192]
    declared = re.search(rb"<meta\b[^>]{0,512}?charset\s*=\s*[\"']?\s*([A-Za-z0-9._-]+)", head, re.I)
    choices = [headers.get_content_charset(), declared[1].decode("ascii") if declared else None,
               "utf-8", "gb18030"]
    if body.startswith(codecs.BOM_UTF8):
        choices.insert(0, "utf-8-sig")
    elif body.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        choices.insert(0, "utf-16")
    for choice in filter(None, choices):
        try:
            name = codecs.lookup(choice).name
            if name in ("utf-7", "unicode-escape", "raw-unicode-escape"):
                continue
            return body.decode("gb18030" if name in ("gb2312", "gbk") else name)
        except (LookupError, UnicodeError):
            pass
    fail("bad_encoding", "No usable HTML text encoding.")


_VOID = set("area base br col embed hr img input link meta param source track wbr".split())
_BLOCK = set("address article aside blockquote br dd div dl dt figcaption figure footer form h1 h2 h3 h4 h5 h6 header hr li main nav ol p pre section table td th tr ul".split())
_INERT = set("style template svg noscript iframe canvas object".split())
_CHROME = set("header footer nav aside".split())
_NOISE = re.compile(r"(?:^|[\s/_.-])(?:logo(?:type)?|icon[s]?|sprite|loading|placeholder|spacer|tracking|pixel|qr(?:code)?|share|wechat|weixin|footer|header|navbar|menu|breadcrumb|recommend(?:ed)?|related|avatar|you-may-like|guess-like)(?=[\s/_.-]|$)", re.I)
_GALLERY = re.compile(r"gallery|album|carousel|swiper|slide|zoom|magnif|product[-_ ]?(?:img|image|pic|photo|media)|goods[-_ ]?(?:img|image|pic)|pro[-_]?(?:pic|img|photo)|big[-_]?(?:pic|img)|slick", re.I)
_DETAIL_IMG = re.compile(r"detail[-_ ]?(?:img|image|pic|description)|(?:product|goods)[-_ ]?description|description[-_ ]?(?:img|image)", re.I)
_IMAGE_EXT = re.compile(r"\.(?:avif|gif|jpe?g|png|webp)(?:[?#]|$)", re.I)


@dataclass
class Frame:
    tag: str
    hint: str
    skip: bool
    role: str = ""
    parts: list | None = None


class ProductHTML(HTMLParser):
    """Extract public HTML, gallery links and explicit schema.org Product markup; never execute JS."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.text = []
        self.headings = []
        self.names = []
        self.meta = {}
        self.title = ""
        self.base = None
        self.images = []
        self.products = []
        self.anchors = []
        self.list_schema = self.password = self.challenge_widget = False
        self.tag_count = 0

    def candidate(self, raw, score, source, alt="", context=""):
        if (isinstance(raw, str) and raw.strip() and len(raw) <= 4096 and len(self.images) < 2500 and
            not _NOISE.search(context) and not _NOISE.search(raw.split("?")[0]) and
            not re.search(r"二维码|公众号|扫码|客服|分享|产品搜索|官方微信|官方微博|备案查询", alt)):
            role = "detail" if _DETAIL_IMG.search(context) else ("gallery" if _GALLERY.search(context) else "")
            self.images.append((raw.strip(), score, source, clean(alt)[:120], role))

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in _VOID:
            self.handle_endtag(tag)

    def handle_starttag(self, tag, attrs):
        self.tag_count += 1
        if len(self.stack) > 256 or self.tag_count > 60000:
            fail("page_too_complex", "HTML exceeds parser complexity limit.")
        a = {k: v or "" for k, v in attrs}
        if self.stack and self.stack[-1].tag == tag and tag in ("li", "p", "option", "tr", "td", "th"):
            self.handle_endtag(tag)
        hint = " ".join((a.get("class", ""), a.get("id", ""), a.get("itemprop", "")))[:300]
        skip = bool(self.stack and self.stack[-1].skip) or tag in _INERT or tag in _CHROME
        skip |= ("hidden" in a or a.get("aria-hidden", "").lower() == "true" or
                 bool(re.search(r"(?:display\s*:\s*none|visibility\s*:\s*hidden)", a.get("style", ""), re.I)))
        ctx = " ".join([f.hint for f in self.stack[-8:]] + [hint])
        key = (a.get("property") or a.get("name") or a.get("itemprop") or "").lower()
        alt = a.get("alt") or a.get("title") or ""
        role = ""
        if tag == "base" and self.base is None and a.get("href"):
            self.base = a["href"]
        if tag == "meta" and not skip:
            content = a.get("content", "")
            self.meta.setdefault(key, content[:4096])
            if key in ("og:image", "og:image:url", "og:image:secure_url", "twitter:image", "image"):
                self.candidate(content, 98 if key.startswith("og:") else 92, key)
        if tag == "link" and not skip and "image_src" in a.get("rel", "").lower().split():
            self.candidate(a.get("href"), 94, "link:image_src")
        if not skip:
            if tag == "a" and a.get("href") and len(self.anchors) < 2000:
                self.anchors.append(a["href"][:4096])
            if tag == "input" and a.get("type", "").lower() == "password":
                self.password = True
            if re.search(r"(?:g-recaptcha|h-captcha|cf-challenge|cf-turnstile|geetest|captcha-box)", hint, re.I):
                self.challenge_widget = True
            if tag == "title":
                role = "title"
            elif tag in ("h1", "h2", "h3"):
                role = "heading"
            elif (a.get("itemprop", "").lower() == "name" or
                  re.search(r"(?:goods|product|pro|detail)[-_ ]?(?:name|title)\b", hint, re.I)):
                role = "name"
        if tag == "script":
            if a.get("type", "").split(";")[0].strip().lower() == "application/ld+json" and not skip:
                role = "ld"
            skip = True
        if not skip:
            bonus = 22 if _GALLERY.search(ctx) else 0
            if a.get("itemprop", "").lower() == "image":
                bonus += 15
            if tag in ("img", "source", "a", "div", "li", "span", "picture"):
                for key, score in (("data-zoom-image", 112), ("data-large-image", 110), ("jqimg", 110),
                                   ("data-large", 107), ("data-full", 106), ("data-original", 85),
                                   ("data-lazy-src", 69), ("data-src", 67), ("data-lazy", 67),
                                   ("src", 52), ("href", 76)):
                    if key == "src" and tag not in ("img", "source"):
                        continue
                    raw = a.get(key)
                    if key == "href" and (tag != "a" or not raw or not _IMAGE_EXT.search(raw)):
                        continue
                    self.candidate(raw, score + bonus, f"{tag}:{key}", alt, ctx)
                for key in ("data-srcset", "srcset"):
                    for item in a.get(key, "").split(",")[:24]:
                        m = re.fullmatch(r"\s*(\S+?)(?:\s+(\d+(?:\.\d+)?)([wx]))?\s*", item)
                        if m and len(m[2] or "") < 8:
                            scale = float(m[2] or 1) * (500 if m[3] == "x" else 1)
                            self.candidate(m[1], 67 + min(22, int(scale / 100)) + bonus, f"{tag}:{key}", alt, ctx)
            for m in re.finditer(r"background(?:-image)?\s*:[^;]{0,150}?url\(\s*[\"']?([^\"')]+)", a.get("style", ""), re.I):
                self.candidate(m[1], 47 + bonus, "style:background-image", alt, ctx)
            if tag in _BLOCK:
                self.text.append("\n")
        if tag not in _VOID:
            self.stack.append(Frame(tag, hint, skip, role, [] if role else None))

    def handle_data(self, data):
        if not self.stack:
            self.text.append(data)
            return
        top = self.stack[-1]
        if top.role == "ld":
            top.parts.append(data)
            return
        if top.skip:
            return
        in_title = False
        for frame in reversed(self.stack):
            if frame.role in ("title", "heading", "name"):
                frame.parts.append(data)
                in_title |= frame.role == "title"
        if not in_title and not any(f.tag == "head" for f in self.stack):
            self.text.append(data)

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i].tag == tag:
                while len(self.stack) > i:
                    self.finish_frame(self.stack.pop())
                return

    def finish_frame(self, f):
        if f.tag in _BLOCK and not f.skip:
            self.text.append("\n")
        if f.parts is None:
            return
        raw = "".join(f.parts)
        if f.role == "ld" and len(raw) <= 400000:
            try:
                self.read_ld(json.loads(raw))
            except (ValueError, RecursionError):
                pass
        elif f.role != "ld":
            val = clean(raw)[:400]
            if val and f.role == "title" and not self.title:
                self.title = val
            if val and f.role in ("heading", "name"):
                (self.names if f.role == "name" else self.headings).append(val)

    def read_ld(self, obj, depth=0):
        if depth > 4:
            return
        if isinstance(obj, list):
            for x in obj[:80]:
                self.read_ld(x, depth + 1)
        if not isinstance(obj, dict):
            return
        types = obj.get("@type")
        types = types if isinstance(types, list) else [types]
        types = [str(t).rsplit("/", 1)[-1].lower() for t in types if isinstance(t, str)]
        if "itemlist" in types or "collectionpage" in types or "searchresultspage" in types:
            self.list_schema = True
        if "product" in types and isinstance(obj.get("name"), str) and clean(obj["name"]):
            self.products.append(obj)
        for key in ("@graph", "mainEntity"):
            if key in obj:
                self.read_ld(obj[key], depth + 1)

    def finish(self):
        self.close()
        while self.stack:
            self.finish_frame(self.stack.pop())
        return [s for x in "".join(self.text).splitlines() if (s := clean(x))]

    def links(self, url, product):
        if product:
            items = product.get("image", [])
            for it in (items if isinstance(items, list) else [items]):
                self.candidate(it.get("contentUrl") or it.get("url") if isinstance(it, dict) else it,
                               115, "jsonld:Product.image", product["name"])
        try:
            base = public_url(urljoin(url, self.base)) if self.base else url
        except (ValueError, CaptureError):
            base = ""
        found, contexts = {}, {}
        for raw, score, source, alt, role in self.images:
            try:
                dest = public_url(urljoin(base, raw))
            except (ValueError, CaptureError):
                continue
            if role:
                contexts.setdefault(dest, set()).add(role)
            if dest not in found or score > found[dest][0]:
                found[dest] = (score, source, alt)
        return [{"url": u, "source": s, "alt": a, "contexts": sorted(contexts.get(u, ()))} for u, (_, s, a) in
                sorted(found.items(), key=lambda x: -x[1][0])]


def product_images(candidates, product_name):
    """Only explicit product markup, product-name matches, or gallery/media hints qualify."""
    def compact(value):
        return re.sub(r"[\W_]+", "", value.casefold())

    name = compact(product_name or "")
    picked = []
    for it in candidates:
        src, alt = it["source"], compact(it.get("alt", ""))
        if (src.startswith(("jsonld:", "og:image", "twitter:image", "link:image_src")) or
            src == "image" or (len(name) >= 3 and name in alt) or
            src.split(":")[-1] in ("jqimg", "data-zoom-image", "data-large-image", "data-large", "data-full") or
            it.get("contexts")):
            picked.append(it)
    # No context-free list of images from a storefront. A lone non-decorative image is a candidate.
    return (picked or (candidates if len(candidates) == 1 else []))[:64]


_CAPTCHA = re.compile(r"人机验证|安全验证|访问验证|滑动.{0,8}验证|请输入.{0,4}验证码|完成.{0,9}验证.{0,8}(?:访问|继续)|verify (?:that )?you are human|checking your browser|just a moment|security verification|captcha challenge|attention required", re.I)
_LOGIN = re.compile(r"请先登录|登录后(?:方可|才能|可见|查看)|需要登录|sign in to (?:continue|view)|log in to (?:continue|view)|login required", re.I)
_LOGIN_TITLE = re.compile(r"^(?:用户|会员|账号|账户)?登[录入](?:$|\s|[-_|—])|^(?:sign in|log ?in)(?:$|\s|[-_|—])", re.I)
_DENIED = re.compile(r"访问被拒绝|拒绝访问|请求被拦截|access denied|request blocked|access forbidden", re.I)
_MISSING = re.compile(r"商品(?:已下架|不存在|已删除)|(?:页面|商品)(?:没有找到|未找到)|product (?:not found|no longer available)|page not found|^404\b", re.I)
_DETAIL = re.compile(r"/(?:products?|items?|goods|dp|p|detail|product-detail)/[^/]+|/(?:[^/?]+/)*\d{5,}(?:\.s?html?)?/?$|/(?:item|detail|goods)\.s?html?\?", re.I)
_GENERIC = re.compile(r"^(?:商品列表|产品列表|全部(?:产品|商品)|产品(?:中心|系列)|搜索结果|首页|购物袋|购物车|商品详情|立即购买|我的|结算|home|all products|product list|product details|shopping (?:bag|cart)|checkout|shop all|search results)(?:\s*[-|_–—].*)?$", re.I)
_PRICE = re.compile(r"(?:[¥￥$€£]|(?:CNY|RMB|USD)\s*)\s*\d|\d[\d.,]*\s*(?:元|RMB|USD|CNY)(?:\b|起|$)", re.I)
_MEASURE = re.compile(r"\d+(?:\.\d+)?\s*(?:毫升|毫克|千克|公克|克|升|m[lL]|[kK]?[gG]|fl\.?\s*oz|oz|cm|mm|[kK]?[wW])(?:\b|(?=[\u3400-\u9fff]))")
_FACT = re.compile(r"(?:功效|规格|型号|净含量|容量|适用肤质|成分|使用方法|材质|商品编号|货号|产品介绍|商品详情|ingredients|specifications|model number|dimensions|directions for use)\s*[:：]?\s*[^\s:：]{2,}", re.I)
_BUY = re.compile(r"加入购物车|立即购买|立即选购|add to (?:cart|bag)|buy now", re.I)
_PRODUCT_WORD = re.compile(r"洁面|洗面|精华|面膜|面霜|乳液|洗发|防晒|粉底|唇[膏釉彩]|爽肤|护肤|凝露|cleanser|shampoo|serum|moisturi[sz]er", re.I)


def analyze_page(url, hop):
    out = {"title": "", "images": [], "image_candidates": [], "signals": [], "product_name": None}
    mime = hop.headers.get_content_type()
    sniff = hop.body[:1024].lstrip().lower()
    is_html = mime in ("text/html", "application/xhtml+xml") or (
        not hop.headers.get("Content-Type") and re.search(rb"<(?:!doctype\s+html|html|head|body)\b", sniff))
    lines = []
    doc = ProductHTML()
    if is_html and hop.body:
        try:
            doc.feed(decode_html(hop.body, hop.headers))
            lines = doc.finish()
        except (AssertionError, ValueError, RecursionError):
            fail("bad_html", "Malformed or excessively nested HTML.")
    body = "\n".join(lines)
    title = out["title"] = doc.title or clean(doc.meta.get("og:title", "")) or next(iter(doc.headings), "")
    lead = "\n".join([title] + lines[:15])[:1600]
    product = doc.products[0] if len(doc.products) == 1 and not doc.list_schema else None
    detail = bool(_DETAIL.search(urlsplit(url).path + ("?" if urlsplit(url).query else "")))
    has_price = bool(_PRICE.search(body))
    has_spec = bool(_MEASURE.search(body) or _FACT.search(body))
    if (_CAPTCHA.search(body) or _CAPTCHA.search(title) or re.search(r"captcha|验证码|robot check", title, re.I) or
        (doc.challenge_widget and (hop.status != 200 or len(body) < 500))):
        status = "captcha_or_challenge"
    elif hop.status == 401 or _LOGIN_TITLE.search(title) or _LOGIN.search(lead) or (doc.password and _LOGIN.search(body)) or (
        doc.password and (not detail or not (has_price or has_spec))) or re.search(
        r"/(?:login|signin|sign-in)(?:[/.]|$)", urlsplit(url).path, re.I):
        status = "login_required"
    elif hop.status in (403, 429, 451) or _DENIED.search(lead):
        status = "access_blocked"
    elif hop.status != 200:
        status = "http_error"
    elif not is_html:
        status = "not_html"
    elif _MISSING.search(lead):
        status = "no_product_content"
    else:
        og_product = doc.meta.get("og:type", "").lower() in ("product", "product.item", "og:product")
        names = (doc.names + doc.headings + [doc.meta.get("og:title", ""), title])
        identity = next((s for s in names if 3 <= len(s) <= 200 and not _GENERIC.match(s)
                         and (_PRODUCT_WORD.search(s) or (s in doc.names) or (s in doc.headings and
                              (s in title or og_product or (detail and has_price))))), None)
        visible = body.casefold()
        # Numeric category URLs and recommendations also contain prices. Discover only link
        # references (no requests) and decline ambiguous unmarked lists.
        other_items = set()
        if not product and not og_product:
            for href in doc.anchors:
                try:
                    dest = public_url(urljoin(url, href))
                except (ValueError, CaptureError):
                    continue
                if dest != url and _DETAIL.search(urlsplit(dest).path):
                    other_items.add(dest)
                if len(other_items) >= 2:
                    break
        schema_facts = False
        if product:
            identity = clean(product["name"])[:200]
            facts = []
            for k in ("description", "sku", "model", "size", "material"):
                v = product.get(k)
                if isinstance(v, (str, int, float)) and clean(str(v)):
                    facts.append(f"{k}: {clean(str(v))[:1200]}")
            offers = product.get("offers", [])
            for offer in (offers if isinstance(offers, list) else [offers])[:3]:
                if isinstance(offer, dict) and isinstance(offer.get("price"), (str, int, float)):
                    pr = str(offer["price"])
                    if re.fullmatch(r"\d+(?:[.,]\d+)?", pr):
                        cur = str(offer.get("priceCurrency", ""))
                        facts.append("price: " + pr + (" " + cur if re.fullmatch("[A-Z]{3}", cur) else ""))
            if facts:
                schema_facts = True
                body += "\n\n[公开 Product 结构化数据]\n" + "\n".join([identity] + facts)
        out["signals"] = [k for k, v in (("single_product_schema", product), ("og_product", og_product),
            ("detail_url", detail), ("product_name", identity), ("price", has_price),
            ("specification", has_spec), ("purchase_action", _BUY.search(body)), ("structured_facts", schema_facts),
            ("multiple_item_links", len(other_items) >= 2)) if v]
        specific = (product is not None or og_product or detail)
        enough = bool(identity and identity.casefold() in visible and specific and (schema_facts or has_price or has_spec))
        if (doc.list_schema or len(doc.products) > 1 or len(other_items) >= 2 or
            (_GENERIC.match(title) and not product) or not enough):
            status = "no_product_content"
        else:
            status = "ok"
            out["product_name"] = identity
            out["image_candidates"] = product_images(doc.links(url, product), identity)
            out["images"] = [item["url"] for item in out["image_candidates"]]
    out.update(status=status, evidence_ok=status == "ok")
    return out, body.strip()


class SafeDir:
    """Pinned directory FD; link a complete 0600 file into place without replacing an existing name."""
    def __init__(self, out):
        self.display = os.path.abspath(os.fspath(out))
        self.fd = None

    def __enter__(self):
        path = self.display
        if sys.platform == "darwin":
            for alias in ("tmp", "var"):
                old = "/" + alias
                if ((path == old or path.startswith(old + "/")) and os.path.islink(old) and
                    os.readlink(old) in ("private/" + alias, "/private/" + alias)):
                    path = "/private" + path
                    break
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        fd = os.open("/", flags)
        try:
            for name in filter(None, path.split("/")):
                try:
                    child = os.open(name, flags, dir_fd=fd)
                except FileNotFoundError:
                    try:
                        os.mkdir(name, 0o700, dir_fd=fd)
                    except FileExistsError:
                        pass
                    child = os.open(name, flags, dir_fd=fd)
                os.close(fd)
                fd = child
        except BaseException:
            os.close(fd)
            raise
        self.fd = fd
        return self

    def __exit__(self, *exc):
        os.close(self.fd)
        self.fd = None

    def put(self, name, data):
        if not re.fullmatch(r"[a-z0-9.-]+", name) or name.startswith("."):
            raise ValueError("capture outputs use generated basenames")
        tmp = ".capture-" + secrets.token_hex(16)
        fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_WRONLY, 0o600, dir_fd=self.fd)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            try:
                os.link(tmp, name, src_dir_fd=self.fd, dst_dir_fd=self.fd, follow_symlinks=False)
            except FileExistsError:
                # Refuse symlinks, FIFOs, devices and pre-existing mismatched bytes (even under a hash name).
                mode = os.stat(name, dir_fd=self.fd, follow_symlinks=False)
                if not stat.S_ISREG(mode.st_mode) or mode.st_size != len(data):
                    fail("unsafe_output", "Unsafe or conflicting existing output.")
                existing = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=self.fd)
                with os.fdopen(existing, "rb") as f:
                    if not stat.S_ISREG(os.fstat(f.fileno()).st_mode) or f.read(len(data) + 1) != data:
                        fail("unsafe_output", "Existing output does not match the captured bytes.")
        finally:
            os.unlink(tmp, dir_fd=self.fd)
        return os.path.join(self.display, name)


def digest(data):
    return hashlib.sha256(data).hexdigest()


_FORMATS = {"JPEG": ("jpg", ("image/jpeg", "image/jpg", "image/pjpeg")),
            "PNG": ("png", ("image/png", "image/x-png")), "WEBP": ("webp", ("image/webp",)),
            "GIF": ("gif", ("image/gif",)), "AVIF": ("avif", ("image/avif",))}


def check_image(data, mime):
    try:
        from PIL import Image, UnidentifiedImageError
    except ImportError:
        fail("missing_pillow", "The image command requires Pillow.")
    if not data:
        fail("invalid_image", "Empty image body.")
    if len(data) > IMAGE_BYTES:
        fail("too_large", "Image exceeds byte limit.")
    if (mime and mime not in ("application/octet-stream", "binary/octet-stream") and
        not any(mime in aliases for _, aliases in _FORMATS.values())):
        fail("not_image", "Media type is not an allowed raster or binary download.")
    try:
        Image.init()
        with Image.open(io.BytesIO(data), formats=[f for f in _FORMATS if f in Image.OPEN]) as im:
            fmt = im.format
            w, h = im.size
            if fmt not in _FORMATS:
                fail("invalid_image", "Unsupported decoded image format.")
            if not (0 < w <= MAX_EDGE and 0 < h <= MAX_EDGE and w * h <= MAX_PIXELS):
                fail("pixel_limit", "Image dimensions exceed the allowed budget.")
            if getattr(im, "is_animated", False):
                fail("animated_image", "Only single-frame product images are accepted.")
            im.verify()
        # verify() alone does not decode JPEG pixel data. Reopen, decode fully, keep original bytes.
        with Image.open(io.BytesIO(data), formats=[fmt]) as im:
            im.load()
        return fmt, w, h
    except (Image.DecompressionBombError, Image.DecompressionBombWarning):
        fail("pixel_limit", "Image dimensions exceed the allowed budget.")
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError, EOFError, struct.error):
        fail("invalid_image", "Unsupported, corrupt or incomplete image data.")


def save_receipt(store, kind, result):
    data = (json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()
    return store.put(f"{kind}-{digest(data)}.json", data)


def page_artifacts(final, hop, store):
    page, body = analyze_page(final, hop)
    doc = (f"状态：{page['status']}｜" + ("商品页证据" if page["evidence_ok"] else "不可作商品证据") +
           f"\nURL：{final}\n标题：{page['title']}\nHTTP：{hop.status}\n\n" +
           (body or "（没有可读取的 HTML 正文）") + "\n").encode("utf-8")
    text_hash = digest(doc)
    path = store.put(f"page-{text_hash}.txt", doc)
    return dict(page, text_path=path, text_sha256=text_hash, html_sha256=digest(hop.body))


def page_source(receipt, image_url):
    """Use only a selected public image from a successful local page receipt; send its origin only."""
    try:
        with os.fdopen(os.open(receipt, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as f:
            st = os.fstat(f.fileno())
            if not stat.S_ISREG(st.st_mode) or st.st_size > 1024 * 1024:
                fail("invalid_source", "Invalid page evidence file.")
            data = json.loads(f.read(1024 * 1024 + 1))
        if not isinstance(data, dict) or data.get("http_status") != 200:
            fail("invalid_source", "Missing successful public HTTP response.")
        image_url = public_url(image_url)
        item = (data.get("status") == "ok" and data.get("evidence_ok") is True and
                isinstance(data.get("images"), list) and image_url in data["images"])
        link = (data.get("kind") == "image_link" and data.get("status") == "link_observed" and
                data.get("evidence_ok") is False and data.get("image_url") == image_url and
                public_url(data.get("product_url")) and data.get("source_excerpt"))
        if not (item or link):
            fail("invalid_source", "Image is not listed in this page or public link receipt.")
        src = public_url(data.get("url"))
        p = urlsplit(src)
        return src, None if p.scheme == "https" and image_url.startswith("http:") else f"{p.scheme}://{p.netloc}/"
    except (OSError, ValueError, RecursionError):
        fail("invalid_source", "Cannot read the page evidence file.")


def capture(kind, url, out, *, from_page=None, before_hop=None):
    if kind not in ("page", "image"):
        raise ValueError("unknown capture kind")
    base = {"url": None, "http_status": None, "status": "error"}
    if kind == "page":
        base.update(title="", text_path=None, images=[], evidence_ok=False)
    else:
        base.update(path=None, width=None, height=None)
    try:
        with SafeDir(out) as store:
            referer = None
            if from_page is not None:
                if kind != "image":
                    fail("invalid_source", "A source page is only supported for image capture.")
                src, referer = page_source(from_page, public_url(url))
                base.update(source_page=src, referer_origin=referer)
            final, hop, hops = fetch(url, kind, referer=referer, **({"before_hop":before_hop} if before_hop else {}))
            base.update(url=final, requested_url=hops[0]["url"], http_status=hop.status, hops=hops,
                        retrieved_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                        content_type=hop.headers.get_content_type() if hop.headers.get("Content-Type") else None)
            if kind == "page":
                base.update(page_artifacts(final, hop, store))
            else:
                if hop.status != 200:
                    fail("http_error", "Image GET did not return HTTP 200.")
                fmt, w, h = check_image(hop.body, base["content_type"])
                sha = digest(hop.body)
                path = store.put(f"{sha}.{_FORMATS[fmt][0]}", hop.body)
                base.update(status="ok", path=path, width=w, height=h, format=fmt,
                            sha256=sha, bytes=len(hop.body),
                            mime_matches_format=base["content_type"] in _FORMATS[fmt][1] if
                            base["content_type"] not in (None, "application/octet-stream", "binary/octet-stream") else None)
            base["receipt_path"] = save_receipt(store, kind, base)
    except CaptureError as exc:
        base.update(status=exc.code, error=exc.detail)
    except OSError:
        base.update(status="io_error", error="I/O failed; output paths must be writable directories without symlinks.")
    if base["status"] != "ok":
        if kind == "page":
            base.update(evidence_ok=False, images=[], image_candidates=[])
        else:
            base.update(path=None, width=None, height=None)
    return base


def capture_page(url, out, *, before_hop=None):
    return capture("page", url, out, before_hop=before_hop)


def capture_image(url, out, *, from_page=None):
    return capture("image", url, out, from_page=from_page)


def main(argv=None):
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("kind", choices=("page", "image"))
    cli.add_argument("url")
    cli.add_argument("--out", required=True, metavar="DIR")
    cli.add_argument("--from-page", metavar="PAGE_RECEIPT", help="image only: send the recorded source origin for a listed image")
    args = cli.parse_args(argv)
    result = capture(args.kind, args.url, args.out, from_page=args.from_page)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["status"] == "ok" else 2


if __name__ == "__main__":
    sys.exit(main())
