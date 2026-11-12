from __future__ import annotations

import ast
import base64
import ctypes
import json
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from quant_guardian.config import ProbeConfig, QmtConfig
from quant_guardian.domain.models import ProbeStatus
from quant_guardian.probe import broker_worker
from quant_guardian.probe.broker_supervisor import BrokerProbeSupervisor
from quant_guardian.probe.protocol import ProbeResponse
from quant_guardian.probe.supervisor import response_status


class Trader:
    def connect(self):
        return 0

    def query_account_infos_async(self, callback):
        callback([SimpleNamespace(account_id="PRIVATE_ACCOUNT", login_status=0)])

    def query_stock_asset_async(self, account, callback):
        callback(object())

    def query_stock_orders_async(self, account, callback, cancelable_only=False):
        callback([] if cancelable_only else [object(), object()])

    def query_stock_positions_async(self, account, callback):
        callback([object()])

    def query_stock_trades_async(self, account, callback):
        return None


def native_client():
    client = broker_worker.NativeClient.__new__(broker_worker.NativeClient)
    client.trader = Trader()
    client.account_type = lambda value: value
    client.connected = False
    return client


def test_native_worker_is_python36_compatible_and_has_no_trade_write_calls():
    source = Path(broker_worker.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source, feature_version=(3, 6))
    calls = {node.func.attr for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)}
    assert not calls & {"order_stock", "order_stock_async", "cancel_order_stock", "cancel_order_stock_async", "passorder", "transfer"}


def test_native_health_does_not_expose_account_or_amount():
    result = native_client().run({"operation": "health", "query_timeout_seconds": 0.01})
    assert result["ok"] and result["details"]["asset_object_valid"]
    assert "PRIVATE_ACCOUNT" not in json.dumps(result)


def test_trade_query_timeout_preserves_order_and_position_counts():
    result = native_client().run({"operation": "reconcile", "query_timeout_seconds": 0.01})
    assert result["status"] == "partial"
    assert result["details"]["orders"] == 2
    assert result["details"]["cancelable_orders"] == 0
    assert result["details"]["positions"] == 1
    assert result["details"]["trades"] == "unknown"
    assert result["details"]["query_status"]["trades"] == "timeout"


@pytest.mark.parametrize("login,status", [(8, "capability_disabled"), (9, "capability_disabled"), (1, "permission_required")])
def test_broker_permission_states_are_not_connection_failures(login, status):
    client = native_client()
    client.trader.query_account_infos_async = lambda callback: callback([SimpleNamespace(account_id="PRIVATE_ACCOUNT", login_status=login)])
    result = client.run({"operation": "health"})
    assert result["status"] == status and not result["ok"]


def test_worker_refuses_arbitrary_operation():
    with pytest.raises(ValueError, match="unsupported"):
        native_client().run({"operation": "order_stock"})


def test_empty_or_multiple_accounts_require_human_identity_check():
    client = native_client()
    for accounts in ([], [SimpleNamespace(account_id="a", login_status=0), SimpleNamespace(account_id="b", login_status=0)]):
        client.trader.query_account_infos_async = lambda callback, accounts=accounts: callback(accounts)
        assert client.run({"operation": "health"})["status"] == "permission_required"


def test_broker_runtime_cannot_use_private_external_xtquant(tmp_path):
    qmt = QmtConfig(launcher=str(tmp_path / "XtItClient.exe"))
    probe = ProbeConfig(python_executable=str(tmp_path / "pythonw.exe"), xtquant_parent=str(tmp_path / "external"))
    supervisor = BrokerProbeSupervisor(probe, qmt)
    result = supervisor.health()
    assert result.status is ProbeStatus.UNAVAILABLE
    assert supervisor._process is None


def test_file_ipc_correlates_response_and_cleans_up():
    supervisor = BrokerProbeSupervisor(ProbeConfig(timeout_seconds=1), QmtConfig())
    supervisor._ipc = tempfile.TemporaryDirectory(prefix="qg-test-broker-")
    directory = Path(supervisor._ipc.name)
    supervisor._process = SimpleNamespace(poll=lambda: 0)

    def responder():
        deadline = time.monotonic() + 2
        while not (directory / "request.json").is_file() and time.monotonic() < deadline:
            time.sleep(0.01)
        request = json.loads((directory / "request.json").read_text(encoding="utf-8"))
        response = ProbeResponse(request["request_id"], True, "healthy", "test read-only", details={"asset_object_valid": True})
        temporary = directory / "reply.tmp"
        temporary.write_text(response.to_json(), encoding="utf-8")
        temporary.replace(directory / f"{request['request_id']}.json")

    thread = threading.Thread(target=responder)
    thread.start()
    with patch.object(supervisor, "_start_locked", return_value=(True, "already running")):
        supervisor._process = SimpleNamespace(poll=lambda: None, terminate=lambda: None, wait=lambda timeout: 0)
        result = supervisor.health()
    thread.join()
    supervisor.stop()
    assert result.status is ProbeStatus.HEALTHY
    assert not directory.exists()


@pytest.mark.parametrize("status", ["partial", "permission_required", "capability_disabled", "unavailable", "timeout"])
def test_response_mapping_preserves_failure_category(status):
    assert response_status(ProbeResponse("x", False, status, "test")) is ProbeStatus(status)


def test_native_worker_uses_only_broker_abi(tmp_path):
    binary = tmp_path / "bin.x64"
    executable = binary / "pythonw.exe"
    library = binary / "Lib" / "site-packages"
    fake_trader = SimpleNamespace(start=lambda: None, stop=lambda: None)
    modules = {
        "xtquant": SimpleNamespace(),
        "xtquant.xttrader": SimpleNamespace(XtQuantTrader=lambda path, session: fake_trader),
        "xtquant.xttype": SimpleNamespace(StockAccount=lambda value: value),
    }
    request = {"xtquant_parent": str(library), "userdata_directory": str(tmp_path / "userdata"), "session_id": 123}
    with patch.object(sys, "executable", str(executable)), patch.object(sys, "path", list(sys.path)), patch.dict(sys.modules, modules):
        client = broker_worker.NativeClient(request)
        client.close()
        with pytest.raises(RuntimeError, match="path mismatch"):
            broker_worker.NativeClient({**request, "xtquant_parent": str(tmp_path / "untrusted")})


def test_dpapi_adapter_preserves_plaintext_only_inside_worker():
    freed = []

    def decrypt(source, description, entropy, reserved, prompt, flags, target):
        target._obj.size = source._obj.size
        target._obj.data = source._obj.data
        return 1

    fake = SimpleNamespace(crypt32=SimpleNamespace(CryptUnprotectData=decrypt), kernel32=SimpleNamespace(LocalFree=lambda value: freed.append(True)))
    with patch.object(ctypes, "windll", fake, create=True):
        assert broker_worker.unprotect(base64.b64encode(b"test-account").decode()) == "test-account"
    assert freed == [True]


def test_worker_failure_response_never_contains_native_exception_text(tmp_path):
    request = {"operation": "health", "request_id": "a" * 32, "userdata_directory": "userdata", "xtquant_parent": "library", "session_id": 123}
    (tmp_path / "request.json").write_text(json.dumps(request), encoding="utf-8")
    fake = SimpleNamespace(run=lambda request: (_ for _ in ()).throw(RuntimeError("PRIVATE_ACCOUNT")), close=lambda: None)
    with patch.object(broker_worker, "NativeClient", return_value=fake):
        broker_worker.serve(str(tmp_path))
    result = json.loads((tmp_path / ("a" * 32 + ".json")).read_text(encoding="utf-8"))
    assert result["fatal"] and result["status"] == "unavailable"
    assert "PRIVATE_ACCOUNT" not in str(result)


def test_worker_persistent_request_and_shutdown(tmp_path):
    request = {"operation": "health", "request_id": "a" * 32, "userdata_directory": "userdata", "xtquant_parent": "library", "session_id": 123}
    (tmp_path / "request.json").write_text(json.dumps(request), encoding="utf-8")
    closed = []
    fake = SimpleNamespace(run=lambda request: {"ok": True, "status": "healthy", "reason": "test"}, close=lambda: closed.append(True))

    def advance(_delay):
        (tmp_path / "request.json").write_text(json.dumps({"operation": "shutdown", "request_id": "b" * 32}), encoding="utf-8")

    with patch.object(broker_worker, "NativeClient", return_value=fake), patch.object(broker_worker.time, "sleep", side_effect=advance):
        broker_worker.serve(str(tmp_path))
    assert closed == [True]
    assert json.loads((tmp_path / ("a" * 32 + ".json")).read_text(encoding="utf-8"))["ok"]


def test_complete_business_counts_and_missing_asset_are_distinct():
    client = native_client()
    client.trader.query_stock_trades_async = lambda account, callback: callback([])
    assert client.run({"operation": "reconcile"})["status"] == "reconciled"
    client.trader.query_stock_asset_async = lambda account, callback: callback(None)
    assert not client.run({"operation": "health"})["ok"]


def test_worker_query_timeout_is_bounded():
    with pytest.raises(TimeoutError):
        broker_worker.wait_query(lambda callback: None, (), 0.001)


def test_native_calendar_contains_only_dates():
    client = native_client()
    xtdata = SimpleNamespace(get_trading_dates=lambda *args: [1794484800000])
    with patch.dict(sys.modules, {"xtquant": SimpleNamespace(xtdata=xtdata)}):
        result = client.run({"operation": "calendar"})
    assert result["ok"]
    assert set(result["details"]) == {"trading_dates"}


def test_broker_supervisor_launch_preserves_broker_install(tmp_path):
    executable = tmp_path / "pythonw.exe"
    executable.touch()
    (tmp_path / "Lib" / "site-packages" / "xtquant").mkdir(parents=True)
    qmt = QmtConfig(launcher=str(tmp_path / "XtItClient.exe"))
    config = ProbeConfig(python_executable=str(executable), xtquant_parent=str(tmp_path / "Lib" / "site-packages"))
    supervisor = BrokerProbeSupervisor(config, qmt)
    fake = SimpleNamespace(poll=lambda: 0)
    with patch("quant_guardian.probe.broker_supervisor.subprocess.Popen", return_value=fake) as launch, patch.dict("os.environ", {"PYTHONPATH": "untrusted", "PYTHONHOME": "untrusted"}):
        started, _ = supervisor._start_locked()
    assert started
    assert launch.call_args.args[0][0] == str(executable)
    assert "PYTHONPATH" not in launch.call_args.kwargs["env"]
    assert "PYTHONHOME" not in launch.call_args.kwargs["env"]
    supervisor.stop()
