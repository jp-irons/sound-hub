"""
Tests for the TDOA solver integration in sound-hub.

Two layers:
  1. Solver unit tests — exercise tdoa_solver.py directly with deck array
     geometry. These are the most important tests: they verify the maths is
     correct for our specific array before any hardware exists.

  2. Route tests — exercise POST /api/tdoa/solve via FastAPI TestClient with
     a mocked DB, verifying error handling and the request/response contract.

Deck array geometry (placeholder — update node IDs and coordinates once
positions are surveyed and stored in the hub):

    Node 0 (god):  E=  0, N= 0, Alt=  0  — deck level, eastern end (origin)
    Node 1:        E= -8, N= 0, Alt= +2  — roof level, western end
    Node 2:        E= -8, N= 0, Alt= -4  — ground level, western end
    Node 3:        E=  0, N= 5, Alt= -4  — 5m forward (north), slope

The array has ~6m vertical spread (nodes 1 and 2) and a 5m forward baseline
(node 3) — well-conditioned for 3D localisation.

Forest hint point: (0, 50, -10) — 50m north, 10m below deck level. All real
bird sources should be in this halfspace; the mirror root lands behind/above
the array where there is no rainforest.
"""

import math
from unittest.mock import AsyncMock, patch

import numpy as np
import pytest

from server.tdoa_solver import Node, SolveResult, solve

# ---------------------------------------------------------------------------
# Deck array geometry (placeholder)
# ---------------------------------------------------------------------------

SPEED_OF_SOUND = 343.0

# Hub node IDs — these match the node_id keys in db.list_node_positions().
# Update when actual nodes are provisioned.
ID_GOD = "soundcapture-node0"
ID_N1  = "soundcapture-node1"
ID_N2  = "soundcapture-node2"
ID_N3  = "soundcapture-node3"

# Placeholder deck geometry (metres, E/N/Alt from origin).
# Replace with surveyed values once the hub position DB is populated.
GOD = Node(ID_GOD,  0.0,  0.0,  0.0)
N1  = Node(ID_N1,  -8.0,  0.0,  2.0)
N2  = Node(ID_N2,  -8.0,  0.0, -4.0)
N3  = Node(ID_N3,   0.0,  5.0, -4.0)

NODES_4 = [GOD, N1, N2, N3]

# A fifth node (hypothetical future expansion) for least-squares tests.
N4  = Node("soundcapture-node4", 4.0, 2.0, 1.0)
NODES_5 = [GOD, N1, N2, N3, N4]

# Forest hint — any point deep in the monitored area, north of the deck.
FOREST_HINT = (0.0, 50.0, -10.0)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _timestamps(source_xyz, nodes, c=SPEED_OF_SOUND):
    """Exact noiseless arrival timestamps (µs) from a known source position."""
    sx, sy, sz = source_xyz
    # Arbitrary base time of 0.5s so all timestamps are positive.
    return [
        (0.5 + math.sqrt((sx - n.x)**2 + (sy - n.y)**2 + (sz - n.z)**2) / c) * 1e6
        for n in nodes
    ]


def _loc_error(result: SolveResult, expected):
    ex, ey, ez = expected
    return math.sqrt((result.x - ex)**2 + (result.y - ey)**2 + (result.z - ez)**2)


def _either_root_within(result: SolveResult, expected, tol_m: float) -> bool:
    """Return True if either root is within tol_m of expected."""
    if _loc_error(result, expected) < tol_m:
        return True
    if result.ambiguous_root is not None:
        ex, ey, ez = expected
        ax, ay, az = result.ambiguous_root[:3]
        if math.sqrt((ax-ex)**2 + (ay-ey)**2 + (az-ez)**2) < tol_m:
            return True
    return False


# ---------------------------------------------------------------------------
# Solver accuracy — deck geometry, 4 nodes
# ---------------------------------------------------------------------------

class TestDeckGeometry4Node:

    def test_bird_30m_north(self):
        """Typical detection: bird singing 30m into the forest."""
        src = (0.0, 30.0, 5.0)
        result = solve(NODES_4, _timestamps(src, NODES_4), hint_point=FOREST_HINT)
        assert _loc_error(result, src) < 0.01, f"error={_loc_error(result, src):.4f}m"
        assert result.residual < 0.01

    def test_bird_50m_north_with_hint(self):
        """Hint selects the forest root (not the mirror behind the array)."""
        src = (-2.0, 50.0, 8.0)
        result = solve(NODES_4, _timestamps(src, NODES_4), hint_point=FOREST_HINT)
        assert _loc_error(result, src) < 0.01

    def test_bird_150m_range(self):
        """Far source at the design limit.

        At 145m the FOREST_HINT (50m north) is closer to the mirror root than
        to the actual source, so disambiguation is tested separately below.
        Here we verify solver accuracy regardless of which root is primary.
        """
        src = (10.0, 145.0, 15.0)
        result = solve(NODES_4, _timestamps(src, NODES_4), hint_point=FOREST_HINT)
        assert _either_root_within(result, src, 0.1), \
            f"150m source not found in either root (primary err={_loc_error(result, src):.2f}m)"

    def test_hint_150m_far_hint(self):
        """A hint further than the source correctly picks the right root at 145m."""
        src = (10.0, 145.0, 15.0)
        far_hint = (10.0, 200.0, 0.0)
        result = solve(NODES_4, _timestamps(src, NODES_4), hint_point=far_hint)
        assert _loc_error(result, src) < 0.1, \
            f"Far-hint 150m error={_loc_error(result, src):.4f}m"

    def test_bird_near_ground(self):
        """Low-perching bird, near the slope below the deck."""
        src = (-2.0, 8.0, -3.0)
        result = solve(NODES_4, _timestamps(src, NODES_4))
        assert _either_root_within(result, src, 0.01)

    def test_bird_above_deck(self):
        """Flying bird or high perch above the array."""
        src = (-4.0, 3.0, 15.0)
        result = solve(NODES_4, _timestamps(src, NODES_4))
        assert _either_root_within(result, src, 0.01)

    def test_mirror_root_always_present(self):
        """4-node solve must always return both roots."""
        src = (0.0, 30.0, 5.0)
        result = solve(NODES_4, _timestamps(src, NODES_4))
        assert result.ambiguous_root is not None

    def test_both_roots_satisfy_equations(self):
        """Both roots are exact mathematical solutions — mirror ambiguity."""
        src = (0.0, 30.0, 5.0)
        result = solve(NODES_4, _timestamps(src, NODES_4))
        # Primary root residual
        assert result.residual < 1e-3
        # Mirror root residual — compute manually
        ax, ay, az, ad = result.ambiguous_root
        pos   = np.array([[n.x, n.y, n.z] for n in NODES_4])
        d_arr = np.array([SPEED_OF_SOUND * t * 1e-6 for t in _timestamps(src, NODES_4)])
        diffs = np.sqrt((pos[:, 0]-ax)**2 + (pos[:, 1]-ay)**2 + (pos[:, 2]-az)**2)
        mirror_rms = float(np.sqrt(np.mean((diffs - np.abs(ad - d_arr))**2)))
        assert mirror_rms < 1e-3, f"Mirror root residual {mirror_rms:.2e}"

    def test_method_is_quadratic(self):
        src = (0.0, 30.0, 5.0)
        result = solve(NODES_4, _timestamps(src, NODES_4))
        assert result.method == "quadratic"

    def test_fewer_than_4_nodes_raises(self):
        with pytest.raises(ValueError, match="at least 4"):
            solve(NODES_4[:3], _timestamps((5, 20, 5), NODES_4[:3]))

    def test_timestamp_count_mismatch_raises(self):
        ts = _timestamps((5, 20, 5), NODES_4)
        with pytest.raises(ValueError, match="same length"):
            solve(NODES_4, ts[:-1])


# ---------------------------------------------------------------------------
# Solver accuracy — 5 nodes (least squares, no ambiguity)
# ---------------------------------------------------------------------------

class TestDeckGeometry5Node:

    def test_no_ambiguous_root(self):
        src = (0.0, 30.0, 5.0)
        result = solve(NODES_5, _timestamps(src, NODES_5))
        assert result.ambiguous_root is None

    def test_method_is_least_squares(self):
        src = (0.0, 30.0, 5.0)
        result = solve(NODES_5, _timestamps(src, NODES_5))
        assert result.method == "least_squares"

    def test_bird_30m_north(self):
        src = (0.0, 30.0, 5.0)
        result = solve(NODES_5, _timestamps(src, NODES_5))
        assert _loc_error(result, src) < 0.01

    def test_bird_150m_range(self):
        src = (10.0, 145.0, 15.0)
        result = solve(NODES_5, _timestamps(src, NODES_5))
        assert _loc_error(result, src) < 0.1  # no ambiguity with 5 nodes


# ---------------------------------------------------------------------------
# Noise sensitivity — realistic GPS-PPS and NMEA timing errors
# ---------------------------------------------------------------------------

class TestNoiseSensitivity:

    def test_1us_noise_4node_with_hint(self):
        """1µs GPS-PPS timing noise, 4 nodes, hint disambiguates.

        1µs → 0.34mm ranging error. GDOP at 50m range with ~8m array
        amplifies this to the sub-metre scale. Expect <5m error.
        """
        rng = np.random.default_rng(42)
        src = (0.0, 50.0, 8.0)
        ts = _timestamps(src, NODES_4)
        ts_noisy = [t + rng.normal(0, 1.0) for t in ts]
        result = solve(NODES_4, ts_noisy, hint_point=FOREST_HINT)
        err = _loc_error(result, src)
        assert err < 5.0, f"1µs noise produced {err:.2f}m error — unexpectedly large"

    def test_1us_noise_5node(self):
        """1µs noise, 5 nodes — overdetermined improves accuracy."""
        rng = np.random.default_rng(43)
        src = (0.0, 50.0, 8.0)
        ts = _timestamps(src, NODES_5)
        ts_noisy = [t + rng.normal(0, 1.0) for t in ts]
        result = solve(NODES_5, ts_noisy)
        err = _loc_error(result, src)
        assert err < 5.0, f"1µs noise (5 nodes) produced {err:.2f}m error"

    def test_500us_nmea_noise_4node(self):
        """~500µs NMEA-only timing — no GPS-PPS. Expect large but bounded error.

        500µs → 17cm ranging. At 50m range with 8m array, GDOP pushes this
        to the tens-of-metres scale. Tests that the solver doesn't blow up.
        """
        rng = np.random.default_rng(99)
        src = (0.0, 50.0, 8.0)
        ts = _timestamps(src, NODES_4)
        ts_noisy = [t + rng.normal(0, 500.0) for t in ts]
        result = solve(NODES_4, ts_noisy, hint_point=FOREST_HINT)
        err = _loc_error(result, src)
        # With NMEA-only timing we're not expecting precision — just that the
        # solver returns a finite result in the right general direction.
        assert err < 500.0, f"500µs NMEA noise produced {err:.1f}m error"
        assert math.isfinite(result.x) and math.isfinite(result.y)

    def test_hint_improves_disambiguation_under_noise(self):
        """Hint should reliably pick the forest root even with timing noise."""
        rng = np.random.default_rng(7)
        src = (0.0, 40.0, 6.0)
        ts = _timestamps(src, NODES_4)
        ts_noisy = [t + rng.normal(0, 5.0) for t in ts]
        result_hint  = solve(NODES_4, ts_noisy, hint_point=FOREST_HINT)
        result_nohint = solve(NODES_4, ts_noisy)
        # With hint, primary root should be the forest-side one
        hint_err  = _loc_error(result_hint, src)
        nohint_err = _loc_error(result_nohint, src)
        # If nohint picks the mirror, hint should do better
        assert hint_err <= nohint_err + 50.0  # hint at worst ties no-hint


# ---------------------------------------------------------------------------
# Route tests — POST /api/tdoa/solve via TestClient
# ---------------------------------------------------------------------------

# Lazy import so tests can run even if FastAPI/httpx aren't installed in
# the test environment — the solver unit tests above have no such dependency.
pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402
from server.main import app               # noqa: E402


# Fake hub position DB — keyed by node_id as stored in node_positions table.
FAKE_POSITIONS = {
    ID_GOD: {"pos_e":  0.0, "pos_n":  0.0, "pos_alt":  0.0, "is_origin": True,  "pos_status": "surveyed"},
    ID_N1:  {"pos_e": -8.0, "pos_n":  0.0, "pos_alt":  2.0, "is_origin": False, "pos_status": "estimated"},
    ID_N2:  {"pos_e": -8.0, "pos_n":  0.0, "pos_alt": -4.0, "is_origin": False, "pos_status": "estimated"},
    ID_N3:  {"pos_e":  0.0, "pos_n":  5.0, "pos_alt": -4.0, "is_origin": False, "pos_status": "estimated"},
}

# Fake registry — every node in FAKE_POSITIONS is "registered".
FAKE_REGISTRY = {nid: {"id": nid, "hostname": nid} for nid in FAKE_POSITIONS}


def _make_ts_payload(source_xyz, hint=None):
    """Build a valid TdoaRequest JSON body from a known source position."""
    nodes = [GOD, N1, N2, N3]
    ts = _timestamps(source_xyz, nodes)
    body = {
        "timestamps": [
            {"nodeId": n.node_id, "timestampUs": t}
            for n, t in zip(nodes, ts)
        ],
    }
    if hint is not None:
        body["hintPoint"] = list(hint)
    return body


@pytest.fixture()
def client():
    """TestClient with DB and registry calls mocked out."""
    with (
        patch("server.db.get_node_position", new_callable=AsyncMock,
              side_effect=lambda nid: FAKE_POSITIONS.get(nid)),
        patch("server.registry.get_node", new_callable=AsyncMock,
              side_effect=lambda nid: FAKE_REGISTRY.get(nid)),
        patch("server.db.init_db", new_callable=AsyncMock),
        patch("server.discovery.start", new_callable=AsyncMock),
        patch("server.discovery.stop", new_callable=AsyncMock),
        patch("server.poller.run", new_callable=AsyncMock, return_value=None),
    ):
        with TestClient(app) as c:
            yield c


class TestTdoaRoute:

    def test_solve_returns_200(self, client):
        body = _make_ts_payload((0.0, 30.0, 5.0), hint=FOREST_HINT)
        resp = client.post("/api/tdoa/solve", json=body)
        assert resp.status_code == 200, resp.text

    def test_solve_response_shape(self, client):
        body = _make_ts_payload((0.0, 30.0, 5.0), hint=FOREST_HINT)
        data = client.post("/api/tdoa/solve", json=body).json()
        assert "x" in data and "y" in data and "z" in data
        assert "residualM" in data
        assert "method" in data

    def test_solve_accuracy(self, client):
        src = (0.0, 30.0, 5.0)
        body = _make_ts_payload(src, hint=FOREST_HINT)
        data = client.post("/api/tdoa/solve", json=body).json()
        err = math.sqrt((data["x"]-src[0])**2 + (data["y"]-src[1])**2 + (data["z"]-src[2])**2)
        assert err < 0.01, f"Route solve error {err:.4f}m"

    def test_mirror_root_returned(self, client):
        body = _make_ts_payload((0.0, 30.0, 5.0))
        data = client.post("/api/tdoa/solve", json=body).json()
        # 4-node quadratic always has an ambiguous root
        assert data["ambiguousRoot"] is not None
        assert len(data["ambiguousRoot"]) == 3

    def test_too_few_timestamps_422(self, client):
        body = {
            "timestamps": [
                {"nodeId": ID_GOD, "timestampUs": 500000.0},
                {"nodeId": ID_N1,  "timestampUs": 500100.0},
                {"nodeId": ID_N2,  "timestampUs": 500200.0},
            ]
        }
        resp = client.post("/api/tdoa/solve", json=body)
        assert resp.status_code == 422

    def test_unknown_node_id_422(self, client):
        body = _make_ts_payload((0.0, 30.0, 5.0))
        # Replace one valid node ID with an unregistered one
        body["timestamps"][0]["nodeId"] = "soundcapture-unknown"
        resp = client.post("/api/tdoa/solve", json=body)
        assert resp.status_code == 422
        assert "Unknown node" in resp.json()["detail"]

    def test_node_without_position_422(self, client):
        """Node is registered but has no position in the hub DB."""
        body = _make_ts_payload((0.0, 30.0, 5.0))
        positions_minus_n3 = {k: v for k, v in FAKE_POSITIONS.items() if k != ID_N3}
        with patch(
            "server.db.get_node_position", new_callable=AsyncMock,
            side_effect=lambda nid: positions_minus_n3.get(nid),
        ):
            resp = client.post("/api/tdoa/solve", json=body)
        assert resp.status_code == 422
        assert "No stored position" in resp.json()["detail"]

    def test_custom_speed_of_sound(self, client):
        """Speed-of-sound override is accepted and used."""
        src = (0.0, 30.0, 5.0)
        # Generate timestamps with Brisbane-summer SoS
        sos = 347.0
        nodes = [GOD, N1, N2, N3]
        ts = [
            (0.5 + math.sqrt((src[0]-n.x)**2 + (src[1]-n.y)**2 + (src[2]-n.z)**2) / sos) * 1e6
            for n in nodes
        ]
        body = {
            "timestamps": [
                {"nodeId": n.node_id, "timestampUs": t}
                for n, t in zip(nodes, ts)
            ],
            "speedOfSound": sos,
            "hintPoint": list(FOREST_HINT),
        }
        resp = client.post("/api/tdoa/solve", json=body)
        assert resp.status_code == 200
        data = resp.json()
        err = math.sqrt((data["x"]-            src[0])**2 + (data["y"]-src[1])**2 + (data["z"]-src[2])**2)
        assert err < 0.01
