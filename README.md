# AVADO Monero Node

An AVADO package that runs [monerod](https://github.com/monero-project/monero)
(the official Monero release) with a plain-language page at
`http://monero.my.ava.do`. Package name: `monero.avado.dnp.dappnode.eth`.

## What it does

- **Space-saving by default.** The node keeps a pruned blockchain
  (`--prune-blockchain --sync-pruned-blocks`): about a third of the full size,
  and it still checks every block and transaction itself. Once pruned, the
  database stays pruned.
- **Mining off.** monerod never mines unless told to; the full RPC that could
  start mining listens only on 127.0.0.1 inside the container, and
  `EXTRA_OPTS` cannot contain any mining option or reopen that RPC.
- **A wallet port, view-only.** Wallets (Feather, Cake Wallet, the Monero GUI)
  connect to monerod's restricted RPC on port 18089, the usual port of Monero
  "remote nodes". It answers look-ups and sends transactions; mining, peer
  changes, bans and stopping the node are refused (404). The page shows the
  box's address on the home network (learned from the DAPPMANAGER's
  `getParams`), the port, and the steps for the common wallets.
  `--disable-rpc-ban` is set because Docker forwards every wallet on the home
  network from one gateway address.
- **Gentle on the home connection.** At most 8 outgoing and 16 incoming peers
  (monerod: 12 and unlimited) and 1 MB/s upload (monerod: 8 MB/s). The P2P
  port 18080 is not published, so the node connects out and takes no peers
  from the internet. UPnP (`--no-igd`) and ZMQ (`--no-zmq`) are off.
- **The page** keeps the main view short (about 120 words, checked by a unit
  test): status, download progress with percent and time left, connections,
  disk used, free space, and the wallet address. Wallet steps, what a wallet
  can do, disk space, mining and the numbers are behind one "Details".
  `/api/*` answers only requests addressed to the box (a `*.my.ava.do` name or
  an IP), buttons need the `X-Avado: 1` header, and the Content-Security-Policy
  allows no inline or foreign scripts.
- **Keeps monerod running** and says in plain words why it stopped (disk full,
  damaged database). After damaged data it offers one button, "Download
  again". Logs go to the container log only (`--log-file=/dev/null`).

Not in this version, on purpose: P2Pool mining (mining costs power and heat,
and the owner asked for mining off) and monero-wallet-rpc (a wallet on the box
would keep keys online). People keep using their own wallet app with this node.

Settings (Admin, package page): `OUT_PEERS` (8), `IN_PEERS` (16),
`UPLOAD_LIMIT_KB` (1024), `EXTRA_OPTS` (extra monerod options for support;
options that change the data folder, the logs, the RPC binding or mining are
ignored).

## Image

`build/Dockerfile` downloads the official `monero-linux-x64-<version>.tar.bz2`
from downloads.getmonero.org and checks its SHA-256 against
[hashes.txt](https://www.getmonero.org/downloads/hashes.txt), whose signature
was verified with binaryFate's key from the Monero source
(`utils/gpg_keys/binaryfate.asc`). The page and supervisor are Python standard
library only (`build/app`); their unit tests run during the image build.
Everything runs as an unprivileged user; a file capability lets the page open
port 80. `avadoweb.py`, `wamp.py` and `public/` (except the avatar) are the
same files as in the AVADO Bitcoin packages; keep the copies identical.

## Licence and reuse

GPL-3.0 (see LICENSE). Monero is BSD-3-Clause licensed.

Prior art: [dappnode/DAppNodePackage-monero](https://github.com/dappnode/DAppNodePackage-monero)
(GPL-3.0, Copyright DAppNode Association and contributors) and the older
AvadoDServer/AVADO-DNP-monero (`monero.dnp.dappnode.eth` 0.2.4, monerod
v0.17). From DAppNode: the avatar (`avatar.png`, the Monero logo, taken
unchanged) and the official-binary download with `--non-interactive`. The
pruning, the restricted wallet port, the peer limits and the page are new.

## Development

```
cd build/app && python3 -m unittest discover -s tests
docker buildx build --platform linux/amd64 --load -t monero-test -f build/Dockerfile .
docker run --rm -p 8080:80 -p 18089:18089 monero-test               # mainnet
docker run --rm -e NETWORK=stagenet -p 8080:80 monero-test         # test network
```

Release: a PR runs the AVADO build (GitHub Actions); merging to `main` (merge
commit, not squash) releases to the staging store.
