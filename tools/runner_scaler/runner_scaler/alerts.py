"""Telegram alerts, one per incident key per cooldown (plan goals G5 and G7).

Uses the same secret names as scripts/tg_helpers.sh (TG_BOT_SECRET,
TG_ADMIN_CHAT_ID). Without them an alert is only logged. A send failure is
logged once and never retried in a loop; the next incident after the cooldown
tries again.
"""

from __future__ import annotations

from typing import Callable

from .api import ApiError, request_json
from .log import LogFn, log as default_log


class Alerter:
	def __init__(self, *, bot_token: str, chat_id: str, cooldown_seconds: float, clock: Callable[[], float], request: Callable[..., object] = request_json, log: LogFn = default_log):
		self.bot_token = bot_token
		self.chat_id = chat_id
		self.cooldown_seconds = cooldown_seconds
		self.clock = clock
		self.request = request
		self.log = log
		self.last_sent: dict[str, float] = {}

	def alert(self, key: str, text: str) -> bool:
		now = self.clock()
		last = self.last_sent.get(key)
		if last is not None and now - last < self.cooldown_seconds:
			return False
		self.last_sent[key] = now
		self.log("alert", key=key)
		if not self.bot_token or not self.chat_id:
			self.log("alert_not_sent", key=key, reason="telegram_not_configured")
			return True
		try:
			self.request(
				"POST",
				f"https://api.telegram.org/bot{self.bot_token}/sendMessage",
				headers={},
				body={"chat_id": self.chat_id, "text": f"Runner scaler: {text}", "disable_web_page_preview": True},
			)
		except ApiError as exc:
			self.log("alert_not_sent", key=key, reason="telegram_error", status=exc.status)
		return True
