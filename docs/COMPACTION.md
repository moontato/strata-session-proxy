# History rewrites, disk reuse, and repeated compaction: investigation

No deployment data, live requests, or private sessions were inspected. This investigation uses
public source and synthetic loopback mocks. The proxy contains **no client-specific compaction
recognition**, marker, header, message transformation, or API conversion.

## Findings: proven vs not proven

**Proven in this proxy:**

* Hashes are cumulative, ordered, length-framed SHA-256 chains. Retained recent messages at a new
  position after a changed summary have different hashes. They do **not** match as a KV suffix.
  Shared system instructions alone are never an eligible identity anchor.
* The previous disk selector accepted *any* shared user-bearing prefix. Strata's disk file does
  **not** contain a checkpoint at every shared message boundary. A partial overlap with a large
  saved request therefore could select a file whose only saved checkpoint lies after the divergence.
  Its successful RESTORE transfers a file, not proof that the next prompt can reuse any of it.
* The same file/input pair could be restored again after confirmed zero reuse. There was no
  bounded negative cache. Replays of a currently active input could also restore older copies
  unnecessarily, despite already having that input's current state in the slot.
* Missing/invalid cache telemetry was converted to zero, conflating unknown reuse with a miss.
* Postflight active identity is replaced by the compacted request after a successful, correlated
  inference, regardless of restore outcome. Normal subsequent full-history continuations then
  skip snapshot I/O. No stale original identity was found in this path.

**Not established by the reported aggregate counters or save/restore logs:**

* Which incoming histories or saved checkpoints caused the deployment's operations. In particular,
  a normal summary-first rewrite that changes the first user message already has zero overlap
  with the original full history in the previous code. That basic case passes both versions.
* That restoring a large file caused the frontend to believe the shorter prompt was still full.
  Inspected Strata code reconciles its state against the incoming prompt and reports that prompt's
  length, not the restored file's length. We did not reproduce stale occupancy in the mock contract.
* That any frontend compaction bug, configuration, version-specific usage handling, repeated
  regeneration, changing request scope, system/tool changes, or backend interference occurred.
  These remain hypotheses until separately approved, privacy-conscious live qualification.

Reproductions against the pre-fix implementation show unnecessary restores for rewrites that
retain an initial user instruction before replacing older history, even though their recent
messages also match; repeated zero-reuse pairs cause repeated full reads; active replays restore
old copies. Both streaming and nonstreaming regressions now pass. These are generic history
patterns, **not a claim that the deployed client emitted one of these exact patterns**.

## Pi source review (research only, no integration)

Public source inspected at Pi commit
[`cedefc53264f521f5a55be61b746a39c6ada8d4e`](https://github.com/earendil-works/pi/tree/cedefc53264f521f5a55be61b746a39c6ada8d4e).
Installed Pi 1.3.0 compaction/session documentation was also read; the deployment version is unknown.

* [Compaction documentation](https://github.com/earendil-works/pi/blob/cedefc53264f521f5a55be61b746a39c6ada8d4e/packages/coding-agent/docs/compaction.md)
  describes a summary followed by retained recent messages, default `keepRecentTokens` 20K,
  and summary requests as separate LLM operations. These operations can legitimately switch
  the backend away from the working conversation; the proxy does not special-case them.
* [`buildContextEntries`, session-manager.ts:458–511](https://github.com/earendil-works/pi/blob/cedefc53264f521f5a55be61b746a39c6ada8d4e/packages/coding-agent/src/core/session-manager.ts#L458-L511)
  selects the latest compaction entry plus the kept entries and later messages. This is not an
  append-only extension of the old model input.
* [`messages.ts:169–184`](https://github.com/earendil-works/pi/blob/cedefc53264f521f5a55be61b746a39c6ada8d4e/packages/coding-agent/src/core/messages.ts#L169-L184)
  converts the summary to a user-role message. It changes the history prefix; retained tool
  results/thinking/messages do not recover their old token positions.
* [`compaction.ts:140–269`](https://github.com/earendil-works/pi/blob/cedefc53264f521f5a55be61b746a39c6ada8d4e/packages/coding-agent/src/core/compaction/compaction.ts#L140-L269)
  calculates context from assistant usage and trailing estimates, with a projected-context path
  that invalidates usage captured before a later compaction/edit. The threshold is context
  window minus a reserve. This source does not prove behavior of an unspecified older version.
* [OpenAI usage parsing](https://github.com/earendil-works/pi/blob/cedefc53264f521f5a55be61b746a39c6ada8d4e/packages/ai/src/api/openai-completions.ts#L1527-L1565)
  separates cached reads from prompt input rather than treating a disk snapshot's size as usage.
  The proxy does not add its private catalog counts to these responses.

## Strata state and context accounting

The pinned Strata revision remains the one in [UPSTREAM.md](UPSTREAM.md). In particular:

* [`conversation_file.cpp:1120–1156`](https://github.com/Niko1221/Strata/blob/e88ee39d1d6be6e3364183af3b39b62a7ed6f211/src/core/conversation_file.cpp#L1120-L1156)
  selects one deepest checkpoint (deepest **pinned** checkpoint if there is a pin), not every
  root/message checkpoint. Prefix overlap is not evidence that the file contains a usable rewind.
* [`generate.cpp:12154–12238`](https://github.com/Niko1221/Strata/blob/e88ee39d1d6be6e3364183af3b39b62a7ed6f211/src/program/generate.cpp#L12154-L12238)
  requires exact token/image prefixes and bounds reuse by the new prompt length. It cannot reuse
  a retained suffix in a different position.
* [`generate.cpp:12569–12642`](https://github.com/Niko1221/Strata/blob/e88ee39d1d6be6e3364183af3b39b62a7ed6f211/src/program/generate.cpp#L12569-L12642)
  drops off-path checkpoints and rewrites cells from the reuse boundary, zeroing state on no match.
  On completion [live becomes the newly consumed sequence](https://github.com/Niko1221/Strata/blob/e88ee39d1d6be6e3364183af3b39b62a7ed6f211/src/program/generate.cpp#L14862-L14879).
* [`server.py:4706–4708`](https://github.com/Niko1221/Strata/blob/e88ee39d1d6be6e3364183af3b39b62a7ed6f211/serve/server.py#L4706-L4708)
  sets `prompt_tokens=len(ids)` for the incoming request; [`/slots`](https://github.com/Niko1221/Strata/blob/e88ee39d1d6be6e3364183af3b39b62a7ed6f211/serve/server.py#L6121-L6139)
  reports this prompt count. [`slot_action`](https://github.com/Niko1221/Strata/blob/e88ee39d1d6be6e3364183af3b39b62a7ed6f211/serve/server.py#L3593-L3600)
  clears it for disk operations, rather than setting it to the snapshot's token count.
* [Response usage](https://github.com/Niko1221/Strata/blob/e88ee39d1d6be6e3364183af3b39b62a7ed6f211/serve/server.py#L5327-L5337)
  uses the incoming (or last internal round's) prompt tokens plus completion tokens, not disk
  occupancy. [`last_timings`](https://github.com/Niko1221/Strata/blob/e88ee39d1d6be6e3364183af3b39b62a7ed6f211/serve/server.py#L4997-L5001)
  and [`request_timings`](https://github.com/Niko1221/Strata/blob/e88ee39d1d6be6e3364183af3b39b62a7ed6f211/serve/server.py#L5100-L5118)
  distinguish cached tokens from newly read prompt tokens.

There can be transient zero/previous status observations before a request starts, and allocated
KV capacity is not prompt usage. We found no code path in this contract that reports an old
230K snapshot as a successfully processed 20K prompt merely because that file was restored.
The mock verifies this contract, **not numerical correctness of Strata on real hardware**.
We do not alter context limits, usage, summaries or any request to hide an accounting problem.

## Corrected selection and retry policy

1. Preserve an eligible outgoing long history on a rewrite/branch, just as on other switches.
   The directional continuation rule from the branch fix is unchanged.
2. Use `Identity.reusable_prefix(saved)` to rank disk candidates: the **entire saved incoming
   history**, including a user message, must be an ordered prefix of the new input, with the
   same scope. Partial `overlap()` remains a relationship score, never a disk-selection rule.
   No arbitrary percentage/minimum token count is imposed. A small saved root can help a new
   branch if its whole input is preserved; a later snapshot beyond the branch point cannot.
3. If the verified active input is already a complete prefix of the incoming input (e.g. replay),
   preserve its generated tail when required but do not restore an older disk copy over it.
4. After a successful restore and correlated completed inference, treat reuse as known only if
   `cache_n` and `prompt_n` are nonnegative integers and sum to the observed prompt count. Missing,
   malformed, or inconsistent values are **unknown**, not zero. Same-sized stale telemetry cannot
   be ruled out atomically by these HTTP APIs; exclusive access is still required.
5. On confirmed zero reuse only, suppress that **file UUID + file digest + compatibility + backend
   boot + authenticated effective input fingerprint** pair for **10 minutes**, with a hard cap of
   **128 pairs**, in memory only. Check this before full-file hashing/restore. A different eligible
   full-input candidate may be tried on a later request, but there is no same-request restore loop.
   Expiry, process restart, replacement file, history/scope/config change or backend boot change
   permits a new attempt. Skips do not refresh expiry. No file is deleted/blacklisted for zero reuse.
6. Any positive validated count remains reported reuse; no guessed "insignificant" threshold is
   used. A one-token hit is not evidence that HDD I/O was worthwhile, nor that net energy was saved.

New counters: `restore_suppressed` (eligible pairs skipped before reads), `restore_reuse_unknown`
(successful restores without trustworthy reuse counts). `restore_without_reported_hit` now means
**confirmed zero reuse** for new operations. Older accumulated counters retain their historical
semantics; they are not rewritten. `restores` means successful file transfer, not a useful cache hit.

### Trade-off

We intentionally forgo possible partial-prefix hits when the file's checkpoint position is not
known. Some pinned checkpoints, older rewinds or shared-prefix branches might in fact be reusable,
but the HTTP API does not expose the saved checkpoint's token/image identity. The whole-input rule
is sufficient to nominate a candidate, not a guarantee of a hit: templates, assistant serialization
and engine settings can still force re-prefill. Safe misses are preferable to repeated full-file
reads of speculative candidates. Strata always performs its own compatibility validation.

## Safe upgrade of an existing installation

1. Leave automatic management disabled while reviewing this fix and the local mock tests.
   Schedule a safe pause for new client requests; let existing inference/disk operations finish.
2. Stop **only the proxy** gracefully. Do not stop/reconfigure/restart Strata as part of this fix.
   If an old snapshot operation timed out, confirm backend quiescence before the documented offline
   recovery; clearing a marker does not cancel a remote operation.
3. Install the updated code in the proxy's existing virtual environment, using that environment's
   Python/pip and your deployment checkout. For a non-editable install with unchanged dependencies:
   ```sh
   .venv/bin/python -m pip install --force-reinstall --no-deps .
   .venv/bin/strata-session-proxy --config config.local.yaml check-config
   ```
   Adjust these generic paths in private configuration, not in committed examples. `check-config`
   makes no network requests. No YAML/catalog migration or snapshot deletion is required. Keep
   the same compatibility key **unless** the model/tokenizer/engine/config actually changed.
4. Start the proxy with management still disabled if an approved validation window is not ready.
   Re-enabling management and sending a synthetic live sequence require the operator's explicit
   approval; that sequence will issue status/save/restore calls and mutate the active slot.
5. In the approved window, compare actual incoming prompt sizes, returned usage and validated
   cache counts using synthetic inputs. Do not publish raw requests/responses, catalog files or
   deployment logs. If frontend compaction repeats, capture version/configuration and redacted
   accounting under a separate approval rather than assuming these fixed selection bugs explain it.

Rollback: gracefully stop the proxy and return to management-disabled forwarding. Preserve the
catalog/snapshots privately; the file and database formats did not change. Investigation and
automated verification use local mocks, not live-server tests or deployment actions. Publishing
the code does not authorize operation of a production server.
