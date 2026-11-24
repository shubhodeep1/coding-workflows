#!/usr/bin/env bash
# runner-boot.sh: cloud-init user data for one single-use `do-runner` droplet.
#
# docs/plans/digitalocean-runners-for-consumer-pipelines-plan.md, Phase 2
# (goals G3 and G4). The runner scaler (tools/runner_scaler) creates one droplet
# per queued `do-runner` job from the stock ubuntu-26-04-x64 image and passes
# this script as user data, with the JIT_CONFIG placeholder replaced by a single-use
# just-in-time runner configuration for that droplet. The script:
#   1. installs the tools the consumer pipeline workflows call (package list
#      below), with 3 attempts and backoff per step;
#   2. runs a self-check; on any failure it writes the reason to the serial
#      console and powers the droplet off WITHOUT registering a runner, so no
#      job ever runs on a broken machine and the scaler counts a boot failure;
#   3. runs exactly one job as the unprivileged `runner` user and powers off.
#
# The droplet holds no DigitalOcean or GitHub App credential: the JIT config
# registers one runner for one job and expires. The scaler destroys the
# droplet when the job completes or the droplet is off.
#
# Package inventory (plan step 2.1): command names called by the 17 reusable
# workflows that read PIPELINE_RUNS_ON and by the scripts/*.sh they run, minus
# what their own setup steps install (actions/setup-python, actions/setup-node)
# and what the Ubuntu server image ships. Repeat it with a word-boundary grep of
# candidate tool names over those files when a workflow starts calling a new
# tool, then add the package here and the command to SELF_CHECK_COMMANDS.

set -euo pipefail

RUNNER_VERSION="2.338.2"
RUNNER_SHA256="baebf204307bac6193480852732f0aba2ae97b6c5597c244f4d5558390147c40"
RUNNER_USER="runner"
RUNNER_HOME="/home/${RUNNER_USER}"
RUNNER_DIR="${RUNNER_HOME}/actions-runner"
MIN_FREE_DISK_GB=10
APT_PACKAGES=(
	build-essential ca-certificates curl docker-buildx docker.io file gawk git gnupg jq
	nodejs npm python3 python3-pip python3-venv ripgrep rsync shellcheck tree unzip
	uuid-runtime xz-utils zip
)
SELF_CHECK_COMMANDS=(docker gh git jq shellcheck rg gawk node npm python3 curl make unzip zip)
JIT_CONFIG="__RUNNER_JIT_CONFIG__"

console()
{
	# The serial console is what the DigitalOcean control panel shows for a
	# droplet that never registered; cloud-init's own log is on the disk.
	printf 'RUNNER_BOOT %s\n' "$*" | tee -a /dev/console >&2 || true
}

fail_boot()
{
	console "outcome=boot_failed reason=$1"
	sync || true
	shutdown -h now || poweroff -f
	exit 1
}

retry()
{
	local attempt
	for attempt in 1 2 3; do
		if "$@"; then
			return 0
		fi
		console "outcome=retry step=$1 attempt=${attempt}/3"
		sleep $((attempt * 10))
	done
	return 1
}

install_packages()
{
	export DEBIAN_FRONTEND=noninteractive
	# First boot: cloud-init or unattended-upgrades may hold the dpkg lock for a while.
	apt-get -o DPkg::Lock::Timeout=300 update -q && apt-get -o DPkg::Lock::Timeout=300 install -y -q --no-install-recommends "${APT_PACKAGES[@]}"
}

install_gh()
{
	# GitHub's own apt repository, as on GitHub-hosted runners (the distro
	# package lags behind the release the workflows are written against).
	install -d -m 0755 /etc/apt/keyrings || return 1
	curl -fsSL --retry 3 https://cli.github.com/packages/githubcli-archive-keyring.gpg -o /etc/apt/keyrings/githubcli-archive-keyring.gpg || return 1
	chmod 0644 /etc/apt/keyrings/githubcli-archive-keyring.gpg || return 1
	printf 'deb [arch=%s signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main\n' "$(dpkg --print-architecture)" > /etc/apt/sources.list.d/github-cli.list || return 1
	apt-get -o DPkg::Lock::Timeout=300 update -q && apt-get -o DPkg::Lock::Timeout=300 install -y -q --no-install-recommends gh
}

install_runner()
{
	local tarball="/tmp/actions-runner-${RUNNER_VERSION}.tar.gz"
	curl -fsSL --retry 3 -o "${tarball}" "https://github.com/actions/runner/releases/download/v${RUNNER_VERSION}/actions-runner-linux-x64-${RUNNER_VERSION}.tar.gz" || return 1
	if ! printf '%s  %s\n' "${RUNNER_SHA256}" "${tarball}" | sha256sum -c --status -; then
		rm -f "${tarball}"
		console "outcome=retry step=install_runner reason=sha256_mismatch"
		return 1
	fi
	install -d -o "${RUNNER_USER}" -g "${RUNNER_USER}" "${RUNNER_DIR}" || return 1
	tar -xzf "${tarball}" -C "${RUNNER_DIR}" || return 1
	rm -f "${tarball}"
	chown -R "${RUNNER_USER}:${RUNNER_USER}" "${RUNNER_DIR}" || return 1
	"${RUNNER_DIR}/bin/installdependencies.sh" > /dev/null || return 1
}

create_runner_user()
{
	# Like GitHub-hosted runners: an unprivileged user with passwordless sudo
	# (workflows apt-get install) and access to the Docker daemon.
	id -u "${RUNNER_USER}" > /dev/null 2>&1 || useradd --create-home --shell /bin/bash "${RUNNER_USER}" || return 1
	usermod -aG docker "${RUNNER_USER}" || return 1
	printf '%s ALL=(ALL) NOPASSWD:ALL\n' "${RUNNER_USER}" > "/etc/sudoers.d/90-${RUNNER_USER}" || return 1
	chmod 0440 "/etc/sudoers.d/90-${RUNNER_USER}" || return 1
}

self_check()
{
	local cmd free_gb
	for cmd in "${SELF_CHECK_COMMANDS[@]}"; do
		command -v "${cmd}" > /dev/null 2>&1 || fail_boot "missing_command_${cmd}"
	done
	systemctl is-active --quiet docker || fail_boot docker_inactive
	# The runner user reaches the daemon (group membership) without pulling an
	# image, so a Docker Hub rate limit cannot fail every boot at once.
	sudo -u "${RUNNER_USER}" docker info > /dev/null 2>&1 || fail_boot docker_unreachable
	sudo -u "${RUNNER_USER}" python3 -m venv /tmp/runner-boot-venv || fail_boot python_venv_failed
	rm -rf /tmp/runner-boot-venv
	gh --version > /dev/null || fail_boot gh_failed
	free_gb="$(df --output=avail -BG / | tail -n 1 | tr -dc '0-9')"
	[ "${free_gb:-0}" -ge "${MIN_FREE_DISK_GB}" ] || fail_boot "low_disk_${free_gb:-0}g"
	[[ "${JIT_CONFIG}" =~ ^[A-Za-z0-9+/=]{100,}$ ]] || fail_boot jit_config_invalid
}

main()
{
	console "outcome=started runner_version=${RUNNER_VERSION}"
	retry install_packages || fail_boot apt_install_failed
	retry install_gh || fail_boot gh_install_failed
	systemctl enable --now docker > /dev/null 2>&1 || fail_boot docker_start_failed
	create_runner_user || fail_boot runner_user_failed
	retry install_runner || fail_boot runner_install_failed
	self_check
	console "outcome=self_check_passed"
	# One job, then exit: a JIT runner is ephemeral by construction.
	set +e
	sudo -u "${RUNNER_USER}" -H bash -c 'cd "$1" && exec ./run.sh --jitconfig "$2"' runner-boot "${RUNNER_DIR}" "${JIT_CONFIG}"
	local rc=$?
	set -e
	console "outcome=runner_exited exit_code=${rc}"
	sync || true
	shutdown -h now || poweroff -f
}

main "$@"
