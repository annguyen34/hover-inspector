"""Cursor-dwell state machine for the hover inspector.

The app calls :meth:`DwellTracker.update` on every UI tick. It returns ``True``
exactly once after the cursor has stayed within ``tolerance`` pixels of the same
spot for ``delay`` seconds. Moving the cursor further than the tolerance re-arms
the tracker. Keeping this pure makes the timing rules testable off-Windows.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class DwellTracker:
    """Fire once after the cursor rests in one place for ``delay`` seconds."""

    delay: float = 1.0
    tolerance: int = 3
    _anchor: tuple[int, int] | None = field(default=None, init=False, repr=False)
    _last_move: float = field(default=0.0, init=False, repr=False)
    _fired: bool = field(default=False, init=False, repr=False)

    def reset(self) -> None:
        """Forget the anchor and allow the next resting spot to fire."""
        self._anchor = None
        self._last_move = 0.0
        self._fired = False

    def mark_fired(self, x: int, y: int, now: float | None = None) -> None:
        """Record a manual capture so the same resting spot does not fire again."""
        if now is None:
            now = time.monotonic()
        self._anchor = (int(x), int(y))
        self._last_move = now
        self._fired = True

    def update(self, x: int, y: int, now: float | None = None) -> bool:
        """Return ``True`` once when the cursor has dwelt long enough."""
        if now is None:
            now = time.monotonic()
        if self._anchor is None:
            self._anchor = (int(x), int(y))
            self._last_move = now
            self._fired = False
            return False

        dx = int(x) - self._anchor[0]
        dy = int(y) - self._anchor[1]
        if dx * dx + dy * dy > self.tolerance * self.tolerance:
            self._anchor = (int(x), int(y))
            self._last_move = now
            self._fired = False
            return False

        if not self._fired and now - self._last_move >= self.delay:
            self._fired = True
            return True
        return False
