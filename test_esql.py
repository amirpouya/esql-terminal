#!/usr/bin/env python3
"""Self-contained tests for esql.py. Run: python3 test_esql.py"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import threading
import time
from contextlib import redirect_stderr
from http.server import BaseHTTPRequestHandler, HTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import esql  # noqa: E402


PASSED = 0
FAILED = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print(f"  ok  {name}")
    else:
        FAILED += 1
        print(f"  FAIL {name}{(': ' + detail) if detail else ''}")


def section(title: str) -> None:
    print(f"\n[{title}]")


def test_splitter() -> None:
    section("split_complete_statements")

    s, rem = esql.split_complete_statements("FROM a; FROM b;")
    check("two simple statements", s == ["FROM a", "FROM b"] and rem == "", f"s={s} rem={rem!r}")

    s, rem = esql.split_complete_statements("FROM a; FROM b")
    check("trailing remainder kept", s == ["FROM a"] and rem == " FROM b", f"s={s} rem={rem!r}")

    s, rem = esql.split_complete_statements("FROM a // ignore ; me\n;")
    check("line comment // hides ;", s == ["FROM a // ignore ; me"], f"s={s}")

    s, rem = esql.split_complete_statements("FROM a /* ; nope */;")
    check("block comment hides ;", s == ["FROM a /* ; nope */"], f"s={s}")

    s, rem = esql.split_complete_statements('FROM a | WHERE x = """;not;""";')
    check("triple-quoted string hides ;", s == ['FROM a | WHERE x = """;not;"""'], f"s={s}")

    s, rem = esql.split_complete_statements('FROM a | KEEP `weird;name`;')
    check("backtick identifier hides ;", s == ['FROM a | KEEP `weird;name`'], f"s={s}")

    s, rem = esql.split_complete_statements(r'FROM a | WHERE x = "a\";b";')
    check("backslash escape inside string", s == [r'FROM a | WHERE x = "a\";b"'], f"s={s}")

    s, rem = esql.split_complete_statements("FROM a | WHERE x = 'it''s' OR y = ';'; FROM b;")
    check(
        "single quotes treated as strings",
        s == ["FROM a | WHERE x = 'it''s' OR y = ';'", "FROM b"],
        f"s={s}",
    )

    s, rem = esql.split_complete_statements(";; ; FROM a;")
    check("empty statements are skipped", s == ["FROM a"] and rem == "", f"s={s} rem={rem!r}")

    s, rem = esql.split_complete_statements(
        "// header comment\nFROM a; /* mid */ FROM b /* tail */;"
    )
    check(
        "leading/middle comments don't break splitting",
        s == ["// header comment\nFROM a", "/* mid */ FROM b /* tail */"],
        f"s={s}",
    )

    s, rem = esql.split_complete_statements('FROM a | WHERE r = """multi\nline ; ok"""; FROM b;')
    check(
        "triple-quoted string spans lines",
        s == ['FROM a | WHERE r = """multi\nline ; ok"""', "FROM b"],
        f"s={s}",
    )


def test_extract_line_col_errors() -> None:
    section("extract_line_col_errors")

    out = esql.extract_line_col_errors("line 1:8: token recognition error at: '>>='")
    check("single position parsed", out == [(1, 8, "token recognition error at: '>>='")], f"out={out}")

    out = esql.extract_line_col_errors(
        "Found 2 problems\nline 1:13: Unknown column [foo]\nline 2:1: missing FROM"
    )
    check(
        "verification multi-error parsed",
        out == [(1, 13, "Unknown column [foo]"), (2, 1, "missing FROM")],
        f"out={out}",
    )

    check("empty reason returns []", esql.extract_line_col_errors("") == [])
    check(
        "no positions returns []",
        esql.extract_line_col_errors("plain reason without positions") == [],
    )


def test_render_query_pointer() -> None:
    section("render_query_pointer")

    src = "FROM logs | WHERE foo >>= 1"
    out = esql.render_query_pointer(src, 1, 23)
    expected = "  FROM logs | WHERE foo >>= 1\n  " + " " * 22 + "^"
    check("caret aligns under column", out == expected, f"out={out!r}")

    out = esql.render_query_pointer("FROM a\nFROM b\nFROM c", 2, 6)
    check(
        "selects correct line in multi-line",
        out == "  FROM b\n  " + " " * 5 + "^",
        f"out={out!r}",
    )

    long_src = "x" * 200 + "BUG" + "y" * 200
    col = 201
    out = esql.render_query_pointer(long_src, 1, col, max_width=40)
    first_line = out.split("\n")[0].lstrip(" ")
    second_line = out.split("\n")[1]
    check("long line truncated with ellipses", first_line.startswith("…") and first_line.endswith("…"), f"first={first_line!r}")
    caret_index = second_line.index("^") - 2
    char_under = first_line[caret_index]
    check("caret points at expected character on truncated line", char_under == "x" or char_under == "B", f"char={char_under!r}")

    check("out-of-range line returns empty", esql.render_query_pointer("FROM a", 5, 1) == "")
    check("zero/negative line returns empty", esql.render_query_pointer("FROM a", 0, 1) == "")


def test_print_error_caret() -> None:
    section("print_error caret integration")

    raw = json.dumps(
        {
            "error": {
                "type": "parsing_exception",
                "reason": "line 1:23: token recognition error at: '>>='",
                "root_cause": [
                    {"type": "parsing_exception", "reason": "line 1:23: token recognition error at: '>>='"}
                ],
            },
            "status": 400,
        }
    )
    buf = io.StringIO()
    with redirect_stderr(buf):
        esql.print_error(raw, 400, "psql", query="FROM logs | WHERE foo >>= 1")
    output = buf.getvalue()
    check("ERROR line present", "ERROR: parsing_exception" in output, output)
    check("source line present", "FROM logs | WHERE foo >>= 1" in output, output)
    check("caret on its own line", any(line.strip() == "^" for line in output.splitlines()), output)
    check("STATUS line present", "STATUS: 400" in output, output)


# ----------------------------------------------------------------------------
# End-to-end: spin up a fake ES, run esql.py against it, check output & timing.
# ----------------------------------------------------------------------------

class FakeESHandler(BaseHTTPRequestHandler):
    routes: dict[str, tuple[int, dict | str]] = {}

    def log_message(self, *_args, **_kw):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(length)  # discard
        path = self.path.split("?")[0]
        status, body = self.routes.get(path, (404, {"error": {"type": "not_found", "reason": path}}))
        encoded = json.dumps(body).encode("utf-8") if isinstance(body, dict) else body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


def start_fake_es(routes: dict[str, tuple[int, dict | str]]) -> tuple[HTTPServer, str]:
    FakeESHandler.routes = routes
    httpd = HTTPServer(("127.0.0.1", 0), FakeESHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    host, port = httpd.server_address
    return httpd, f"http://{host}:{port}"


def run_cli(url: str, query: str, *extra: str, timing_env: str = "") -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["ES_URL"] = url
    env["ES_USER"] = "u"
    env["ES_PASSWORD"] = "p"
    if timing_env:
        env["ES_TIMING"] = timing_env
    return subprocess.run(
        [sys.executable, os.path.join(HERE, "esql.py"), *extra],
        input=query.encode("utf-8"),
        capture_output=True,
        env=env,
        timeout=10,
    )


def test_e2e_success() -> None:
    section("end-to-end: success path")
    routes = {
        "/_query": (
            200,
            {
                "columns": [{"name": "id", "type": "long"}, {"name": "name", "type": "keyword"}],
                "values": [[1, "a"], [2, "b"]],
            },
        )
    }
    httpd, url = start_fake_es(routes)
    try:
        proc = run_cli(url, "FROM x;\n")
        out = proc.stdout.decode()
        err = proc.stderr.decode()
        check("exit 0 on success", proc.returncode == 0, f"rc={proc.returncode} err={err}")
        check("psql header rendered", "id" in out and "name" in out, out)
        check("row count footer", "(2 rows)" in out, out)
        check("no timing line by default", "Time:" not in err, err)
    finally:
        httpd.shutdown()


def test_e2e_timing_env() -> None:
    section("end-to-end: ES_TIMING=1")
    routes = {
        "/_query": (
            200,
            {"columns": [{"name": "id", "type": "long"}], "values": [[1]]},
        )
    }
    httpd, url = start_fake_es(routes)
    try:
        proc = run_cli(url, "FROM x;\n", timing_env="1")
        err = proc.stderr.decode()
        check("Time line present in stderr", "Time:" in err, err)
        check("timing reports ms", "ms" in err, err)
    finally:
        httpd.shutdown()


def test_e2e_parse_error_caret() -> None:
    section("end-to-end: parse error caret")
    routes = {
        "/_query": (
            400,
            {
                "error": {
                    "type": "parsing_exception",
                    "reason": "line 1:23: token recognition error at: '>>='",
                },
                "status": 400,
            },
        )
    }
    httpd, url = start_fake_es(routes)
    try:
        proc = run_cli(url, "FROM logs | WHERE foo >>= 1;\n")
        err = proc.stderr.decode()
        check("ERROR rendered", "ERROR: parsing_exception" in err, err)
        check("source line present", "FROM logs | WHERE foo >>= 1" in err, err)
        check("caret line present", any(s.strip() == "^" for s in err.splitlines()), err)
        check("STATUS rendered", "STATUS: 400" in err, err)
        check("nonzero exit", proc.returncode != 0, f"rc={proc.returncode}")
    finally:
        httpd.shutdown()


def test_e2e_multistatement_pipe() -> None:
    section("end-to-end: multi-statement with comments via pipe")
    calls: list[str] = []

    class CountingHandler(BaseHTTPRequestHandler):
        def log_message(self, *_a, **_k): pass

        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            payload = self.rfile.read(length).decode("utf-8")
            try:
                calls.append(json.loads(payload).get("query", ""))
            except Exception:
                calls.append(payload)
            body = json.dumps({"columns": [{"name": "n", "type": "long"}], "values": [[1]]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    httpd = HTTPServer(("127.0.0.1", 0), CountingHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    host, port = httpd.server_address
    url = f"http://{host}:{port}"
    try:
        script = '// header\nFROM a /* ; */;\nFROM b | WHERE x = """ ; """;\n'
        proc = run_cli(url, script)
        check("exit 0 on multi-statement script", proc.returncode == 0, proc.stderr.decode())
        check("server saw 2 statements", len(calls) == 2, f"calls={calls}")
        check("first statement preserved", "FROM a" in calls[0] and ";" not in calls[0].split("/*")[0], f"call0={calls[0]!r}")
        check("second statement triple-quoted intact", '"""' in calls[1] and "FROM b" in calls[1], f"call1={calls[1]!r}")
    finally:
        httpd.shutdown()


def test_parse_repl_command() -> None:
    section("parse_repl_command")

    check("plain text -> (None, '')", esql.parse_repl_command("FROM logs") == (None, ""))

    check("/q canonicalized", esql.parse_repl_command("/q") == ("quit", ""))
    check("\\q canonicalized", esql.parse_repl_command("\\q") == ("quit", ""))
    check("/timing alias t", esql.parse_repl_command("/t") == ("timing", ""))

    check("/i path captured", esql.parse_repl_command("/i ./queries.esql") == ("include", "./queries.esql"))
    check("\\set with spaces", esql.parse_repl_command("\\set name foo bar") == ("set", "name foo bar"))
    check("/watch interval", esql.parse_repl_command("/watch 5") == ("watch", "5"))
    check("/! shell command", esql.parse_repl_command("/! echo hi") == ("shell", "echo hi"))
    check("\\unset arg", esql.parse_repl_command("\\unset foo") == ("unset", "foo"))
    check("/conninfo no args", esql.parse_repl_command("/conninfo") == ("conninfo", ""))

    check("unknown slash kept verbatim", esql.parse_repl_command("/whatever") == ("whatever", ""))


def test_apply_variables() -> None:
    section("apply_variables")

    out = esql.apply_variables("FROM ${idx} | LIMIT ${n}", {"idx": "logs", "n": "10"})
    check("simple substitution", out == "FROM logs | LIMIT 10", f"out={out!r}")

    out = esql.apply_variables("FROM ${idx}", {})
    check("empty variables -> identity", out == "FROM ${idx}", f"out={out!r}")

    out = esql.apply_variables("FROM ${unknown}", {"x": "y"})
    check("unknown name kept literal", out == "FROM ${unknown}", f"out={out!r}")

    out = esql.apply_variables("FROM a | WHERE x = '${name}'", {"name": "bob"})
    check("substitution inside string", out == "FROM a | WHERE x = 'bob'", f"out={out!r}")


def test_e2e_set_and_substitute() -> None:
    section("end-to-end: \\set + variable substitution via \\g")
    seen: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_a, **_k): pass

        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length).decode("utf-8")
            try:
                seen.append(json.loads(body).get("query", ""))
            except Exception:
                seen.append(body)
            payload = json.dumps({"columns": [{"name": "n"}], "values": [[1]]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    httpd = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    host, port = httpd.server_address
    url = f"http://{host}:{port}"
    try:
        script = "\\set idx logs\nFROM ${idx} | LIMIT 1;\n"
        proc = run_cli(url, script)
        check("exit 0", proc.returncode == 0, proc.stderr.decode())
        check("server saw substituted index", any("FROM logs" in q for q in seen), f"seen={seen}")
        check("no raw ${idx} reached server", all("${idx}" not in q for q in seen), f"seen={seen}")
    finally:
        httpd.shutdown()


def test_e2e_show_functions_via_slash_df() -> None:
    section("end-to-end: \\df runs SHOW FUNCTIONS")
    seen: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_a, **_k): pass

        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length).decode("utf-8")
            try:
                seen.append(json.loads(body).get("query", ""))
            except Exception:
                seen.append(body)
            payload = json.dumps({"columns": [{"name": "name"}], "values": [["abs"]]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    httpd = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    host, port = httpd.server_address
    url = f"http://{host}:{port}"
    try:
        proc = run_cli(url, "\\df\n")
        check("exit 0", proc.returncode == 0, proc.stderr.decode())
        check("server saw SHOW FUNCTIONS", seen == ["SHOW FUNCTIONS"], f"seen={seen}")
    finally:
        httpd.shutdown()


def main() -> int:
    test_splitter()
    test_extract_line_col_errors()
    test_render_query_pointer()
    test_print_error_caret()
    test_parse_repl_command()
    test_apply_variables()
    test_e2e_success()
    test_e2e_timing_env()
    test_e2e_parse_error_caret()
    test_e2e_multistatement_pipe()
    test_e2e_set_and_substitute()
    test_e2e_show_functions_via_slash_df()
    print(f"\n{PASSED} passed, {FAILED} failed")
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
