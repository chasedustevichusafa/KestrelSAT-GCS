"""Hardcoded preview starting points from the owner's measured transfer rates.

JPEG size depends on scene detail; a resolution is not a byte-size guarantee.
Reserve two seconds for capture/SD/display/cleanup and expose the resulting
useful-byte budget. Measured rates are approximate, not a radio specification.
"""
from dataclasses import dataclass

OVERHEAD_SECONDS = 2.0


@dataclass(frozen=True)
class GuideRow:
    name: str
    speed: int
    resolution_5s: str
    resolution_10s: str

    def budget(self, interval):
        if interval not in (5, 10):
            raise ValueError('Preview refresh must be 5 or 10 seconds')
        return int(self.speed*(interval-OVERHEAD_SECONDS))


GUIDE = (
    GuideRow('Baseline', 1500, '160×120', '320×240'),
    GuideRow('Auto / Fast', 3850, '320×240', '320×240; VGA if ≤30 KiB'),
    GuideRow('Experimental', 4700, '320×240', '640×480'),
)


def guide_for_speed(speed):
    """Nearest measured tier for advice only; never changes the user's settings."""
    if speed <= 0:
        return None
    return min(GUIDE, key=lambda row: abs(row.speed-speed))


def recommendation(speed, interval, size):
    row = guide_for_speed(speed)
    if row is None:
        # A fast mock/small transfer can finish within one Windows clock tick.
        # There is no defensible B/s estimate in that case; do not invent one
        # or tell an already-running preview to start again.
        return 'Last download speed unavailable (duration below clock resolution). Use the approximate starting guide.'
    budget = int(max(0, interval-OVERHEAD_SECONDS)*speed)
    suggestion = row.resolution_5s if interval == 5 else row.resolution_10s
    fits = 'within budget' if size <= budget else 'over budget; lower resolution or choose 10 s'
    return (f'Last download {speed:,.0f} B/s | {interval} s target: about {budget/1024:.1f} KiB/JPEG | '
            f'last JPEG {size/1024:.1f} KiB: {fits}. Starting point: {suggestion}.')
