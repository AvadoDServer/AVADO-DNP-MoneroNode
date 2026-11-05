import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import avadoweb  # noqa: E402
import main  # noqa: E402

GB = 1000 ** 3


def config(**env):
    return main.Config(env)


def main_view_words(m):
    """Words an owner sees without opening "Details" (same count as the Bitcoin packages' tests)."""
    parts = [m.get("headline")] + list(m.get("text") or [])
    parts += [(m.get("progress") or {}).get(k) for k in ("left", "right")]
    for f in m.get("facts") or []:
        parts += [f["k"], f["v"]]
    for s in m.get("sections") or []:
        parts += [s.get("title")] + list(s.get("text") or []) + list(s.get("howto") or []) + list(s.get("steps") or [])
        parts += [s.get("after")] if isinstance(s.get("after"), str) else list(s.get("after") or [])
        for f in s.get("fields") or []:
            parts += [f["k"], f["v"]] + (["Copy"] if f.get("copy") else [])
        parts += [b["label"] for b in (s.get("links") or []) + (s.get("buttons") or [])]
        if s.get("more") or s.get("advanced"):
            parts.append(s.get("advancedTitle") or "Details")
    return len(" ".join(p for p in parts if p).split())


def details(m):
    return [s for s in m["sections"] if s.get("id") == "details"][0]


def details_text(m):
    d = details(m)
    return " ".join(" ".join(x.get("text") or []) + " ".join(x.get("steps") or []) for x in d["more"])


def get_info(**kw):
    base = {"height": 2500000, "target_height": 3600000, "synchronized": False, "busy_syncing": True,
            "outgoing_connections_count": 8, "incoming_connections_count": 0, "version": "0.18.5.4-release",
            "nettype": "mainnet", "status": "OK"}
    base.update(kw)
    return base


class ArgsTest(unittest.TestCase):
    def test_defaults(self):
        args = main.monerod_args(config())
        for opt in ("--prune-blockchain", "--sync-pruned-blocks", "--rpc-bind-ip=127.0.0.1",
                    "--rpc-restricted-bind-ip=0.0.0.0", "--rpc-restricted-bind-port=18089", "--no-igd",
                    "--no-zmq", "--out-peers=8", "--in-peers=16", "--limit-rate-up=1024", "--non-interactive",
                    "--log-file=/dev/null"):
            self.assertIn(opt, args)
        # The full RPC is never reachable from outside the container, and nothing mines.
        self.assertNotIn("--confirm-external-bind", args)
        self.assertFalse([a for a in args if "mining" in a])
        self.assertFalse([a for a in args if a.startswith(("--stagenet", "--testnet"))])

    def test_test_networks(self):
        self.assertIn("--stagenet", main.monerod_args(config(NETWORK="stagenet")))
        self.assertEqual(config(NETWORK="nonsense").chain, "main")

    def test_limits_are_clamped(self):
        c = config(OUT_PEERS="0", IN_PEERS="99999", UPLOAD_LIMIT_KB="abc")
        self.assertEqual((c.out_peers, c.in_peers, c.upload_kb), (2, 256, 1024))

    def test_extra_opts_cannot_open_the_full_rpc_or_mine(self):
        c = config(EXTRA_OPTS="--rpc-bind-ip=0.0.0.0 --confirm-external-bind --start-mining=4abc "
                              "--bg-mining-enable --data-dir=/tmp --db-sync-mode=safe ; rm -rf / --log-level=1")
        self.assertEqual(c.extra_opts, ["--db-sync-mode=safe", "--log-level=1"])


class ExitTest(unittest.TestCase):
    def test_disk_full(self):
        self.assertEqual(main.explain_exit(["Error: No space left on device"]), "disk")
        self.assertEqual(main.explain_exit(["E MDB_MAP_FULL: Environment mapsize limit reached"]), "disk")

    def test_database(self):
        self.assertEqual(main.explain_exit(["Error opening database: MDB_CORRUPTED: Located page was wrong type"]),
                         "database")

    def test_unknown(self):
        self.assertIsNone(main.explain_exit(["Stopping cryptonote protocol..."]))


class ModelTest(unittest.TestCase):
    def model(self, info, **env):
        base = {"running": True, "free": 2000 * GB, "used": 40 * GB, "lanIp": "192.168.1.20"}
        base.update(info)
        return main.build_model(base, config(**env), avadoweb.Rate(), None)

    def test_downloading(self):
        m = self.model({"info": get_info()})
        self.assertEqual(m["state"], "working")
        self.assertIn("downloading", m["headline"])
        self.assertEqual(m["progress"]["percent"], 69.44)
        self.assertIn("3,600,000", m["progress"]["left"])
        self.assertIn({"k": "Connected to", "v": "8 Monero nodes", "tone": None}, m["facts"])
        self.assertIn({"k": "Disk used", "v": "40 GB"}, m["facts"])

    def test_looking_for_peers(self):
        m = self.model({"info": get_info(height=1, target_height=0, outgoing_connections_count=0)})
        self.assertIn("Looking for other Monero nodes", m["progress"]["left"])
        self.assertEqual(m["progress"]["percent"], 0)
        self.assertIn({"k": "Disk used", "v": "Just started"}, self.model({"info": get_info(), "used": 4096})["facts"])

    def test_synced(self):
        m = self.model({"info": get_info(height=3600000, target_height=0, synchronized=True, busy_syncing=False)})
        self.assertEqual(m["state"], "ok")
        self.assertNotIn("progress", m)
        connect = m["sections"][0]
        self.assertEqual(connect["id"], "connect")
        self.assertNotIn("download is done", " ".join(connect["howto"]))

    def test_wallet_address_in_plain_words(self):
        connect = self.model({"info": get_info()})["sections"][0]
        self.assertEqual(connect["title"], "Connect your wallet")
        self.assertEqual(connect["fields"], [{"k": "Address", "v": "192.168.1.20", "copy": True},
                                             {"k": "Port", "v": "18089", "copy": True}])
        self.assertIn("when the download is done", " ".join(connect["howto"]))
        self.assertIn("192.168.1.20:18089", details_text(self.model({"info": get_info()})))

    def test_wallet_address_without_lan_ip(self):
        m = self.model({"info": get_info(), "lanIp": None})
        self.assertEqual(m["sections"][0]["fields"][0]["v"], "monero.my.ava.do")
        self.assertIn("address your router shows", details_text(m))

    def test_starting(self):
        m = self.model({"rpcError": "connection refused"})
        self.assertIn("starting", m["headline"])
        self.assertEqual(m["sections"][0]["id"], "connect")

    def test_stopped_disk_full(self):
        m = self.model({"running": False, "free": 1 * GB, "lastExit": {"why": "disk", "at": 0, "pause": 60}})
        self.assertEqual(m["state"], "error")
        self.assertIn("disk is full", m["headline"])
        self.assertEqual([s["id"] for s in m["sections"]], ["details"])

    def test_damaged_data_offers_one_button(self):
        m = self.model({"running": False, "lastExit": {"why": "database", "at": 0, "pause": 60}})
        self.assertIn("damaged", m["headline"])
        self.assertEqual(m["sections"][0]["buttons"][0]["action"], "fresh-download")
        self.assertIn("confirm", m["sections"][0]["buttons"][0])

    def test_low_disk_warns(self):
        m = self.model({"info": get_info(), "free": 10 * GB})
        self.assertEqual(m["state"], "warn")
        self.assertIn("disk is full", " ".join(m["text"]))

    def test_mining_off_and_extras_explained(self):
        text = details_text(self.model({"info": get_info()}))
        self.assertIn("Mining is off", text)
        self.assertIn("P2Pool", text)
        self.assertIn("pruned", text)

    def test_main_view_is_short(self):
        """Status, progress, the numbers and the wallet address on top (about 120 words)."""
        cases = [
            {"info": get_info()},
            {"info": get_info(height=1, target_height=0, outgoing_connections_count=0)},
            {"info": get_info(height=3600000, target_height=0, synchronized=True)},
            {"info": get_info(), "free": 10 * GB},
            {"info": get_info(), "lanIp": None},
            {"rpcError": "refused"},
            {"running": False, "free": 1 * GB, "lastExit": {"why": "disk", "at": 0, "pause": 60}},
            {"running": False, "lastExit": {"why": "database", "at": 0, "pause": 60}},
            {"running": False, "lastExit": {"why": None, "at": 0, "pause": 60}},
        ]
        for case in cases:
            m = self.model(case)
            self.assertLessEqual(main_view_words(m), 120, case)
            self.assertEqual(m["sections"][-1]["id"], "details")
            self.assertEqual(sum(1 for s in m["sections"] if s.get("more") or s.get("advanced")), 1)
            self.assertLessEqual(len(m["sections"]), 2)


class TimeLeftTest(unittest.TestCase):
    def test_no_estimate_from_the_small_old_blocks(self):
        self.assertEqual(main.time_left_text(900000, 3600), "Usually 1 to 3 days in total")

    def test_no_estimate_yet(self):
        self.assertEqual(main.time_left_text(2500000, None), "Usually 1 to 3 days in total")

    def test_estimate_on_recent_blocks(self):
        self.assertEqual(main.time_left_text(2500000, 20 * 3600), "about 20 hours left")

    def test_very_slow_node(self):
        self.assertEqual(main.time_left_text(2500000, 40 * 86400), "More than 2 weeks left at this speed")


class HostTest(unittest.TestCase):
    def test_allowed(self):
        for host in ("monero.my.ava.do", "my.ava.do:80", "172.33.0.5", "localhost:8080",
                     "DAppNodePackage-monero.avado.dnp.dappnode.eth"):
            self.assertTrue(avadoweb.host_allowed(host), host)

    def test_refused(self):
        for host in ("evil.example", "monero.my.ava.do.evil.example", "", None):
            self.assertFalse(avadoweb.host_allowed(host), host)


class FramingTest(unittest.TestCase):
    """The Admin's Setup tab shows this page in a frame; other sites may not."""

    def test_only_the_admin_may_frame_the_page(self):
        csp = avadoweb.SECURITY_HEADERS["Content-Security-Policy"]
        self.assertIn("frame-ancestors 'self' http://my.ava.do https://my.ava.do;", csp)


if __name__ == "__main__":
    unittest.main()
