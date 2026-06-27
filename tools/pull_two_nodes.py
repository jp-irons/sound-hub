"""
pull_two_nodes.py — Pull the same absolute time window of audio from two nodes.

Computes a single [tStartUs, tEndUs] window (now - lead, now - lead + duration)
and requests it from both nodes via the hub-mediated audio pull endpoint
(POST /api/nodes/{node_id}/sample), so both recordings are anchored to the
identical absolute epoch window. Polls both requests until done/unavailable/
error, and prints the resulting WAV paths — ready to feed straight into
clap_sync_check.py.

Usage:
    python tools/pull_two_nodes.py soundcapture-ed5de4 soundcapture-a1b2c3 \\
        --username admin --password secret

    # 30s of audio ending 10s ago, against a non-default hub:
    python tools/pull_two_nodes.py nodeA nodeB --duration 30 --lead 10 \\
        --hub-url http://192.168.101.220:8000 --username admin --password secret

    # Skip login and use an existing bearer token instead:
    python tools/pull_two_nodes.py nodeA nodeB --token eyJ...

Run from the sound-hub root directory. Requires: requests (see
tools/requirements.txt).
"""

import argparse
import getpass
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import requests


def _login(hub_url: str, username: str, password: str) -> str:
    resp = requests.post(
        f"{hub_url}/api/auth/login",
        json={"username": username, "password": password},
        timeout=10,
    )
    resp.raise_for_status()
    token = resp.json().get("access_token")
    if not token:
        raise RuntimeError(f"Login succeeded but no access_token in response: {resp.json()}")
    return token


def _request_pull(hub_url: str, node_id: str, t_start_us: int, t_end_us: int, headers: dict) -> dict:
    resp = requests.post(
        f"{hub_url}/api/nodes/{node_id}/sample",
        json={"tStartUs": t_start_us, "tEndUs": t_end_us},
        headers=headers,
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


def _poll_result(hub_url: str, node_id: str, request_id: int, poll_secs: float, headers: dict) -> dict:
    deadline = time.monotonic() + poll_secs
    last = {}
    seen = 0
    while time.monotonic() < deadline:
        resp = requests.get(f"{hub_url}/api/audio/requests/{request_id}", headers=headers, timeout=10)
        resp.raise_for_status()
        last = resp.json()
        acks = last.get("acks") or []
        for ack in acks[seen:]:
            print(f"     [{node_id}] ack: {ack.get('status')}  (from {ack.get('srcMac')} at {ack.get('at')})")
        seen = len(acks)
        status = acks[-1]["status"] if acks else None
        if status in ("done", "unavailable", "error"):
            return last
        time.sleep(2)
    last["_timeout"] = True
    return last


def _pull_and_poll(hub_url: str, node_id: str, t_start_us: int, t_end_us: int, poll_secs: float, headers: dict):
    print(f"  -> requesting {node_id} ...")
    relayed = _request_pull(hub_url, node_id, t_start_us, t_end_us, headers)
    request_id = relayed.get("requestId")
    if request_id is None:
        return node_id, {"_error": f"no requestId in response: {relayed}"}
    result = _poll_result(hub_url, node_id, request_id, poll_secs, headers)
    return node_id, result


def main():
    parser = argparse.ArgumentParser(
        description="Pull an identical absolute time window of audio from two nodes.")
    parser.add_argument("node_a", help="Hub node ID for node A")
    parser.add_argument("node_b", help="Hub node ID for node B")
    parser.add_argument("--duration", type=float, default=20.0,
                         help="Length of the audio window in seconds (default 20).")
    parser.add_argument("--lead", type=float, default=20.0,
                         help="How far before 'now' the window starts, in seconds (default 20, "
                              "i.e. window is [now-20s, now-20s+duration]).")
    parser.add_argument("--hub-url", default="http://localhost:8000",
                         help="Hub base URL (default http://localhost:8000).")
    parser.add_argument("--poll-secs", type=float, default=45.0,
                         help="How long to poll each request before giving up (default 45s — "
                              "Wi-Fi wake + WAV push for long segments can take a while after "
                              "the initial 'ack').")
    parser.add_argument("--username", default=None,
                         help="Hub login username. Prompted if omitted and --token not given.")
    parser.add_argument("--password", default=None,
                         help="Hub login password. Prompted (hidden) if omitted and --token not given.")
    parser.add_argument("--token", default=None,
                         help="Use an existing bearer token instead of logging in with --username/--password.")
    args = parser.parse_args()

    if args.token:
        token = args.token
    else:
        username = args.username or input("Hub username: ")
        password = args.password or getpass.getpass("Hub password: ")
        try:
            token = _login(args.hub_url, username, password)
        except requests.HTTPError as e:
            print(f"  [ERROR] login failed: {e}")
            sys.exit(1)
    headers = {"Authorization": f"Bearer {token}"}

    now_us = int(time.time() * 1_000_000)
    t_start_us = now_us - int(args.lead * 1_000_000)
    t_end_us = t_start_us + int(args.duration * 1_000_000)

    fmt = lambda us: time.strftime("%H:%M:%S", time.gmtime(us / 1_000_000)) + f".{us % 1_000_000 // 1000:03d}"
    print("--- Two-node audio pull ---")
    print(f"  Hub      : {args.hub_url}")
    print(f"  Window   : {t_start_us} .. {t_end_us}  [{fmt(t_start_us)} .. {fmt(t_end_us)} UTC]")
    print(f"  Node A   : {args.node_a}")
    print(f"  Node B   : {args.node_b}")
    print()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(_pull_and_poll, args.hub_url, args.node_a, t_start_us, t_end_us, args.poll_secs, headers),
            pool.submit(_pull_and_poll, args.hub_url, args.node_b, t_start_us, t_end_us, args.poll_secs, headers),
        ]
        results = dict(f.result() for f in futures)

    print()
    ok = True
    paths = {}
    for node_id in (args.node_a, args.node_b):
        r = results.get(node_id, {})
        if r.get("_error"):
            print(f"  [ERROR] {node_id}: {r['_error']}")
            ok = False
            continue
        acks = r.get("acks") or []
        status = acks[-1]["status"] if acks else None
        if r.get("file"):
            paths[node_id] = f"audio/{r['file']}"
            print(f"  [OK] {node_id}: {paths[node_id]}  ({r.get('bytes', '?')} bytes)")
        elif status == "unavailable":
            print(f"  [UNAVAILABLE] {node_id}: node has no audio for that window "
                  f"(GPS locked? on SD? within stored history?)")
            ok = False
        elif r.get("_timeout"):
            print(f"  [TIMEOUT] {node_id}: no result within {args.poll_secs}s")
            ok = False
        else:
            print(f"  [ERROR] {node_id}: unexpected response: {r}")
            ok = False

    if ok and len(paths) == 2:
        print()
        print("  Download locally (clap_sync_check.py needs local files, not hub paths):")
        local_names = {}
        for node_id in (args.node_a, args.node_b):
            url = f"{args.hub_url}/{paths[node_id]}"
            local = paths[node_id].split('/')[-1]
            local_names[node_id] = local
            print(f"    curl -O {url}   # or: Invoke-WebRequest {url} -OutFile {local}")
        print()
        print("  Next step:")
        print(f"    python tools/clap_sync_check.py {local_names[args.node_a]} {local_names[args.node_b]}")

    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
