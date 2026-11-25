"""GitHub App client: installation tokens, JIT runners, runners, jobs, annotations, re-runs.

The App is installed on the account for the consumer repositories only, with
repository permissions Actions (read and write), Administration (read and write),
Checks (read, for the lost-runner annotations) and Metadata (read), and the
`workflow_job` webhook event. A GitHub App has one installation per account, so
the installation id and token are cached per repository owner and shared by
that owner's repositories (the token until 5 minutes before it expires). PyJWT and cryptography are
imported only when a token is minted, so the unit tests need neither.

API calls per use (CLAUDE.md §15) are listed on each method.
"""

from __future__ import annotations

import time
import urllib.parse
from datetime import datetime, timezone
from typing import Any, Callable

from .api import ApiError, request_json

API_URL = "https://api.github.com"
API_VERSION = "2022-11-28"
RUNNER_GROUP_DEFAULT = 1
RUNNER_LIST_MAX_PAGES = 3


def _default_jwt_encode(payload: dict[str, Any], private_key: str) -> str:
	import jwt  # PyJWT, with the cryptography backend for RS256

	return jwt.encode(payload, private_key, algorithm="RS256")


def _owner(repo: str) -> str:
	return repo.split("/", 1)[0].lower()


class GitHubApp:
	def __init__(self, *, app_id: str, private_key: str, clock: Callable[[], float] = time.time, request: Callable[..., Any] = request_json, jwt_encode: Callable[[dict[str, Any], str], str] = _default_jwt_encode, api_url: str = API_URL):
		self.app_id = app_id
		self.private_key = private_key
		self.clock = clock
		self.request = request
		self.jwt_encode = jwt_encode
		self.api_url = api_url.rstrip("/")
		# Keyed by the lower-cased repository owner (one installation per account).
		self._installation_ids: dict[str, int] = {}
		self._tokens: dict[str, tuple[str, float]] = {}

	def _headers(self, token: str) -> dict[str, str]:
		return {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": API_VERSION}

	def _app_jwt(self) -> str:
		now = int(self.clock())
		return self.jwt_encode({"iat": now - 60, "exp": now + 540, "iss": self.app_id}, self.private_key)

	def _installation_token(self, repo: str) -> str:
		"""1 call for the installation id (once per owner per process) + 1 per token (about hourly per owner)."""
		owner = _owner(repo)
		cached = self._tokens.get(owner)
		if cached and self.clock() < cached[1] - 300:
			return cached[0]
		app_jwt = self._app_jwt()
		installation_id = self._installation_ids.get(owner)
		if installation_id is None:
			doc = self.request("GET", f"{self.api_url}/repos/{repo}/installation", headers=self._headers(app_jwt))
			installation_id = int(doc["id"])
			self._installation_ids[owner] = installation_id
		doc = self.request("POST", f"{self.api_url}/app/installations/{installation_id}/access_tokens", headers=self._headers(app_jwt), body={})
		token = str(doc["token"])
		token_expires = self.clock() + 3600
		expires_at = doc.get("expires_at")
		if isinstance(expires_at, str):
			try:
				token_expires = datetime.strptime(expires_at[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc).timestamp()
			except ValueError:
				pass
		self._tokens[owner] = (token, token_expires)
		return token

	def api(self, method: str, repo: str, path: str, *, body: Any = None, query: dict[str, Any] | None = None) -> Any:
		url = f"{self.api_url}/repos/{repo}/{path}"
		if query:
			url = f"{url}?{urllib.parse.urlencode(query)}"
		try:
			return self.request(method, url, headers=self._headers(self._installation_token(repo)), body=body)
		except ApiError as exc:
			if exc.status == 401:
				# A revoked or rotated token: mint a fresh one on the next call.
				self._tokens.pop(_owner(repo), None)
			raise

	def generate_jit_config(self, repo: str, *, name: str, label: str) -> tuple[int, str]:
		"""1 call. Registers a single-use runner and returns (runner id, encoded JIT config)."""
		doc = self.api("POST", repo, "actions/runners/generate-jitconfig", body={"name": name, "runner_group_id": RUNNER_GROUP_DEFAULT, "labels": [label], "work_folder": "_work"})
		return int(doc["runner"]["id"]), str(doc["encoded_jit_config"])

	def list_runners(self, repo: str) -> list[dict[str, Any]]:
		"""1 call per 100 runners, at most RUNNER_LIST_MAX_PAGES."""
		runners: list[dict[str, Any]] = []
		for page in range(1, RUNNER_LIST_MAX_PAGES + 1):
			doc = self.api("GET", repo, "actions/runners", query={"per_page": 100, "page": page})
			batch = doc.get("runners") if isinstance(doc, dict) else None
			items = [item for item in batch or [] if isinstance(item, dict)]
			runners.extend(items)
			if len(items) < 100:
				break
		return runners

	def delete_runner(self, repo: str, runner_id: int) -> None:
		"""1 call."""
		self.api("DELETE", repo, f"actions/runners/{int(runner_id)}")

	def list_runs(self, repo: str, *, status: str, per_page: int) -> list[dict[str, Any]]:
		"""1 call."""
		doc = self.api("GET", repo, "actions/runs", query={"status": status, "per_page": per_page})
		runs = doc.get("workflow_runs") if isinstance(doc, dict) else None
		return [item for item in runs or [] if isinstance(item, dict)]

	def list_run_jobs(self, repo: str, run_id: int) -> list[dict[str, Any]]:
		"""1 call (latest attempt, first 100 jobs)."""
		doc = self.api("GET", repo, f"actions/runs/{int(run_id)}/jobs", query={"filter": "latest", "per_page": 100})
		jobs = doc.get("jobs") if isinstance(doc, dict) else None
		return [item for item in jobs or [] if isinstance(item, dict)]

	def job_annotations(self, repo: str, job_id: int) -> list[dict[str, Any]]:
		"""1 call. An Actions job id is also its check-run id."""
		doc = self.api("GET", repo, f"check-runs/{int(job_id)}/annotations", query={"per_page": 100})
		return [item for item in doc or [] if isinstance(item, dict)] if isinstance(doc, list) else []

	def rerun_job(self, repo: str, job_id: int) -> None:
		"""1 call. Re-runs one job (and the jobs that need it) as the run's next attempt."""
		self.api("POST", repo, f"actions/jobs/{int(job_id)}/rerun", body={})
