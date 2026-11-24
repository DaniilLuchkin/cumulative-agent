#!/usr/bin/env bash
# Install or update the Telegram LLM bot on Ubuntu as the systemd service llm-bot.
#   curl -fsSL https://raw.githubusercontent.com/DaniilLuchkin/cumulative-agent/Alt/deploy/install.sh | sudo bash
# Re-running updates the code and keeps /etc/llm-bot.env and the conversation history.
# Keys are taken from an existing /etc/cumulative-agent.env when present.
# The timer llm-bot-update runs this script with AUTO_UPDATE=1 every 5 minutes: it deploys
# new commits of the branch after the tests pass and rolls back if the bot does not start.
set -euo pipefail
ref=${BOT_REF:-Alt}
repo=${REPO_URL:-https://github.com/DaniilLuchkin/cumulative-agent.git}
app=/opt/llm-bot
env_file=/etc/llm-bot.env
auto=${AUTO_UPDATE:-0}
[[ ${EUID} -eq 0 ]] || { echo "Run with sudo" >&2; exit 1; }

if [[ ${auto} == 1 ]]; then
    [[ -f ${env_file} ]] || { echo "Not installed yet" >&2; exit 1; }
    remote=$(git ls-remote "${repo}" "refs/heads/${ref}" | cut -f1)
    [[ -n ${remote} ]] || { echo "Cannot read ${ref} from ${repo}" >&2; exit 1; }
    # Skip the running commit and a commit that already failed tests or startup.
    [[ ${remote} == "$(cat "${app}/COMMIT" 2>/dev/null)" || ${remote} == "$(cat "${app}/FAILED" 2>/dev/null)" ]] && exit 0
    echo "New commit ${remote:0:7} on ${ref}"
fi

if ! command -v git >/dev/null || ! python3 -c "import ensurepip" 2>/dev/null; then
    apt-get install -y -qq python3 python3-venv git ca-certificates >/dev/null
fi
work=$(mktemp -d)
trap 'rm -rf "${work}"' EXIT
git clone --quiet --depth 1 --branch "${ref}" "${repo}" "${work}/src"
commit=$(git -C "${work}/src" rev-parse HEAD)
# Run the installer of the new commit, not this older copy: fixes to installation itself apply now.
if [[ ${INSTALL_REEXEC:-0} != 1 ]] && ! cmp -s "${BASH_SOURCE[0]:-/dev/null}" "${work}/src/deploy/install.sh"; then
    fresh=$(mktemp)
    cp "${work}/src/deploy/install.sh" "${fresh}"
    rm -rf "${work}"
    trap - EXIT
    echo "Running the installer of ${commit:0:7}"
    exec env INSTALL_REEXEC=1 INSTALL_SELF="${fresh}" bash "${fresh}"
fi
[[ -n ${INSTALL_SELF:-} ]] && rm -f "${INSTALL_SELF}"  # bash keeps the open file; the copy is not needed on disk

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
    # The old agent kept its models in YAML: take the first enabled OpenRouter model.
    model=${LLM_MODEL:-}
    if [[ -z ${model} && -f /etc/cumulative-agent.yaml ]]; then
        model=$(python3 - /etc/cumulative-agent.yaml <<'PY' 2>/dev/null || true
import re, sys
text = open(sys.argv[1]).read()
try:
    import yaml
    models = (yaml.safe_load(text) or {}).get("models") or []
    print(next((m["id"] for m in models if m.get("enabled") and m.get("provider", "openrouter") == "openrouter"), ""))
except ImportError:
    for block in re.split(r"\n\s*-\s+(?=id:)", text)[1:]:
        if re.search(r"^\s*enabled:\s*true", block, re.M):
            print(re.match(r"id:\s*['\"]?([^\s'\"#]+)", block).group(1)); break
PY
)
        [[ -n ${model} ]] && echo "Using model ${model} from /etc/cumulative-agent.yaml"
    fi
    [[ -n ${token} ]] && echo "Using Telegram token from ${old_env}" || { read -rsp "Telegram bot token (from @BotFather): " token </dev/tty; echo; }
    [[ -n ${key} ]] && echo "Using OpenRouter key from ${old_env}" || { read -rsp "OpenRouter API key: " key </dev/tty; echo; }
    [[ -n ${token} && -n ${key} ]] || { echo "Token and key are required" >&2; exit 1; }
    ( umask 077
      printf 'TELEGRAM_BOT_TOKEN=%s\nOPENROUTER_API_KEY=%s\nLLM_MODEL=%s\nALLOWED_USER_IDS=%s\n' \
          "${token}" "${key}" "${model:-openrouter/auto}" "${ids}" > "${env_file}" )
    echo "Settings saved to ${env_file}${ids:+ (allowed user: ${ids})}"
fi

# Mini App: reuse the HTTPS that served the previous cumulative-agent's Mini App. find_miniapp.py
# reads nginx: the TLS server block that proxies to a local port gives the public address (with its
# port, e.g. https://<ip>:9443/app) and the port the panel must listen on.
# An address typed by hand is kept; one written by this script (MINIAPP_AUTO=1, or a port-less
# address from an earlier version) is re-checked on every update.
miniapp=$(env_value MINIAPP_URL "${env_file}")
auto=$(env_value MINIAPP_AUTO "${env_file}")
# Caddy configs: --config of running caddy processes (also inside containers, read through
# /proc/<pid>/root), a Caddyfile in their working folder, /etc/caddy.
caddy_args=()
for pid in $(pgrep -if caddy 2>/dev/null || true); do
    args=$(tr '\0' ' ' < "/proc/${pid}/cmdline" 2>/dev/null || true)
    config=$(sed -nE 's/.*(--config|-config)[ =]([^ ]+).*/\2/p' <<<"${args}")
    cwd=$(readlink "/proc/${pid}/cwd" 2>/dev/null || true)
    [[ -z ${config} ]] && config="${cwd}/Caddyfile"
    [[ ${config} != /* ]] && config="${cwd}/${config}"
    for candidate in "/proc/${pid}/root${config}" "${config}"; do
        if [[ -f ${candidate} ]]; then caddy_args+=(--caddy "${candidate}"); break; fi
    done
done
[[ -f /etc/caddy/Caddyfile ]] && caddy_args+=(--caddy /etc/caddy/Caddyfile)
old_host=$(env_value TELEGRAM_WEBAPP_URL "${old_env}" | sed -nE 's#https://([^:/]+).*#\1#p')
if found=$(python3 "${work}/src/deploy/find_miniapp.py" ${NGINX_SITES:-} "${caddy_args[@]}" ${old_host:+--host "${old_host}"}); then
    url=$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["url"])' "${found}")
    port=$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["port"])' "${found}")
    legacy=$(python3 -c 'import re,sys; print(re.sub(r":\d+/app$", "/app", sys.argv[1]))' "${url}")
    if [[ -z ${miniapp} || ${auto} == 1 || ${miniapp} == "${legacy}" ]] && [[ ${miniapp} != "${url}" || $(env_value WEB_PORT "${env_file}") != "${port}" ]]; then
        sed -i '/^MINIAPP_URL=/d; /^WEB_PORT=/d; /^MINIAPP_AUTO=/d' "${env_file}"
        printf 'MINIAPP_URL=%s\nWEB_PORT=%s\nMINIAPP_AUTO=1\n' "${url}" "${port}" >> "${env_file}"
        miniapp=${url}
        echo "Mini App: ${url} (nginx HTTPS of the previous installation, panel on 127.0.0.1:${port})"
    fi
fi
# The panel's port must be free. Other services may own it (on this server port 8000 belongs to
# another app), so never stop anything: report the owner and, with no port chosen yet, take a free one.
web_port=$(env_value WEB_PORT "${env_file}")
port_owner() { ss -ltnpH "sport = :$1" 2>/dev/null | grep -o 'users:(("[^"]*' | head -n 1 | cut -d'"' -f2 || true; }
holder=$(port_owner "${web_port:-8000}")
if [[ -n ${holder} && ${holder} != llm-bot && ${holder} != python* ]]; then
    containers=$(docker ps --filter "publish=${web_port:-8000}" --format '{{.Names}}' 2>/dev/null | paste -sd' ' || true)
    echo "Port ${web_port:-8000} belongs to ${containers:-${holder}}; the panel will not use it." >&2
    if [[ -z ${web_port} ]]; then
        for candidate in $(seq 8010 8099); do
            if [[ -z $(port_owner "${candidate}") ]]; then
                echo "WEB_PORT=${candidate}" >> "${env_file}"
                echo "Panel moved to 127.0.0.1:${candidate}; a proxy must forward the Mini App address there." >&2
                break
            fi
        done
    fi
fi
if [[ -n ${miniapp} ]]; then
    if [[ ${#caddy_args[@]} -eq 0 ]] && command -v nginx >/dev/null && ! systemctl is-active --quiet nginx; then
        systemctl enable --now nginx && echo "Started nginx for the Mini App" || echo "Warning: nginx does not start; the panel will not open." >&2
    fi
else
    echo "Mini App is off: no nginx or Caddy HTTPS site proxies to a local port. See README (Mini App)."
fi

# Telegram delivers updates to one poller only. The old agent is stopped, not deleted:
# its code, settings and database stay in place for a later return.
if systemctl is-enabled --quiet cumulative-agent 2>/dev/null || systemctl is-active --quiet cumulative-agent 2>/dev/null; then
    systemctl disable --now cumulative-agent
    echo "Stopped cumulative-agent (files kept; return: sudo systemctl disable --now llm-bot && sudo systemctl enable --now cumulative-agent)"
fi

install -d -m 755 "${app}"
[[ -x ${app}/.venv/bin/python ]] || python3 -m venv "${app}/.venv"
pip=("${app}/.venv/bin/pip" install --quiet --disable-pip-version-check)

# Test the new release before it replaces the running one. The tests are code from the branch:
# run them as an unprivileged user, never as root.
"${pip[@]}" "${work}/src"
chmod 755 "${work}" && chmod -R a+rX "${work}/src"
runner=()
if id nobody >/dev/null 2>&1 && command -v runuser >/dev/null; then runner=(runuser -u nobody --); fi
if ! (cd "${work}/src" && "${runner[@]}" env HOME=/tmp PYTHONDONTWRITEBYTECODE=1 "${app}/.venv/bin/python" -m unittest discover -s tests >/dev/null 2>"${work}/tests.log"); then
    tail -n 20 "${work}/tests.log" >&2
    echo "Tests failed for ${commit:0:7}; the running version is kept." >&2
    echo "${commit}" > "${app}/FAILED"
    [[ -d ${app}/llm_bot ]] && "${pip[@]}" --no-deps --force-reinstall "${app}"
    exit 1
fi

# Keep the current release for rollback, then switch.
rm -rf "${app}/previous"
if [[ -d ${app}/llm_bot ]]; then
    mkdir "${app}/previous"
    cp -r "${app}/llm_bot" "${app}/pyproject.toml" "${app}/previous/"
    cp "${app}/COMMIT" "${app}/previous/" 2>/dev/null || true
fi
rm -rf "${app}/llm_bot" "${app}/build"
cp -r "${work}/src/llm_bot" "${work}/src/pyproject.toml" "${app}/"
"${pip[@]}" --no-deps --force-reinstall "${app}"
echo "${commit}" > "${app}/COMMIT"
# install creates a new file, so a running copy of this script is not overwritten in place.
install -m 755 "${work}/src/deploy/install.sh" "${app}/install.sh"
chmod -R u=rwX,go=rX "${app}"
for unit in llm-bot.service llm-bot-update.service llm-bot-update.timer; do
    install -m 644 "${work}/src/deploy/${unit}" "/etc/systemd/system/${unit}"
done
systemctl daemon-reload
systemctl enable --quiet llm-bot
systemctl enable --quiet --now llm-bot-update.timer

started() {
    local since=$1
    for _ in $(seq ${START_WAIT:-30}); do
        sleep 1
        journalctl -u llm-bot --since "@${since}" --no-pager -q | grep -q "started as @" && return 0
    done
    return 1
}
since=$(date +%s)
systemctl restart llm-bot
if ! started "${since}"; then
    journalctl -u llm-bot --since "@${since}" -n 20 --no-pager >&2 || true
    if [[ -d ${app}/previous ]]; then
        echo "The bot did not start on ${commit:0:7}; rolling back." >&2
        rm -rf "${app}/llm_bot" "${app}/build"
        cp -r "${app}/previous/llm_bot" "${app}/previous/pyproject.toml" "${app}/"
        cp "${app}/previous/COMMIT" "${app}/COMMIT" 2>/dev/null || rm -f "${app}/COMMIT"
        "${pip[@]}" --no-deps --force-reinstall "${app}"
        systemctl restart llm-bot
    fi
    echo "${commit}" > "${app}/FAILED"
    exit 1
fi
rm -f "${app}/FAILED"
journalctl -u llm-bot --since "@${since}" -n 5 --no-pager || true
echo "Installed ${commit:0:7}. Logs: journalctl -u llm-bot -f; updates: journalctl -u llm-bot-update"
