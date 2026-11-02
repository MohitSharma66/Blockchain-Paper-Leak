# Private chain (Besu QBFT, all in Docker on one PC)

One container = one node = one "center" (plus one board node). All nodes are validators.

## Start
Run everything from the project root (the folder with `compose.yaml`).

1. `pip install web3` (once)
2. `python chain/make_network.py --centers 6` -> 7 validators. Writes `chain/network/`.
   Re-running needs `--force` (new keys and genesis; first run `python chain/chainctl.py down --wipe`).
3. `python chain/chainctl.py up`, then `python chain/chainctl.py status`
   (blocks should climb every ~2 s, each node should show 6 peers).
4. Deploy the contract with the board's key. The deployer becomes `examBoard`, so it must be the same key
   as the backend's `BOARD_PRIVATE_KEY`:
   `BOARD_PRIVATE_KEY=0x<fresh key> CHAIN_RPC_URL=http://127.0.0.1:8545 npx hardhat run <path>/deploy.js --network private`
   (Hardhat 3 may prefer `npx hardhat keystore set BOARD_PRIVATE_KEY`.)
   Use a NEW key, not your Sepolia one. Gas is free, so the key needs no ETH.
5. Start the apps on the chain: `python chain/chainctl.py up --apps`.
   The backend must receive `BOARD_PRIVATE_KEY` (Vault/.env as before) and the new `contract_deployment.json`.

## Differences from Sepolia
- No ETH anywhere: base fee is 0, printers are not funded, no "no funds" warning.
- No clock skipping (Besu is not Hardhat). The Fast-forward panel hides itself. To demo the time-lock:
  register with a start time 2 minutes ahead, press Release (refused), wait, press Release again.
- The contract is compiled for EVM `paris` (see hardhat.config.ts). With default solc settings the bytecode
  contains PUSH0/MCOPY and would fail on this chain.
- Each printer talks to its own center's node (`http://node-center-00N:8545`); the backend talks to the board node.
- `CHAIN_RPC_URL` now takes priority over `SEPOLIA_RPC_URL` / `RPC_URL` in backend and printer agent.

## Day to day
`chainctl.py --help`: status, validators, vote-add / vote-remove, rotate, stop / start, resilience.
Resilience demo: 7 validators tolerate 2 offline; stopping a 3rd halts the chain; restarting restores it.

## Scale (for the report)
QBFT needs more than 2/3 of validators online (tolerates f = floor((N-1)/3)) and each block costs O(N^2) messages.
7 nodes: trivial. Hundreds: slow. About 6,000 centers (~36 million messages per round) is not realistic as a flat validator set.

Proposed design: a rotating committee. A small validator set (for example 10-30 centers drawn pseudo-randomly
from a public seed such as a recent block hash, plus the board) is changed per exam through validator votes.
All other centers run non-validating nodes that verify every block, so they do not have to trust anyone.
`python chain/chainctl.py rotate --size 4 --seed <text>` demonstrates this on the demo network.
Open point to state honestly in the report: who is allowed to vote validators in and out (governance) is a policy
decision the demo does not solve.

## NOT tested
Written without access to Docker or Besu. Verified here: genesis extraData decodes and matches the node keys,
the compose files merge (`docker compose config`), chainctl logic against a mock RPC, and the Python clients plus
the paris-compiled contract on a Ganache dev node (register, early release refused, release after the time passes,
zero-fee branch via mock).
Unverified until you run it: Besu accepting the genesis and flags, key-file format, node discovery over fixed IPs,
`qbft_*` votes, the resilience test, RAM use (~0.5-1 GB per node; try `--centers 4` if tight; `BESU_OPTS` sets the heap).
If a node will not start: `docker logs neet-node-board` and send me the first error lines.

to deploy the contract
npx hardhat run contracts/deploy.js --network private

python chain/make_network.py --centers 6

in case of restart
python chain/chainctl.py down --wipe
python chain/make_network.py --force
python chain/chainctl.py up

after that chech status
python chain/chainctl.py status

python chain/chainctl.py up --apps

and then migrate secrets to vault
python scripts/migrate_secrets_to_vault.py

and restart backend if need be
docker compose restart backend

for any backend changes
docker compose up --build -d backend
python scripts/migrate_secrets_to_vault.py