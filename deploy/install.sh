#!/usr/bin/env bash
# Install or update the Telegram LLM bot on Ubuntu as the systemd service llm-bot.
#   curl -fsSL https://raw.githubusercontent.com/DaniilLuchkin/cumulative-agent/Alt/deploy/install.sh | sudo bash
# Re-running updates the code and keeps /etc/llm-bot.env and the conversation history.
# Keys are taken from an existing /etc/cumulative-agent.env when present.
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

old_env=/etc/cumulative-agent.env
old_db=/var/lib/cumulative-agent/agent.sqlite3

# Value of KEY from an env file, without sourcing it.
env_value() { [[ -f $2 ]] && sed -n "s/^$1=//p" "$2" | tail -n 1 | tr -d '\r' || true; }

if [[ ! -f ${env_file} ]]; then
    # Reuse the keys of the previous cumulative-agent installation when present.
    token=$(env_value TELEGRAM_BOT_TOKEN "${old_env}")
    key=$(env_value OPENROUTER_API_KEY "${old_env}")
    ids=$(env_value TELEGRAM_OWNER_IDS "${old_env}")
    if [[ -z ${ids} && -f ${old_db} ]]; then
        ids=$(python3 -c "import sqlite3,sys; r=sqlite3.connect('file:'+sys.argv[1]+'?mode=ro',uri=True).execute(\"SELECT value FROM settings WHERE key='telegram_claimed_owner'\").fetchone(); print(r[0] if r else '')" "${old_db}" 2>/dev/null || true)
    fi
    [[ -n ${token} ]] && echo "Using Telegram token from ${old_env}" || { read -rsp "Telegram bot token (from @BotFather): " token </dev/tty; echo; }
    [[ -n ${key} ]] && echo "Using OpenRouter key from ${old_env}" || { read -rsp "OpenRouter API key: " key </dev/tty; echo; }
    [[ -n ${token} && -n ${key} ]] || { echo "Token and key are required" >&2; exit 1; }
    ( umask 077
      printf 'TELEGRAM_BOT_TOKEN=%s\nOPENROUTER_API_KEY=%s\nLLM_MODEL=%s\nALLOWED_USER_IDS=%s\n' \
          "${token}" "${key}" "${LLM_MODEL:-openrouter/auto}" "${ids}" > "${env_file}" )
    echo "Settings saved to ${env_file}${ids:+ (allowed user: ${ids})}"
fi

# Telegram delivers updates to one poller only. The old agent is stopped, not deleted:
# its code, settings and database stay in place for a later return.
if systemctl is-enabled --quiet cumulative-agent 2>/dev/null || systemctl is-active --quiet cumulative-agent 2>/dev/null; then
    systemctl disable --now cumulative-agent
    echo "Stopped cumulative-agent (files kept; return: sudo systemctl disable --now llm-bot && sudo systemctl enable --now cumulative-agent)"
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
