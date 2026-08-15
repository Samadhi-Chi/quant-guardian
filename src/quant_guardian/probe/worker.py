from __future__ import annotations

import argparse
import sys
from typing import TextIO

from quant_guardian.probe.protocol import ProbeRequest, ProbeResponse
from quant_guardian.probe.readonly_xtquant import ReadonlyXtQuantClient


def serve(input_stream: TextIO, output_stream: TextIO) -> int:
    client: ReadonlyXtQuantClient | None = None
    client_key: tuple[str, str, int, str] | None = None
    for raw_line in input_stream:
        line = raw_line.strip()
        if not line:
            continue
        try:
            request = ProbeRequest.from_json(line)
            if request.operation == "shutdown":
                response = ProbeResponse(
                    request_id=request.request_id,
                    ok=True,
                    status="stopped",
                    reason="probe shutdown requested",
                )
                output_stream.write(response.to_json() + "\n")
                output_stream.flush()
                break
            key = (
                request.userdata_directory,
                request.xtquant_parent,
                request.session_id,
                request.account_id_protected,
            )
            if client is None or key != client_key:
                if client is not None:
                    client.close()
                client = ReadonlyXtQuantClient(request)
                client_key = key
            if request.operation == "health":
                response = client.health(request.request_id)
            elif request.operation == "calendar":
                response = client.calendar(request.request_id)
            else:
                response = client.reconcile(request.request_id)
        except Exception as exc:
            response = ProbeResponse(
                request_id=(request.request_id if "request" in locals() else "unknown"),
                ok=False,
                status="failed",
                reason=f"probe worker error: {type(exc).__name__}: {exc}",
                fatal=True,
            )
        output_stream.write(response.to_json() + "\n")
        output_stream.flush()
        if response.fatal:
            break
    if client is not None:
        client.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Quant Guardian read-only probe worker")
    parser.parse_args(argv)
    return serve(sys.stdin, sys.stdout)


if __name__ == "__main__":
    raise SystemExit(main())
