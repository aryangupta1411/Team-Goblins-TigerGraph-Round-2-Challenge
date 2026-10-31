# Delta mode: append a month to the base snapshot

The final round ships one more month of the same bank (transactions, identity, a replaced closed-case history, a new case
pack). Delta mode layers that month on the checked-in base snapshot (`data/hhgoa_engine.duckdb`, July to December 2016) **without
the Round 1 raw CSVs**, re-derives everything with the ETL's own SQL, and writes only the rows that change, in the format
`graph/load_all.py` already uploads. Nothing here touches the graph; loading is a separate, explicit step (below).

```bash
# put transactions_*.csv, identity_*.csv, closed_cases_history.csv and cases/case_pack_final.csv in data/raw/round2/
make delta                      # build + gates + export + engine facts DB, about 10 seconds
make delta SCORE=none           # leave the month's cms_p at -1 (no lightgbm needed)
make delta-build / delta-export # the two halves
MONTH_DIR=/path/to/files make delta
```

`SCORE=frozen` (the default) scores the month with the Round 1 scorer, unchanged (`data/models/lgb_fraud.txt`, isotonic
calibrator, same feature frame): the month has no labels, so nothing is retrained. LightGBM needs the OpenMP runtime; on a Mac
without `brew install libomp` the tool re-runs itself once with the copy scikit-learn bundles and says so.

## What the build does

1. `txc` = the base rows (card4 / card6 rebuilt from `card_feat`) + the month's rows through `etl.features.TXC_SQL`. A month row
   that matches no base card is a FAIL: delta mode cannot add cards.
2. `etl.features.build_all(rebuild_txc=False)` re-derives `txn_feat`, `burst_member`, `device_profile`, `card_feat` from that union.
   The prior-history columns of every base row cannot change (they only look backwards); the gates check that they do not.
3. `etl.parse_closed_cases.parse` parses base + appended closed cases (two new analyst-review templates, see below).
4. The month is scored (or marked `cms_p = -1`, `cms_fold = 'unscored'`); base rows keep their stored scores.
5. Gates run. Any FAIL stops the build's use: `export` refuses it.

The work DB (`data/hhgoa_v2.duckdb`) is ETL-shaped, so `engine.facts_from_etl` builds the engine's facts DB for base + month from
it unchanged (`data/hhgoa_engine_r2.duckdb`; the base file is never overwritten).

## Gates

| gate | FAIL when |
|---|---|
| `base_snapshot` | the checked-in base is not the Round 1 snapshot (counts), or its tables do not re-derive the 18 workspace counts |
| `replay_fidelity` | the base replayed through `etl.features` differs from the shipped `card_feat` / `device_profile` / `txn_feat` (order of equal-ranked `recurring_amounts` entries is ignored) |
| `month_inputs` | a month row has no base card, an id already exists, a value is unreadable, the month does not start after the base ends, a card network disagrees with the card's, identity rows are orphaned |
| `closed_case_history` | the replaced history drops or rewrites a base case, appended ids are not contiguous, a base note parses to different template counts |
| `history_invariance` | any derived column of a base transaction changes (except `ring_hit`, whose flips must each trace to a card whose ring device changed), a base NEXT edge changes, a card or device stops being append-only |
| `case_pack` | a flagged transaction or card does not exist, the pack's customer differs from the transaction's |
| `scorer` | never fails: reports how the frozen model sees the month |

Export adds `delta_totals` (base counts + new rows = the month's totals for all 18 vertex and edge types), `roundtrip`
(base + delta = month, month - delta + undo = base, nothing removed), `loader_format`, `embed_masking` and `patches`.

## What `export` writes (`data/out_delta/`)

| file | content |
|---|---|
| `customer.csv card.csv device_profile.csv email_domain.csv billing_region.csv closed_case.csv` | new + changed vertex rows, the exact lines a full export would hold |
| `txn_000.csv next.csv` | the month's transactions and the NEXT edges they add (each card's first month row chains onto its last base row) |
| `involves.csv on_card.csv connected_to.csv matches.csv` | edges of the appended closed cases |
| `patch_transaction_ring_hit.tsv` | `id<TAB>ring_hit` for the base transactions whose flag flips; a partial update by id, never a full row (the base's `c_counts` / `d_deltas` / `m_flags` exist only in the graph) |
| `closed_case_embed.jsonl` | masked texts of the appended cases, input to `rag.embed` |
| `undo/` | the base's line of every changed row, `new_vertices.json` (what to delete), the base `ring_hit` values |
| `delta_manifest.json`, `manifest.json`, `load_plan.md` | provenance (input hashes, commit, scorer), expected live counts after the load, the loader-style row/column manifest, the steps |

`delta_digest` in the manifest is a hash of every written file: two builds from the same inputs give the same digest.

## Before loading: back up what only the graph holds

Every `Transaction` attribute except `c_counts`, `d_deltas` and `m_flags` (the Vesta C, D and M columns as JSON) is in the checked-in
base snapshot; those three came from Round 1's raw CSVs, which delta mode does not have. `make backup-txn-json` copies them out of the
live graph (**reads only**: the graph is reached through `ops.backup_txn_json.ReadOnly`, which refuses every non-read call):

```bash
make backup-txn-json            # data/backup/txn_json.tsv.gz + txn_json_manifest.json (git-ignored)
```

It asks for the transactions in `ts` windows (a vertex listing with epoch-second filters; DATETIME filters take integers) and the base
snapshot says how many each window must hold, so a short or padded answer is an error. Then it reads the file back and checks that the
ids equal the snapshot's, every value is valid JSON with the expected keys in the expected order, a random sample re-read through
`getVerticesById` is identical, and the Transaction DDL has no attribute that is in neither the snapshot nor the backup. It also checks
that the month's new Transaction lines write the three columns the way the graph already holds them. An existing backup is never replaced
by a different one: if the graph's values changed, the new copy is written beside it and the run fails.

Restore, if a load ever damages those columns: a partial vertex upsert by id (`{id: {c_counts, d_deltas, m_flags}}` through REST), never the
loading jobs. That path is not automated here.

## Loading it (not done by this tool)

0. `make backup-txn-json` (once, before the first write)
1. `uv run python graph/load_all.py --data-out data/out_delta --only <the stems in load_plan.md> --skip-counts` (upserts; `load_all` also
   re-creates the 18 loading jobs, as it does on every run)
2. apply `patch_transaction_ring_hit.tsv` by id
3. re-run the algorithms (`make algos`): a Card upsert writes the ETL defaults into `wcc_id` / `device_degree` / `fraud_ppr`
4. embed `closed_case_embed.jsonl` with the same model as the existing vectors (`bge-large`, 1024-d) and load the new vectors
5. compare the live counts with `expected_after` in the manifest

Undo: delete the vertices in `undo/new_vertices.json` (their edges go with them), upsert the `undo/*.csv` lines, restore the
`ring_hit` values, re-run the algorithms.

## Limits

* Append-only: no new cards or customers, no removed rows, nothing earlier than the base's last transaction.
* No retraining, and no raw V / C / D columns for the base months (the checked-in snapshot has none).
* Whole-history attributes (`Card.known_device_ids`, `recurring_amounts`, `DeviceProfile.n_fraud_cases`, `is_strong`, `Transaction.ring_hit`)
  are recomputed over base + month, so a case opened early in the month sees later activity in them unless the query or the agent
  time-boxes them. `agent/mcp_client.py` withholds the whole-history card statistics it can detect but treats `known_device_ids` and
  `recurring_amounts` as always all-time; the entries of `known_device_ids` carry `first_seen`, so a consumer can filter on it.
* The two closed cases that close on 2017-01-01 (CC-5569, CC-5576) postdate January cases opened earlier that day; the retrieval
  that uses them must time-box on `closed_at`.

## Tests

`tests/unit/test_delta.py` needs only the checked-in base: it proves the base replay equals the shipped tables, then builds and
exports a synthetic month (40 cards x 4 transactions) and checks the gates, the totals, the undo pack, determinism, and that a month
with an unknown card, a colliding id or a rewritten base case fails closed and cannot be exported.
