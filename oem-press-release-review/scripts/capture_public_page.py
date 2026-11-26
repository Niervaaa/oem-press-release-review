#!/usr/bin/env python3
import argparse
import hashlib
import http.client
import ipaddress
import json
import re
import socket
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path


MAX_HTML_BYTES = 25 * 1024 * 1024
MAX_SCRIPT_BYTES = 20 * 1024 * 1024
DISCLAIMER_PATTERN = re.compile(r"仅供参考|以.{0,12}(?:为准|为准)|实际.{0,12}(?:为准|为准)|最终.{0,12}(?:为准|为准)|配置.{0,8}(?:变更|调整)|图片.{0,8}(?:示意|参考)|数据.{0,8}来源|广告", re.I)


class PageParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title_parts = []
        self.text_parts = []
        self.scripts = []
        self.in_title = False
        self.skip_depth = 0

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in {"script", "style", "noscript", "template"}:
            self.skip_depth += 1
        if tag == "title":
            self.in_title = True
        if tag == "script":
            src = dict(attrs).get("src")
            if src:
                self.scripts.append(src)
        if tag in {"p", "div", "br", "li", "h1", "h2", "h3", "tr", "section", "article"} and self.skip_depth == 0:
            self.text_parts.append("\n")

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in {"script", "style", "noscript", "template"} and self.skip_depth:
            self.skip_depth -= 1
        if tag == "title":
            self.in_title = False
        if tag in {"p", "div", "li", "h1", "h2", "h3", "tr", "section", "article"} and self.skip_depth == 0:
            self.text_parts.append("\n")

    def handle_data(self, data):
        if self.in_title:
            self.title_parts.append(data)
        if self.skip_depth == 0:
            self.text_parts.append(data)

    @property
    def title(self):
        return " ".join("".join(self.title_parts).split())

    @property
    def visible_text(self):
        lines = [" ".join(line.split()) for line in "".join(self.text_parts).splitlines()]
        return "\n".join(line for line in lines if line)


def validate_ip(address, allow_loopback=False):
    if isinstance(address, str):
        address = ipaddress.ip_address(address)
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    if allow_loopback and address.is_loopback:
        return
    if address.is_loopback:
        raise ValueError("默认拒绝回环地址")
    if not address.is_global:
        raise ValueError("默认拒绝非全局地址（私网、共享地址、链路本地或保留地址）")


def validate_url(url, allow_loopback=False):
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("只允许 http/https URL")
    if not parsed.netloc or parsed.username or parsed.password:
        raise ValueError("URL 缺少主机或包含账号凭据")
    host = (parsed.hostname or "").lower()
    try:
        addresses = [ipaddress.ip_address(host)]
    except ValueError:
        try:
            addresses = [ipaddress.ip_address(item[4][0]) for item in socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)]
        except (OSError, ValueError) as exc:
            raise ValueError(f"无法解析主机：{host}") from exc
    if not addresses:
        raise ValueError(f"无法解析主机：{host}")
    for address in addresses:
        validate_ip(address, allow_loopback)
    return parsed


def origin(parsed):
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return parsed.scheme.lower(), (parsed.hostname or "").lower(), port


class GuardedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, host, port=None, timeout=25, allow_loopback=False):
        super().__init__(host, port=port, timeout=timeout)
        self.allow_loopback = allow_loopback

    def connect(self):
        self.sock = socket.create_connection((self.host, self.port), self.timeout, self.source_address)
        validate_ip(self.sock.getpeername()[0], self.allow_loopback)


class GuardedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host, port=None, timeout=25, allow_loopback=False):
        super().__init__(host, port=port, timeout=timeout)
        self.allow_loopback = allow_loopback

    def connect(self):
        self.sock = socket.create_connection((self.host, self.port), self.timeout, self.source_address)
        validate_ip(self.sock.getpeername()[0], self.allow_loopback)
        self.sock = self._context.wrap_socket(self.sock, server_hostname=self.host)


def fetch(url, timeout, max_bytes, allow_loopback=False, expected_origin=None):
    current_url = url
    for _ in range(6):
        parsed = validate_url(current_url, allow_loopback)
        if expected_origin is not None and origin(parsed) != expected_origin:
            raise ValueError("同源资源重定向后跨源（scheme/host/port 不一致）")
        connection_class = GuardedHTTPSConnection if parsed.scheme == "https" else GuardedHTTPConnection
        connection = connection_class(parsed.hostname, parsed.port, timeout, allow_loopback)
        path = urllib.parse.urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
        try:
            connection.request("GET", path, headers={"User-Agent": "Mozilla/5.0 (compatible; OEMPressReview/1.1.2)", "Accept": "text/html,application/json,application/javascript;q=0.9,*/*;q=0.8", "Connection": "close"})
            response = connection.getresponse()
            if response.status in {301, 302, 303, 307, 308} and response.getheader("Location"):
                current_url = urllib.parse.urljoin(current_url, response.getheader("Location"))
                response.close()
                continue
            data = response.read(max_bytes + 1)
            if len(data) > max_bytes:
                raise ValueError(f"响应超过大小上限：{current_url}")
            return {"url": current_url, "status": response.status, "content_type": response.getheader("Content-Type", ""), "data": data}
        finally:
            connection.close()
    raise ValueError("重定向次数超过上限")


def safe_name(value):
    name = re.sub(r"[^A-Za-z0-9._-]", "_", value) or "asset"
    return name[:120]


def scan_api_candidates(scripts):
    candidates = []
    pattern = re.compile(r'''["'`]([^"'`\n]{0,300}(?:/api/|/car/|/web-main/|getParamConf)[^"'`\n]{0,300})["'`]''')
    for script in scripts:
        text = script["text"]
        for match in pattern.finditer(text):
            value = match.group(1).strip()
            if value and value not in candidates:
                candidates.append(value)
        for match in re.finditer(r"(?:https?:)?//[^\s\"'`<>]{0,300}(?:/api/|/car/|/web-main/)[^\s\"'`<>]{0,300}", text):
            value = match.group(0)
            if value not in candidates:
                candidates.append(value)
    return candidates


def capture_public_page(url, out_dir, timeout=25, fetch_scripts=False, scan_api=False, api_url=None, allow_loopback=False):
    out_dir.mkdir(parents=True, exist_ok=True)
    fetched_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    response = fetch(url, timeout, MAX_HTML_BYTES, allow_loopback)
    completed_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    html = response["data"]
    (out_dir / "page.html").write_bytes(html)
    parser = PageParser()
    charset_match = re.search(r"charset\s*=\s*[\"']?([A-Za-z0-9._-]+)", response["content_type"], re.I)
    encoding = charset_match.group(1) if charset_match else "utf-8"
    parser.feed(html.decode(encoding, errors="replace"))
    visible_text = parser.visible_text
    (out_dir / "visible_text.txt").write_text(visible_text, encoding="utf-8")
    disclaimers = [line for line in visible_text.splitlines() if DISCLAIMER_PATTERN.search(line)]
    (out_dir / "disclaimers.json").write_text(json.dumps(disclaimers, ensure_ascii=False, indent=2), encoding="utf-8")
    page_origin = origin(urllib.parse.urlsplit(response["url"]))
    scripts_manifest = []
    downloaded_scripts = []
    warnings = []
    if not 200 <= response["status"] < 300:
        warnings.append(f"PAGE_HTTP_ERROR: HTTP {response['status']}")
    if fetch_scripts:
        scripts_dir = out_dir / "scripts"
        scripts_dir.mkdir(parents=True, exist_ok=True)
        for index, src in enumerate(parser.scripts, 1):
            script_url = urllib.parse.urljoin(response["url"], src)
            script_started_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
            try:
                parsed = validate_url(script_url, allow_loopback)
                if origin(parsed) != page_origin:
                    scripts_manifest.append({"src": src, "url": script_url, "status": "skipped_cross_origin", "fetched_at": script_started_at})
                    continue
                script_response = fetch(script_url, timeout, MAX_SCRIPT_BYTES, allow_loopback, expected_origin=page_origin)
                if origin(urllib.parse.urlsplit(script_response["url"])) != page_origin:
                    raise ValueError("脚本重定向后跨源")
                if not 200 <= script_response["status"] < 300:
                    raise ValueError(f"脚本 HTTP 状态非 2xx：{script_response['status']}")
                script_data = script_response["data"]
                filename = safe_name(urllib.parse.unquote(urllib.parse.urlsplit(script_url).path.rsplit("/", 1)[-1]) or f"script-{index}.js")
                destination = scripts_dir / filename
                if destination.exists() and hashlib.sha256(destination.read_bytes()).hexdigest() != hashlib.sha256(script_data).hexdigest():
                    destination = scripts_dir / f"{index}_{filename}"
                destination.write_bytes(script_data)
                text = script_data.decode("utf-8", errors="replace")
                downloaded_scripts.append({"file": destination.relative_to(out_dir).as_posix(), "url": script_url, "text": text})
                scripts_manifest.append({"src": src, "url": script_url, "final_url": script_response["url"], "status": script_response["status"], "file": destination.relative_to(out_dir).as_posix(), "bytes": len(script_data), "sha256": hashlib.sha256(script_data).hexdigest(), "fetched_at": script_started_at, "completed_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")})
            except Exception as exc:
                scripts_manifest.append({"src": src, "url": script_url, "status": "failed", "error": str(exc)[:300], "fetched_at": script_started_at})
                warnings.append(f"SCRIPT_FETCH_FAILED: {script_url}: {str(exc)[:200]}")
    (out_dir / "scripts.json").write_text(json.dumps(scripts_manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    candidates = scan_api_candidates(downloaded_scripts) if scan_api else []
    (out_dir / "api_candidates.json").write_text(json.dumps(candidates, ensure_ascii=False, indent=2), encoding="utf-8")
    api_metadata = None
    if api_url:
        api_parsed = validate_url(api_url, allow_loopback)
        if origin(api_parsed) != page_origin:
            raise ValueError("API 必须与页面同源")
        api_started_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        try:
            api_response = fetch(api_url, timeout, MAX_HTML_BYTES, allow_loopback, expected_origin=page_origin)
            if origin(urllib.parse.urlsplit(api_response["url"])) != page_origin:
                raise ValueError("API 重定向后跨源")
            if not 200 <= api_response["status"] < 300:
                raise ValueError(f"API HTTP 状态非 2xx：{api_response['status']}")
            api_data = api_response["data"]
            api_completed_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
            try:
                parsed_json = json.loads(api_data.decode("utf-8-sig"))
                (out_dir / "api.raw.json").write_bytes(api_data)
                (out_dir / "api.json").write_text(json.dumps(parsed_json, ensure_ascii=False, indent=2), encoding="utf-8")
                api_format = "json"
            except (UnicodeDecodeError, json.JSONDecodeError):
                (out_dir / "api.raw.bin").write_bytes(api_data)
                api_format = "binary"
                warnings.append("API_NOT_JSON: 已保存原始响应，需人工判断")
            api_metadata = {"url": api_url, "final_url": api_response["url"], "status": api_response["status"], "content_type": api_response["content_type"], "format": api_format, "bytes": len(api_data), "sha256": hashlib.sha256(api_data).hexdigest(), "fetched_at": api_started_at, "completed_at": api_completed_at}
        except Exception as exc:
            api_metadata = {"url": api_url, "status": "failed", "error": str(exc)[:300], "fetched_at": api_started_at}
            warnings.append(f"API_FETCH_FAILED: {api_url}: {str(exc)[:200]}")
    metadata = {
        "requested_url": url,
        "final_url": response["url"],
        "fetched_at": fetched_at,
        "completed_at": completed_at,
        "status": response["status"],
        "content_type": response["content_type"],
        "title": parser.title,
        "source_type": "public_web_non_official",
        "source_type_label": "公开网页（非官方）",
        "verification_status": "user_provided_unverified",
        "verification_status_label": "用户提供，未独立核验",
        "page_disclaimer": disclaimers,
        "html_bytes": len(html),
        "html_sha256": hashlib.sha256(html).hexdigest(),
        "script_count": len(scripts_manifest),
        "api": api_metadata,
        "warnings": warnings,
    }
    (out_dir / "metadata.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    return metadata


def main():
    parser = argparse.ArgumentParser(description="捕获公开网页原始 HTML、可见文本、免责声明和可选同源脚本/API 快照")
    parser.add_argument("url")
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--timeout", type=int, default=25)
    parser.add_argument("--fetch-scripts", action="store_true")
    parser.add_argument("--scan-api", action="store_true")
    parser.add_argument("--api-url")
    parser.add_argument("--allow-loopback", action="store_true", help="仅测试用：允许回环地址；生产默认拒绝")
    args = parser.parse_args()
    try:
        metadata = capture_public_page(args.url, args.out, args.timeout, args.fetch_scripts, args.scan_api, args.api_url, args.allow_loopback)
    except (OSError, ValueError, LookupError, UnicodeError, http.client.HTTPException, urllib.error.URLError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    print(json.dumps({"out": str(args.out), "title": metadata["title"], "fetched_at": metadata["fetched_at"], "warnings": metadata["warnings"]}, ensure_ascii=False))
    if any(item.startswith("PAGE_HTTP_ERROR:") for item in metadata["warnings"]):
        return 2
    if any(item.startswith(("API_FETCH_FAILED:", "SCRIPT_FETCH_FAILED:", "API_NOT_JSON:")) for item in metadata["warnings"]):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
