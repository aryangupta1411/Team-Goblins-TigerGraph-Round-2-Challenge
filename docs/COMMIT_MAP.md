# Commit map: hashes before and after the 2026-11-02 history rewrite

On 2026-11-02 the commit messages in this repository's history were rewritten to remove a co-author line. Nothing else
changed: every commit keeps its files (tree), author, committer, dates and subject, so the code at each commit is
byte-identical to before. Every commit hash changed, and the tags were moved to the rewritten commits.

Records written before the rewrite keep the hashes they were written with (`runs/*/run_manifest.json` `git_sha`,
`runs/*/provenance.json`). Use this table to find the commit in this history.

Check the submitted run's commit (`c2bcb29` in `runs/hhf-final-3`, `05bc21d` here, tag `hhf-final-3-run`):

```bash
git checkout 05bc21d && git ls-files -z | xargs -0 shasum -a 256 | shasum -a 256
```

prints `77ecb484c68f15e9d9fcf3c879079d4678ec7d86dc633594b78236e586ded4b9`, the `code.list_sha256` that
`runs/hhf-final-3/provenance.json` recorded before and after the run (747 tracked files, the same contents).

| Before | After | Subject |
|---|---|---|
| `dd9928e9e6` | `2bb732d6f7` | Initial commit |
| `f87d347a07` | `8836a76a03` | FIRST PUSH |
| `813a5f1df4` | `652844f627` | Second Push |
| `85770b68bb` | `2473c3b25a` | After Live test |
| `6abd54b204` | `a93c547149` | Apply README R2 from intake and cite README rules in the engine |
| `e6d85fb76a` | `11964a2e69` | Check SAR facts and answer text before an answer is accepted |
| `5ebc6773f7` | `755e9df796` | Time-box card history and agent memory in the GSQL queries |
| `26f761a176` | `3d72189c43` | Fix the CI mock run on a clean clone and correct the docs |
| `d26c8b7ae6` | `14c7450346` | Add the tooling for a clean live re-run of the 20 cases |
| `49768d051b` | `ef5057f6a1` | Sync IMPLEMENTATION.md with the fixed sources |
| `be035338a0` | `50f227f633` | FINAL REAL TEST |
| `b4f8b292c0` | `5671b9c7d0` | LATEST TEST |
| `57be0d5de8` | `03d0f524d8` | NEW |
| `cbbb1f510b` | `d809d9aa67` | gemini based architecture |
| `8d63ce9da0` | `dc2e959fa7` | Wire UI queue runner and fix live approval graph write-backs |
| `65564d0398` | `7d710541a2` | Add Round 2 schema engine, typology detectors, RBAC hardening, and UI console |
| `7915dbef48` | `078621d6e2` | Add comment-blind AI/AST GSQL schema mapper for Bank 1/2/3 and remove detector hardcoding |
| `7de7ddc018` | `7eaa2fed39` | Add Final Round answer files (HHF-001..018) and updated Jan 2017 DuckDB tables |
| `0f42b3c049` | `36b080cf79` | Final Round: January in the graph, graph-native detectors, hardened agent, new UI |
| `b397c2c97b` | `6945275562` | Freeze for the final run: spaced-letter sanitizer fix, card layout fix, image ships hhf-final |
| `3b88ec2163` | `e4ca15f81f` | Final Round answers from run hhf-final; PR #1 queue features ported into the new UI |
| `ac9dbf8e44` | `e579275e59` | UI: console redesign and follow-up fixes to the PR #1 port |
| `8897442a9e` | `8897442a9e` | Create railway.json |
| `2ec0109ba5` | `eceea3c1d0` | Merge the Team-Goblins repo's initial railway.json; the full project's railway.json supersedes it |
| `a7fc92c468` | `159f3b2db4` | Railway: build main with the Dockerfile (the first deploy built the initial commit, which had none) |
| `5d1b1f2c85` | `1dd75fa647` | tests: skip the variant-mapper test when the raw Round 2 pack is absent (data/raw/ is git-ignored, so CI faile |
| `b63c8c88b1` | `b67723da12` | tests: skip the variant-mapper test when the raw Round 2 pack is absent (data/raw/ is git-ignored, so CI faile |
| `f023d677cc` | `68c16446b8` | UI: overview tables fit their rows, graph legend shapes, fixed layout with reset and expand, agent's notes; Ru |
| `59d48a7ece` | `a4c5288b7a` | Merge round2-final (f023d67): UI fixes and the CI test skip |
| `9cf46511f9` | `21eb4edfe7` | Live runs from the Railway deployment: full app image, namespaced write-backs, approver-gated Run console |
| `2567591a73` | `597b4df66e` | Dockerfile: fetch the embedding model with huggingface_hub (no torch at build time, no ONNX/OpenVINO copies) |
| `848927b91f` | `69c7dc8af1` | Merge round2-final (2567591): live runs from the deployment |
| `f4a5bec2f5` | `74a1801c7c` | Run console: Live/Mock per session, choice of TigerGraph workspace, uploaded or typed alerts, own-data pipelin |
| `560ade5dd2` | `00edef5bed` | Merge round2-final: Run console live targets, uploaded alerts and own-data pipeline |
| `fffe405b18` | `47228fe420` | Roles with permissions and a server admin; replay stage strip as one grid |
| `40ad6ae912` | `ac248dbdcd` | Merge round2-final: roles with permissions, server admin, replay stage grid |
| `be9e171e84` | `361f3ff37a` | Graph page: valid number format for the live hours box ("%d h" crashed the page in live mode) |
| `44e2fb0e78` | `be5bb6f9fb` | Docs: SUBMISSION.md index, live deployment, roles, current Run console, public repo URLs |
| `602c35320a` | `9c12cefd5f` | Merge round2-final: submission docs and the live graph page fix |
| `e915e822d5` | `2de8fd88e1` | Case file: plain tabs and a full-height tab panel, so switching tabs no longer scrolls the page |
| `b7f2927bc0` | `9a815d4017` | Merge round2-final: Case file tabs no longer scroll the page |
| `48dd61aade` | `7f53d577e8` | UI: open in light mode on a browser's first visit; a theme picked later in the menu is kept |
| `6ecab0c83b` | `b0c9bb5d6c` | Pipeline strip: a skipped stage says why (P6/P7 settled at P5 or no request, P8 no SAR owed) instead of 'no ro |
| `40f2c4d855` | `5e39c5b988` | Merge round2-final: skipped stages say why; light mode on first visit |
| `d552519f76` | `0149072868` | Engine: final-round verdicts follow the raw January data (HHF-008, 010, 013, 014) |
| `8f4ce5520e` | `ab83cd273e` | Delta: refuse to build or export January until the schema migration is signed (Challenge 12 gate) |
| `954784d753` | `43ccb8c035` | Containment: rank wave cards by exposure prevented after the alert, block-k curves, smallest set by stopped mi |
| `2d18afa476` | `aaa0ce7f3a` | SAR: sar.md is a fileable document (cited numbered narrative, timeline, Mermaid evidence graph, attestation fo |
| `e6aafe7281` | `7ed5f5d8f3` | Challenge 3: compile R5/R7 policy prose into SQL detectors, backtest on closed cases, rule-vs-agent on 38 case |
| `613f2b9aa9` | `7f588e356b` | Challenge 13: ops/variant_rehearsal.py maps a variant, translates it, and decides its cases next to the canoni |
| `8f0326c7d8` | `8656f61adc` | Promote hhf-final-2: the submitted Final Round answers, from 8dc9e07 on gemini-3.6-flash |
| `b6e67f9b71` | `54c3869e28` | policy_compiler: a def instead of an assigned lambda (ruff E731 failed CI) |
| `6b53a8178d` | `35bddf637e` | tests: sort imports and rename an ambiguous loop variable (ruff I001, E741 failed CI) |
| `3c561965bc` | `dfb7d4795b` | Engine: R4 decline waits on uncertain cases; graph-neighbourhood evidence; analyst feedback (Challenge 8) |
| `427bc8f841` | `1487779d4c` | Lifecycle: final approval or customer reply closes the case (close_case + CaseEvent), hand-over page, alert wa |
| `750732f76b` | `3b273ae29a` | SUBMISSION: what changed on the answer path after 8dc9e07, checked against the engine on the pushed head |
| `9ec89dd7ef` | `707969937d` | SUBMISSION: the pushed head differs from the submitted answers on HHF-008's final actions (3c56196); lint fix  |
| `96ff08f317` | `d8a76dd0e9` | Merge round2-final (9ec89dd): submission switched to hhf-final-2 |
| `c2bcb29641` | `05bc21de1e` | Challenge 13: ground-truth scoring, flat single-file variants, and a review list of unmapped / low-confidence  |
| `4fd59d46a2` | `01ca4d1cca` | Merge round2-final (c2bcb29): Challenge 13 ground-truth scoring and flat variants |
| `3152d8999d` | `09426c40bb` | Graph: icon nodes in semantic colours, the case layer in live mode, true full-screen Expand; light theme on ev |
| `44f7a34a52` | `1f82d23209` | Merge round2-final (3152d89): graph redesign, full-screen Expand, light theme on every page |
| `dc079e27f7` | `f4fa85b853` | Proof board reads the measured evidence; cost per case for hhf-final-2 on Gemini 3.6 Flash prices |
| `a29ac25b15` | `10dcac5e3f` | Challenge 13 in the UI: upload or generate a variant, attested mapping with a review list, run on variant, acc |
| `21df8c8369` | `4e0c952a12` | Merge round2-final (a29ac25): Challenge 13 UI, proof board and cost report |
| `3bfde5fa5a` | `cc339263db` | Promote hhf-final-3: the submitted Final Round answers, from c2bcb29 on gemini-3.6-flash |
| `801ed4d7d8` | `e8d3183a17` | Merge round2-final (3bfde5f): submitted run hhf-final-3 from c2bcb29, Challenges 2/3/4/7/8/9/12/13/14 |
| `f095eae1ff` | `00f59839a0` | SUBMISSION: the freeze is the final pushed commit (tag round2-freeze); answers produced at c2bcb29 (tag hhf-fi |
| `d2969756a2` | `81c26ab918` | Merge round2-final (f095eae): the freeze |
| `8dc9e0723f` | `0010621eea` | Engine: final-round verdicts follow the raw January data (HHF-008, 010, 013, 014) (reached by tag `hhf-final-2-run`) |
