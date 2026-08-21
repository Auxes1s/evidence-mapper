#!/bin/sh

set -eu

script_dir=$(CDPATH= cd "$(dirname "$0")" && pwd)
install_root=${REPO_RESEARCH_INSTALL_ROOT:-"${HOME}/.local/share/repo-research"}
bin_dir=${REPO_RESEARCH_BIN_DIR:-"${HOME}/.local/bin"}
skills_dir=${REPO_RESEARCH_SKILLS_DIR:-"${HOME}/.codex/skills"}
venv_dir="${install_root}/venv"
skill_target="${skills_dir}/local-repo-research"

python3 -m venv "$venv_dir"
"${venv_dir}/bin/python" -m pip install --upgrade "$script_dir"
mkdir -p "$bin_dir" "$skills_dir"
ln -sfn "${venv_dir}/bin/repo-research" "${bin_dir}/repo-research"

skill_tmp="${skills_dir}/.local-repo-research.install.$$"
cleanup() { rm -rf "$skill_tmp"; }
trap cleanup EXIT HUP INT TERM
cp -R "${script_dir}/skill/local-repo-research" "$skill_tmp"
rm -rf "$skill_target"
mv "$skill_tmp" "$skill_target"
skill_tmp=

printf 'Installed repo-research %s to %s\n' \
  "$("${venv_dir}/bin/repo-research" --version | awk '{print $2}')" "${venv_dir}"
printf 'Linked CLI at %s/repo-research\n' "$bin_dir"
printf 'Installed Codex skill at %s\n' "$skill_target"
