"""humanize.py — Human-like input generation (deterministic with seed).

Mouse paths are generated with a momentum-aware, ease-in-out cubic Bézier
that:

- adapts its step count to the distance (no more fixed per-segment
  ``steps=5`` repetition),
- progresses monotonically toward the target along the segment axis (no
  artificial U-turns or backtracking),
- applies a *tapered* organic wobble (low-frequency sine + small shaped
  noise) instead of a constant per-point Gaussian jitter,
- blends into the previous movement direction (momentum) so consecutive
  calls form one continuous gesture instead of independent arcs,
- and optionally overshoots the target by a couple of pixels with a
  small natural correction settle.

Typing cadence and click delays keep their previous bounded behaviour.
Everything is seed-able so tests and replays are reproducible.

Every generator is bounded: trajectory steps are capped at 200, jitter at
20px (truncated to ±jitter so output never leaves the declared range),
delays are clamped to their base-relative range, and cadence length is
capped. No generator can emit unbounded output.
"""

from __future__ import annotations

import math
import random

_DEFAULT_STEPS = 24
_DEFAULT_JITTER = 2.0
_JITTER_MAX = 20.0
_STEPS_MAX = 200
_STEPS_MIN = 2
_AUTO_STEP_PIXELS = 5.0  # one dispatched point per ~5px of travel
_AUTO_STEPS_MIN = 10
_AUTO_STEPS_MAX = 120
_CADENCE_MAX_LENGTH = 10_000


def _ease_in_out_sine(t: float) -> float:
    """0..1 with zero velocity at both ends (slow → fast → slow)."""
    t = max(0.0, min(1.0, t))
    return 0.5 - 0.5 * math.cos(math.pi * t)


def _blend_direction(
    dx: float, dy: float, entry_dir: tuple[float, float] | None, *, max_blend: float = 0.45,
) -> tuple[float, float]:
    """Blend the straight-line direction toward the previous movement dir.

    ``max_blend`` is the maximum angular correction applied; a large turn
    keeps most of its own direction while still flowing out of the old
    one. Returns a unit vector (falls back to the straight direction).
    """
    length = math.hypot(dx, dy)
    if length <= 1e-9:
        return (1.0, 0.0)
    ux, uy = dx / length, dy / length
    if not entry_dir:
        return (ux, uy)
    ex, ey = entry_dir
    elen = math.hypot(ex, ey)
    if elen <= 1e-9:
        return (ux, uy)
    ex, ey = ex / elen, ey / elen
    dot = max(-1.0, min(1.0, ux * ex + uy * ey))  # 1 = same way, -1 = U-turn
    if dot <= 0.0:
        # Real direction change: no momentum blend (a deliberate turn).
        return (ux, uy)
    blend = max_blend * dot
    bx, by = ex * blend + ux * (1.0 - blend), ey * blend + uy * (1.0 - blend)
    blen = math.hypot(bx, by) or 1.0
    return (bx / blen, by / blen)


def bezier_trajectory(
    x0: float, y0: float, x1: float, y1: float, *,
    steps: int | None = None, jitter: float = _DEFAULT_JITTER,
    seed: int | None = None,
    entry_dir: tuple[float, float] | None = None,
    overshoot: bool = True,
) -> list[tuple[float, float]]:
    """Natural points from (x0,y0) to (x1,y1) for one mouse gesture.

    - ``steps=None`` (or <= 0) auto-adapts to the distance.
    - Point spacing follows an ease-in-out profile (dense at the ends,
      sparse in the middle) so the cursor starts slow, speeds up, and
      slows down again as it settles on the target.
    - Lateral movement is a tapered organic wobble on a gently curved
      axis -- never a constant per-point jitter.
    - The projection along the segment axis is forced to be monotonic:
      the path always advances toward the target.
    - With some probability (and enough room) the path overshoots the
      target by a few pixels and settles back with small corrections.
    - ``entry_dir`` carries momentum from the previous gesture so chained
      calls read as one continuous movement.
    """
    rng = random.Random(seed)
    dx, dy = x1 - x0, y1 - y0
    distance = math.hypot(dx, dy)

    jitter = max(0.0, min(float(jitter), _JITTER_MAX))

    if steps is None or int(steps) <= 0:
        auto = int(round(distance / _AUTO_STEP_PIXELS))
        count = max(_AUTO_STEPS_MIN, min(auto, _AUTO_STEPS_MAX))
    else:
        count = max(_STEPS_MIN, min(int(steps), _STEPS_MAX))

    if distance <= 1e-6:
        return [(float(x0), float(y0)), (float(x1), float(y1))]

    ux, uy = _blend_direction(dx, dy, entry_dir)
    px, py = -uy, ux  # perpendicular

    # Gentle single-direction bend (correlated control offsets), bounded
    # to a small corridor so the path never loops or reverses.
    bend_scale = min(0.12 * distance, max(0.0, jitter) * 6.0 + 2.0)
    bend_a = rng.uniform(-bend_scale, bend_scale)
    bend_b = bend_a * 0.6 + rng.uniform(-bend_scale, bend_scale) * 0.4

    wob_freq = rng.uniform(0.6, 1.8)
    wob_phase = rng.uniform(0.0, 2.0 * math.pi)
    wob_amp = jitter * 1.5

    overshoot_prob = 0.3
    do_overshoot = bool(overshoot) and distance > 40.0 and rng.random() < overshoot_prob
    over_px = rng.uniform(1.5, 4.0) if do_overshoot else 0.0
    settle_from = max(1, count - 3)

    points: list[tuple[float, float]] = []
    prev_s = 0.0
    for i in range(count):
        t = i / (count - 1)
        u = _ease_in_out_sine(t)
        # Cubic Bézier along the (direction-blended) axis with the bend.
        inv = 1.0 - u
        c1 = (x0 + ux * distance / 3.0 + px * bend_a, y0 + uy * distance / 3.0 + py * bend_a)
        c2 = (x0 + ux * 2.0 * distance / 3.0 + px * bend_b, y0 + uy * 2.0 * distance / 3.0 + py * bend_b)
        bx = inv * inv * inv * x0 + 3 * inv * inv * u * c1[0] + 3 * inv * u * u * c2[0] + u * u * u * x1
        by = inv * inv * inv * y0 + 3 * inv * inv * u * c1[1] + 3 * inv * u * u * c2[1] + u * u * u * y1

        # Progress along the axis, forced monotonic (no backtracking).
        s = (bx - x0) * ux + (by - y0) * uy
        if i > 0:
            s = max(s, prev_s)
        if i == count - 1:
            s = distance  # settle exactly on target
        prev_s = s

        # Lateral: base curve offset + tapered organic wobble.
        lateral = (bx - x0) * px + (by - y0) * py
        taper = 0.15 + math.sin(math.pi * t) ** 0.8
        wobble = math.sin(2.0 * math.pi * wob_freq * t + wob_phase) * wob_amp
        noise = rng.gauss(0.0, jitter * 0.35)
        lateral += (wobble + noise) * taper

        if do_overshoot and i >= settle_from:
            # Overshoot past the target, then settle back with a small
            # correction instead of a hard stop.
            if i == settle_from:
                s = distance + over_px
            elif i == count - 1:
                s = distance
            else:
                back = (i - settle_from) / max(1, count - 1 - settle_from)
                s = distance + over_px * (1.0 - _ease_in_out_sine(back))

        sx = x0 + ux * s + px * lateral
        sy = y0 + uy * s + py * lateral
        points.append((round(sx, 2), round(sy, 2)))

    points[0] = (float(x0), float(y0))
    points[-1] = (float(x1), float(y1))
    return points


def step_delays(
    count: int, base_ms: float = 4.0, jitter_ms: float = 2.0,
    seed: int | None = None,
) -> list[float]:
    """Per-point dispatch delays realising the ease-in-out speed profile.

    Slow near both ends, faster in the middle; every value stays inside
    [0.5ms, 4*base_ms] and randomises a little so the timing never looks
    like a metronome.
    """
    count = max(0, min(int(count), _STEPS_MAX))
    base = max(0.1, float(base_ms))
    jitter = max(0.0, float(jitter_ms))
    rng = random.Random(seed)
    delays: list[float] = []
    for i in range(count):
        t = (i / (count - 1)) if count > 1 else 0.0
        speed = math.sin(math.pi * t) ** 0.7  # 0 at the ends, 1 mid-path
        delay = base * (1.7 - 0.9 * speed) + rng.uniform(-jitter, jitter)
        delays.append(round(max(0.5, min(delay, base * 4.0)), 2))
    return delays


def jittered_delay(base_ms: float, jitter_ms: float, seed: int | None = None) -> float:
    """base ± jitter, clamped to [0.25*base, 4*base]."""
    base = max(0.0, float(base_ms))
    jitter = max(0.0, float(jitter_ms))
    rng = random.Random(seed)
    delay = base + rng.uniform(-jitter, jitter)
    return round(max(base * 0.25, min(delay, base * 4.0)), 1)


def typing_cadence(
    length: int, base_ms: float = 45.0, jitter_ms: float = 25.0,
    seed: int | None = None,
) -> list[float]:
    """Per-character delays (ms) for typing ``length`` characters.

    Most delays sit at base ± jitter; every 4-7 characters a slight
    slowdown is attempted, clamped so the total never exceeds base +
    jitter — the cadence range stays bounded and consistent.
    """
    length = max(0, min(int(length), _CADENCE_MAX_LENGTH))
    base = max(0.0, float(base_ms))
    jitter = max(0.0, float(jitter_ms))
    rng = random.Random(seed)
    delays: list[float] = []
    for index in range(length):
        # slight slowdown after every 4-7 chars mimics hand pauses
        burst = rng.randint(4, 7)
        extra = rng.uniform(0.0, 90.0) if index > 0 and index % burst == 0 else 0.0
        delay = base + rng.uniform(-jitter, jitter) + extra
        delays.append(round(min(max(delay, 5.0), base + jitter), 1))
    return delays
