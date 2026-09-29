#!/usr/bin/env bash
# Concurrency re-verification for recipe draft-vs-publish races.
#
# Against a running API (Docker Compose deployment):
#
#   docker compose up --build -d
#   bash examples/concurrency_check.sh
#
# Each round fires two requests at the same draft version simultaneously and
# then checks the database-level win/lose invariants:
#
#   PUT vs publish   -> publish freezes either the old or the updated draft;
#                       a PUT that lost returns 409 RECIPE_VERSION_IMMUTABLE
#                       and the published row keeps its original content.
#   DELETE vs publish -> either the delete wins (publish -> 404, no snapshot
#                       left) or the publish wins (DELETE -> 409, published
#                       row unchanged). A published version never disappears.
#
# Both outcomes of each race are legal; the invariants are what is verified.
# Run more rounds to observe both outcomes:  ROUNDS=100 bash examples/concurrency_check.sh
#
set -euo pipefail

BASE="${BASE_URL:-http://localhost:8000}"
ROUNDS="${ROUNDS:-20}"
RUN="race-$(date +%s)-$$"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

echo "== concurrency check against $BASE (run id: $RUN, rounds: $ROUNDS) =="
curl -fsS "$BASE/health" >/dev/null

# Content A is the original draft; content B is what the racing PUT writes.
A='[{"kind":"ingredient","code":"sugar","quantity":10,"unit":"g"}]'
B='[{"kind":"ingredient","code":"sugar","quantity":99,"unit":"g"}]'

mk_draft() { # $1=recipe code
  curl -fsS -X POST "$BASE/recipes" -H 'Content-Type: application/json' \
    -d "{\"code\":\"$1\",\"name\":\"$1\"}" >/dev/null
  curl -fsS -X POST "$BASE/recipes/$1/versions" -H 'Content-Type: application/json' \
    -d "{\"version\":1,\"items\":$A}" >/dev/null
}

fire() { # $1=method $2=url $3=output body file $4=optional json body -> prints http code
  if [ $# -ge 4 ]; then
    curl -s -o "$3" -w '%{http_code}' -X "$1" "$2" -H 'Content-Type: application/json' -d "$4"
  else
    curl -s -o "$3" -w '%{http_code}' -X "$1" "$2"
  fi
}

put_won=0; publish_won_put=0
echo "== part 1: PUT vs publish ($ROUNDS rounds) =="
for i in $(seq 1 "$ROUNDS"); do
  code="$RUN-put-$i"
  mk_draft "$code"
  fire PUT    "$BASE/recipes/$code/versions/1"         "$TMP/put.json" "{\"items\":$B}" > "$TMP/put.code" &
  fire POST   "$BASE/recipes/$code/versions/1/publish" "$TMP/pub.json"                  > "$TMP/pub.code" &
  wait
  put_code=$(cat "$TMP/put.code"); pub_code=$(cat "$TMP/pub.code")
  get_code=$(fire GET "$BASE/recipes/$code/versions/1" "$TMP/get.json")

  PUT_CODE=$put_code PUB_CODE=$pub_code GET_CODE=$get_code BODY="$TMP/get.json" python3 - <<'PY'
import json, os, sys
put, pub, get = os.environ["PUT_CODE"], os.environ["PUB_CODE"], os.environ["GET_CODE"]
body = json.load(open(os.environ["BODY"]))
assert pub == "200", f"publish must always succeed here, got {pub}: {body}"
assert get == "200", f"published version must be readable, got {get}"
assert body["status"] == "published", f"version must end up published: {body}"
q = body["items"][0]["quantity"]
if put == "409":
    # Publish committed first: the published row keeps its original content.
    assert q == 10, f"PUT lost but published content changed: {body['items']}"
elif put == "200":
    # PUT committed first: publish froze the updated draft.
    assert q == 99, f"PUT won but published content is not the update: {body['items']}"
else:
    sys.exit(f"unexpected PUT status {put}")
PY
  if [ "$put_code" = "200" ]; then
    put_won=$((put_won + 1))
  else
    publish_won_put=$((publish_won_put + 1))
  fi
done
echo "   ok: PUT committed first $put_won x, publish committed first $publish_won_put x"

delete_won=0; publish_won_del=0
echo "== part 2: DELETE vs publish ($ROUNDS rounds) =="
for i in $(seq 1 "$ROUNDS"); do
  code="$RUN-del-$i"
  mk_draft "$code"
  fire DELETE "$BASE/recipes/$code/versions/1"         "$TMP/del.json" > "$TMP/del.code" &
  fire POST   "$BASE/recipes/$code/versions/1/publish" "$TMP/pub.json" > "$TMP/pub.code" &
  wait
  del_code=$(cat "$TMP/del.code"); pub_code=$(cat "$TMP/pub.code")
  get_code=$(fire GET "$BASE/recipes/$code/versions/1" "$TMP/get.json")
  recipe=$(curl -fsS "$BASE/recipes/$code")

  DEL_CODE=$del_code PUB_CODE=$pub_code GET_CODE=$get_code \
    BODY="$TMP/get.json" RECIPE="$recipe" python3 - <<'PY'
import json, os, sys
delc, pub, get = os.environ["DEL_CODE"], os.environ["PUB_CODE"], os.environ["GET_CODE"]
versions = json.loads(os.environ["RECIPE"])["versions"]
if delc == "204":
    # Delete committed first: publish must fail and leave no snapshot behind.
    assert pub == "404", f"publish must fail after delete, got {pub}"
    assert get == "404", f"deleted version must stay gone, got {get}"
    assert versions == [], f"no version rows may remain: {versions}"
elif delc == "409":
    # Publish committed first: the published row is immutable and survives.
    assert pub == "200", f"publish must succeed when it wins, got {pub}"
    assert get == "200", f"published version must be readable, got {get}"
    body = json.load(open(os.environ["BODY"]))
    assert body["status"] == "published", body
    assert body["items"][0]["quantity"] == 10, f"published content changed: {body['items']}"
    assert len(versions) == 1, versions
else:
    sys.exit(f"unexpected DELETE status {delc}")
PY
  if [ "$del_code" = "204" ]; then
    delete_won=$((delete_won + 1))
  else
    publish_won_del=$((publish_won_del + 1))
  fi
done
echo "   ok: DELETE committed first $delete_won x, publish committed first $publish_won_del x"

echo "== all invariants held across $((ROUNDS * 2)) races =="
