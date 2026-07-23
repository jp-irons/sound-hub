"""
Acoustic TDOA solver — closed-form, arbitrary node geometry.

Method: Schau-Robinson linearisation in arbitrary coordinates.

Each node i gives the range equation:
    (x - xi)² + (y - yi)² + (z - zi)² = (d - di)²

where d = c·t_emission is the unknown emission range-equivalent (metres),
and di = c·ti is the scaled arrival time at node i (metres).

Subtracting node 0's equation from each other node's equation cancels
the quadratic terms, yielding a linear system in [x, y, z, d].

For N nodes this gives N-1 linear equations in 4 unknowns:
  - N=4 : 3 equations → underdetermined; express x,y,z as linear functions
          of d, substitute back into node 0's quadratic → solve quadratic in d.
  - N≥5 : 4+ equations → solve directly by least squares (no quadratic needed).

All coordinates and distances in metres.
Timestamps in microseconds (µs); converted internally.

Coordinate convention (matches hub's node_positions table):
    x = posE  (east,  metres)
    y = posN  (north, metres)
    z = posAlt (altitude, metres)
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np


# Default speed of sound at ~20°C, sea level.
# Brisbane subtropical conditions may warrant adjustment (~343–348 m/s).
DEFAULT_SPEED_OF_SOUND = 343.0  # m/s


def speed_of_sound_c(temp_celsius: float) -> float:
    """Speed of sound in dry air (m/s) at the given temperature (°C), sea
    level, via the standard linear approximation 331.3 + 0.606*T. Humidity
    effects (~0.1-0.6 m/s over realistic range) are ignored as negligible
    next to Brisbane's seasonal temperature swing."""
    return 331.3 + 0.606 * temp_celsius


# Conservative (slowest-plausible) speed of sound, used ONLY for flooring the
# TDOA pull-window travel-time margin (see routes.py's travel_time_floor_s) —
# never for the actual solve, which should use a realistic-for-conditions
# value (DEFAULT_SPEED_OF_SOUND above) since biasing that cold would degrade
# everyday solve accuracy. -10°C is a deliberately conservative floor past
# Brisbane's realistic overnight minimum; a slower assumed speed only ever
# widens the pull window (safe), never narrows it.
WORST_CASE_SPEED_OF_SOUND = speed_of_sound_c(-10.0)  # m/s, ~325.2


@dataclass(frozen=True)
class Node:
    """A receiver node at a known position.

    Attributes:
        node_id: Human-readable identifier, e.g. 'soundcapture-ed5de4'.
        x, y, z: Position in metres relative to the array origin
                 (x=east, y=north, z=altitude).
    """
    node_id: str
    x: float
    y: float
    z: float


@dataclass
class SolveResult:
    """Result from the TDOA solver.

    Attributes:
        x, y, z: Estimated source position in metres (same frame as nodes).
        d:        Emission range-equivalent c·t_emission (metres). Not directly
                  meaningful physically, but useful for residual checking.
        residual: RMS range residual across all nodes (metres). Lower is better.
                  For an exact 4-node solution this will be near zero.
        ambiguous_root: The other quadratic root (x,y,z,d), or None if the
                        problem was over-determined (5+ nodes) or one root was
                        clearly unphysical.
        method:   'quadratic' (4-node exact) or 'least_squares' (5+ nodes).
    """
    x: float
    y: float
    z: float
    d: float
    residual: float
    ambiguous_root: tuple[float, float, float, float] | None = None
    method: str = 'quadratic'


def solve(
    nodes: list[Node],
    timestamps_us: list[float],
    speed_of_sound: float = DEFAULT_SPEED_OF_SOUND,
    hint_point: tuple[float, float, float] | None = None,
) -> SolveResult:
    """Solve for the acoustic source position given node timestamps.

    Args:
        nodes:           List of Node objects (at least 4).
        timestamps_us:   GPS-disciplined arrival time at each node, in
                         microseconds. Order must match `nodes`.
        speed_of_sound:  Speed of sound in m/s. Default 343.0.
        hint_point:      Optional (x, y, z) point in the expected source
                         halfspace (e.g. a point deep in the forest). When
                         both quadratic roots are plausible, the root closer
                         to hint_point is selected. If None, falls back to
                         the root closer to the array centroid — which is
                         unreliable when sources are far from the array.

    Returns:
        SolveResult with estimated source position and quality metrics.

    Raises:
        ValueError: Fewer than 4 nodes, or singular geometry.
    """
    if len(nodes) < 4:
        raise ValueError(f"Need at least 4 nodes, got {len(nodes)}")
    if len(timestamps_us) != len(nodes):
        raise ValueError("nodes and timestamps_us must have the same length")

    c = speed_of_sound

    # Scale timestamps to metres: di = c * ti_seconds
    d_arr = np.array([c * t * 1e-6 for t in timestamps_us])
    pos = np.array([[n.x, n.y, n.z] for n in nodes])

    n = len(nodes)

    # Build the linearised system by subtracting node 0's equation from each other.
    # Node i equation expanded:
    #   x²-2xi·x + xi² + y²-2yi·y + yi² + z²-2zi·z + zi² = d²-2di·d + di²
    # Node 0 equation:
    #   x²-2x0·x + x0² + y²-2y0·y + y0² + z²-2z0·z + z0² = d²-2d0·d + d0²
    # Difference (quadratic terms cancel):
    #   2(x0-xi)x + 2(y0-yi)y + 2(z0-zi)z + 2(di-d0)d = x0²-xi² + y0²-yi² + z0²-zi² - d0²+di²
    #
    # Rearranged as A·[x,y,z,d]ᵀ = b:

    A = np.zeros((n - 1, 4))
    b = np.zeros(n - 1)

    x0, y0, z0 = pos[0]
    d0 = d_arr[0]

    for i in range(1, n):
        xi, yi, zi = pos[i]
        di = d_arr[i]
        A[i-1, 0] = 2 * (x0 - xi)
        A[i-1, 1] = 2 * (y0 - yi)
        A[i-1, 2] = 2 * (z0 - zi)
        A[i-1, 3] = 2 * (di - d0)
        b[i-1] = (x0**2 - xi**2 + y0**2 - yi**2 + z0**2 - zi**2
                  - d0**2 + di**2)

    if n >= 5:
        return _solve_least_squares(A, b, pos, d_arr, nodes)
    else:
        return _solve_quadratic(A, b, pos, d_arr, nodes, hint_point)


def _solve_least_squares(
    A: np.ndarray,
    b: np.ndarray,
    pos: np.ndarray,
    d_arr: np.ndarray,
    nodes: list[Node],
) -> SolveResult:
    """Over-determined case (N≥5): direct least-squares solution."""
    sol, residuals, rank, sv = np.linalg.lstsq(A, b, rcond=None)
    if rank < 4:
        raise ValueError(
            f"Geometry is singular or near-singular (rank={rank}). "
            "Nodes may be coplanar or collinear."
        )

    # rank<4 only catches exact degeneracy. Near-singular geometry (short
    # baselines relative to source distance, or a poorly-spread node subset
    # for this particular attempt) can still amplify ordinary timestamp
    # noise into wildly wrong output — mirrors the same check already done
    # for the 4-node quadratic path via np.linalg.cond(Axyz). lstsq already
    # computes the singular values needed for this; sv is sorted descending,
    # so sv[0]/sv[-1] is the condition number at no extra cost.
    cond = sv[0] / sv[-1] if sv[-1] > 0 else float('inf')
    if cond > 1e10:
        raise ValueError(
            f"Geometry is ill-conditioned (cond={cond:.2e}). "
            "Node subset may be poorly spread for this attempt."
        )

    x, y, z, d = sol
    rms = _rms_residual(x, y, z, d, pos, d_arr)
    return SolveResult(x=x, y=y, z=z, d=d, residual=rms, method='least_squares')


def _solve_quadratic(
    A: np.ndarray,
    b: np.ndarray,
    pos: np.ndarray,
    d_arr: np.ndarray,
    nodes: list[Node],
    hint_point: tuple[float, float, float] | None = None,
) -> SolveResult:
    """Exact 4-node case: express x,y,z as linear functions of d, solve quadratic."""
    # 3 equations, 4 unknowns. Partition A into spatial [Axyz | Ad]:
    #   Axyz · [x,y,z]ᵀ = b - Ad·d
    # => [x,y,z]ᵀ = Axyz⁻¹·b - Axyz⁻¹·Ad·d
    # => [x,y,z]ᵀ = p + q·d

    Axyz = A[:, :3]   # 3×3
    Ad   = A[:, 3]    # 3,

    try:
        Axyz_inv = np.linalg.inv(Axyz)
    except np.linalg.LinAlgError:
        raise ValueError(
            "Spatial submatrix is singular. Nodes may be coplanar or collinear."
        )

    cond = np.linalg.cond(Axyz)
    if cond > 1e10:
        raise ValueError(
            f"Spatial geometry is ill-conditioned (cond={cond:.2e}). "
            "Consider repositioning nodes."
        )

    p = Axyz_inv @ b        # constant term  [x,y,z] at d=0
    q = -Axyz_inv @ Ad      # slope term     d[x,y,z]/dd

    # Now substitute x=p[0]+q[0]·d, y=p[1]+q[1]·d, z=p[2]+q[2]·d
    # into node 0's quadratic equation:
    #   (x-x0)²+(y-y0)²+(z-z0)² = (d-d0)²
    #
    # Let u = p - pos[0], giving:
    #   (u[0]+q[0]·d)²+(u[1]+q[1]·d)²+(u[2]+q[2]·d)² = (d-d0)²
    # Expand:
    #   (q·q - 1)d² + 2(u·q + d0)d + (u·u - d0²) = 0

    u = p - pos[0]
    d0 = d_arr[0]

    G = float(np.dot(q, q) - 1.0)
    H = float(2.0 * (np.dot(u, q) + d0))
    I = float(np.dot(u, u) - d0**2)

    discriminant = H**2 - 4.0 * G * I

    if discriminant < 0:
        # Numerical noise can push it slightly negative; clamp if tiny.
        if discriminant > -1e-6:
            discriminant = 0.0
        else:
            raise ValueError(
                f"Negative discriminant ({discriminant:.3e}). "
                "Check node geometry and timestamp consistency."
            )

    sqrt_disc = math.sqrt(discriminant)

    if abs(G) < 1e-12:
        # Degenerate: quadratic collapses to linear in d
        if abs(H) < 1e-12:
            raise ValueError("Both G and H are zero — geometry fully degenerate.")
        d_sol = [-I / H]
    else:
        d_sol = [(-H + sqrt_disc) / (2.0 * G), (-H - sqrt_disc) / (2.0 * G)]

    # Compute (x,y,z) for each root and pick the physical one.
    candidates = []
    for d_val in d_sol:
        xyz = p + q * d_val
        rms = _rms_residual(xyz[0], xyz[1], xyz[2], d_val, pos, d_arr)
        candidates.append((d_val, xyz, rms))

    chosen, other = _select_root(candidates, pos, hint_point)
    d_val, xyz, rms = chosen

    ambiguous = None
    if other is not None:
        d_o, xyz_o, _ = other
        ambiguous = (float(xyz_o[0]), float(xyz_o[1]), float(xyz_o[2]), float(d_o))

    return SolveResult(
        x=float(xyz[0]), y=float(xyz[1]), z=float(xyz[2]),
        d=float(d_val), residual=rms,
        ambiguous_root=ambiguous,
        method='quadratic',
    )


def _select_root(
    candidates: list[tuple],
    pos: np.ndarray,
    hint_point: tuple[float, float, float] | None = None,
) -> tuple:
    """Choose the physically meaningful root.

    Both roots of the quadratic exactly satisfy all 4 range equations — they
    are mirror images across the plane of the nodes. Neither residuals nor
    the range equations alone can distinguish them without external information.

    Selection strategy:
      1. If residuals differ by >1000× and the larger exceeds 1mm (noisy data):
         prefer lower residual. Below that floor both are floating-point noise.
      2. If hint_point provided: prefer root closer to hint_point. This is the
         most reliable criterion — provide any point in the expected source
         halfspace (e.g. a known forest location).
      3. Fallback: prefer root farther from the array centroid. The mirror root
         often lands near or inside the node cluster, while the physical source
         is outside it. This heuristic fails for sources very close to the array.
    """
    if len(candidates) == 1:
        return candidates[0], None

    d0, xyz0, rms0 = candidates[0]
    d1, xyz1, rms1 = candidates[1]

    # 1. Residual criterion (noisy data only — below noise floor it is meaningless)
    NOISE_FLOOR_M = 1e-3
    if rms0 > NOISE_FLOOR_M or rms1 > NOISE_FLOOR_M:
        if rms0 < rms1 * 1e-3:
            return candidates[0], candidates[1]
        if rms1 < rms0 * 1e-3:
            return candidates[1], candidates[0]

    # 2. Hint point: prefer root closer to the supplied reference location.
    if hint_point is not None:
        hp = np.array(hint_point, dtype=float)
        dist0 = float(np.linalg.norm(xyz0 - hp))
        dist1 = float(np.linalg.norm(xyz1 - hp))
        if dist0 <= dist1:
            return candidates[0], candidates[1]
        return candidates[1], candidates[0]

    # 3. Fallback: prefer root farther from the array centroid.
    #    The mirror root typically appears near/inside the node cluster.
    centroid = pos.mean(axis=0)
    dist0 = float(np.linalg.norm(xyz0 - centroid))
    dist1 = float(np.linalg.norm(xyz1 - centroid))
    if dist0 >= dist1:
        return candidates[0], candidates[1]
    return candidates[1], candidates[0]


def _rms_residual(
    x: float, y: float, z: float, d: float,
    pos: np.ndarray, d_arr: np.ndarray,
) -> float:
    """RMS range residual across all nodes (metres)."""
    diffs = np.sqrt((pos[:, 0] - x)**2 + (pos[:, 1] - y)**2 + (pos[:, 2] - z)**2)
    expected = np.abs(d - d_arr)
    return float(np.sqrt(np.mean((diffs - expected)**2)))
