"""Minimal client for monerod's JSON-RPC (standard library only).

The page talks only to the full RPC port, which monerod binds to 127.0.0.1
inside this container; wallets use the restricted port instead.
"""

import json
import urllib.error
import urllib.request


class RpcError(Exception):
    pass


class MoneroRpc:
    def __init__(self, host="127.0.0.1", port=18081, timeout=8):
        self.url = "http://%s:%d/json_rpc" % (host, port)
        self.timeout = timeout

    def call(self, method, params=None):
        body = json.dumps({"jsonrpc": "2.0", "id": "0", "method": method, "params": params or {}}).encode("utf-8")
        request = urllib.request.Request(self.url, data=body, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, OSError, ValueError) as err:
            raise RpcError(str(err))
        if not isinstance(data, dict):
            raise RpcError("bad answer")
        if data.get("error"):
            raise RpcError(str(data["error"].get("message") if isinstance(data["error"], dict) else data["error"]))
        result = data.get("result")
        if not isinstance(result, dict):
            raise RpcError("no result")
        return result
