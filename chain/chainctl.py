#!/usr/bin/env python3
"""
chainctl.py - run and poke the private Besu QBFT network made by make_network.py.

Run from the project root:
  python chain/chainctl.py up                  start chain nodes (+ apps with --apps)
  python chain/chainctl.py down [--wipe]       stop; --wipe also deletes chain data
  python chain/chainctl.py status              block height, peers, validators per node
  python chain/chainctl.py validators          current validator set
  python chain/chainctl.py vote-add  <node>    every current validator votes to add <node>
  python chain/chainctl.py vote-remove <node>  every current validator votes to remove <node>
  python chain/chainctl.py rotate --size 4 --seed <text>
                                               move the validator set to a pseudo-random
                                               committee of centers (the board node stays)
  python chain/chainctl.py stop <node> [...]   kill nodes (simulate a center going offline)
  python chain/chainctl.py start <node> [...]
  python chain/chainctl.py resilience          stop nodes one by one, show when the chain halts

<node> is a name from nodes.json (node-center-003) or a number (3 = node-center-003, 0 = board).
Only needs the Python standard library.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import random
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

NET = Path("chain/network")
COMPOSE = ["docker", "compose", "--project-directory", ".", "-f", "compose.yaml",
           "-f", str(NET / "docker-compose.chain.yml"), "-f", str(NET / "docker-compose.apps.yml")]


def load() -> dict:
    p = NET / "nodes.json"
    if not p.exists():
        sys.exit("chain/network/nodes.json not found. Run from the project root after: python chain/make_network.py")
    return json.loads(p.read_text())


def resolve(net: dict, ref: str) -> dict:
    nodes = net["nodes"]
    if ref.isdigit():
        i = int(ref)
        if i >= len(nodes):
            sys.exit(f"no node number {i}")
        return nodes[i]
    for n in nodes:
        if n["name"] == ref:
            return n
    sys.exit(f"unknown node {ref!r}; known: {', '.join(n['name'] for n in nodes)}")


def rpc(node: dict, method: str, params=None, timeout: float = 4.0):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params or []}).encode()
    req = urllib.request.Request(f"http://127.0.0.1:{node['rpc_port']}", body, {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        out = json.loads(r.read())
    if "error" in out:
        raise RuntimeError(f"{method}: {out['error']}")
    return out["result"]


def try_rpc(node, method, params=None):
    try:
        return rpc(node, method, params)
    except Exception:
        return None


def height(node) -> int | None:
    h = try_rpc(node, "eth_blockNumber")
    return int(h, 16) if h else None


def validators(net) -> list[str]:
    for n in net["nodes"]:
        v = try_rpc(n, "qbft_getValidatorsByBlockNumber", ["latest"])
        if v is not None:
            return [x.lower() for x in v]
    sys.exit("no node answered - is the chain up?  (python chain/chainctl.py up)")


def name_of(net, addr: str) -> str:
    for n in net["nodes"]:
        if n["address"] == addr.lower():
            return n["name"]
    return addr


def sh(cmd: list[str]) -> int:
    print("$", " ".join(cmd))
    return subprocess.call(cmd)


# ------------------------------------------------------------ commands
def cmd_up(a, net):
    files = COMPOSE if a.apps else ["docker", "compose", "--project-directory", ".",
                                    "-f", str(NET / "docker-compose.chain.yml")]
    cmd = files + ["up", "-d"]
    if not a.apps:
        cmd += [n["name"] for n in net["nodes"]]
    sys.exit(sh(cmd))


def cmd_down(a, net):
    sys.exit(sh(COMPOSE + ["down"] + (["-v"] if a.wipe else [])))


def cmd_status(a, net):
    vals = set(validators(net))
    print(f"{'node':18} {'block':>7} {'peers':>5}  validator")
    for n in net["nodes"]:
        h = height(n)
        peers = try_rpc(n, "net_peerCount")
        print(f"{n['name']:18} {h if h is not None else 'DOWN':>7} {int(peers, 16) if peers else '-':>5}  "
              f"{'yes' if n['address'] in vals else 'no'}")
    print(f"validators: {len(vals)}  (tolerates {(len(vals) - 1) // 3} faulty; halts if more than that go offline)")


def cmd_validators(a, net):
    for v in validators(net):
        print(v, name_of(net, v))


def vote(net, target: dict, add: bool):
    cur = validators(net)
    voters = [n for n in net["nodes"] if n["address"] in cur]
    for n in voters:
        try:
            rpc(n, "qbft_proposeValidatorVote", [target["address"], add])
        except Exception as e:
            print(f"  {n['name']}: vote failed ({e})")
    print(f"voted {'ADD' if add else 'REMOVE'} {target['name']} on {len(voters)} validators; "
          f"it takes effect after the next block(s)")


def wait_for(net, target_set: set[str], seconds=60) -> bool:
    end = time.time() + seconds
    while time.time() < end:
        if set(validators(net)) == target_set:
            return True
        time.sleep(2)
    return False


def cmd_vote_add(a, net):
    t = resolve(net, a.node)
    vote(net, t, True)
    ok = wait_for(net, set(validators(net)) | {t["address"]})
    print("done" if ok else "not applied yet - a majority of validators must vote; check `status`")


def cmd_vote_remove(a, net):
    t = resolve(net, a.node)
    vote(net, t, False)
    ok = wait_for(net, set(validators(net)) - {t["address"]})
    print("done" if ok else "not applied yet - a majority of validators must vote; check `status`")


def cmd_rotate(a, net):
    """Deterministic committee from a seed. Add first (so quorum only grows), then remove."""
    centers = [n for n in net["nodes"] if n["name"] != "node-board"]
    if a.size > len(centers):
        sys.exit(f"--size {a.size} > {len(centers)} centers")
    r = random.Random(hashlib.sha256(a.seed.encode()).digest())
    pick = {n["address"] for n in r.sample(centers, a.size)} | {net["nodes"][0]["address"]}
    cur = set(validators(net))
    to_add = [n for n in net["nodes"] if n["address"] in pick - cur]
    to_rm = [n for n in net["nodes"] if n["address"] in cur - pick]
    print("committee:", ", ".join(sorted(name_of(net, p) for p in pick)))
    for n in to_add:
        vote(net, n, True)
        wait_for(net, set(validators(net)) | {n["address"]})
    for n in to_rm:
        vote(net, n, False)
        wait_for(net, set(validators(net)) - {n["address"]})
    now = set(validators(net))
    print("rotation", "complete" if now == pick else f"INCOMPLETE: now {len(now)} validators, wanted {len(pick)}")


def cmd_stop(a, net):
    sys.exit(sh(["docker", "stop"] + [f"neet-{resolve(net, r)['name']}" for r in a.nodes]))


def cmd_start(a, net):
    sys.exit(sh(["docker", "start"] + [f"neet-{resolve(net, r)['name']}" for r in a.nodes]))


def produces_blocks(net, seconds=12) -> bool:
    live = [n for n in net["nodes"] if height(n) is not None]
    if not live:
        return False
    h0 = max(height(n) or 0 for n in live)
    time.sleep(seconds)
    return max(height(n) or 0 for n in live) > h0


def cmd_resilience(a, net):
    """Stops center nodes one by one (never the board) and reports when blocks stop."""
    n_val = len(validators(net))
    f = (n_val - 1) // 3
    print(f"{n_val} validators -> theory: survives {f} offline, halts at {f + 1}")
    stopped = []
    for n in [x for x in net["nodes"] if x["name"] != "node-board"]:
        if len(stopped) > f + 1:
            break
        subprocess.call(["docker", "stop", f"neet-{n['name']}"], stdout=subprocess.DEVNULL)
        stopped.append(n["name"])
        ok = produces_blocks(net)
        print(f"{len(stopped)} offline ({n['name']} just stopped): {'blocks still produced' if ok else 'CHAIN HALTED'}")
        if not ok:
            break
    if a.restore:
        for s in stopped:
            subprocess.call(["docker", "start", f"neet-{s}"], stdout=subprocess.DEVNULL)
        print("restarted:", ", ".join(stopped), "- chain should resume once a quorum is back")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest="cmd", required=True)
    p = sp.add_parser("up"); p.add_argument("--apps", action="store_true", help="also start vault, backend and printers"); p.set_defaults(f=cmd_up)
    p = sp.add_parser("down"); p.add_argument("--wipe", action="store_true"); p.set_defaults(f=cmd_down)
    sp.add_parser("status").set_defaults(f=cmd_status)
    sp.add_parser("validators").set_defaults(f=cmd_validators)
    p = sp.add_parser("vote-add"); p.add_argument("node"); p.set_defaults(f=cmd_vote_add)
    p = sp.add_parser("vote-remove"); p.add_argument("node"); p.set_defaults(f=cmd_vote_remove)
    p = sp.add_parser("rotate"); p.add_argument("--size", type=int, required=True); p.add_argument("--seed", required=True); p.set_defaults(f=cmd_rotate)
    p = sp.add_parser("stop"); p.add_argument("nodes", nargs="+"); p.set_defaults(f=cmd_stop)
    p = sp.add_parser("start"); p.add_argument("nodes", nargs="+"); p.set_defaults(f=cmd_start)
    p = sp.add_parser("resilience"); p.add_argument("--restore", action="store_true", help="restart stopped nodes afterwards"); p.set_defaults(f=cmd_resilience)
    a = ap.parse_args()
    a.f(a, load())


if __name__ == "__main__":
    main()
