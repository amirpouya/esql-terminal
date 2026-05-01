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
"""

from __future__ import annotations

import atexit
import argparse
import json
import os
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from base64 import b64encode


class ESQLClient:
    def __init__(self, base: str, fmt: str, timeout: float, insecure: bool):
        self.base = base.rstrip("/")
        self.fmt = fmt
        self.timeout = timeout
        self.insecure = insecure
        request_format = "json" if fmt.lower() == "psql" else fmt
        self.url = f"{self.base}/_query?{urllib.parse.urlencode({'format': request_format})}"
        self.context = None
        if self.url.lower().startswith("https://") and insecure:
            self.context = ssl.create_default_context()
            self.context.check_hostname = False
            self.context.verify_mode = ssl.CERT_NONE

    def execute(self, query: str) -> int:
        body = json.dumps({"query": query}).encode("utf-8")
        req = urllib.request.Request(self.url, data=body, method="POST")

        for name, value in build_auth_headers():
            req.add_header(name, value)

        req.add_header("Content-Type", "application/json; charset=utf-8")
        if self.fmt.lower() in ("json", "psql"):
            req.add_header("Accept", "application/json")

        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=self.context) as resp:
                raw = resp.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")
            if err_body:
                print_error(err_body, e.code, self.fmt)
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


def print_error(raw: str, status_code: int, fmt: str) -> None:
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
    statements: list[str] = []
    start = 0
    quote: str | None = None
    escaped = False

    for index, char in enumerate(text):
        if quote is not None:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue

        if char in ("'", '"'):
            quote = char
        elif char == ";":
            statement = text[start:index].strip()
            if statement:
                statements.append(statement)
            start = index + 1

    return statements, text[start:]


def print_repl_help() -> None:
    print(
        "Tiny ES|QL terminal. End a query with ';' to execute.\n"
        "Use arrow keys for input history. Press Ctrl+C, or type \\q / exit, to quit.\n"
        "Commands: \\q or exit to quit, \\h for this help, \\clear to reset the current buffer.\n"
    )


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
            return 130

        command = line.strip()
        if not buffer.strip() and command in ("\\q", "quit", "exit"):
            return last_status
        if not buffer.strip() and command == "\\h":
            print_repl_help()
            continue
        if command == "\\clear":
            buffer = ""
            continue
        if not command:
            continue

        buffer += line + "\n"
        statements, buffer = split_complete_statements(buffer)
        for statement in statements:
            add_query_to_history(statement)
            last_status = client.execute(statement)


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
    args = parser.parse_args()

    insecure = args.insecure or os.environ.get("ES_INSECURE", "").lower() in ("1", "true", "yes")
    client = ESQLClient(
        base=os.environ.get("ES_URL", "http://127.0.0.1:9200"),
        fmt=args.format,
        timeout=args.timeout,
        insecure=insecure,
    )

    if args.query_file is None and sys.stdin.isatty():
        return run_repl(client)

    query = sys.stdin.read().lstrip("\ufeff").strip() if args.query_file in (None, "-") else read_query_file(args.query_file)
    if not query:
        sys.stderr.write("No ES|QL query provided. Run ./test.py for interactive mode, or pass a query file.\n")
        return 2

    statements, remainder = split_complete_statements(query)
    if statements and not remainder.strip():
        status = 0
        for statement in statements:
            status = client.execute(statement)
        return status
    return client.execute(query)


if __name__ == "__main__":
    raise SystemExit(main())
