#!/usr/bin/env python3
"""
Tiny ES|QL terminal.

Interactive mode:

  ./test.py

  esql> FROM ul_logs
     > | WHERE id IN (63, 70, 82)
     > | LIMIT 3;

Pipe/file mode:

  ./test.py < query.esql
  ./test.py query.esql

Environment:
  ES_URL              Base URL (default http://127.0.0.1:9200)
  ES_USER / ES_PASSWORD   Basic auth (defaults elastic / password)
  ES_API_KEY          If set, sends Authorization: ApiKey <base64(id:key)> instead of basic
  ES_FORMAT           Output format: psql, json, txt, csv, yaml, ... (default psql)
  ES_INSECURE=1       Skip TLS certificate verification (development only)
  ES_TIMING=1         Print elapsed time after each query (toggle with \\timing)
"""

from __future__ import annotations

import atexit
import argparse
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from base64 import b64encode


class ESQLClient:
    def __init__(self, base: str, fmt: str, timeout: float, insecure: bool, timing: bool = False):
        self.base = base.rstrip("/")
        self.fmt = fmt
        self.timeout = timeout
        self.insecure = insecure
        self.timing = timing
        request_format = "json" if fmt.lower() == "psql" else fmt
        self.url = f"{self.base}/_query?{urllib.parse.urlencode({'format': request_format})}"
        self.context = None
        if self.url.lower().startswith("https://") and insecure:
            self.context = ssl.create_default_context()
            self.context.check_hostname = False
            self.context.verify_mode = ssl.CERT_NONE
        self.last_statement: str = ""
        self.variables: dict[str, str] = {}
        self.output_path: str | None = None

    def execute(self, query: str) -> int:
        query = apply_variables(query, self.variables)
        self.last_statement = query
        body = json.dumps({"query": query}).encode("utf-8")
        req = urllib.request.Request(self.url, data=body, method="POST")

        for name, value in build_auth_headers():
            req.add_header(name, value)

        req.add_header("Content-Type", "application/json; charset=utf-8")
        if self.fmt.lower() in ("json", "psql"):
            req.add_header("Accept", "application/json")

        t0 = time.perf_counter()
        try:
            try:
                with urllib.request.urlopen(req, timeout=self.timeout, context=self.context) as resp:
                    raw = resp.read().decode("utf-8")
            except urllib.error.HTTPError as e:
                err_body = e.read().decode("utf-8", errors="replace")
                if err_body:
                    print_error(err_body, e.code, self.fmt, query=query)
                else:
                    sys.stderr.write(f"ERROR: HTTP {e.code} {e.reason}\n")
                return e.code
            except urllib.error.URLError as e:
                sys.stderr.write(
                    f"{e}\n\n"
                    "Hints:\n"
                    "  - For https with self-signed certs: ES_INSECURE=1 or --insecure\n"
                    "  - Check ES_URL (include https:// if TLS)\n"
                    "  - Ensure the cluster is running and credentials are correct\n"
                )
                return 1

            print_response(raw, self.fmt)
            return 0
        finally:
            if self.timing:
                elapsed_ms = (time.perf_counter() - t0) * 1000.0
                sys.stderr.write(f"Time: {elapsed_ms:.3f} ms\n")


def read_query_file(path: str) -> str:
    with open(path, encoding="utf-8") as handle:
        return handle.read().lstrip("\ufeff").strip()


def build_auth_headers() -> list[tuple[str, str]]:
    api_key = os.environ.get("ES_API_KEY")
    if api_key:
        # Accept either the raw "id:secret" form or an already-base64-encoded key.
        if ":" in api_key and api_key.strip().isascii():
            token = b64encode(api_key.encode("ascii")).decode("ascii")
        else:
            token = api_key.strip()
        return [("Authorization", f"ApiKey {token}")]

    user = os.environ.get("ES_USER", "elastic")
    password = os.environ.get("ES_PASSWORD", "password")
    basic = b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")
    return [("Authorization", f"Basic {basic}")]


def print_response(raw: str, fmt: str) -> None:
    if fmt.lower() == "psql":
        try:
            print_psql_table(json.loads(raw))
        except json.JSONDecodeError:
            sys.stderr.write("Warning: response was not valid JSON; printing raw body.\n")
            print(raw)
    elif fmt.lower() == "json":
        try:
            print(json.dumps(json.loads(raw), indent=2, sort_keys=False))
        except json.JSONDecodeError:
            sys.stderr.write("Warning: response was not valid JSON; printing raw body.\n")
            print(raw)
    else:
        print(raw.rstrip())


def format_cell(value: object) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, dict)):
        return json.dumps(value, separators=(",", ":"))
    return str(value)


_LINE_COL_RE = re.compile(r"line\s+(\d+):(\d+):\s*(.+)")


def extract_line_col_errors(reason: str) -> list[tuple[int, int, str]]:
    """Parse 'line N:M: message' occurrences from an ES error reason."""
    if not reason:
        return []
    out: list[tuple[int, int, str]] = []
    for piece in reason.splitlines():
        match = _LINE_COL_RE.search(piece)
        if not match:
            continue
        try:
            line = int(match.group(1))
            col = int(match.group(2))
        except ValueError:
            continue
        out.append((line, col, match.group(3).strip()))
    return out


def render_query_pointer(query: str, line: int, col: int, max_width: int = 100) -> str:
    """Return a two-line rendering of the source line with a '^' caret under col.

    line/col are 1-based as returned by Elasticsearch. Long lines are truncated
    around the caret with ellipses so the indicator stays visible.
    """
    if not query or line < 1:
        return ""
    lines = query.splitlines() or [""]
    if line > len(lines):
        return ""
    src = lines[line - 1]
    indicator = max(0, col - 1)

    if len(src) > max_width:
        half = max_width // 2
        window_start = max(0, indicator - half)
        window_end = min(len(src), window_start + max_width)
        window_start = max(0, window_end - max_width)
        prefix = "…" if window_start > 0 else ""
        suffix = "…" if window_end < len(src) else ""
        shown = prefix + src[window_start:window_end] + suffix
        caret_pos = len(prefix) + (indicator - window_start)
    else:
        shown = src
        caret_pos = indicator

    caret_pos = max(0, min(caret_pos, len(shown)))
    pointer = " " * caret_pos + "^"
    return f"  {shown}\n  {pointer}"


def print_error(raw: str, status_code: int, fmt: str, query: str = "") -> None:
    fmt_lc = fmt.lower()

    if fmt_lc not in ("psql", "json"):
        sys.stderr.write(raw if raw.endswith("\n") else raw + "\n")
        return

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        sys.stderr.write(raw if raw.endswith("\n") else raw + "\n")
        return

    if fmt_lc == "json":
        sys.stderr.write(json.dumps(payload, indent=2, sort_keys=False) + "\n")
        return

    error = payload.get("error") if isinstance(payload, dict) else None
    if not isinstance(error, dict):
        sys.stderr.write(raw if raw.endswith("\n") else raw + "\n")
        return

    primary_type = error.get("type")
    primary_reason = error.get("reason")
    if primary_type and primary_reason:
        sys.stderr.write(f"ERROR: {primary_type}: {primary_reason}\n")
    elif primary_reason:
        sys.stderr.write(f"ERROR: {primary_reason}\n")
    elif primary_type:
        sys.stderr.write(f"ERROR: {primary_type}\n")
    else:
        sys.stderr.write("ERROR: request failed\n")

    if query and isinstance(primary_reason, str):
        for line, col, _msg in extract_line_col_errors(primary_reason):
            pointer = render_query_pointer(query, line, col)
            if pointer:
                sys.stderr.write(pointer + "\n")

    sys.stderr.write(f"STATUS: {status_code}\n")

    seen_messages: set[str] = set()
    if primary_reason:
        seen_messages.add(primary_reason)

    root_causes = error.get("root_cause")
    if isinstance(root_causes, list):
        for root in root_causes:
            if not isinstance(root, dict):
                continue
            root_type = root.get("type")
            root_reason = root.get("reason")
            if not root_reason or root_reason in seen_messages:
                continue
            seen_messages.add(root_reason)
            if root_type:
                sys.stderr.write(f"ROOT CAUSE: {root_type}: {root_reason}\n")
            else:
                sys.stderr.write(f"ROOT CAUSE: {root_reason}\n")

    cause = error.get("caused_by") if isinstance(error.get("caused_by"), dict) else None
    while isinstance(cause, dict):
        cause_type = cause.get("type")
        cause_reason = cause.get("reason")
        if cause_reason and cause_reason not in seen_messages:
            seen_messages.add(cause_reason)
            if cause_type:
                sys.stderr.write(f"CAUSED BY: {cause_type}: {cause_reason}\n")
            else:
                sys.stderr.write(f"CAUSED BY: {cause_reason}\n")
        next_cause = cause.get("caused_by")
        cause = next_cause if isinstance(next_cause, dict) else None


def print_psql_table(response: dict[str, object]) -> None:
    columns = response.get("columns")
    values = response.get("values", [])
    if not isinstance(columns, list) or not isinstance(values, list):
        print(json.dumps(response, indent=2, sort_keys=False))
        return

    headers = [str(column.get("name", "")) if isinstance(column, dict) else str(column) for column in columns]
    rows = [[format_cell(value) for value in row] for row in values if isinstance(row, list)]
    widths = [len(header) for header in headers]
    for row in rows:
        for index, cell in enumerate(row[: len(widths)]):
            widths[index] = max(widths[index], len(cell))

    def render_row(row: list[str]) -> str:
        return " | ".join(cell.ljust(widths[index]) for index, cell in enumerate(row))

    print(render_row(headers))
    print("-+-".join("-" * width for width in widths))
    for row in rows:
        print(render_row(row + [""] * (len(widths) - len(row))))
    print(f"({len(rows)} {'row' if len(rows) == 1 else 'rows'})")


def split_complete_statements(text: str) -> tuple[list[str], str]:
    """Split on ';' while ignoring terminators inside ES|QL strings/comments/identifiers.

    Recognizes:
      - Line comments:   // ... \\n
      - Block comments:  /* ... */
      - Triple-quoted strings: \"\"\" ... \"\"\" (no escapes inside, like Python raw)
      - Single/double-quoted strings with backslash escapes
      - Backtick-quoted identifiers: `name with ;`
    """
    statements: list[str] = []
    start = 0
    i = 0
    n = len(text)

    while i < n:
        ch = text[i]

        if ch == "/" and i + 1 < n and text[i + 1] == "/":
            j = text.find("\n", i + 2)
            i = n if j == -1 else j + 1
            continue

        if ch == "/" and i + 1 < n and text[i + 1] == "*":
            j = text.find("*/", i + 2)
            i = n if j == -1 else j + 2
            continue

        if ch == '"' and text.startswith('"""', i):
            j = text.find('"""', i + 3)
            i = n if j == -1 else j + 3
            continue

        if ch in ('"', "'"):
            quote = ch
            i += 1
            while i < n:
                c = text[i]
                if c == "\\" and i + 1 < n:
                    i += 2
                    continue
                if c == quote:
                    i += 1
                    break
                i += 1
            continue

        if ch == "`":
            i += 1
            while i < n and text[i] != "`":
                i += 1
            if i < n:
                i += 1
            continue

        if ch == ";":
            statement = text[start:i].strip()
            if statement:
                statements.append(statement)
            i += 1
            start = i
            continue

        i += 1

    return statements, text[start:]


def print_repl_help() -> None:
    print(
        "Tiny ES|QL terminal. End a query with ';' to execute.\n"
        "Use arrow keys for input history.\n"
        "  Ctrl+C  Cancel the current input line / multiline buffer, or abort a running query.\n"
        "  /q, \\q, exit, quit  Exit the terminal.\n"
        "Commands (slash forms with / or \\):\n"
        "  \\h, /?              Show this help\n"
        "  \\clear              Reset the current buffer\n"
        "  \\timing             Toggle elapsed-time display\n"
        "  \\g                  Execute the current buffer (or rerun last statement)\n"
        "  \\i <path>           Include (read & run) a file of ES|QL\n"
        "  \\e                  Edit the current buffer in $EDITOR, then run\n"
        "  \\o [<path>]         Send query output to <path>, or back to stdout if blank\n"
        "  \\watch [<secs>]     Re-run the last statement every N seconds (Ctrl+C to stop)\n"
        "  \\set [<name> <val>] Set a substitution variable (used as ${name}); list if no args\n"
        "  \\unset <name>       Remove a substitution variable\n"
        "  \\conninfo           Show current connection settings\n"
        "  \\df                 Run SHOW FUNCTIONS\n"
        "  \\info               Run SHOW INFO\n"
        "  \\! <cmd>            Run a shell command\n"
    )


def parse_repl_command(line_stripped: str) -> tuple[str | None, str]:
    """Normalize /foo and \\foo slash commands to (canonical_name, args_string).

    Returns (None, "") for non-command input.
    """
    if not line_stripped.startswith(("/", "\\")):
        return None, ""
    parts = line_stripped.split(None, 1)
    head = parts[0].lstrip("/\\").lower()
    args = parts[1].strip() if len(parts) > 1 else ""
    if not head:
        return None, ""
    aliases = {
        "q": "quit", "quit": "quit", "exit": "quit",
        "h": "help", "?": "help", "help": "help",
        "clear": "clear", "c": "clear", "reset": "clear",
        "timing": "timing", "t": "timing",
        "g": "go",
        "i": "include", "include": "include",
        "e": "editor", "edit": "editor", "editor": "editor",
        "watch": "watch", "w": "watch",
        "set": "set",
        "unset": "unset",
        "conninfo": "conninfo", "status": "conninfo",
        "df": "functions", "functions": "functions",
        "info": "info",
        "!": "shell",
        "o": "output", "output": "output",
    }
    return aliases.get(head, head), args


_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z_0-9]*)\}")


def apply_variables(query: str, variables: dict[str, str]) -> str:
    """Replace ${name} with values from variables; leave unknown names intact."""
    if not variables:
        return query

    def repl(match: re.Match[str]) -> str:
        return variables.get(match.group(1), match.group(0))

    return _VAR_RE.sub(repl, query)


def setup_readline() -> None:
    try:
        import readline
    except ImportError:
        return

    history_file = os.path.expanduser(os.environ.get("ESQL_HISTORY", "~/.esql_history"))
    try:
        readline.read_history_file(history_file)
    except FileNotFoundError:
        pass

    def save_history() -> None:
        try:
            readline.write_history_file(history_file)
        except OSError:
            pass

    atexit.register(save_history)


def _execute_with_output_redirect(client: ESQLClient, statement: str) -> int:
    if not client.output_path:
        return client.execute(statement)
    import contextlib
    try:
        handle = open(client.output_path, "a", encoding="utf-8")
    except OSError as exc:
        sys.stderr.write(f"Cannot open '{client.output_path}': {exc}\n")
        return 1
    with handle, contextlib.redirect_stdout(handle):
        return client.execute(statement)


def _run_statements(client: ESQLClient, text: str) -> tuple[int, str]:
    """Tokenize, execute every complete statement, return (last_status, remainder)."""
    statements, remainder = split_complete_statements(text)
    last_status = 0
    for statement in statements:
        add_query_to_history(statement)
        try:
            last_status = _execute_with_output_redirect(client, statement)
        except KeyboardInterrupt:
            print("^C", file=sys.stderr)
            sys.stderr.write("Query execution aborted.\n")
            return 130, ""
    return last_status, remainder


def _cmd_set(client: ESQLClient, args: str) -> None:
    if not args:
        if not client.variables:
            print("(no variables set)")
            return
        for name, value in sorted(client.variables.items()):
            print(f"  {name} = {value}")
        return
    parts = args.split(None, 1)
    name = parts[0].lstrip(":$")
    if not name.replace("_", "").isalnum():
        sys.stderr.write(f"Invalid variable name: {name!r}\n")
        return
    value = parts[1] if len(parts) > 1 else ""
    if value.startswith("=") and not value.startswith("=="):
        value = value[1:].lstrip()
    client.variables[name] = value
    print(f"{name} = {value}")


def _cmd_unset(client: ESQLClient, args: str) -> None:
    name = args.strip().lstrip(":$")
    if not name:
        sys.stderr.write("Usage: \\unset <name>\n")
        return
    if client.variables.pop(name, None) is None:
        sys.stderr.write(f"No such variable: {name}\n")
    else:
        print(f"Unset {name}.")


def _cmd_conninfo(client: ESQLClient) -> None:
    auth_method = "ApiKey" if os.environ.get("ES_API_KEY") else "Basic"
    user = os.environ.get("ES_USER", "elastic") if auth_method == "Basic" else "-"
    print(
        "Connection:\n"
        f"  URL       {client.base}\n"
        f"  Endpoint  {client.url}\n"
        f"  Format    {client.fmt}\n"
        f"  Timeout   {client.timeout}s\n"
        f"  Insecure  {client.insecure}\n"
        f"  Timing    {'on' if client.timing else 'off'}\n"
        f"  Auth      {auth_method}{(' (' + user + ')') if user != '-' else ''}\n"
        f"  Output    {client.output_path or 'stdout'}\n"
        f"  Variables {len(client.variables)} set\n"
    )


def _cmd_output(client: ESQLClient, args: str) -> None:
    if not args:
        client.output_path = None
        print("Output redirected back to stdout.")
        return
    client.output_path = os.path.expanduser(args)
    print(f"Output appending to {client.output_path}")


def _cmd_shell(args: str) -> None:
    if not args:
        sys.stderr.write("Usage: \\! <command>\n")
        return
    import subprocess
    try:
        subprocess.run(args, shell=True, check=False)
    except OSError as exc:
        sys.stderr.write(f"Shell error: {exc}\n")


def _cmd_editor(buffer: str) -> str:
    import subprocess
    import tempfile
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi"
    with tempfile.NamedTemporaryFile("w+", suffix=".esql", delete=False, encoding="utf-8") as tf:
        tf.write(buffer)
        path = tf.name
    try:
        subprocess.run([*editor.split(), path], check=False)
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read()
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def _cmd_include(client: ESQLClient, args: str) -> tuple[int, str]:
    path = os.path.expanduser(args.strip())
    if not path:
        sys.stderr.write("Usage: \\i <path>\n")
        return 1, ""
    try:
        contents = read_query_file(path)
    except OSError as exc:
        sys.stderr.write(f"Cannot read {path}: {exc}\n")
        return 1, ""
    if not contents.endswith(";"):
        contents += ";"
    return _run_statements(client, contents + "\n")


def _cmd_watch(client: ESQLClient, args: str) -> int:
    if not client.last_statement.strip():
        sys.stderr.write("No previous statement to watch. Run a query first.\n")
        return 1
    try:
        interval = float(args) if args else 2.0
    except ValueError:
        sys.stderr.write(f"Invalid interval: {args!r}\n")
        return 1
    if interval <= 0:
        sys.stderr.write("Interval must be > 0.\n")
        return 1
    statement = client.last_statement
    last_status = 0
    use_ansi = sys.stdout.isatty()
    try:
        while True:
            if use_ansi:
                sys.stdout.write("\x1b[2J\x1b[H")
                sys.stdout.flush()
            header = time.strftime("%Y-%m-%d %H:%M:%S")
            print(f"-- \\watch every {interval}s -- {header}")
            try:
                last_status = client.execute(statement)
            except KeyboardInterrupt:
                raise
            time.sleep(interval)
    except KeyboardInterrupt:
        print("^C  watch stopped", file=sys.stderr)
        return last_status


def _dispatch_line(client: ESQLClient, line: str, buffer: str, last_status: int) -> tuple[str, int, bool]:
    """Process a single input line. Returns (new_buffer, new_status, should_quit)."""
    command = line.strip()
    cmd_kind, cmd_args = parse_repl_command(command)

    if cmd_kind == "quit" or command.lower() in ("quit", "exit"):
        return buffer, last_status, True
    if cmd_kind == "help":
        print_repl_help()
        return buffer, last_status, False
    if cmd_kind == "clear":
        return "", last_status, False
    if cmd_kind == "timing":
        client.timing = not client.timing
        print(f"Timing is {'on' if client.timing else 'off'}.")
        return buffer, last_status, False
    if cmd_kind == "conninfo":
        _cmd_conninfo(client)
        return buffer, last_status, False
    if cmd_kind == "set":
        _cmd_set(client, cmd_args)
        return buffer, last_status, False
    if cmd_kind == "unset":
        _cmd_unset(client, cmd_args)
        return buffer, last_status, False
    if cmd_kind == "output":
        _cmd_output(client, cmd_args)
        return buffer, last_status, False
    if cmd_kind == "shell":
        _cmd_shell(cmd_args)
        return buffer, last_status, False
    if cmd_kind == "include":
        status, leftover = _cmd_include(client, cmd_args)
        return buffer + leftover, status, False
    if cmd_kind == "editor":
        edited = _cmd_editor(buffer)
        status, new_buffer = _run_statements(client, edited)
        return new_buffer, status, False
    if cmd_kind == "watch":
        return buffer, _cmd_watch(client, cmd_args), False
    if cmd_kind == "go":
        target = buffer.strip() or client.last_statement
        if not target:
            sys.stderr.write("No buffer or previous statement to execute.\n")
            return "", last_status, False
        status, leftover = _run_statements(client, target + ";")
        return leftover, status, False
    if cmd_kind == "functions":
        return buffer, _execute_with_output_redirect(client, "SHOW FUNCTIONS"), False
    if cmd_kind == "info":
        return buffer, _execute_with_output_redirect(client, "SHOW INFO"), False
    if not command:
        return buffer, last_status, False

    new_buffer = buffer + line + "\n"
    status, new_buffer = _run_statements(client, new_buffer)
    return new_buffer, status, False


def run_repl(client: ESQLClient) -> int:
    setup_readline()
    print_repl_help()
    buffer = ""
    last_status = 0

    while True:
        try:
            line = input("esql> " if not buffer.strip() else "   > ")
        except EOFError:
            print()
            if buffer.strip():
                sys.stderr.write("Discarding unterminated query; add ';' to execute before EOF.\n")
            return last_status
        except KeyboardInterrupt:
            print("^C")
            if buffer.strip():
                buffer = ""
                sys.stderr.write("Buffer cleared.\n")
            continue

        buffer, last_status, should_quit = _dispatch_line(client, line, buffer, last_status)
        if should_quit:
            return last_status


def run_script(client: ESQLClient, text: str) -> int:
    buffer = ""
    last_status = 0
    for line in text.splitlines():
        buffer, last_status, should_quit = _dispatch_line(client, line, buffer, last_status)
        if should_quit:
            return last_status
    if buffer.strip():
        status, _ = _run_statements(client, buffer + ";")
        last_status = status
    return last_status


def add_query_to_history(statement: str) -> None:
    try:
        import readline
    except ImportError:
        return
    readline.add_history(" ".join(statement.split()))


def main() -> int:
    parser = argparse.ArgumentParser(description="Run ES|QL via POST /_query")
    parser.add_argument(
        "query_file",
        nargs="?",
        default=None,
        help="Path to a file containing ES|QL; omit for REPL when stdin is a terminal; use '-' to read stdin",
    )
    parser.add_argument(
        "--format",
        default=os.environ.get("ES_FORMAT", "psql"),
        help="Output format: psql, json, txt, csv, yaml, ... (default psql or ES_FORMAT)",
    )
    parser.add_argument(
        "--insecure",
        action="store_true",
        help="Skip TLS certificate verification (same as ES_INSECURE=1)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=float(os.environ.get("ES_TIMEOUT", "120")),
        help="HTTP timeout in seconds (default 120 or ES_TIMEOUT)",
    )
    parser.add_argument(
        "--timing",
        action="store_true",
        help="Print elapsed time after each query (toggle in REPL with \\timing)",
    )
    args = parser.parse_args()

    insecure = args.insecure or os.environ.get("ES_INSECURE", "").lower() in ("1", "true", "yes")
    timing = args.timing or os.environ.get("ES_TIMING", "").lower() in ("1", "true", "yes")
    client = ESQLClient(
        base=os.environ.get("ES_URL", "http://127.0.0.1:9200"),
        fmt=args.format,
        timeout=args.timeout,
        insecure=insecure,
        timing=timing,
    )

    if args.query_file is None and sys.stdin.isatty():
        return run_repl(client)

    query = sys.stdin.read().lstrip("\ufeff") if args.query_file in (None, "-") else read_query_file(args.query_file)
    if not query.strip():
        sys.stderr.write("No ES|QL query provided. Run ./esql.py for interactive mode, or pass a query file.\n")
        return 2

    return run_script(client, query)


if __name__ == "__main__":
    raise SystemExit(main())
