"""Find the public HTTPS address and the local port of an existing Mini App behind nginx or Caddy.

nginx: site files are split into server blocks; one that terminates TLS (listen ... ssl, with an
existing certificate) and proxies to a local port is picked.
Caddy: Caddyfiles (--caddy PATH) are split into site blocks; one with an HTTPS address
(host[:port], https://host[:port]) and a reverse_proxy to a local port is picked. Caddy manages
its certificates itself.
Prints JSON {"url": "https://<host>[:<port>]/app", "port": <upstream port>}; exits 1 if none.
Used by install.sh so the bot's panel listens where the previous cumulative-agent did.
"""
import json
import os
import re
import sys
from pathlib import Path

LOCAL = re.compile(r"proxy_pass\s+https?://(?:127\.0\.0\.1|localhost|\[::1\]):(\d+)")


def server_blocks(text: str) -> list[str]:
    text = re.sub(r"#[^\n]*", "", text)
    blocks, index = [], 0
    for match in re.finditer(r"\bserver\s*\{", text):
        if match.start() < index:
            continue  # nested
        depth, position = 1, match.end()
        while depth and position < len(text):
            depth += {"{": 1, "}": -1}.get(text[position], 0)
            position += 1
        blocks.append(text[match.end():position - 1])
        index = position
    return blocks


def candidate(block: str, exists=os.path.exists) -> dict | None:
    listens = re.findall(r"^\s*listen\s+([^;]+);", block, re.M)
    tls_ports = []
    for value in listens:
        parts = value.split()
        port = re.search(r"(?:^|:)(\d+)$", parts[0])
        if port and ("ssl" in parts[1:] or "quic" in parts[1:]):
            tls_ports.append(int(port.group(1)))
    names = [n for line in re.findall(r"^\s*server_name\s+([^;]+);", block, re.M) for n in line.split() if n not in ("_", "localhost")]
    cert = re.search(r"^\s*ssl_certificate\s+([^;]+);", block, re.M)
    upstream = LOCAL.search(block)
    if not tls_ports or not names or not cert or not upstream or not exists(cert.group(1).strip().strip('"')):
        return None
    port = 443 if 443 in tls_ports else tls_ports[0]
    host = names[0] + ("" if port == 443 else f":{port}")
    return {"url": f"https://{host}/app", "port": int(upstream.group(1))}


def find(paths: list[Path], exists=os.path.exists) -> dict | None:
    for path in paths:
        try:
            text = path.read_text(errors="replace")
        except OSError:
            continue
        for block in server_blocks(text):
            found = candidate(block, exists)
            if found:
                return found
    return None


LOCAL_ADDRESS = re.compile(r"(?:https?://)?(?:127\.0\.0\.1|localhost|\[::1\]):(\d+)")


def caddy_sites(text: str) -> list[tuple[str, str]]:
    """Top-level (address line, body) pairs of a Caddyfile; global options and snippets skipped."""
    text = re.sub(r"(?m)(^|\s)#.*$", "", text)
    sites, depth, header, start = [], 0, "", 0
    for index, char in enumerate(text):
        if char == "{":
            if depth == 0:
                header, start = text[:index].rsplit("\n", 1)[-1].strip() if "\n" in text[:index] else text[:index].strip(), index + 1
            depth += 1
        elif char == "}" and depth:
            depth -= 1
            if depth == 0 and header and not header.startswith("("):
                sites.append((header, text[start:index]))
    return sites


def caddy_candidate(header: str, body: str, fallback_host: str | None = None) -> dict | None:
    upstream = None
    for line in body.splitlines():
        if line.strip().startswith("reverse_proxy"):
            upstream = LOCAL_ADDRESS.search(line)
            if upstream:
                break
    if not upstream:
        return None
    for address in re.split(r"[,\s]+", header):
        if not address or address.startswith("http://"):
            continue
        match = re.fullmatch(r"(?:https://)?([^:/\s]*)(?::(\d+))?(/.*)?", address)
        if not match:
            continue
        host, port = match.group(1) or fallback_host, int(match.group(2) or 443)
        if host in (None, "", "localhost", "127.0.0.1") or port == 80:
            continue
        return {"url": f"https://{host}{'' if port == 443 else f':{port}'}/app", "port": int(upstream.group(1))}
    return None


def find_caddy(paths: list[Path], fallback_host: str | None = None) -> dict | None:
    for path in paths:
        try:
            text = path.read_text(errors="replace")
        except OSError:
            continue
        for header, body in caddy_sites(text):
            found = caddy_candidate(header, body, fallback_host)
            if found:
                return found
    return None


def main() -> int:
    args, caddy, host, roots = sys.argv[1:], [], None, []
    while args:
        arg = args.pop(0)
        if arg == "--caddy" and args:
            caddy.append(Path(args.pop(0)))
        elif arg == "--host" and args:
            host = args.pop(0)
        else:
            roots.append(arg)
    roots = roots or ["/etc/nginx/sites-enabled", "/etc/nginx/conf.d"]
    paths = sorted(p for root in roots if Path(root).is_dir() for p in Path(root).glob("*") if p.is_file())
    found = find(paths) or find_caddy(caddy, host)
    if not found:
        return 1
    print(json.dumps(found))
    return 0


if __name__ == "__main__":
    sys.exit(main())
