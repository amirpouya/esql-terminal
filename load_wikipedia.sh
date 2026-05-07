#!/usr/bin/env bash
set -euo pipefail

# Load a small Wikipedia sample into Elasticsearch for ES|QL experiments.
#
# Defaults target local Elasticsearch and create/index documents into "wikipedia".
# Configure with:
#   ES_URL=http://127.0.0.1:9200
#   ES_USER=elastic ES_PASSWORD=password (default auth when ES_API_KEY is unset)
#   ES_API_KEY=<base64(id:key)> or <id:key>
#   ES_NO_AUTH=1
#   ES_INSECURE=1
#   WIKI_INDEX=wikipedia
#   WIKI_PAGES=100
#   WIKI_LANG=en

ES_URL="${ES_URL:-http://127.0.0.1:9200}"
WIKI_INDEX="${WIKI_INDEX:-wikipedia}"
WIKI_PAGES="${WIKI_PAGES:-100}"
WIKI_LANG="${WIKI_LANG:-en}"

tmp_dir="$(mktemp -d)"
bulk_file="${tmp_dir}/wikipedia.ndjson"
trap 'rm -rf "${tmp_dir}"' EXIT

curl_common=(-sS)
if [[ "${ES_INSECURE:-}" =~ ^(1|true|yes|on)$ ]]; then
  curl_common+=(-k)
fi

if [[ -n "${ES_API_KEY:-}" ]]; then
  api_key="${ES_API_KEY}"
  if [[ "${api_key}" == *:* ]]; then
    api_key="$(printf '%s' "${api_key}" | base64 | tr -d '\n')"
  fi
  curl_common+=(-H "Authorization: ApiKey ${api_key}")
elif [[ ! "${ES_NO_AUTH:-}" =~ ^(1|true|yes|on)$ ]]; then
  curl_common+=(-u "${ES_USER:-elastic}:${ES_PASSWORD:-password}")
fi
curl_fail=("${curl_common[@]}" -f)

echo "Creating index '${WIKI_INDEX}' at ${ES_URL} ..."
create_response="${tmp_dir}/create-index-response.json"
create_status="$(curl "${curl_common[@]}" \
  -X PUT "${ES_URL%/}/${WIKI_INDEX}" \
  -H "Content-Type: application/json" \
  -o "${create_response}" \
  -w "%{http_code}" \
  -d '{
    "mappings": {
      "properties": {
        "page_id": { "type": "long" },
        "title": { "type": "text", "fields": { "keyword": { "type": "keyword" } } },
        "extract": { "type": "text" },
        "description": { "type": "text" },
        "url": { "type": "keyword" },
        "language": { "type": "keyword" },
        "indexed_at": { "type": "date" }
      }
    }
  }')"
if [[ "${create_status}" =~ ^20[0-9]$ ]]; then
  :
elif [[ "${create_status}" == "400" ]] && grep -q "resource_already_exists_exception" "${create_response}"; then
  echo "Index '${WIKI_INDEX}' already exists; continuing with bulk load." >&2
else
  echo "Failed to create index '${WIKI_INDEX}' (HTTP ${create_status}):" >&2
  cat "${create_response}" >&2
  exit 1
fi

echo "Fetching ${WIKI_PAGES} random ${WIKI_LANG}.wikipedia.org pages ..."
python3 - "${bulk_file}" "${WIKI_INDEX}" "${WIKI_PAGES}" "${WIKI_LANG}" <<'PY'
import datetime as dt
import json
import os
import subprocess
import sys
import time
import urllib.parse

bulk_path, index, page_count_raw, lang = sys.argv[1:]
page_count = int(page_count_raw)
api_url = f"https://{lang}.wikipedia.org/w/api.php"
seen: set[int] = set()


def fetch_json(url: str) -> dict:
    command = [
        "curl",
        "-fsS",
        "-H",
        "User-Agent: esql-terminal-wikipedia-loader/1.0",
    ]
    if os.environ.get("WIKI_INSECURE", "").lower() in ("1", "true", "yes", "on"):
        command.append("-k")
    command.append(url)
    return json.loads(subprocess.check_output(command, text=True, timeout=30))


with open(bulk_path, "w", encoding="utf-8") as bulk:
    while len(seen) < page_count:
        needed = min(50, page_count - len(seen))
        params = urllib.parse.urlencode(
            {
                "action": "query",
                "format": "json",
                "generator": "random",
                "grnnamespace": 0,
                "grnlimit": needed,
                "prop": "extracts|description|info",
                "exintro": "1",
                "explaintext": "1",
                "inprop": "url",
            }
        )
        payload = fetch_json(f"{api_url}?{params}")
        pages = payload.get("query", {}).get("pages", {})
        for page in pages.values():
            page_id = page.get("pageid")
            if not page_id or page_id in seen:
                continue
            seen.add(page_id)
            doc = {
                "page_id": page_id,
                "title": page.get("title", ""),
                "extract": page.get("extract", ""),
                "description": page.get("description", ""),
                "url": page.get("fullurl", ""),
                "language": lang,
                "indexed_at": dt.datetime.now(dt.UTC).isoformat(),
            }
            bulk.write(json.dumps({"index": {"_index": index, "_id": page_id}}, ensure_ascii=False) + "\n")
            bulk.write(json.dumps(doc, ensure_ascii=False) + "\n")
        time.sleep(0.1)

print(f"Wrote {len(seen)} documents to {bulk_path}")
PY

echo "Bulk indexing into '${WIKI_INDEX}' ..."
curl "${curl_fail[@]}" \
  -X POST "${ES_URL%/}/_bulk?refresh=true" \
  -H "Content-Type: application/x-ndjson" \
  --data-binary "@${bulk_file}" \
  | python3 -c 'import json,sys; r=json.load(sys.stdin); print(json.dumps({"errors": r.get("errors"), "took": r.get("took"), "items": len(r.get("items", []))}, indent=2)); sys.exit(1 if r.get("errors") else 0)'

echo
echo "Loaded ${WIKI_PAGES} docs. Try:"
echo "  FROM ${WIKI_INDEX} | LIMIT 5;"
echo "  FROM ${WIKI_INDEX} | STATS pages = COUNT(*) BY language;"
