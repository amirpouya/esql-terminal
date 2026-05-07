# esql-terminal

Tiny psql-style ES|QL terminal for Elasticsearch.

- Interactive REPL: type/paste multiline ES|QL; complete statements execute on `;`.
- Arrow-key history (persisted to `~/.esql_history`).
- `Ctrl+C` aborts current input/query and clears buffer instead of exiting the app.
- Quit with `/q`, `\q`, `quit`, or `exit`.
- Default output is a psql-style table; `--format json|txt|csv|yaml` also supported.
- Friendly error formatting for Elasticsearch error responses, including query line/caret pointers for parse errors.
- Optional prompt coloring and autocomplete (via `prompt-toolkit` + `pygments`).

## Usage

```bash
./esql.py                 # interactive REPL
./esql.py query.esql      # run a file
./esql.py < query.esql    # run from stdin
./esql.py --timing        # print elapsed query time
./esql.py --profile       # send {"profile": true} with each ES|QL request
./esql.py --no-auto-keywords  # disable keyword auto-uppercase (enabled by default)
```

## Optional REPL UX dependencies

Install these for inline syntax highlighting and autocomplete while typing:

```bash
python3 -m pip install prompt-toolkit pygments
```

## Environment

| Variable | Default | Purpose |
| --- | --- | --- |
| `ES_URL` | `http://127.0.0.1:9200` | Cluster base URL |
| `ES_USER` / `ES_PASSWORD` | `elastic` / `password` | Basic auth |
| `ES_API_KEY` | _(unset)_ | Use `ApiKey` auth instead of basic |
| `ES_FORMAT` | `psql` | Output format |
| `ES_INSECURE` | _(unset)_ | `1` to skip TLS verification |
| `ES_TIMING` | _(unset)_ | `1` to print elapsed time per query |
| `ES_PROFILE` | _(unset)_ | `1` to send `profile: true` in ES|QL request bodies |
| `ES_AUTO_KEYWORDS` | `1` | Auto-uppercase ES|QL keywords before execution (`0` disables) |
| `ESQL_HISTORY` | `~/.esql_history` | Path to history file |

## REPL commands

Slash commands work with both `\` and `/` prefixes.

- `\q`/`/q`, `quit`, `exit`: quit REPL
- `\h`/`/?`: help
- `\clear`: clear the screen and current multiline buffer
- `\timing`: toggle elapsed time printing
- `\autokeywords` (`\ak`): toggle keyword auto-uppercase
- `\profile` (`\p`): toggle `profile: true` in ES|QL request bodies
- `\g`: run current buffer (or rerun last statement)
- `\i <path>`: run statements from a file
- `\e`: edit current buffer in `$EDITOR`/`$VISUAL`, then run
- `\o [<path>]`: redirect output to file (blank to restore stdout)
- `\watch [secs]`: rerun last query every N seconds
- `\set [name value]` / `\unset <name>`: manage `${name}` substitutions
- `\conninfo`: show current session settings
- `\df`: run `SHOW FUNCTIONS`
- `\info`: run `SHOW INFO`
- `\! <cmd>`: run shell command
