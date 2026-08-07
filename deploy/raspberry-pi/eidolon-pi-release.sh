#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: eidolon-pi-release.sh \
  --target <user@host> --release-id <id> --output <bundle-dir> \
  --kernel-revision <40-hex> --data-revision <40-hex> \
  --hub-revision <40-hex> --admin-revision <40-hex> \
  --sdk-revision <40-hex> [--resume] [--activate]

Without --activate the command transfers, prepares, seals and dry-runs only.
Use --resume --activate with the same arguments after reviewing that dry-run.
The target must already be provisioned with users, secrets, Host identity,
Data V2 baseline, current release links, Python 3 and /usr/local/bin/uv.
EOF
}

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "${script_dir}/../.." && pwd)"
eidolon_root="$(cd "${repo_root}/.." && pwd)"
release_cli="${EIDOLON_RELEASE_CLI:-${repo_root}/.venv/bin/eidolon-release}"
remote_uv="${EIDOLON_REMOTE_UV:-/usr/local/bin/uv}"

target=""
release_id=""
output=""
kernel_revision=""
data_revision=""
hub_revision=""
admin_revision=""
sdk_revision=""
activate=false
resume=false

while (($#)); do
  case "$1" in
    --target) target="${2:-}"; shift 2 ;;
    --release-id) release_id="${2:-}"; shift 2 ;;
    --output) output="${2:-}"; shift 2 ;;
    --kernel-revision) kernel_revision="${2:-}"; shift 2 ;;
    --data-revision) data_revision="${2:-}"; shift 2 ;;
    --hub-revision) hub_revision="${2:-}"; shift 2 ;;
    --admin-revision) admin_revision="${2:-}"; shift 2 ;;
    --sdk-revision) sdk_revision="${2:-}"; shift 2 ;;
    --resume) resume=true; shift ;;
    --activate) activate=true; shift ;;
    --help|-h) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

if [[ ! "${release_id}" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$ ]]; then
  echo "invalid or missing --release-id" >&2
  exit 2
fi
if [[ ! "${target}" =~ ^[A-Za-z0-9_][A-Za-z0-9_.@-]*$ ]]; then
  echo "invalid or missing --target" >&2
  exit 2
fi
if [[ -z "${output}" || "${output}" != /* ]]; then
  echo "--output must be an absolute path" >&2
  exit 2
fi
if [[ ! "${remote_uv}" =~ ^/[A-Za-z0-9_./-]+$ ]]; then
  echo "EIDOLON_REMOTE_UV must be a safe absolute path" >&2
  exit 2
fi
for revision in \
  "${kernel_revision}" "${data_revision}" "${hub_revision}" \
  "${admin_revision}" "${sdk_revision}"; do
  if [[ ! "${revision}" =~ ^[0-9a-f]{40}$ ]]; then
    echo "every revision must be one full lowercase 40-hex commit ID" >&2
    exit 2
  fi
done
for executable in "${release_cli}" ssh scp; do
  if ! command -v "${executable}" >/dev/null 2>&1; then
    echo "required executable is missing: ${executable}" >&2
    exit 1
  fi
done

ssh_options=(-o BatchMode=yes -o ConnectTimeout=10)
remote_bundle="/var/tmp/eidolon-release-${release_id}"
remote_release="/srv/eidolon/releases/${release_id}"
remote_cli="${remote_release}/eidolon_kernel/.venv/bin/eidolon-release"
descriptor="${remote_release}/release.json"

if [[ "${resume}" == false ]]; then
  "${release_cli}" bundle "${release_id}" "${output}" \
    --kernel-repo "${repo_root}" \
    --data-repo "${eidolon_root}/eidolon_data" \
    --hub-repo "${eidolon_root}/eidolon_hub" \
    --admin-repo "${eidolon_root}/eidolon_admin" \
    --sdk-repo "${eidolon_root}/eidolon_sdk" \
    --kernel-revision "${kernel_revision}" \
    --data-revision "${data_revision}" \
    --hub-revision "${hub_revision}" \
    --admin-revision "${admin_revision}" \
    --sdk-revision "${sdk_revision}"
  ssh "${ssh_options[@]}" "${target}" test ! -e "${remote_bundle}"
  scp "${ssh_options[@]}" -r "${output}" "${target}:${remote_bundle}"
  ssh "${ssh_options[@]}" "${target}" \
    sudo python3 "${remote_bundle}/prepare_target.py" "${remote_bundle}" --uv "${remote_uv}"
fi
ssh "${ssh_options[@]}" "${target}" \
  sudo "${remote_cli}" deploy "${descriptor}" --dry-run

if [[ "${activate}" == true ]]; then
  ssh "${ssh_options[@]}" "${target}" sudo "${remote_cli}" deploy "${descriptor}"
  ssh "${ssh_options[@]}" "${target}" sudo "${remote_cli}" doctor "${descriptor}"
else
  echo "dry-run complete; rerun with --resume --activate after reviewing output"
fi
