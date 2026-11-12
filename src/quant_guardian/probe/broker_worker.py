"""Standalone read-only worker, compatible with the broker's Python 3.6+.

Do not import Quant Guardian: its desktop runtime requires Python 3.11+.
Only health, counts and trading-calendar queries are accepted. No account,
security, price, amount, order IDs or raw exception text leave this process.
"""
import argparse
import base64
import ctypes
import json
import os
import re
import sys
import threading
import time
from datetime import datetime

sys.dont_write_bytecode = True
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w")


def unprotect(value):
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_byte))]

    raw = base64.b64decode(value)
    buffer = ctypes.create_string_buffer(raw)
    source = Blob(len(raw), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte)))
    target = Blob()
    if not ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(source), None, None, None, None, 0, ctypes.byref(target)
    ):
        raise RuntimeError("protected account cannot be read")
    try:
        return ctypes.string_at(target.data, target.size).decode("utf-8")
    finally:
        ctypes.windll.kernel32.LocalFree(target.data)


def wait_query(method, args, timeout):
    event = threading.Event()
    results = []

    def callback(value):
        results.append(value)
        event.set()

    method(*(args + (callback,)))
    if not event.wait(timeout):
        raise TimeoutError("read-only query timed out")
    return results[0]


class NativeClient:
    def __init__(self, request):
        library = os.path.join(os.path.dirname(sys.executable), "Lib", "site-packages")
        if os.path.normcase(os.path.realpath(request["xtquant_parent"])) != os.path.normcase(os.path.realpath(library)):
            raise RuntimeError("broker package path mismatch")
        sys.path.insert(0, library)
        from xtquant.xttrader import XtQuantTrader
        from xtquant.xttype import StockAccount

        self.account_type = StockAccount
        self.trader = XtQuantTrader(request["userdata_directory"], request["session_id"])
        self.trader.start()
        self.connected = False

    def close(self):
        self.trader.stop()

    def account(self, request):
        if not self.connected:
            if self.trader.connect() != 0:
                return None, "disconnected"
            self.connected = True
        timeout = float(request.get("query_timeout_seconds", 1.0))
        infos = wait_query(self.trader.query_account_infos_async, (), timeout)
        expected = unprotect(request["account_id_protected"]) if request.get("account_id_protected") else ""
        candidates = [i for i in (infos or []) if getattr(i, "account_id", "")]
        if expected:
            candidates = [i for i in candidates if str(i.account_id) == expected]
        if len(candidates) != 1:
            return None, "account_unverified"
        info = candidates[0]
        state = getattr(info, "login_status", getattr(info, "status", None))
        if state in (8, 9):
            return None, "capability_disabled"
        if state not in (0, 6):
            return None, "login_required"
        return self.account_type(str(info.account_id)), "ok" if state == 0 else "closed"

    def run(self, request):
        operation = request.get("operation")
        if operation not in ("health", "reconcile", "calendar"):
            raise ValueError("unsupported operation")
        if operation == "calendar":
            from xtquant import xtdata

            raw = xtdata.get_trading_dates(
                request.get("market", "SH"), request.get("start_date", ""),
                request.get("end_date", ""), -1,
            )
            dates = sorted(set(datetime.fromtimestamp(int(v) / 1000).strftime("%Y-%m-%d") for v in (raw or [])))
            return {"ok": bool(dates), "status": "calendar", "reason": "券商只读交易日历已返回", "details": {"trading_dates": dates}}
        account, state = self.account(request)
        if account is None:
            return {
                "ok": False,
                "status": "capability_disabled" if state == "capability_disabled" else "permission_required" if state != "disconnected" else "failed",
                "reason": "券商接口权限不可用，重启不能恢复权限" if state == "capability_disabled" else "账户需要登录或身份确认" if state != "disconnected" else "XtQuant connect returned -1",
                "account_status": "unknown" if state == "disconnected" else state,
            }
        timeout = float(request.get("query_timeout_seconds", 1.0))
        if operation == "health":
            asset = wait_query(self.trader.query_stock_asset_async, (account,), timeout)
            return {
                "ok": asset is not None, "status": "healthy" if asset is not None else "failed",
                "reason": "券商配套接口连接、账户与资产只读查询正常" if asset is not None else "只读资产查询结果不明确",
                "account_status": state, "details": {"asset_object_valid": asset is not None, "adapter": "broker_runtime"},
            }
        # Start independent queries together. A missing deal callback must not
        # discard returned order/position counts or delay the health worker.
        results, events = {}, {}
        specifications = (
            ("orders", self.trader.query_stock_orders_async, False),
            ("cancelable_orders", self.trader.query_stock_orders_async, True),
            ("positions", self.trader.query_stock_positions_async, None),
            ("trades", self.trader.query_stock_trades_async, None),
        )
        for key, method, cancelable in specifications:
            event = threading.Event()
            events[key] = event

            def callback(value, key=key, event=event):
                results[key] = len(value) if value is not None else "unknown"
                event.set()

            if cancelable is None:
                method(account, callback)
            else:
                method(account, callback, cancelable_only=cancelable)
        deadline = time.monotonic() + timeout
        for event in events.values():
            event.wait(max(0, deadline - time.monotonic()))
        counts = {key: results.get(key, "unknown") for key in events}
        complete = all(isinstance(v, int) for v in counts.values())
        return {
            "ok": complete, "status": "reconciled" if complete else "partial",
            "reason": "委托、可撤、成交和持仓数量已返回" if complete else "部分只读查询未返回；未知数量不计为零",
            "account_status": state,
            "details": dict(counts, query_status={key: "healthy" if isinstance(value, int) else "timeout" for key, value in counts.items()}),
        }


def serve(directory):
    request_path = os.path.join(directory, "request.json")
    client, key, last_id = None, None, ""
    last_activity = time.monotonic()
    try:
        while os.path.isdir(directory) and time.monotonic() - last_activity < 120:
            try:
                with open(request_path, encoding="utf-8") as stream:
                    request = json.load(stream)
            except (OSError, ValueError):
                time.sleep(0.05)
                continue
            request_id = request.get("request_id", "")
            if request_id == last_id or not re.match(r"^[a-f0-9]{32}$", request_id):
                time.sleep(0.05)
                continue
            last_id, last_activity = request_id, time.monotonic()
            started = time.monotonic()
            if request.get("operation") == "shutdown":
                break
            try:
                if request.get("operation") not in ("health", "reconcile", "calendar"):
                    raise ValueError("unsupported operation")
                new_key = (request["userdata_directory"], request["xtquant_parent"], request["session_id"])
                if key != new_key:
                    if client:
                        client.close()
                    client, key = NativeClient(request), new_key
                response = client.run(request)
            except Exception as error:
                response = {
                    "ok": False, "status": "timeout" if isinstance(error, TimeoutError) else "unavailable",
                    "reason": "券商只读查询超时" if isinstance(error, TimeoutError) else "券商只读运行环境不可用（%s）" % type(error).__name__,
                    "fatal": True,
                }
            response.update(request_id=request_id, latency_ms=int((time.monotonic() - started) * 1000))
            target = os.path.join(directory, request_id + ".json")
            with open(target + ".tmp", "w", encoding="utf-8") as stream:
                json.dump(response, stream, ensure_ascii=False)
            os.replace(target + ".tmp", target)
            if response.get("fatal"):
                break
    finally:
        if client:
            client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", required=True)
    serve(parser.parse_args().directory)
