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
  python chain/chainctl.py votes               show validator votes still pending on each node
  python chain/chainctl.py clear-votes         discard all pending votes (do this if a vote never applied)
  python chain/chainctl.py rotate --size 4 --seed <text>
                                               move the validator set to a pseudo-random
                                               committee of centers (the board node stays)
  python chain/chainctl.py add-node 7 8 9      give centers 7, 8, 9 their own node (non-validating) on the RUNNING chain
  python chain/chainctl.py add-node --up-to 12 add nodes for every center up to 12
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


def live_addresses(net, lag: int = 10, min_peers: int = 2) -> set[str]:
    """
    Addresses of nodes that answer, are within `lag` blocks of the head, and are connected to other nodes
    (a node with 0-1 peers hangs off a single neighbour and would be a fragile validator).
    The network may be tiny, so the peer requirement is capped by how many nodes are running.
    """
    heights = {n["name"]: height(n) for n in net["nodes"]}
    known = [h for h in heights.values() if h is not None]
    if not known:
        return set()
    top = max(known)
    synced = [n for n in net["nodes"] if heights[n["name"]] is not None and top - heights[n["name"]] <= lag]
    need = min(min_peers, max(len(synced) - 1, 0))
    out = set()
    for n in synced:
        p = try_rpc(n, "net_peerCount")
        if p is None or int(p, 16) >= need:
            out.add(n["address"])
    return out


def quorum(n: int) -> int:
    """QBFT needs more than 2/3 of the validators: ceil(2n/3)."""
    return -(-2 * n // 3)


def safe_set(net, new_set: set[str], live: set[str]) -> tuple[bool, int, int]:
    up, need = len(new_set & live), quorum(len(new_set))
    return up >= need, up, need


def vote(net, target: dict, add: bool) -> bool:
    cur = validators(net)
    voters = [n for n in net["nodes"] if n["address"] in cur]
    ok = 0
    for n in voters:
        try:
            rpc(n, "qbft_proposeValidatorVote", [target["address"], add])
            ok += 1
        except Exception as e:
            print(f"  {n['name']}: vote failed ({e})")
    need = len(cur) // 2 + 1
    print(f"voted {'ADD' if add else 'REMOVE'} {target['name']}: {ok} of {len(cur)} validators voted "
          f"(a majority of {need} is needed); it takes effect after the next block(s)")
    if ok < need:
        print("  NOT ENOUGH VOTES REACHED: the validators that did vote may keep this vote pending. Start the "
              "validators that are down and try again.")
    return ok >= need


def wait_for(net, target_set: set[str], seconds=240) -> bool:
    """Wait for the validator set to change; print block progress so a slow chain and a halted one look different."""
    end = time.time() + seconds
    last_h, last_move, shown = None, time.time(), 0.0
    while time.time() < end:
        if set(validators(net)) == target_set:
            return True
        h = next((x for x in (height(n) for n in net["nodes"]) if x is not None), None)
        if h != last_h:
            last_h, last_move = h, time.time()
        if time.time() - shown >= 10:
            shown = time.time()
            stalled = int(time.time() - last_move)
            print(f"  waiting... block {h}" + (f"  (NO new block for {stalled}s)" if stalled >= 10 else ""))
        if time.time() - last_move > 90:
            print("  The chain has produced no block for 90 s: it is halted, not slow. Check `status`; restart "
                  "all validators together if too many were down.")
            return False
        time.sleep(2)
    return False


def guard(net, new_set: set[str], what: str, force: bool = False) -> None:
    live = live_addresses(net)
    cur = set(validators(net))
    ok_now, up_now, need_now = safe_set(net, cur, live)
    if not ok_now and not force:
        sys.exit(f"Refusing to {what}: only {up_now} of the {len(cur)} current validators are running "
                 f"(need {need_now}). Start the stopped validators first (chainctl.py start <n>).")
    ok, up, need = safe_set(net, new_set, live)
    if not ok and not force:
        down = [name_of(net, x) for x in new_set - live]
        sys.exit(f"Refusing to {what}: the new validator set of {len(new_set)} would have only {up} running "
                 f"(need {need}), so the chain would stop. Not running or behind: {', '.join(down)}. "
                 f"Start them first, or pass --force.")


def pending_votes(net) -> dict:
    """{node name: {address: add?}} for every node that answers."""
    out = {}
    for n in net["nodes"]:
        pv = try_rpc(n, "qbft_getPendingVotes")
        if pv:
            out[n["name"]] = pv
    return out


def clear_votes(net) -> int:
    """Discard every pending vote. A vote stays on its node until it reaches a majority, so a half-finished
    earlier attempt would otherwise keep pushing the validator set toward the OLD plan."""
    cleared = 0
    for n in net["nodes"]:
        for addr in (try_rpc(n, "qbft_getPendingVotes") or {}):
            if try_rpc(n, "qbft_discardValidatorVote", [addr]) is not None:
                cleared += 1
    return cleared


def cmd_votes(a, net):
    pv = pending_votes(net)
    if not pv:
        print("No pending votes.")
    for name, votes in pv.items():
        for addr, add in votes.items():
            print(f"{name}: {'ADD' if add else 'REMOVE'} {name_of(net, addr)}")


def cmd_clear_votes(a, net):
    print(f"Discarded {clear_votes(net)} pending vote(s).")


def cmd_vote_add(a, net):
    t = resolve(net, a.node)
    if t["address"] not in live_addresses(net) and not a.force:
        sys.exit(f"{t['name']} is not running or not caught up, so it cannot vote. Start it first "
                 f"(chainctl.py start <n>) and wait until `status` shows its block number, or pass --force.")
    guard(net, set(validators(net)) | {t["address"]}, f"add {t['name']}", a.force)
    vote(net, t, True)
    ok = wait_for(net, set(validators(net)) | {t["address"]})
    print("done" if ok else "not applied yet - a majority of validators must vote; check `status`")


def cmd_vote_remove(a, net):
    t = resolve(net, a.node)
    guard(net, set(validators(net)) - {t["address"]}, f"remove {t['name']}", a.force)
    vote(net, t, False)
    ok = wait_for(net, set(validators(net)) - {t["address"]})
    print("done" if ok else "not applied yet - a majority of validators must vote; check `status`")


def cmd_rotate(a, net):
    """Deterministic committee from a seed. Add first (so quorum only grows), then remove."""
    live = live_addresses(net)
    # Only nodes that are running, caught up and connected to peers can be chosen: a stopped or isolated node would
    # become a validator that cannot take part.
    centers = [n for n in net["nodes"] if n["name"] != "node-board" and n["address"] in live]
    if net["nodes"][0]["address"] not in live:
        sys.exit("The board node is not running.")
    if a.size > len(centers):
        sys.exit(f"--size {a.size} but only {len(centers)} center nodes are running, synced and connected (see `status`). "
                 f"Start more nodes (chainctl.py start <n>) or lower --size.")
    if a.size + 1 < 4:
        sys.exit("A committee needs at least 4 validators (board + 3 centers) to tolerate a fault.")
    r = random.Random(hashlib.sha256(a.seed.encode()).digest())
    pick = {n["address"] for n in r.sample(sorted(centers, key=lambda n: n["name"]), a.size)} | {net["nodes"][0]["address"]}
    cur = set(validators(net))
    to_add = [n for n in net["nodes"] if n["address"] in pick - cur]
    to_rm = [n for n in net["nodes"] if n["address"] in cur - pick]
    print("committee:", ", ".join(sorted(name_of(net, p) for p in pick)))
    print(f"changes: add {len(to_add)}, remove {len(to_rm)}")
    guard(net, cur, "start the rotation")
    stale = clear_votes(net)
    if stale:
        print(f"Discarded {stale} old pending vote(s) from earlier attempts.")
    for n in to_add:
        guard(net, set(validators(net)) | {n["address"]}, f"add {n['name']}")
        if not vote(net, n, True):
            sys.exit("Stopped: the add vote did not reach a majority.")
        if not wait_for(net, set(validators(net)) | {n["address"]}):
            sys.exit(f"{n['name']} was not added in time. Check `status` and `votes`; `clear-votes` resets.")
    for n in to_rm:
        guard(net, set(validators(net)) - {n["address"]}, f"remove {n['name']}")
        if not vote(net, n, False):
            sys.exit("Stopped: the remove vote did not reach a majority.")
        if not wait_for(net, set(validators(net)) - {n["address"]}):
            sys.exit(f"{n['name']} was not removed in time. Check `status` and `votes`; `clear-votes` resets.")
    now = set(validators(net))
    print("rotation", "complete" if now == pick else f"INCOMPLETE: now {len(now)} validators, wanted {len(pick)}")


def cmd_add_node(a, net):
    """
    Add non-validating nodes for more centers to the running network: same genesis, new keys, next free IP and
    host port. Existing nodes are not touched (their compose config does not change, so Docker leaves them running).
    A new node syncs and verifies every block but does not vote; use vote-add / rotate to make it a validator.
    """
    import ipaddress
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import make_network as mk

    nodes = net["nodes"]
    existing = {n["name"] for n in nodes}
    wanted = sorted(set(a.numbers) | (set(range(1, a.up_to + 1)) if a.up_to else set()))
    todo = [n for n in wanted if f"node-center-{n:03d}" not in existing]
    if not wanted:
        sys.exit("Say which centers: add-node 7 8 9   or   add-node --up-to 12")
    if not todo:
        sys.exit("Those centers already have nodes.")
    if any(n < 1 or n > 999 for n in todo):
        sys.exit("Center numbers must be between 1 and 999.")

    hosts = list(ipaddress.ip_network(net["subnet"]).hosts())
    used_ips = {n["ip"] for n in nodes}
    used_ports = {n["rpc_port"] for n in nodes}
    base_port = nodes[0]["rpc_port"]
    out = Path.cwd() / NET
    new = []
    for num in todo:
        i = 9 + num
        while i < len(hosts) and str(hosts[i]) in used_ips:
            i += 1
        if i >= len(hosts):
            sys.exit(f"The subnet {net['subnet']} is full. Regenerate with a bigger --subnet.")
        port = base_port + num
        while port in used_ports:
            port += 1
        node = mk.make_node(f"node-center-{num:03d}", str(hosts[i]), port)
        kd = out / "keys" / node["name"]
        kd.mkdir(parents=True, exist_ok=True)
        (kd / "key").write_text(node["private_key"][2:] + "\n")
        used_ips.add(node["ip"]); used_ports.add(port)
        nodes.append({k: v for k, v in node.items() if k != "private_key"})
        new.append(node["name"])

    (out / "nodes.json").write_text(json.dumps(net, indent=2))
    rel_out = str(NET).replace("\\", "/")
    (out / "docker-compose.chain.yml").write_text(mk.chain_compose(nodes, net["subnet"], rel_out, net["chain_id"]))
    base = Path.cwd() / "compose.yaml"
    if base.exists():
        txt, warns = mk.apps_compose(base, nodes, f"http://{nodes[0]['name']}:8545")
        (out / "docker-compose.apps.yml").write_text(txt)
        for w in warns:
            print("  note:", w)
    print(f"Added {len(new)} node(s): {', '.join(new)}   (total nodes now {len(nodes)})")
    if len(nodes) > 8:
        print("  Memory: every Besu node uses roughly 0.5 GB. Check Docker Desktop's memory limit "
              "(WSL2 defaults to half of your RAM) before adding many more.")
    if a.no_start:
        print("Not started (--no-start). Start with: python chain/chainctl.py up")
        return
    if sh(["docker", "compose", "--project-directory", ".", "-f", str(NET / "docker-compose.chain.yml"), "up", "-d"] + new):
        sys.exit("docker compose failed")
    fresh = {n["name"]: n for n in nodes}
    for name in new:
        node, ok = fresh[name], False
        for _ in range(45):
            h = height(node)
            if h is not None and try_rpc(node, "net_peerCount") not in (None, "0x0"):
                ok = True
                break
            time.sleep(2)
        print(f"  {name}: " + (f"up, syncing at block {height(node)}" if ok else "did not answer yet; see: docker logs neet-" + name))
    print("They verify every block but do not vote. To let one vote:  python chain/chainctl.py vote-add <number>")


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
    cur = set(validators(net))
    for n in [x for x in net["nodes"] if x["name"] != "node-board" and x["address"] in cur]:   # only voters matter
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
    p = sp.add_parser("vote-add"); p.add_argument("node"); p.add_argument("--force", action="store_true"); p.set_defaults(f=cmd_vote_add)
    p = sp.add_parser("vote-remove"); p.add_argument("node"); p.add_argument("--force", action="store_true"); p.set_defaults(f=cmd_vote_remove)
    sp.add_parser("votes").set_defaults(f=cmd_votes)
    sp.add_parser("clear-votes").set_defaults(f=cmd_clear_votes)
    p = sp.add_parser("rotate"); p.add_argument("--size", type=int, required=True); p.add_argument("--seed", required=True); p.set_defaults(f=cmd_rotate)
    p = sp.add_parser("add-node")
    p.add_argument("numbers", nargs="*", type=int, help="center numbers, e.g. 7 8 9")
    p.add_argument("--up-to", type=int, help="add every missing center node up to this number")
    p.add_argument("--no-start", action="store_true", help="write the files but do not start the containers")
    p.set_defaults(f=cmd_add_node)
    p = sp.add_parser("stop"); p.add_argument("nodes", nargs="+"); p.set_defaults(f=cmd_stop)
    p = sp.add_parser("start"); p.add_argument("nodes", nargs="+"); p.set_defaults(f=cmd_start)
    p = sp.add_parser("resilience"); p.add_argument("--restore", action="store_true", help="restart stopped nodes afterwards"); p.set_defaults(f=cmd_resilience)
    a = ap.parse_args()
    a.f(a, load())


if __name__ == "__main__":
    main()
