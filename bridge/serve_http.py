"""serve_http -- loopback HTTP sidecar in front of resolve_cli's commands.

Why this exists: Resolve's automation surface is a local scripting bridge, not
an HTTP API. Tools that speak REST (a generated CLI, an MCP server, any agent
runtime) cannot talk to it directly. This module puts a thin, loopback-only
HTTP face on the SAME command functions resolve_cli already ships, so nothing
about the proven connect/watchdog/page-null behaviour is reimplemented here.

    python resolve_cli.py serve [--port 18800]

Surface:
    GET  /healthz            server state only -- never calls Resolve, so it
                             cannot hang and is safe as a liveness probe.
    GET  /v1/commands        self-describing catalog: every command, whether it
                             writes, and the route to reach it.
    GET  /v1/<read-command>  query-string args. Refused for write commands.
    POST /v1/<command>       JSON-body args. Writes additionally require
                             {"yes": true}, mirroring the CLI's --yes guard.

Responses are {"ok": bool, "data": {...}, "lines": [...], "exit_code": int}.
`data` is what `<command> --json` prints, because it IS that value -- captured
through resolve_cli's output sink rather than re-derived here.

THREE properties this server must preserve, all inherited from the CLI:

1. SERIALIZED. Every Resolve API call is blocking IPC into a single-threaded
   host. Concurrent requests would interleave calls on one connection, so a
   single lock funnels every command; this is a sidecar, not a scale-out tier.

2. WATCHDOGGED. A GUI modal in Resolve stalls the API forever and the hung IPC
   call cannot be cancelled, only abandoned. Each request runs in a worker
   thread with a join timeout, exactly like the CLI.

3. FATAL AFTER A STALL. An abandoned worker has not stopped -- it is parked
   inside a live Resolve call and will COMPLETE its mutation when the modal
   clears. So a trip answers 503 with the reason and then ends the process,
   which is what the CLI's os._exit buys and the only way to stop that write
   from landing later, unobserved, while the operator is still recovering.
   Requests already queued on the lock re-check the latch and get 503 rather
   than entering the wedged connection. Start the sidecar again to recover.

Loopback only, by construction. These endpoints create, delete and render
projects; there is deliberately no flag to widen the bind. Reach it from another
machine the way the rest of this ecosystem does: an SSH local-forward.
"""

import argparse
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import resolve_cli
from connect import get_resolve

DEFAULT_PORT = 18800

# Floor under the per-request watchdog budget. A trip is not a cheap event: it
# means the connection is wedged, so this process stops (see _stop_after_trip).
# Without a floor, `?timeout=0` makes join() return before any command could
# possibly finish, so ANY caller -- on a plain GET, no write guard involved --
# could report a stall that never happened and take the sidecar down for
# everyone. The floor keeps a trip meaning what it claims to mean.
MIN_REQUEST_TIMEOUT_S = 5

# Booleans arrive from a query string as text; accept the obvious spellings and
# reject everything else loudly rather than silently reading "false" as true.
_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


def _coerce_bool(value, field):
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    raise ValueError("%s must be a boolean (got %r)" % (field, value))


def _coerce_int(value, field):
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ValueError("%s must be an integer (got %r)" % (field, value))


def _coerce_text(value, field):
    """Everything the CLI reads off argv is a str; keep that contract.

    A JSON caller may reasonably send `{"fps": 29.97}`, so numbers are
    stringified, and a bool becomes "true"/"false" (the spelling a tri-state
    text option accepts). Anything structural is refused here with a 400
    rather than reaching a handler and failing as a confusing exit-4
    TypeError deep inside a Resolve call.
    """
    if value is None or isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if not isinstance(value, (int, float)):
        raise ValueError("%s must be a string (got %r)" % (field, value))
    return str(value)


def _coerce_float(value, field):
    if isinstance(value, bool):
        raise ValueError("%s must be a number (got %r)" % (field, value))
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ValueError("%s must be a number (got %r)" % (field, value))


def _coerce_json(value, field):
    """A `json` kind field: structured on the wire, or a JSON string (the
    CLI spelling). @file references are NOT honoured over HTTP -- the caller
    is not on this machine's filesystem by contract."""
    if isinstance(value, str):
        if value.strip().startswith("@"):
            raise ValueError("%s: @file references are not accepted over HTTP; send the JSON value" % field)
        try:
            return json.loads(value)
        except ValueError as exc:
            raise ValueError("%s must be JSON (got %r): %s" % (field, value[:80], exc))
    return value


_COERCE = {"bool": _coerce_bool, "int": _coerce_int, "float": _coerce_float,
           "text": _coerce_text, "json": _coerce_json}


def _build_args(command, supplied):
    """Namespace for a command handler: CLI defaults overlaid with the request.

    Legacy commands take their defaults from resolve_cli.ARG_DEFAULTS -- the
    same dict main() feeds argparse; commands declared in ops_common.SPECS
    take the spec's own per-command defaults and coerce each field by its
    declared kind -- so an HTTP call and a CLI call of the same command cannot
    drift apart in what an unset flag means or what type it carries.
    """
    spec = resolve_cli.spec_for(command)
    values = {"json": True, "yes": False, "timeout": resolve_cli.DEFAULT_TIMEOUT_S}
    if spec is None:
        values.update(resolve_cli.ARG_DEFAULTS)
    else:
        values.update(resolve_cli.spec_defaults(spec))
    values["json"] = True  # the sink captures structured data either way
    values["command"] = command
    kinds = {resolve_cli.field_name(a["name"]): a["kind"] for a in spec["args"]} if spec else {}

    for key, raw in supplied.items():
        field = key.replace("-", "_")
        if field == "yes":
            values[field] = _coerce_bool(raw, key)
        elif field == "timeout":
            values[field] = _coerce_int(raw, key)
        elif spec is not None:
            kind = kinds.get(field)
            if kind is None:
                raise ValueError("%r is not an argument of %s (accepts: %s)"
                                 % (key, command, ", ".join(a["name"] for a in spec["args"]) or "none"))
            if kind == "list":
                if isinstance(raw, str) and raw.strip().startswith("["):
                    raw = _coerce_json(raw, key)  # a JSON array in a query string
                values[field] = [str(v) for v in raw] if isinstance(raw, (list, tuple)) else [str(raw)]
            else:
                values[field] = _COERCE[kind](raw, key)
        elif field in ("audio", "recursive", "attributes"):
            values[field] = _coerce_bool(raw, key)
        elif field in ("paths", "lut"):
            values[field] = list(raw) if isinstance(raw, (list, tuple)) else [raw]
        else:
            values[field] = _coerce_text(raw, key)

    if values["timeout"] < MIN_REQUEST_TIMEOUT_S:
        raise ValueError(
            "timeout must be at least %ds (got %r). A shorter budget reports a "
            "stall that never happened, and a watchdog trip stops this sidecar."
            % (MIN_REQUEST_TIMEOUT_S, values["timeout"]))

    return argparse.Namespace(**values)


def _run_command(command, args, resolve, state):
    """Run one command in a worker thread under the CLI's watchdog contract.

    Returns (http_status, payload). The worker writes into a box it owns and
    resolve_cli's output sink is thread-local, so a worker abandoned by the
    watchdog can never scribble into a later request's response.
    """
    box = {}
    outcome = {}

    def work():
        resolve_cli.set_output_sink(box)
        try:
            resolve_cli.COMMANDS[command](args, resolve)
            outcome["code"] = 0
            # Context stamp for callers that keep history (the printed CLI's
            # local log): which project/timeline the command acted on. Two
            # cheap reads, inside the worker so the watchdog still covers them.
            try:
                project = resolve.GetProjectManager().GetCurrentProject()
                timeline = project.GetCurrentTimeline() if project else None
                box["context"] = {"project": project.GetName() if project else None,
                                  "timeline": timeline.GetName() if timeline else None,
                                  "page": resolve.GetCurrentPage()}
            except Exception:  # noqa: BLE001 - context is best-effort, never the result
                box["context"] = None
        except SystemExit as exc:
            # Handlers report state/usage errors via sys.exit("ERROR: ...").
            if isinstance(exc.code, str):
                outcome["message"] = exc.code
                outcome["code"] = 1
            else:
                outcome["code"] = exc.code if exc.code is not None else 0
        except Exception as exc:  # noqa: BLE001 - unguarded None deref etc.
            outcome["message"] = ("ERROR: %s: %s (unexpected failure while running "
                                  "%r -- a Resolve API call likely returned an "
                                  "unguarded None)" % (type(exc).__name__, exc, command))
            outcome["code"] = 4

    worker = threading.Thread(target=work, daemon=True)
    worker.start()
    worker.join(args.timeout)

    if worker.is_alive():
        state["degraded"] = (
            "a %r call was still blocked after %ds and had to be abandoned. A modal "
            "dialog in the Resolve GUI stalls the scripting API and the hung call "
            "cannot be cancelled, so every later call would hang the same way. "
            "This sidecar is stopping; clear the dialog in Resolve and start it again."
            % (command, args.timeout))
        # The abandoned worker is still inside a live Resolve call on the shared
        # connection. It is not finished -- it is waiting, and it will COMPLETE
        # its mutation the moment the modal clears. Answering 503 and staying up
        # would leave a create/render/build to land later, unobserved, while the
        # operator is recovering. The caller is told first, then the process
        # goes (see _stop_after_trip); that is what the CLI's os._exit bought.
        state["fatal"] = True
        return 503, {"ok": False, "exit_code": resolve_cli.EXIT_TIMEOUT,
                     "error": state["degraded"]}

    code = outcome.get("code", 1)
    if code != 0:
        status = 400 if code in (1, 2) else 500
        payload = {"ok": False, "exit_code": code,
                   "error": outcome.get("message", "command failed")}
        # A handler that reported a structured result and THEN exited non-zero
        # (validate-dctl on bad source, a partial settings apply) has said
        # something worth more than "command failed": pass it through.
        if "data" in box:
            payload["data"] = box["data"]
            payload["lines"] = box.get("lines", [])
            if payload["error"] == "command failed" and payload["lines"]:
                payload["error"] = payload["lines"][0]
        return status, payload

    return 200, {"ok": True, "exit_code": 0,
                 "data": box.get("data", {}), "lines": box.get("lines", []),
                 "context": box.get("context")}


def _catalog():
    """Self-describing route table. Spec'd commands carry their argument
    contract (name, kind, required, choices, help) so a generated client can
    be built from this endpoint alone."""
    writes = set(resolve_cli.WRITE_COMMANDS)
    rows = []
    for name in sorted(resolve_cli.COMMANDS):
        if name == "serve":
            continue
        row = {"name": name, "writes": name in writes,
               "method": "POST" if name in writes else "GET", "path": "/v1/%s" % name}
        spec = resolve_cli.spec_for(name)
        if spec is not None:
            row["help"] = spec["help"]
            row["args"] = [{"name": a["name"], "kind": a["kind"], "required": a["required"],
                            "choices": a["choices"], "default": a["default"], "help": a["help"]}
                           for a in spec["args"]]
        rows.append(row)
    return {
        "commands": rows,
        "count": len(rows),
        "note": ("write commands require {\"yes\": true} in a JSON body, mirroring "
                 "the CLI's --yes guard; `json` kind arguments are sent as JSON values"),
    }


class _Handler(BaseHTTPRequestHandler):
    server_version = "resolve-bridge-sidecar"

    # Bound by run_server before serve_forever.
    state = None
    lock = None
    resolve = None

    def log_message(self, fmt, *fmt_args):  # one tidy line per request on stderr
        sys.stderr.write("[sidecar] %s - %s\n" % (self.address_string(), fmt % fmt_args))

    def _stop_after_trip(self):
        """Answer sent; now stop the process so the abandoned call cannot land.

        os._exit, not sys.exit: the whole point is to go without waiting on
        anything, and the stalled worker is exactly the kind of thing a clean
        shutdown would wait for. This mirrors the CLI's watchdog, which exits
        for the same reason -- the difference is only that the caller has
        already been told why.
        """
        sys.stderr.write("[sidecar] watchdog tripped -- exiting so the abandoned "
                         "call cannot complete unobserved. Clear the dialog in "
                         "Resolve, then start the sidecar again.\n")
        sys.stderr.flush()
        try:
            self.wfile.flush()
        except OSError:
            pass  # caller already hung up; the exit still has to happen
        os._exit(resolve_cli.EXIT_TIMEOUT)

    def _reply(self, status, payload):
        body = json.dumps(payload, indent=2, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _dispatch(self, command, supplied, is_post):
        if command not in resolve_cli.COMMANDS or command == "serve":
            self._reply(404, {"ok": False, "error": "unknown command %r" % command})
            return
        if self.state.get("degraded"):
            self._reply(503, {"ok": False, "exit_code": resolve_cli.EXIT_TIMEOUT,
                              "error": self.state["degraded"]})
            return

        writes = command in resolve_cli.WRITE_COMMANDS
        if writes and not is_post:
            self._reply(405, {"ok": False, "error":
                              "%r modifies the project: POST it with {\"yes\": true}" % command})
            return

        try:
            args = _build_args(command, supplied)
        except ValueError as exc:
            self._reply(400, {"ok": False, "exit_code": 2, "error": str(exc)})
            return

        if writes and not getattr(args, "yes", False):
            self._reply(400, {"ok": False, "exit_code": 2, "error":
                              "refusing to run %r without {\"yes\": true} "
                              "(this modifies the project)" % command})
            return

        # One command at a time: the bridge underneath is a single blocking
        # connection, so requests queue here rather than interleaving there.
        with self.lock:
            # Re-check UNDER the lock. The check above ran before queuing, so a
            # request that was already waiting here when an earlier one tripped
            # the watchdog would otherwise sail into the now-wedged connection
            # and burn its own full timeout -- rebuilding the exact queue of
            # serial hangs the latch exists to prevent.
            if self.state.get("degraded"):
                status, payload = 503, {"ok": False,
                                        "exit_code": resolve_cli.EXIT_TIMEOUT,
                                        "error": self.state["degraded"]}
            else:
                status, payload = _run_command(command, args, self.resolve, self.state)
        self._reply(status, payload)
        if self.state.get("fatal"):
            self._stop_after_trip()

    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler's required name
        parsed = urlparse(self.path)
        route = parsed.path.rstrip("/")
        if route in ("/healthz", ""):
            self._reply(200, {"ok": not self.state.get("degraded"),
                              "degraded": self.state.get("degraded"),
                              "port": self.state.get("port"),
                              "cli": resolve_cli.version_string()})
            return
        if route == "/v1/commands":
            self._reply(200, _catalog())
            return
        if not route.startswith("/v1/"):
            self._reply(404, {"ok": False, "error": "no route %r" % self.path})
            return
        supplied = {k: (v[0] if len(v) == 1 else v)
                    for k, v in parse_qs(parsed.query).items()}
        self._dispatch(route[len("/v1/"):], supplied, is_post=False)

    def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler's required name
        route = urlparse(self.path).path.rstrip("/")
        if not route.startswith("/v1/"):
            self._reply(404, {"ok": False, "error": "no route %r" % self.path})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            supplied = json.loads(self.rfile.read(length) or b"{}") if length else {}
        except (ValueError, TypeError) as exc:
            self._reply(400, {"ok": False, "exit_code": 2,
                              "error": "request body is not valid JSON: %s" % exc})
            return
        if not isinstance(supplied, dict):
            self._reply(400, {"ok": False, "exit_code": 2,
                              "error": "request body must be a JSON object"})
            return
        self._dispatch(route[len("/v1/"):], supplied, is_post=True)


def run_server(args):
    """Warm one connection, then serve until interrupted."""
    port = getattr(args, "port", DEFAULT_PORT) or DEFAULT_PORT

    try:
        resolve = get_resolve()
    except RuntimeError as exc:
        sys.exit("ERROR: %s" % exc)

    state = {"degraded": None, "port": port}
    _Handler.state = state
    _Handler.lock = threading.Lock()
    _Handler.resolve = resolve

    httpd = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    sys.stderr.write("[sidecar] %s %s\n"
                     % (resolve.GetProductName(), resolve.GetVersionString()))
    sys.stderr.write("[sidecar] listening on http://127.0.0.1:%d (loopback only)\n" % port)
    sys.stderr.write("[sidecar] catalog: GET /v1/commands   liveness: GET /healthz\n")
    sys.stderr.flush()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        sys.stderr.write("[sidecar] shutting down\n")
    finally:
        httpd.server_close()
    return 0
