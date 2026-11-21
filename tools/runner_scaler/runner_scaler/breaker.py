"""Circuit breaker over droplet boots (plan goal G5).

A boot succeeds when the droplet's runner registers with GitHub and fails when
the droplet powers itself off before registering (its self-check failed) or
does not register within RUNNER_SCALER_REGISTER_TIMEOUT_SECONDS. The breaker
opens after `consecutive` failures in a row, or when more than `fail_ratio` of
the boots in the last `window_seconds` failed (with at least `min_boots` boots
in the window). While open it allows no droplet; after `cooldown_seconds` it
allows exactly one trial droplet (half-open): a success closes it, a failure
re-opens it. A restart starts closed, which allows at most a few more boots
before it opens again, so the breaker can never be reset into a creation loop.
"""

from __future__ import annotations

from collections import deque
from typing import Callable

CLOSED = "closed"
OPEN = "open"
HALF_OPEN = "half_open"


class CircuitBreaker:
	def __init__(self, *, consecutive: int, window_seconds: float, fail_ratio: float, min_boots: int, cooldown_seconds: float, clock: Callable[[], float]):
		self.consecutive = consecutive
		self.window_seconds = window_seconds
		self.fail_ratio = fail_ratio
		self.min_boots = min_boots
		self.cooldown_seconds = cooldown_seconds
		self.clock = clock
		self.state = CLOSED
		self.opened_at = 0.0
		self.failures_in_a_row = 0
		self.events: deque[tuple[float, bool]] = deque()

	def _trim(self, now: float) -> None:
		while self.events and now - self.events[0][0] > self.window_seconds:
			self.events.popleft()

	def record(self, success: bool) -> bool:
		"""Record one boot outcome; return True when this outcome opened the breaker."""
		now = self.clock()
		self.events.append((now, success))
		self._trim(now)
		if success:
			self.failures_in_a_row = 0
			if self.state == HALF_OPEN:
				self.state = CLOSED
				self.events.clear()
			return False
		self.failures_in_a_row += 1
		if self.state == HALF_OPEN:
			self.state = OPEN
			self.opened_at = now
			return True
		if self.state == OPEN:
			return False
		boots = len(self.events)
		failed = sum(1 for _, ok in self.events if not ok)
		if self.failures_in_a_row >= self.consecutive or (boots >= self.min_boots and failed / boots > self.fail_ratio):
			self.state = OPEN
			self.opened_at = now
			return True
		return False

	def creates_allowed(self, *, trial_in_flight: bool) -> int | None:
		"""How many new droplets may be created now; None means no breaker limit."""
		if self.state == OPEN and self.clock() - self.opened_at >= self.cooldown_seconds:
			self.state = HALF_OPEN
		if self.state == CLOSED:
			return None
		if self.state == HALF_OPEN:
			return 0 if trial_in_flight else 1
		return 0
