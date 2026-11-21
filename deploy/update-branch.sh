#!/usr/bin/env bash
# Update an existing /opt/cumulative-agent installation to a GitHub branch or tag.
# Usage: sudo bash update-branch.sh [ref]   (default ref: Alt)
# Secrets, YAML and the SQLite StateDirectory are kept; the DB is backed up first.
set -euo pipefail
ref=${1:-Alt}
repo=${REPO_URL:-https://github.com/DaniilLuchkin/cumulative-agent.git}
app=/opt/cumulative-agent
state=/var/lib/cumulative-agent
[[ ${EUID} -eq 0 ]] || { echo "Run with sudo" >&2; exit 1; }
[[ -x ${app}/.venv/bin/cumulative-agent ]] || { echo "No installation in ${app}; use install-ubuntu.sh" >&2; exit 1; }
[[ ${ref} =~ ^[A-Za-z0-9._/-]{1,100}$ ]] || { echo "Invalid ref" >&2; exit 1; }
work=$(mktemp -d)
trap 'rm -rf "${work}"' EXIT
git clone --quiet --depth 1 --branch "${ref}" "${repo}" "${work}/src"
commit=$(git -C "${work}/src" rev-parse --short HEAD)
systemctl stop cumulative-agent
if [[ -f ${state}/agent.sqlite3 ]]; then
    backup="${state}/agent.sqlite3.before-${commit}-$(date +%Y%m%d%H%M%S)"
    AGENT_DATABASE="${state}/agent.sqlite3" "${app}/.venv/bin/cumulative-agent" backup --output "${backup}"
    echo "Database backup: ${backup}"
fi
find "${app}" -mindepth 1 -maxdepth 1 ! -name .venv -exec rm -rf {} +
git -C "${work}/src" archive HEAD | tar -x -C "${app}"
"${app}/.venv/bin/pip" install --quiet --disable-pip-version-check -r "${app}/requirements.lock"
"${app}/.venv/bin/pip" install --quiet --disable-pip-version-check --no-deps --force-reinstall "${app}"
chmod -R u=rwX,go=rX "${app}"
systemctl start cumulative-agent
sleep 3
systemctl --no-pager --lines=5 status cumulative-agent || true
echo "Installed ${ref} (${commit})."
