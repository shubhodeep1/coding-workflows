"""DigitalOcean client: create, list by tag, delete droplets.

The token is a custom-scoped token limited to droplet create, read and delete
and tag create and read; it is held only by the scaler, never by a droplet.
API calls (CLAUDE.md §15): 1 per create, 1 per delete, and 1 per 200 droplets
listed (the cap keeps that to one page).
"""

from __future__ import annotations

from typing import Any, Callable

from .api import request_json

API_URL = "https://api.digitalocean.com/v2"
MAX_LIST_PAGES = 3


class DigitalOcean:
	def __init__(self, *, token: str, request: Callable[..., Any] = request_json, api_url: str = API_URL):
		self.token = token
		self.request = request
		self.api_url = api_url.rstrip("/")

	def _headers(self) -> dict[str, str]:
		return {"Authorization": f"Bearer {self.token}"}

	def create_droplet(self, *, name: str, region: str, size: str, image: str, tags: list[str], user_data: str, vpc_uuid: str) -> dict[str, Any]:
		"""Create in the dedicated runner VPC, never the region's default VPC."""
		if not vpc_uuid:
			raise ValueError("vpc_uuid is required")
		body = {"name": name, "region": region, "size": size, "image": image, "tags": tags, "user_data": user_data, "vpc_uuid": vpc_uuid, "ipv6": False, "monitoring": False, "backups": False}
		doc = self.request("POST", f"{self.api_url}/droplets", headers=self._headers(), body=body)
		return doc.get("droplet", {}) if isinstance(doc, dict) else {}

	def list_droplets(self, *, tag: str) -> list[dict[str, Any]]:
		droplets: list[dict[str, Any]] = []
		for page in range(1, MAX_LIST_PAGES + 1):
			doc = self.request("GET", f"{self.api_url}/droplets?tag_name={tag}&per_page=200&page={page}", headers=self._headers())
			items = doc.get("droplets") if isinstance(doc, dict) else None
			batch = [item for item in items or [] if isinstance(item, dict)]
			droplets.extend(batch)
			if len(batch) < 200:
				break
		return droplets

	def delete_droplet(self, droplet_id: int) -> None:
		self.request("DELETE", f"{self.api_url}/droplets/{int(droplet_id)}", headers=self._headers())
