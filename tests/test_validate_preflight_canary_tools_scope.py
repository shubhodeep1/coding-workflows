#!/usr/bin/env python3
"""Tests for the CANARY_TOOLS scope preflight check in validate_process.sh.

The check enforces the validation prompt contract: service-side CLIs
(mongosh, mongo, psql, redis-cli, mysql, mysqladmin, kafkacat, kcat) must not
appear in the app container's CANARY_TOOLS unless the app image explicitly
installs that exact binary. See the motivating run documented in the
validation-improvements ledger (false-positive `mongosh is NOT available in
app` on a python:3.12-slim image where the app uses pymongo).

The check is embedded inside run_preflight_checks() in scripts/validate_process.sh.
These tests reproduce the canonical extraction + denylist logic in a
standalone bash harness so regressions in the pattern (for example a sed
expression that fails on a new CANARY_TOOLS literal shape) surface here before
reaching a live validation run.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import textwrap
import unittest


# The canonical lint snippet lifted from scripts/validate_process.sh. Keep in
# sync with the run_preflight_checks() implementation.
_LINT_SNIPPET = textwrap.dedent(r"""
    #!/usr/bin/env bash
    set -euo pipefail

    # Arg 1: working directory containing validation/tests/00_canary.sh and
    # validation/Dockerfile.app. The snippet chdirs into it and runs the same
    # extraction/denylist logic used inside run_preflight_checks().

    cd "$1"

    if [ ! -f validation/tests/00_canary.sh ]; then
        echo "NO_CANARY"
        exit 0
    fi

    _canary_tools_line="$(grep -E '^[[:space:]]*CANARY_TOOLS=' validation/tests/00_canary.sh | head -n 1 || true)"
    if [ -z "${_canary_tools_line}" ]; then
        echo "NO_CANARY_TOOLS_LINE"
        exit 0
    fi

    _canary_tools_raw="$(printf '%s\n' "${_canary_tools_line}" \
        | sed -E 's/^[[:space:]]*CANARY_TOOLS=//' \
        | sed -E 's/[[:space:]]*#.*$//' \
        | sed -E 's/^"\$\{CANARY_TOOLS:-//; s/\}"[[:space:]]*$//' \
        | sed -E "s/^'\\\$\\{CANARY_TOOLS:-//; s/\\}'[[:space:]]*$//" \
        | sed -E 's/^\$\{CANARY_TOOLS:-//; s/\}[[:space:]]*$//' \
        | sed -E 's/^"//; s/"$//; s/^'"'"'//; s/'"'"'$//')"

    _denylist="mongosh mongo psql redis-cli mysql mysqladmin kafkacat kcat"
    _offenders=""
    set -f
    for _tool in ${_canary_tools_raw}; do
        case " ${_denylist} " in
            *" ${_tool} "*)
                case "${_tool}" in
                    psql) _tool_pattern='psql|postgresql-client' ;;
                    redis-cli) _tool_pattern='redis-cli|redis-tools' ;;
                    mysql|mysqladmin) _tool_pattern="${_tool}|mysql-client|default-mysql-client|mariadb-client" ;;
                    kcat|kafkacat) _tool_pattern='kcat|kafkacat' ;;
                    *) _tool_pattern="${_tool}" ;;
                esac
                if [ -f validation/Dockerfile.app ] && \
                    grep -Ev '^[[:space:]]*#' validation/Dockerfile.app \
                    | sed -E 's/[[:space:]]+#.*$//' \
                    | sed -E ':a;N;$!ba;s/\\[[:space:]]*\n[[:space:]]*/ /g' \
                    | grep -qE "(^|[^[:alnum:]_])(${_tool_pattern})([^[:alnum:]_]|$)"; then
                    :
                else
                    _offenders="${_offenders:+${_offenders} }${_tool}"
                fi
                ;;
        esac
    done
    set +f

    if [ -n "${_offenders}" ]; then
        echo "OFFENDERS:${_offenders}"
        exit 1
    fi
    echo "OK"
""")


def _write(path: str, content: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(content)


def _run(tmpdir: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", "-c", _LINT_SNIPPET, "_", tmpdir],
        capture_output=True,
        text=True,
        check=False,
    )


class CanaryToolsScopeLintTests(unittest.TestCase):
    def test_no_canary_file_is_not_a_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = _run(tmp)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("NO_CANARY", result.stdout)

    def test_clean_canary_tools_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _write(
                os.path.join(tmp, "validation/tests/00_canary.sh"),
                'CANARY_TOOLS="${CANARY_TOOLS:-curl jq python3 pytest}"\n',
            )
            _write(
                os.path.join(tmp, "validation/Dockerfile.app"),
                "FROM python:3.12-slim\nRUN apt-get install -y curl jq\n",
            )
            result = _run(tmp)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("OK", result.stdout)

    def test_mongosh_in_canary_without_dockerfile_install_fails(self) -> None:
        # Reproduction of the log-5 failure: CANARY_TOOLS asserts mongosh
        # against the app container on a python:slim image that never
        # installs mongosh (app uses pymongo).
        with tempfile.TemporaryDirectory() as tmp:
            _write(
                os.path.join(tmp, "validation/tests/00_canary.sh"),
                'CANARY_TOOLS="${CANARY_TOOLS:-curl jq python3 pytest mongosh}"\n',
            )
            _write(
                os.path.join(tmp, "validation/Dockerfile.app"),
                "FROM python:3.12-slim\nRUN apt-get install -y curl jq\n",
            )
            result = _run(tmp)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("OFFENDERS:mongosh", result.stdout)

    def test_mongosh_installed_via_dockerfile_is_accepted(self) -> None:
        # Satisfies the "fix option 2" path: if the app image genuinely
        # installs mongosh, leaving it in CANARY_TOOLS is consistent.
        with tempfile.TemporaryDirectory() as tmp:
            _write(
                os.path.join(tmp, "validation/tests/00_canary.sh"),
                'CANARY_TOOLS="${CANARY_TOOLS:-curl jq python3 mongosh}"\n',
            )
            _write(
                os.path.join(tmp, "validation/Dockerfile.app"),
                "FROM python:3.12-slim\n"
                "RUN apt-get install -y curl jq mongodb-mongosh\n",
            )
            result = _run(tmp)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("OK", result.stdout)

    def test_multiple_offenders_reported_together(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _write(
                os.path.join(tmp, "validation/tests/00_canary.sh"),
                'CANARY_TOOLS="${CANARY_TOOLS:-curl jq mongosh psql redis-cli}"\n',
            )
            _write(
                os.path.join(tmp, "validation/Dockerfile.app"),
                "FROM python:3.12-slim\nRUN apt-get install -y curl jq\n",
            )
            result = _run(tmp)
            self.assertNotEqual(result.returncode, 0)
            # All three denylisted tools should be listed as offenders.
            self.assertIn("mongosh", result.stdout)
            self.assertIn("psql", result.stdout)
            self.assertIn("redis-cli", result.stdout)

    def test_non_denylist_tools_never_fail(self) -> None:
        # `git` and `make` are legitimate extras installed via Dockerfile.app
        # per the prompt's custom-test dependency analysis rule. They must
        # never trip the denylist.
        with tempfile.TemporaryDirectory() as tmp:
            _write(
                os.path.join(tmp, "validation/tests/00_canary.sh"),
                'CANARY_TOOLS="${CANARY_TOOLS:-curl jq python3 git make}"\n',
            )
            _write(
                os.path.join(tmp, "validation/Dockerfile.app"),
                "FROM python:3.12-slim\nRUN apt-get install -y curl jq git make\n",
            )
            result = _run(tmp)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("OK", result.stdout)

    def test_commented_dockerfile_install_does_not_satisfy(self) -> None:
        # A commented-out apt-get install must not be counted as installing
        # the tool, otherwise the lint is trivially bypassable.
        with tempfile.TemporaryDirectory() as tmp:
            _write(
                os.path.join(tmp, "validation/tests/00_canary.sh"),
                'CANARY_TOOLS="${CANARY_TOOLS:-curl jq mongosh}"\n',
            )
            _write(
                os.path.join(tmp, "validation/Dockerfile.app"),
                "FROM python:3.12-slim\n"
                "# RUN apt-get install -y mongosh\n"
                "RUN apt-get install -y curl jq\n",
            )
            result = _run(tmp)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("OFFENDERS:mongosh", result.stdout)

    def test_single_quoted_canary_tools_literal(self) -> None:
        # The prompt's recommended pattern uses double quotes, but a
        # single-quoted literal still appears in the wild and must extract
        # correctly.
        with tempfile.TemporaryDirectory() as tmp:
            _write(
                os.path.join(tmp, "validation/tests/00_canary.sh"),
                "CANARY_TOOLS='${CANARY_TOOLS:-curl jq python3 mongosh}'\n",
            )
            _write(
                os.path.join(tmp, "validation/Dockerfile.app"),
                "FROM python:3.12-slim\nRUN apt-get install -y curl jq\n",
            )
            result = _run(tmp)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("OFFENDERS:mongosh", result.stdout)

    def test_inline_comment_does_not_satisfy_install(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _write(
                os.path.join(tmp, "validation/tests/00_canary.sh"),
                'CANARY_TOOLS="${CANARY_TOOLS:-curl jq mongosh}"\n',
            )
            _write(
                os.path.join(tmp, "validation/Dockerfile.app"),
                "FROM python:3.12-slim\n"
                "RUN apt-get install -y curl jq # mongosh\n",
            )
            result = _run(tmp)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("OFFENDERS:mongosh", result.stdout)

    def test_package_alias_install_is_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _write(
                os.path.join(tmp, "validation/tests/00_canary.sh"),
                'CANARY_TOOLS="${CANARY_TOOLS:-curl jq psql}"\n',
            )
            _write(
                os.path.join(tmp, "validation/Dockerfile.app"),
                "FROM python:3.12-slim\n"
                "RUN apt-get install -y curl jq postgresql-client\n",
            )
            result = _run(tmp)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("OK", result.stdout)

    def test_mysql_alias_install_is_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _write(
                os.path.join(tmp, "validation/tests/00_canary.sh"),
                'CANARY_TOOLS="${CANARY_TOOLS:-curl jq mysql mysqladmin}"\n',
            )
            _write(
                os.path.join(tmp, "validation/Dockerfile.app"),
                "FROM python:3.12-slim\n"
                "RUN apt-get install -y curl jq default-mysql-client mariadb-client\n",
            )
            result = _run(tmp)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("OK", result.stdout)

    def test_tool_match_handles_shell_punctuation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _write(
                os.path.join(tmp, "validation/tests/00_canary.sh"),
                'CANARY_TOOLS="${CANARY_TOOLS:-curl jq psql}"\n',
            )
            _write(
                os.path.join(tmp, "validation/Dockerfile.app"),
                "FROM python:3.12-slim\n"
                "RUN apt-get install -y psql; echo done\n",
            )
            result = _run(tmp)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("OK", result.stdout)

    def test_line_continuation_install_is_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _write(
                os.path.join(tmp, "validation/tests/00_canary.sh"),
                'CANARY_TOOLS="${CANARY_TOOLS:-curl jq redis-cli}"\n',
            )
            _write(
                os.path.join(tmp, "validation/Dockerfile.app"),
                "FROM python:3.12-slim\n"
                "RUN apt-get install -y curl jq \\\n                    redis-tools\n",
            )
            result = _run(tmp)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("OK", result.stdout)

    def test_glob_literal_does_not_expand_to_filenames(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _write(
                os.path.join(tmp, "validation/tests/00_canary.sh"),
                'CANARY_TOOLS="${CANARY_TOOLS:-*}"\n',
            )
            _write(
                os.path.join(tmp, "validation/Dockerfile.app"),
                "FROM python:3.12-slim\nRUN apt-get install -y curl jq\n",
            )
            _write(os.path.join(tmp, "mongosh"), "sentinel\n")
            result = _run(tmp)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("OK", result.stdout)

    def test_canary_tools_line_trailing_comment_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _write(
                os.path.join(tmp, "validation/tests/00_canary.sh"),
                'CANARY_TOOLS="${CANARY_TOOLS:-curl jq python3}" # mongosh\n',
            )
            _write(
                os.path.join(tmp, "validation/Dockerfile.app"),
                "FROM python:3.12-slim\nRUN apt-get install -y curl jq\n",
            )
            result = _run(tmp)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("OK", result.stdout)


if __name__ == "__main__":
    unittest.main()
