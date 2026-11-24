"""Find the public HTTPS address and the local port of an existing Mini App behind nginx.

Reads nginx site files (sites-enabled and conf.d), splits them into server blocks and picks one
that terminates TLS (listen ... ssl, with an existing certificate) and proxies to a local port.
Prints JSON {"url": "https://<server_name>[:<port>]/app", "port": <upstream port>}; exits 1 if none.
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


def main() -> int:
    roots = sys.argv[1:] or ["/etc/nginx/sites-enabled", "/etc/nginx/conf.d"]
    paths = sorted(p for root in roots for p in Path(root).glob("*") if p.is_file())
    found = find(paths)
    if not found:
        return 1
    print(json.dumps(found))
    return 0


if __name__ == "__main__":
    sys.exit(main())
