#!/usr/bin/env bash
# Trusted HTTPS for a public IPv4, including Telegram Mini App, without a domain.
set -euo pipefail
umask 077
if [[ ${EUID} -ne 0 || $# -ne 1 ]]; then
    echo 'Usage: sudo bash deploy/enable-ip-https.sh PUBLIC_IPV4' >&2
    exit 1
fi
source /etc/os-release
if [[ ${ID} != ubuntu || ${VERSION_ID} != 24.04 ]]; then
    echo 'Ubuntu 24.04 is required.' >&2
    exit 1
fi
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
public_ip=$1
python3 "${script_dir}/render_nginx.py" "${public_ip}" >/dev/null
if [[ ! -f /etc/cumulative-agent.env ]]; then
    echo 'Install the service first.' >&2
    exit 1
fi
if [[ ( -e /etc/nginx/sites-available/cumulative-agent-ip || -e /etc/letsencrypt/live/cumulative-agent-ip ) && ! -e /etc/cumulative-agent-ip-managed ]]; then
    echo 'Refusing to overwrite an unmanaged nginx site.' >&2
    exit 1
fi
read -r -p "Let's Encrypt contact email: " acme_email </dev/tty
if [[ ! ${acme_email} =~ ^[^[:space:]@]+@[^[:space:]@]+\.[^[:space:]@]+$ ]]; then
    echo 'Invalid email.' >&2
    exit 1
fi
echo 'This creates/uses a free Let’s Encrypt ACME account and requests a public IP certificate.'
echo 'Certificate metadata is public. Terms: https://letsencrypt.org/repository/'
read -r -p 'Agree to these terms and obtain the certificate? [y/N]: ' agree </dev/tty
if [[ ${agree} != y && ${agree} != Y ]]; then
    echo 'Certificate setup cancelled.' >&2
    exit 1
fi
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y nginx python3-venv ca-certificates
if [[ ! -d /opt/cumulative-agent-certbot ]]; then
    python3 -m venv /opt/cumulative-agent-certbot
fi
# Ubuntu's packaged Certbot predates IP webroot support. Keep it isolated.
/opt/cumulative-agent-certbot/bin/pip install --disable-pip-version-check 'certbot>=5.4,<6'
install -d -m 755 /var/lib/cumulative-agent-acme
site=/etc/nginx/sites-available/cumulative-agent-ip
install -d -m 755 /etc/nginx/sites-available /etc/nginx/sites-enabled
if [[ -e /etc/nginx/sites-enabled/cumulative-agent-ip && ! -L /etc/nginx/sites-enabled/cumulative-agent-ip ]]; then
    echo 'Refusing to replace an existing nginx file.' >&2
    exit 1
fi
if [[ -L /etc/nginx/sites-enabled/cumulative-agent-ip && $(readlink -f /etc/nginx/sites-enabled/cumulative-agent-ip) != ${site} ]]; then
    echo 'Refusing to replace a different nginx symlink.' >&2
    exit 1
fi
# Preserve a managed site's previous config if certificate issuance fails.
site_backup=''
if [[ -f ${site} ]]; then
    site_backup=$(mktemp /etc/nginx/sites-available/cumulative-agent-ip.previous.XXXXXX)
    cp -p "${site}" "${site_backup}"
fi
restore_site() {
    if [[ -n ${site_backup} ]]; then
        cp -p "${site_backup}" "${site}"
        if nginx -t; then systemctl reload nginx || true; fi
        echo "Previous nginx config restored; backup: ${site_backup}" >&2
    fi
}
trap restore_site ERR
# Bootstrap the requested IP, including when the server's public IP has changed.
# The timer renews certificates directly and doesn't interrupt the TLS site.
python3 "${script_dir}/render_nginx.py" "${public_ip}" > "${site}"
chmod 644 "${site}"
touch /etc/cumulative-agent-ip-managed
if [[ ! -L /etc/nginx/sites-enabled/cumulative-agent-ip ]]; then
    ln -s "${site}" /etc/nginx/sites-enabled/cumulative-agent-ip
fi
nginx -t
systemctl enable --now nginx
systemctl reload nginx
/opt/cumulative-agent-certbot/bin/certbot certonly --non-interactive --agree-tos \
    --email "${acme_email}" --webroot --webroot-path /var/lib/cumulative-agent-acme \
    --ip-address "${public_ip}" --required-profile shortlived --cert-name cumulative-agent-ip --renew-with-new-domains
python3 "${script_dir}/render_nginx.py" "${public_ip}" --tls > "${site}"
chmod 644 "${site}"
nginx -t
systemctl reload nginx

install -d -m 755 /etc/letsencrypt/renewal-hooks/deploy
cat > /etc/letsencrypt/renewal-hooks/deploy/cumulative-agent-nginx <<'HOOK'
#!/usr/bin/env bash
set -euo pipefail
/usr/sbin/nginx -t
/usr/bin/systemctl reload nginx
HOOK
chmod 700 /etc/letsencrypt/renewal-hooks/deploy/cumulative-agent-nginx
install -m 644 "${script_dir}/cumulative-agent-certbot.service" /etc/systemd/system/cumulative-agent-certbot.service
install -m 644 "${script_dir}/cumulative-agent-certbot.timer" /etc/systemd/system/cumulative-agent-certbot.timer
systemctl daemon-reload
systemctl enable --now cumulative-agent-certbot.timer
python3 - "${public_ip}" <<'PY'
import pathlib
import sys
path = pathlib.Path('/etc/cumulative-agent.env')
lines = [line for line in path.read_text().splitlines() if not line.startswith('TELEGRAM_WEBAPP_URL=')]
lines.append('TELEGRAM_WEBAPP_URL=https://' + sys.argv[1] + '/app')
path.write_text('\n'.join(lines) + '\n')
path.chmod(0o600)
PY
if systemctl is-active --quiet cumulative-agent; then
    systemctl restart cumulative-agent
fi
trap - ERR
echo "HTTPS ready: https://${public_ip}/app — send /app to your bot."
