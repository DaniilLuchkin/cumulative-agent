#!/usr/bin/env bash
# Install or update the Telegram LLM bot on Ubuntu as the systemd service llm-bot.
#   curl -fsSL https://raw.githubusercontent.com/DaniilLuchkin/cumulative-agent/Alt/deploy/install.sh | sudo bash
# Re-running updates the code and keeps /etc/llm-bot.env and the conversation history.
set -euo pipefail
ref=${BOT_REF:-Alt}
repo=${REPO_URL:-https://github.com/DaniilLuchkin/cumulative-agent.git}
app=/opt/llm-bot
env_file=/etc/llm-bot.env
[[ ${EUID} -eq 0 ]] || { echo "Run with sudo" >&2; exit 1; }

apt-get install -y -qq python3 python3-venv git ca-certificates >/dev/null
work=$(mktemp -d)
trap 'rm -rf "${work}"' EXIT
git clone --quiet --depth 1 --branch "${ref}" "${repo}" "${work}/src"

if [[ ! -f ${env_file} ]]; then
    read -rsp "Telegram bot token (from @BotFather): " token </dev/tty; echo
    read -rsp "OpenRouter API key: " key </dev/tty; echo
    read -rp "Model [openrouter/auto]: " model </dev/tty
    read -rp "Allowed Telegram user IDs, comma-separated (empty: the first user who writes): " ids </dev/tty
    [[ -n ${token} && -n ${key} ]] || { echo "Token and key are required" >&2; exit 1; }
    ( umask 077
      printf 'TELEGRAM_BOT_TOKEN=%s\nOPENROUTER_API_KEY=%s\nLLM_MODEL=%s\nALLOWED_USER_IDS=%s\n' \
          "${token}" "${key}" "${model:-openrouter/auto}" "${ids}" > "${env_file}" )
    echo "Settings saved to ${env_file}"
fi

# Telegram delivers updates to one poller only; the old agent uses the same bot.
if systemctl is-active --quiet cumulative-agent 2>/dev/null; then
    read -rp "cumulative-agent is running and would compete for the same bot. Stop and disable it? [y/N] " answer </dev/tty
    if [[ ${answer} =~ ^[Yy] ]]; then
        systemctl disable --now cumulative-agent
    else
        echo "Warning: both services will compete for updates if they use the same token." >&2
    fi
fi

install -d -m 755 "${app}"
rm -rf "${app}/llm_bot" "${app}/build"
cp -r "${work}/src/llm_bot" "${work}/src/pyproject.toml" "${app}/"
[[ -x ${app}/.venv/bin/python ]] || python3 -m venv "${app}/.venv"
"${app}/.venv/bin/pip" install --quiet --disable-pip-version-check --upgrade "${app}"
chmod -R u=rwX,go=rX "${app}"
install -m 644 "${work}/src/deploy/llm-bot.service" /etc/systemd/system/llm-bot.service
systemctl daemon-reload
systemctl enable --quiet llm-bot
systemctl restart llm-bot
sleep 4
journalctl -u llm-bot -n 5 --no-pager || true
echo "Installed $(git -C "${work}/src" rev-parse --short HEAD). Logs: journalctl -u llm-bot -f"
