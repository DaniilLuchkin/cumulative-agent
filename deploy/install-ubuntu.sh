#!/usr/bin/env bash
# Fresh installation only. Run from the cloned repository with sudo bash.
set -euo pipefail
umask 077

if [[ ${EUID} -ne 0 ]]; then
    echo 'Run with sudo bash deploy/install-ubuntu.sh.' >&2
    exit 1
fi
source /etc/os-release
if [[ ${ID} != ubuntu || ${VERSION_ID} != 24.04 ]]; then
    echo 'This installer supports Ubuntu 24.04 (ARM64 or x86_64).' >&2
    exit 1
fi
if [[ ! -d /run/systemd/system ]]; then
    echo 'A running systemd host is required.' >&2
    exit 1
fi
repo_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
for target in /opt/cumulative-agent /etc/cumulative-agent.env /etc/cumulative-agent.yaml /etc/systemd/system/cumulative-agent.service; do
    if [[ -e ${target} || -L ${target} ]]; then
        echo "Refusing to overwrite ${target}. See docs/OPERATIONS.md for existing installs." >&2
        exit 1
    fi
done

echo 'The first person to send a private text message will become the owner.'
echo 'Use your own bot and send /start immediately after installation.'
read -r -s -p 'Telegram bot token (BotFather): ' bot_token </dev/tty
printf '\n'
if [[ ! ${bot_token} =~ ^[0-9]+:[A-Za-z0-9_-]+$ ]]; then
    echo 'Invalid Telegram bot token format.' >&2
    exit 1
fi
read -r -s -p 'OpenRouter API key (Enter to skip): ' openrouter_key </dev/tty
printf '\n'
if [[ ! ${openrouter_key} =~ ^[A-Za-z0-9_-]*$ ]]; then
    echo 'Invalid API key format.' >&2
    exit 1
fi
read -r -p 'Public Oracle IPv4 for HTTPS (Enter for Telegram chat + SSH tunnel only): ' public_ip </dev/tty
if [[ -n ${public_ip} ]]; then
    python3 "${repo_dir}/deploy/render_nginx.py" "${public_ip}" >/dev/null
    echo 'Allow inbound TCP 80/443 in Oracle security rules and your host firewall.'
    echo "HTTPS setup: sudo bash /opt/cumulative-agent/deploy/enable-ip-https.sh ${public_ip}"
fi

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y python3 python3-venv python3-pip git ca-certificates curl bubblewrap util-linux
install -d -m 755 /opt/cumulative-agent
# A root process may read a checkout owned by the deploying Ubuntu user.
# Trust only this explicit path for this command, not all Git directories.
if [[ -d ${repo_dir}/.git || -f ${repo_dir}/.git ]]; then
    git -c safe.directory="${repo_dir}" -C "${repo_dir}" archive HEAD | tar -x -C /opt/cumulative-agent
else
    # The downloadable source archive has no .git. Copy an explicit file list;
    # no .env, local config, databases, caches or unrelated server files.
    tar -C "${repo_dir}" --exclude=__pycache__ --exclude='*.pyc' -cf - \
        src deploy docs config/default.yaml pyproject.toml requirements.lock README.md ARCHITECTURE.md ROADMAP.md | tar -x -C /opt/cumulative-agent
fi
chmod -R u=rwX,go=rX /opt/cumulative-agent
python3 -m venv /opt/cumulative-agent/.venv
/opt/cumulative-agent/.venv/bin/pip install --disable-pip-version-check -r /opt/cumulative-agent/requirements.lock
/opt/cumulative-agent/.venv/bin/pip install --disable-pip-version-check --no-deps /opt/cumulative-agent
chmod -R u=rwX,go=rX /opt/cumulative-agent
install -m 644 /opt/cumulative-agent/config/default.yaml /etc/cumulative-agent.yaml
api_token=$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')
{
    printf 'AGENT_API_TOKEN=%s\n' "${api_token}"
    printf 'TELEGRAM_BOT_TOKEN=%s\n' "${bot_token}"
    printf 'OPENROUTER_API_KEY=%s\n' "${openrouter_key}"
    printf 'TELEGRAM_OWNER_IDS=\nTELEGRAM_CLAIM_FIRST_USER=true\nTELEGRAM_WEBAPP_URL=\n'
} > /etc/cumulative-agent.env
chmod 600 /etc/cumulative-agent.env
unset api_token bot_token openrouter_key
install -m 644 /opt/cumulative-agent/deploy/cumulative-agent.service /etc/systemd/system/cumulative-agent.service
systemctl daemon-reload

# A certificate failure must not expose the application via insecure HTTP.
if [[ -n ${public_ip} ]]; then
    if ! bash /opt/cumulative-agent/deploy/enable-ip-https.sh "${public_ip}"; then
        echo 'HTTPS setup failed. Telegram chat will run; Mini App stays disabled.' >&2
        echo 'Fix port access, then rerun the HTTPS command above.' >&2
    fi
fi
systemctl enable --now cumulative-agent
for attempt in {1..20}; do
    if curl --silent --fail http://127.0.0.1:8000/health >/dev/null; then
        echo 'Installed. Send /start to your bot now, then /app.'
        echo 'Paid LLM calls stay disabled until you configure models/budgets.'
        echo 'Status: sudo systemctl status cumulative-agent'
        echo 'Logs: sudo journalctl -u cumulative-agent -f'
        echo 'Secrets: sudoedit /etc/cumulative-agent.env'
        echo 'Models/budgets: sudoedit /etc/cumulative-agent.yaml'
        exit 0
    fi
    sleep 1
done
echo 'Service health check failed. Inspect sudo journalctl -u cumulative-agent.' >&2
exit 1
