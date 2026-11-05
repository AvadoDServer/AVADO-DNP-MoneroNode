#!/usr/bin/env python3
"""AVADO Monero Node: runs monerod and its status page.

- Pruned blockchain (about a third of the full size), mining off.
- Wallets connect to the restricted RPC on port 18089: view-only commands plus
  sending transactions; nothing that mines, stops the node or changes peers.
  The full RPC listens only on 127.0.0.1 inside this container, for this page.
- A gentle peer and upload limit, so the node does not crowd the home internet.
- Restarts monerod when it stops, and explains on the page why it stopped.
"""

import collections
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time

import avadoweb
import wamp
from avadoweb import UserError, log
from monerorpc import MoneroRpc, RpcError

VERSION = (open(os.path.join(os.path.dirname(__file__), "package-version")).read().strip()
           if os.path.exists(os.path.join(os.path.dirname(__file__), "package-version")) else "dev")

DATA_DIR = os.environ.get("DATA_DIR", "/data")
MONERO_DIR = os.path.join(DATA_DIR, "monero")
PUBLIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "public")

GB = 1000 ** 3
NETWORKS = {"mainnet": "main", "stagenet": "stagenet", "testnet": "testnet"}
RPC_PORT = 18081      # full RPC, 127.0.0.1 inside the container only
WALLET_PORT = 18089   # restricted RPC, for wallets (the usual port of Monero "remote nodes")
P2P_PORTS = {"main": 18080, "testnet": 28080, "stagenet": 38080}

# Blocks before about 2019 are small, so the speed of the first part of the
# download says nothing about the rest. Below this height the page gives the
# usual total instead of an estimate.
STEADY_HEIGHT = 2000000

# Options the app sets itself, and anything about mining: EXTRA_OPTS may not change them.
FORBIDDEN_OPTS = ("--data-dir", "--config-file", "--log-file", "--rpc-bind-ip", "--rpc-bind-port",
                  "--rpc-restricted-bind-ip", "--rpc-restricted-bind-port", "--confirm-external-bind",
                  "--restricted-rpc", "--rpc-login", "--rpc-access-control-origins", "--detach", "--pidfile",
                  "--non-interactive", "--start-mining", "--mining-threads")


def env_int(name, default, low=None, high=None, env=os.environ):
    try:
        value = int(str(env.get(name, default)).strip())
    except ValueError:
        value = default
    if low is not None:
        value = max(low, value)
    if high is not None:
        value = min(high, value)
    return value


class Config:
    def __init__(self, env=os.environ):
        network = (env.get("NETWORK") or "mainnet").strip().lower()
        self.chain = NETWORKS.get(network, "main")
        # Gentle defaults: monerod would use 12 outgoing peers, unlimited incoming
        # ones and up to 8 MB/s upload.
        self.out_peers = env_int("OUT_PEERS", 8, 2, 64, env=env)
        self.in_peers = env_int("IN_PEERS", 16, 0, 256, env=env)
        self.upload_kb = env_int("UPLOAD_LIMIT_KB", 1024, 64, 65536, env=env)
        # Extra monerod options for support (passed as arguments, never through a shell).
        self.extra_opts = [o for o in (env.get("EXTRA_OPTS") or "").split()
                           if re.fullmatch(r"--[a-z][a-z0-9-]*(=\S*)?", o)
                           and o.split("=", 1)[0] not in FORBIDDEN_OPTS
                           and "mining" not in o.split("=", 1)[0]]
        self.monerod = env.get("MONEROD", "/usr/local/bin/monerod")
        self.wamp_host = env.get("WAMP_HOST") or "wamp.my.ava.do"

    @property
    def p2p_port(self):
        return P2P_PORTS[self.chain]


def monerod_args(config):
    args = [
        config.monerod,
        "--data-dir=%s" % MONERO_DIR,
        "--non-interactive",
        # Space-saving: keeps a random eighth of the old transaction data. Once
        # pruned, the database stays pruned.
        "--prune-blockchain",
        "--sync-pruned-blocks",
        # The full RPC only for this page; wallets get the restricted one.
        "--rpc-bind-ip=127.0.0.1",
        "--rpc-bind-port=%d" % RPC_PORT,
        "--rpc-restricted-bind-ip=0.0.0.0",
        "--rpc-restricted-bind-port=%d" % WALLET_PORT,
        # Docker forwards wallets on the home network from one gateway address:
        # one misbehaving wallet must not lock all of them out.
        "--disable-rpc-ban",
        "--no-igd",
        "--no-zmq",
        "--enable-dns-blocklist",
        # AVADO updates the app; monerod's own update check only adds log lines.
        "--check-updates=disabled",
        "--out-peers=%d" % config.out_peers,
        "--in-peers=%d" % config.in_peers,
        "--limit-rate-up=%d" % config.upload_kb,
        # Log to the container only (no growing log file in the volume).
        "--log-file=/dev/null",
        "--max-log-file-size=0",
        "--log-level=0",
    ]
    if config.chain != "main":
        args.append("--%s" % config.chain)
    return args + config.extra_opts


def chain_dir(config):
    return MONERO_DIR if config.chain == "main" else os.path.join(MONERO_DIR, config.chain)


def time_left_text(height, seconds_left):
    """The time-left note under the download bar. Old blocks are small and quick,
    so an estimate from them would be far too short; until the download reaches
    the blocks of recent years the page gives the usual total instead."""
    if height < STEADY_HEIGHT or not seconds_left:
        return "Usually 1 to 3 days in total"
    if seconds_left > 14 * 86400:
        return "More than 2 weeks left at this speed"
    return "%s left" % avadoweb.human_duration(seconds_left)


# ---------------------------------------------------------------- monerod

class Monerod:
    """Keeps monerod running; remembers why it stopped."""

    def __init__(self, config):
        self.config = config
        self.lock = threading.RLock()
        self.proc = None
        self.wanted = True
        self.lines = collections.deque(maxlen=80)
        self.started_at = None
        self.last_exit = None  # {"code", "at", "why", "pause"}
        self.restarts = 0
        self.next_start = 0

    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def _reader(self, proc):
        for raw in iter(proc.stdout.readline, b""):
            line = raw.decode("utf-8", "replace").rstrip()
            self.lines.append(line)
            print(line, flush=True)

    def start(self):
        with self.lock:
            if self.running() or not self.wanted:
                return
            os.makedirs(MONERO_DIR, exist_ok=True)
            args = monerod_args(self.config)
            log("starting", " ".join(args))
            self.lines.clear()
            self.proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                         stdin=subprocess.DEVNULL)
            self.started_at = time.time()
            threading.Thread(target=self._reader, args=(self.proc,), daemon=True).start()

    def stop(self, timeout=600):
        with self.lock:
            proc = self.proc
            if proc is None or proc.poll() is not None:
                return
            log("stopping monerod")
            proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()

    def watch(self):
        """Run in a thread: restart monerod after it stops, with a growing pause."""
        while True:
            time.sleep(2)
            with self.lock:
                if not self.wanted:
                    continue
                if self.proc is not None and self.proc.poll() is not None:
                    code = self.proc.returncode
                    why = explain_exit(list(self.lines))
                    uptime = time.time() - (self.started_at or time.time())
                    self.restarts = 0 if uptime > 600 else self.restarts + 1
                    pause = min(300, 10 * (2 ** min(self.restarts, 5)))
                    self.last_exit = {"code": code, "at": time.time(), "why": why, "pause": pause}
                    log("monerod stopped (exit %s): %s; next try in %d s" % (code, why or "no reason given", pause))
                    self.proc = None
                    self.next_start = time.time() + pause
                if self.proc is None and time.time() >= self.next_start:
                    try:
                        self.start()
                    except OSError as err:
                        log("cannot start monerod:", err)
                        self.next_start = time.time() + 60


def explain_exit(lines):
    text = "\n".join(lines[-40:])
    if re.search(r"No space left on device|not enough (?:disk|free) space|Free space is below|MDB_MAP_FULL", text, re.I):
        return "disk"
    if re.search(r"MDB_CORRUPTED|MDB_PAGE_NOTFOUND|MDB_INVALID|Error opening database|Failed to open db"
                 r"|database (?:is )?corrupt|Failed to initialize a database", text, re.I):
        return "database"
    return None


# ---------------------------------------------------------------- app

class App:
    def __init__(self, config):
        self.config = config
        os.makedirs(MONERO_DIR, exist_ok=True)
        self.node = Monerod(config)
        self.rpc = MoneroRpc("127.0.0.1", RPC_PORT, timeout=8)
        self.rate = avadoweb.Rate(window=3600)
        self.cache = {"at": 0, "model": None}
        self.cache_lock = threading.Lock()
        self.zero_peers_since = None
        self.lan_ip = None
        self.lan_ip_at = 0
        self.used = None
        self.used_at = 0

    # -- actions
    def action_fresh_download(self, _body):
        last = self.node.last_exit or {}
        if self.node.running() or last.get("why") != "database":
            raise UserError("Your node is not stopped by damaged data, so there is nothing to repair.")

        def fresh():
            with self.node.lock:
                self.node.wanted = False
                self.node.stop()
                log("owner asked for a fresh download: deleting the blockchain database")
                shutil.rmtree(os.path.join(chain_dir(self.config), "lmdb"), ignore_errors=True)
                self.rate = avadoweb.Rate(window=3600)
                self.used_at = 0
                self.node.last_exit = None
                self.node.restarts = 0
                self.node.wanted = True
                self.node.next_start = 0
                self.node.start()

        threading.Thread(target=fresh, daemon=True).start()
        return {"message": "Your node deletes its damaged data and downloads the Monero history again."}

    # -- page
    def box_ip(self):
        if time.time() - self.lan_ip_at > 600 or (self.lan_ip is None and time.time() - self.lan_ip_at > 60):
            self.lan_ip = wamp.box_lan_ip(host=self.config.wamp_host)
            self.lan_ip_at = time.time()
        return self.lan_ip

    def disk_used(self):
        if time.time() - self.used_at > 60:
            self.used = avadoweb.dir_size(MONERO_DIR)
            self.used_at = time.time()
        return self.used

    def status(self):
        with self.cache_lock:
            if self.cache["model"] is not None and time.time() - self.cache["at"] < 4:
                return self.cache["model"]
        model = build_model(self.gather(), self.config, self.rate, self)
        with self.cache_lock:
            self.cache = {"at": time.time(), "model": model}
        return model

    def gather(self):
        free, total = avadoweb.disk_free(DATA_DIR)
        info = {"running": self.node.running(), "lastExit": self.node.last_exit, "free": free, "total": total,
                "used": self.disk_used(), "lanIp": self.box_ip(), "startedAt": self.node.started_at}
        if info["running"]:
            try:
                info["info"] = self.rpc.call("get_info")
            except RpcError as err:
                info["rpcError"] = str(err)
        return info

    def run(self):
        signal.signal(signal.SIGTERM, self.shutdown)
        signal.signal(signal.SIGINT, self.shutdown)
        web = avadoweb.WebApp(PUBLIC_DIR, self.status, actions={"fresh-download": self.action_fresh_download},
                              version=VERSION, title="Monero Node")
        self.server = web.serve(port=env_int("PORT", 80))
        self.node.start()
        threading.Thread(target=self.node.watch, daemon=True).start()
        while True:
            time.sleep(3600)

    def shutdown(self, *_args):
        log("shutting down")
        self.node.wanted = False
        self.node.stop(timeout=600)
        sys.exit(0)


# ---------------------------------------------------------------- page model

def fmt_int(n):
    return "{:,}".format(int(n))


def build_model(info, config, rate, app=None):
    """The page: status, download progress, the numbers owners asked for and the
    wallet address on top (about 120 words); everything else behind one "Details"."""
    free = info.get("free")
    gi = info.get("info") or {}
    model = {"title": "Monero Node", "facts": [], "sections": [], "refresh": 10}
    network_note = "" if config.chain == "main" else " (test network: %s)" % config.chain

    synced = False
    stopped_why = None
    peers = None
    if not info.get("running"):
        last = info.get("lastExit") or {}
        stopped_why = last.get("why")
        wait = max(0, int(last.get("at", 0) + last.get("pause", 0) - time.time()))
        model["state"] = "error"
        if stopped_why == "disk":
            model["headline"] = "Your Monero node stopped: the disk is full"
            model["text"] = ["Your AVADO has %s free. The node tries again by itself every few minutes. Make room by "
                             "removing apps you no longer use." % avadoweb.human_bytes(free)]
        elif stopped_why == "database":
            model["headline"] = "Your Monero node stopped: its data is damaged"
            model["text"] = ["This can happen after a power cut. The node tries again by itself. If this message "
                             "stays, download the Monero history again with the button below."]
        else:
            model["headline"] = "Your Monero node is starting again"
            model["text"] = ["It stopped unexpectedly and starts again in %s." % avadoweb.human_duration(wait)
                             if last else "It is starting."]
    elif info.get("rpcError") or not gi:
        model["state"] = "working"
        model["headline"] = "Your Monero node is starting"
        model["text"] = ["It opens its data first. This can take a few minutes, longer after a power cut."]
    else:
        height = int(gi.get("height") or 0)
        target = int(gi.get("target_height") or 0)
        peers = int(gi.get("outgoing_connections_count") or 0) + int(gi.get("incoming_connections_count") or 0)
        if gi.get("synchronized") and not (target and height < target - 5):
            synced = True
            model["state"] = "ok"
            model["headline"] = "Your Monero node is up to date%s" % network_note
            model["text"] = ["It has checked every block itself and follows new blocks as they arrive."]
        else:
            model["state"] = "working"
            model["headline"] = "Your Monero node is downloading the Monero history%s" % network_note
            if target <= height:
                # The target is the height other nodes report; none answered yet.
                model["progress"] = {"percent": 0, "left": "Looking for other Monero nodes first",
                                     "right": "Usually 1 to 3 days in total"}
            else:
                progress = height / float(target)
                if height >= STEADY_HEIGHT:
                    rate.add(progress)
                model["progress"] = {
                    "percent": round(progress * 100, 2),
                    "left": "%.1f%% done, block %s of %s" % (progress * 100, fmt_int(height), fmt_int(target)),
                    "right": time_left_text(height, rate.seconds_left()),
                }
            model["text"] = ["It checks every block since 2014 itself. You can close this page: the download goes on."]
        if peers == 0:
            if app is not None:
                app.zero_peers_since = app.zero_peers_since or time.time()
                if time.time() - app.zero_peers_since > 300:
                    model["state"] = "warn"
                    model["text"].append("It cannot reach other Monero nodes. Check that the AVADO is connected to "
                                         "the internet.")
        elif app is not None:
            app.zero_peers_since = None

    if free is not None and free < 20 * GB and info.get("running"):
        model["state"] = "warn"
        model.setdefault("text", []).append("Only %s free on your AVADO. When the disk is full the node stops."
                                            % avadoweb.human_bytes(free))

    used = info.get("used")
    if peers is not None:
        model["facts"].append({"k": "Connected to", "v": "%d Monero node%s" % (peers, "" if peers == 1 else "s"),
                               "tone": "warn" if peers == 0 else None})
    model["facts"].append({"k": "Disk used", "v": avadoweb.human_bytes(used) if used and used >= 10 ** 6
                           else "Just started"})
    model["facts"].append({"k": "Free on your AVADO", "v": avadoweb.human_bytes(free),
                           "tone": "warn" if free is not None and free < 50 * GB else None})

    if stopped_why == "database":
        model["sections"].append({"id": "repair", "title": "Download again",
                                  "text": ["Deletes the damaged data and downloads the Monero history again."],
                                  "buttons": [{"action": "fresh-download", "label": "Download again",
                                               "style": "primary",
                                               "confirm": "Your node deletes its Monero data and downloads the "
                                                          "history again (1 to 3 days). Your wallets and money are "
                                                          "not affected. Continue?"}]})
    elif stopped_why != "disk":
        model["sections"].append(connect_section(info, synced))
    model["sections"].append(details_section(info, config))
    return model


def wallet_host(info):
    return info.get("lanIp") or "monero.my.ava.do"


def connect_section(info, synced):
    """The wallet address in plain words. Its id "connect" can be linked to."""
    host = wallet_host(info)
    s = {"id": "connect", "title": "Connect your wallet"}
    s["fields"] = [
        {"k": "Address", "v": host, "copy": True},
        {"k": "Port", "v": str(WALLET_PORT), "copy": True},
    ]
    s["howto"] = ["In your Monero wallet, choose to use a remote node and type this address and port."]
    if not synced:
        s["howto"].append("Your wallet can use it when the download is done.")
    return s


def details_section(info, config):
    """One "Details" card: wallet steps, safety, disk space, mining, the numbers."""
    gi = info.get("info") or {}
    host = wallet_host(info)
    if info.get("lanIp"):
        away = ("The address above works on your home network. Away from home, with AVADO Remote Access on (or on "
                "the AVADO Wi-Fi), use monero.my.ava.do instead.")
    else:
        away = ("monero.my.ava.do works when your device is on the AVADO Wi-Fi or uses AVADO Remote Access. On your "
                "home network, use the address your router shows for the AVADO.")
    more = [
        {"title": "Feather Wallet", "steps": [
            "Open Settings, then Network, then Node.",
            "Choose \"Specify custom node(s)\" and add %s:%d." % (host, WALLET_PORT),
        ]},
        {"title": "Monero GUI and Cake Wallet", "steps": [
            "Monero GUI: Settings, Node, \"Remote node\", then \"Add remote node\".",
            "Cake Wallet: Settings, \"Connection and sync\", \"Manage nodes\", then +.",
            "Type the address and port above. Leave login and SSL empty or automatic.",
        ]},
        {"title": "Away from home", "text": [away]},
        {"title": "What a wallet can do here",
         "text": ["Look up the blockchain and send your transactions. It cannot stop the node, change its settings "
                  "or start mining. Your keys stay in your wallet: the node never sees them."]},
        {"title": "Disk space",
         "text": ["Your node keeps a pruned copy of the blockchain: about a third of the full size (roughly 100 GB). "
                  "It still checks every block and transaction itself."]},
        {"title": "Mining and other extras",
         "text": ["Mining is off: the node does not use your AVADO's power for it. P2Pool mining and a wallet on the "
                  "AVADO are not part of this app, so no keys are kept on the box."]},
    ]
    adv = []
    if gi:
        adv.append({"k": "Blocks checked", "v": fmt_int(gi.get("height") or 0)})
        if gi.get("target_height"):
            adv.append({"k": "Newest block (other nodes)", "v": fmt_int(gi["target_height"])})
        adv.append({"k": "Connections out / in", "v": "%s / %s" % (gi.get("outgoing_connections_count", "?"),
                                                                   gi.get("incoming_connections_count", "?"))})
        adv.append({"k": "Monero version", "v": str(gi.get("version") or "Unknown")})
    adv += [
        {"k": "Network", "v": {"main": "Monero (mainnet)"}.get(config.chain, config.chain)},
        {"k": "Blockchain", "v": "Pruned (space-saving)"},
        {"k": "Port for wallets", "v": "%d (view-only)" % WALLET_PORT},
        {"k": "Peer limit", "v": "%d out, %d in" % (config.out_peers, config.in_peers)},
        {"k": "Upload limit", "v": "%s per second" % avadoweb.human_bytes(config.upload_kb * 1024)},
        {"k": "Port for other Monero nodes", "v": "%d (inside the AVADO; the node connects out)" % config.p2p_port},
    ]
    return {"id": "details", "more": more, "advanced": adv}


if __name__ == "__main__":
    App(Config()).run()
