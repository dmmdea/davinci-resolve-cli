"""check_readonly -- static gate: no mutating Resolve API calls in read paths.

The read-safety promise of resolve_cli.py ("--yes guards every mutation, and
reads never switch the editor's GUI state") is enforced here, not just stated.
deploy.ps1 runs this before every deploy; exit 1 blocks the deploy.

Mechanism (DEFAULT-DENY): Resolve API methods are PascalCase. Inside any
function NOT whitelisted as a write command, every PascalCase method call must
match the read-safe pattern (Get*/Is*/Has*) or be explicitly allowlisted --
anything else is a violation. An unknown/new API verb therefore FAILS CLOSED
instead of slipping through a closed verb list. getattr(obj, "Name") with a
PascalCase literal is checked the same way.

The write-command whitelist is DERIVED from resolve_cli.py's own
WRITE_COMMANDS dict (parsed from its AST, no import side effects) -- the two
files cannot drift silently; if the dict is missing this gate fails loudly.

Boundary: this is a guardrail over our own reviewed code, not a sandbox --
method aliasing through intermediate variables is out of scope (review catches
that); getattr with a non-literal name is flagged as a violation outright.
Scanned files: the bridge's python modules listed in SCAN_FILES; extend the
list when the bridge grows.
"""

import ast
import re
import sys
import os

HERE = os.path.dirname(os.path.abspath(__file__))
SCAN_FILES = ("resolve_cli.py", "connect.py", "_probe.py", "serve_http.py", "ops_common.py",
              "ops_project.py", "ops_media.py", "ops_timeline.py", "ops_color.py", "ops_render.py")

# ANY capitalized method call is suspect by default (single-word mutators like
# Quit/Stabilize must not slip a two-hump pattern).
PASCAL = re.compile(r"^[A-Z]")
READ_OK = re.compile(r"^(Get|Is|Has)[A-Z_]")

# Read-safe capitalized names the READ_OK pattern misses. Every entry needs a
# one-line justification.
ALLOW = {
    "Thread",          # threading.Thread -- stdlib, not a Resolve API object
    "ArgumentParser",  # argparse.ArgumentParser -- stdlib
    "Lock",            # threading.Lock -- stdlib; serializes sidecar requests
    "Namespace",       # argparse.Namespace -- stdlib; sidecar builds handler args
    "ThreadingHTTPServer",  # http.server -- stdlib; the sidecar's listener
    "Fusion",          # resolve.Fusion() -- accessor for the Fusion app object (a read)
    "FindTool",        # comp.FindTool(name) -- lookup, returns a tool handle (a read)
    "FindToolByID",    # comp.FindToolByID(regid) -- lookup (a read)
    "ValidateDCTL",    # resolve.ValidateDCTL(source) -- compiles text, writes nothing (21.1)
}


def write_funcs_from_resolve_cli():
    """Derive the set of functions allowed to call mutating APIs, straight from
    resolve_cli.py's own AST -- the two files cannot drift silently:
      - WRITE_COMMANDS values: the --yes-guarded command handlers.
      - WRITE_HELPERS elements: private write-path helpers those handlers call
        (e.g. the render mechanics). Explicit allowlist, not call-graph analysis:
        adding a mutating helper means naming it here on purpose.
    WRITE_COMMANDS is required; WRITE_HELPERS is optional (empty on read-only
    builds)."""
    path = os.path.join(HERE, "resolve_cli.py")
    with open(path, "r", encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), filename=path)
    commands = None
    helpers = set()
    # Module-level statements ONLY: a same-named local inside some function must
    # never be able to widen (or shadow) the allowlist.
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if not isinstance(target, ast.Name):
                continue
            if target.id == "WRITE_COMMANDS" and isinstance(node.value, ast.Dict):
                commands = {v.value for v in node.value.values
                            if isinstance(v, ast.Constant) and isinstance(v.value, str)}
            elif target.id == "WRITE_HELPERS" and isinstance(node.value, (ast.Set, ast.List, ast.Tuple)):
                helpers = {e.value for e in node.value.elts
                           if isinstance(e, ast.Constant) and isinstance(e.value, str)}
    if not commands:
        sys.exit("check_readonly: FATAL - could not parse WRITE_COMMANDS from resolve_cli.py; "
                 "the write whitelist is derived from it and must exist.")
    return commands | helpers


def violations(path, write_funcs):
    with open(path, "r", encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), filename=path)
    bad = []

    class Walker(ast.NodeVisitor):
        def __init__(self):
            self.stack = ["(module)"]

        def visit_FunctionDef(self, node):
            self.stack.append(node.name)
            self.generic_visit(node)
            self.stack.pop()

        visit_AsyncFunctionDef = visit_FunctionDef

        def _in_write_func(self):
            # The whitelist names TOP-LEVEL functions; a closure defined inside
            # a whitelisted function is part of that write path (the Fusion
            # Lock/Undo wrapper takes one), while a nested function that merely
            # REUSES a whitelisted name inside a read function does not count.
            return len(self.stack) >= 2 and self.stack[1] in write_funcs

        def _check(self, name, lineno):
            if (PASCAL.match(name) and not READ_OK.match(name)
                    and name not in ALLOW and not self._in_write_func()):
                bad.append((path, lineno, ".".join(self.stack[1:]) or "(module)", name))

        def visit_Call(self, node):
            if isinstance(node.func, ast.Attribute):
                self._check(node.func.attr, node.lineno)
            elif isinstance(node.func, ast.Name) and node.func.id == "getattr":
                if (len(node.args) >= 2 and isinstance(node.args[1], ast.Constant)
                        and isinstance(node.args[1].value, str)):
                    self._check(node.args[1].value, node.lineno)
                elif (node.args and isinstance(node.args[0], ast.Name)
                        and node.args[0].id == "args"):
                    pass  # getattr(args, field): the argparse namespace, never a Resolve object
                else:
                    bad.append((path, node.lineno, ".".join(self.stack[1:]) or "(module)",
                                "getattr(<dynamic name>)"))
            self.generic_visit(node)

    Walker().visit(tree)
    return bad


def main():
    write_funcs = write_funcs_from_resolve_cli()
    all_bad = []
    for fname in SCAN_FILES:
        all_bad.extend(violations(os.path.join(HERE, fname), write_funcs))
    if all_bad:
        print("READ-SAFETY VIOLATIONS (non-read PascalCase call outside a write command):")
        for path, line, func, name in all_bad:
            print("  %s:%d in %s(): %s" % (os.path.basename(path), line, func, name))
        sys.exit(1)
    print("check_readonly: clean (write whitelist derived from WRITE_COMMANDS: %s)"
          % ", ".join(sorted(write_funcs)))


if __name__ == "__main__":
    main()
