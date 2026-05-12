# esql-terminal

Tiny psql-style ES|QL terminal for Elasticsearch.

- Interactive REPL: type/paste multiline ES|QL; complete statements execute on `;`.
- Arrow-key history (persisted to `~/.esql_history`).
- `Ctrl+C` aborts current input/query and clears buffer instead of exiting the app.
- Quit with `/q`, `\q`, `quit`, or `exit`.
- Default output is a psql-style table; `--format json|txt|csv|yaml` also supported.
- Friendly error formatting for Elasticsearch error responses, including query line/caret pointers for parse errors.
- Optional prompt coloring and autocomplete (via `prompt-toolkit` + `pygments`).

## Public repository readiness

- No cluster credentials are stored in the repository.
- Keep local credentials in environment variables or ignored `.env` files.
- Sample basic-auth defaults are only intended for disposable local Elasticsearch setups.
- See [`SECURITY.md`](SECURITY.md) for secret-handling guidance.

## Install

This project is intentionally dependency-light and runs with the Python standard library.

```bash
chmod +x ./esql.py
python3 ./esql.py --help
```

## Usage

```bash
./esql.py                 # interactive REPL
./esql.py query.esql      # run a file
./esql.py < query.esql    # run from stdin
./esql.py --timing        # print elapsed query time
./esql.py --profile       # send {"profile": true} with each ES|QL request
./esql.py --no-auth       # talk to an unsecured local cluster without auth headers
./esql.py --no-auto-keywords  # disable keyword auto-uppercase (enabled by default)
```

## Load Wikipedia Sample Data

Load random Wikipedia page summaries into a local Elasticsearch index:

```bash
./load_wikipedia.sh
```

Useful options:

```bash
WIKI_INDEX=wikipedia WIKI_PAGES=500 ./load_wikipedia.sh
ES_URL=https://example.es:9200 ES_API_KEY=id:secret ./load_wikipedia.sh
ES_NO_AUTH=1 ./load_wikipedia.sh  # for unsecured local clusters
```

The loader uses `ES_USER=elastic` and `ES_PASSWORD=password` by default, matching
`esql.py`. If Python has trouble with local CA certificates, the loader still fetches
Wikipedia with `curl`; set `WIKI_INSECURE=1` only if your environment requires it.

## Development

Run the self-contained test suite:

```bash
python3 test_esql.py
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
| `ES_NO_AUTH` | _(unset)_ | Skip auth headers for unsecured local clusters |
| `ES_FORMAT` | `psql` | Output format |
| `ES_INSECURE` | _(unset)_ | `1` to skip TLS verification |
| `ES_TIMING` | _(unset)_ | `1` to print elapsed time per query |
| `ES_PROFILE` | _(unset)_ | `1` to send `profile: true` in ES|QL request bodies |
| `ESQL_COLOR` | `auto` | `1`/`always` to force JSON color, `0`/`never` to disable |
| `ES_AUTO_KEYWORDS` | `1` | Auto-uppercase ES|QL keywords before execution (`0` disables) |
| `ESQL_HISTORY` | `~/.esql_history` | Path to history file |

## REPL commands

Slash commands work with both `\` and `/` prefixes.

- `\q`/`/q`, `quit`, `exit`: quit REPL
- `\h`/`/?`: help
- `\clear`: clear the screen and current multiline buffer
- `\format [fmt]` (`\f`): show or change output format, for example `\format json`
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
- `\indices` (`\di`): list Elasticsearch indexes via `_cat/indices`
- `\health`: show cluster health
- `\nodes`: list cluster nodes
- `\shards [index]`: list shard allocation
- `\aliases`: list aliases
- `\templates`: list index templates
- `\datastreams` (`\ds`): list data streams
- `\tasks`: list running tasks
- `\count [index]`: count documents
- `\mapping <index>`: show index mappings
- `\get /_path`: run a read-only Elasticsearch GET API request
- `\df`: run `SHOW FUNCTIONS`
- `\info`: run `SHOW INFO`
- `\! <cmd>`: run shell command
