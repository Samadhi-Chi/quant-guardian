from __future__ import annotations

import contextlib
import io
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from quant_guardian.diagnostics.redaction import mask_identifier
from quant_guardian.probe.protocol import ProbeRequest, ProbeResponse
from quant_guardian.security.dpapi import unprotect_text

ACCEPTABLE_LOGIN_STATUSES = {0, 6}
_DLL_DIRECTORY_HANDLES: list[object] = []

LOGIN_STATUS_NAMES = {
    0: "ok",
    1: "waiting_login",
    2: "logging_in",
    3: "login_failed",
    4: "initializing",
    5: "correcting",
    6: "closed",
    7: "penetration_link_disconnected",
    8: "system_disabled",
    9: "user_disabled",
}


def configure_xtquant_import(xtquant_parent: str) -> None:
    path = Path(xtquant_parent)
    nested_package = path / "xtquant"
    if nested_package.is_dir():
        package_path = nested_package
    elif path.name.casefold() == "xtquant" and path.is_dir():
        package_path = path
    else:
        raise FileNotFoundError(f"xtquant package was not found under {path}")
    parent = package_path.parent
    parent_text = str(parent)
    if parent_text not in sys.path:
        sys.path.insert(0, parent_text)
    if os.name == "nt" and hasattr(os, "add_dll_directory"):
        handle = os.add_dll_directory(str(package_path))
        _DLL_DIRECTORY_HANDLES.append(handle)


class ReadonlyXtQuantClient:
    """Strictly read-only XtQuant adapter.

    This class intentionally exposes no order, cancel, credit, transfer, or
    account mutation methods.
    """

    def __init__(self, request: ProbeRequest) -> None:
        configure_xtquant_import(request.xtquant_parent)
        from xtquant import xtdata, xttrader, xttype  # type: ignore[import-not-found]

        self._xtdata = xtdata
        self._xttype = xttype
        self._request = request
        self._trader = xttrader.XtQuantTrader(
            request.userdata_directory,
            int(request.session_id),
        )
        self._started = False
        self._connected = False

    def close(self) -> None:
        try:
            stop = getattr(self._trader, "stop", None)
            if callable(stop):
                stop()
        finally:
            self._connected = False

    def _connect(self) -> None:
        if self._connected:
            return
        if not self._started:
            self._trader.start()
            self._started = True
        result = self._trader.connect()
        if result != 0:
            raise ConnectionError(f"XtQuant connect returned {result}")
        self._connected = True

    @staticmethod
    def _account_id(info: Any) -> str:
        return str(
            getattr(info, "account_id", "")
            or getattr(info, "accountid", "")
            or getattr(info, "m_strAccountID", "")
        )

    @staticmethod
    def _login_status(info: Any) -> int | None:
        raw = getattr(info, "login_status", None)
        if raw is None:
            raw = getattr(info, "status", None)
        try:
            return int(raw) if raw is not None else None
        except (TypeError, ValueError):
            return None

    def _account_infos(self) -> list[Any]:
        query = getattr(self._trader, "query_account_infos", None)
        if callable(query):
            result = query()
            return list(result or [])
        query_status = getattr(self._trader, "query_account_status", None)
        if callable(query_status):
            result = query_status()
            return list(result or [])
        raise RuntimeError("this XtQuant build has no supported account-status query")

    def _select_account(self, infos: list[Any]) -> tuple[str, int | None]:
        expected = (
            unprotect_text(self._request.account_id_protected)
            if self._request.account_id_protected
            else ""
        )
        candidates = [
            (self._account_id(info), self._login_status(info))
            for info in infos
            if self._account_id(info)
        ]
        if expected:
            for account_id, login_status in candidates:
                if account_id == expected:
                    return account_id, login_status
            raise RuntimeError("configured account was not returned by QMT")
        if len(candidates) != 1:
            raise RuntimeError(
                "QMT must expose exactly one account when no protected account is configured"
            )
        return candidates[0]

    def _stock_account(self, account_id: str) -> Any:
        return self._xttype.StockAccount(account_id)

    def health(self, request_id: str) -> ProbeResponse:
        started = time.perf_counter()
        try:
            self._connect()
            infos = self._account_infos()
            account_id, login_status = self._select_account(infos)
            account_status = LOGIN_STATUS_NAMES.get(login_status, f"status_{login_status}")
            if login_status not in ACCEPTABLE_LOGIN_STATUSES:
                return ProbeResponse(
                    request_id=request_id,
                    ok=False,
                    status="failed",
                    reason="QMT account login status is not healthy",
                    latency_ms=int((time.perf_counter() - started) * 1000),
                    account_ref=mask_identifier(account_id),
                    account_status=account_status,
                )
            asset = self._trader.query_stock_asset(self._stock_account(account_id))
            if asset is None:
                return ProbeResponse(
                    request_id=request_id,
                    ok=False,
                    status="failed",
                    reason="read-only asset query returned no object",
                    latency_ms=int((time.perf_counter() - started) * 1000),
                    account_ref=mask_identifier(account_id),
                    account_status=account_status,
                )
            return ProbeResponse(
                request_id=request_id,
                ok=True,
                status="healthy",
                reason="account status and read-only asset query succeeded",
                latency_ms=int((time.perf_counter() - started) * 1000),
                account_ref=mask_identifier(account_id),
                account_status=account_status,
                details={"asset_object_valid": True},
            )
        except Exception as exc:  # isolated worker converts native/API errors to data
            self._connected = False
            return ProbeResponse(
                request_id=request_id,
                ok=False,
                status="failed",
                reason=f"read-only probe failed: {type(exc).__name__}: {exc}",
                latency_ms=int((time.perf_counter() - started) * 1000),
                fatal=True,
            )

    @staticmethod
    def _count_or_unknown(value: Any) -> int | str:
        if value is None:
            return "unknown"
        try:
            return len(value)
        except TypeError:
            return "unknown"

    def reconcile(self, request_id: str) -> ProbeResponse:
        started = time.perf_counter()
        try:
            self._connect()
            infos = self._account_infos()
            account_id, login_status = self._select_account(infos)
            account = self._stock_account(account_id)
            orders = self._trader.query_stock_orders(account, False)
            cancelable_orders = self._trader.query_stock_orders(account, True)
            trades = self._trader.query_stock_trades(account)
            positions = self._trader.query_stock_positions(account)
            counts = {
                "orders": self._count_or_unknown(orders),
                "cancelable_orders": self._count_or_unknown(cancelable_orders),
                "trades": self._count_or_unknown(trades),
                "positions": self._count_or_unknown(positions),
            }
            complete = all(isinstance(value, int) for value in counts.values())
            return ProbeResponse(
                request_id=request_id,
                ok=complete,
                status="reconciled" if complete else "unknown",
                reason=(
                    "read-only order, trade, and position counts were returned"
                    if complete
                    else "one or more reconciliation queries returned an ambiguous result"
                ),
                latency_ms=int((time.perf_counter() - started) * 1000),
                account_ref=mask_identifier(account_id),
                account_status=LOGIN_STATUS_NAMES.get(
                    login_status, f"status_{login_status}"
                ),
                details=counts,
            )
        except Exception as exc:
            self._connected = False
            return ProbeResponse(
                request_id=request_id,
                ok=False,
                status="failed",
                reason=f"read-only reconciliation failed: {type(exc).__name__}: {exc}",
                latency_ms=int((time.perf_counter() - started) * 1000),
                fatal=True,
            )

    def calendar(self, request_id: str) -> ProbeResponse:
        started = time.perf_counter()
        try:
            start_date = self._request.start_date or f"{datetime.now():%Y}0101"
            end_date = self._request.end_date or f"{datetime.now():%Y%m%d}"
            # Some XtData builds print a connection banner to stdout. stdout is
            # the worker's JSON protocol, so capture the banner locally.
            with contextlib.redirect_stdout(io.StringIO()):
                raw_dates = self._xtdata.get_trading_dates(
                    self._request.market or "SH",
                    start_date,
                    end_date,
                    -1,
                )
            values = sorted(
                {
                    datetime.fromtimestamp(int(value) / 1000).astimezone().date().isoformat()
                    for value in (raw_dates or [])
                }
            )
            return ProbeResponse(
                request_id=request_id,
                ok=bool(values),
                status="calendar" if values else "unavailable",
                reason=(
                    f"read-only {self._request.market or 'SH'} trading calendar returned "
                    f"{len(values)} dates"
                ),
                latency_ms=int((time.perf_counter() - started) * 1000),
                details={
                    "market": self._request.market or "SH",
                    "trading_dates": values,
                    "coverage_end": end_date,
                },
            )
        except Exception as exc:
            return ProbeResponse(
                request_id=request_id,
                ok=False,
                status="failed",
                reason=f"read-only calendar query failed: {type(exc).__name__}: {exc}",
                latency_ms=int((time.perf_counter() - started) * 1000),
            )
