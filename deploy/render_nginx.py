"""Render a dedicated IP virtual host; reject untrusted config interpolation."""

import argparse
import ipaddress


def render(address: str, tls: bool = False) -> str:
    ip = ipaddress.IPv4Address(address)
    if not ip.is_global:
        raise ValueError("Use the public IPv4 assigned to your Oracle instance")
    challenge = """    location ^~ /.well-known/acme-challenge/ {
        root /var/lib/cumulative-agent-acme;
        default_type text/plain;
        try_files $uri =404;
    }
"""
    http = f"""server {{
    listen 80;
    server_name {ip};
    server_tokens off;
{challenge}    location / {{ return 308 https://{ip}$request_uri; }}
}}
"""
    if not tls:
        # Bootstrap challenges only: no passwords/data over public plaintext HTTP.
        return http.replace(f"return 308 https://{ip}$request_uri;", "return 503;")
    return http + f"""
server {{
    listen 443 ssl;
    server_name {ip};
    ssl_certificate /etc/letsencrypt/live/cumulative-agent-ip/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/cumulative-agent-ip/privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;
    server_tokens off;
    access_log off;
    client_max_body_size 256k;
    location / {{
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header Authorization $http_authorization;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_read_timeout 60s;
    }}
}}
"""


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("address")
    parser.add_argument("--tls", action="store_true")
    args = parser.parse_args()
    try:
        print(render(args.address, args.tls), end="")
    except ValueError as error:
        parser.error(str(error))
