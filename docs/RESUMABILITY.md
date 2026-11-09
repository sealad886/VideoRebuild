# Resuming a recovery

Re-run the original command with the same case directory and inputs. Recovery
uses committed case state automatically; there is no separate resume command.
For example:

```bash
videorebuild run --case /healthy/recovery-case --input /recovered/files \
  --deep --search-workers 8 --max-groups 50000
```

Keep the original search and validation options when continuing an interrupted
run. `--retry` requests reconsideration of completed work; it is not required
merely to continue an unfinished ordinary recovery pass. If the original
command used `--retry`, repeating that command still resumes its unfinished pass.

Use Ctrl+C to interrupt the foreground process. A detached process can receive
SIGINT after its PID and command have been verified. Wait for the process to
exit before starting another writer for the same case. The presence of a
`.lock` file alone does not mean a process still holds the case lock.

## Work that survives an interruption

- Indexed inputs and completed source scans remain in the case.
- Ordinary preliminary matching records which recordings still need deep
  search. Completed deep jobs are skipped on an interrupted pass.
- Structural sample postings under `work/structural-index` retain completed
  discovery, including sources with no usable locations.
- Certified groups in uncapped indexed deep association retain committed
  checked locations and every matching physical location in
  `work/association-checkpoint.sqlite3`.
  Restarting can continue an unfinished association scan instead of hashing
  every earlier location again.
- Accepted outputs, mapping evidence and validation records remain subject to
  integrity verification before reuse. Interrupted publication does not turn
  incomplete output into an accepted recording.

The association checkpoint stores matching hits and the corresponding scan
position in one transaction. A committed cursor therefore never claims work
whose hits were lost. A graceful interruption preserves the last committed
window; an abrupt process exit or failed write can repeat an uncommitted window.
Completed association coverage remains available if interruption occurs before
the batch's remaining reconstruction work completes.

Association transactions normally commit at the next completed association
after 60 seconds. Set `VIDEOREBUILD_ASSOCIATION_CHECKPOINT_SECONDS` to a positive,
finite number of seconds to change that interval, for example:

```bash
VIDEOREBUILD_ASSOCIATION_CHECKPOINT_SECONDS=120 videorebuild run \
  --case /healthy/recovery-case --input /recovered/files --deep
```

Changing this interval does not invalidate existing association progress.
Transactions also commit after 250,000 newly stored matching hit rows to
bound pending database work, and at association completion. Source bytes read
and rejected locations do not trigger extra commits. Free-space checks run
independently of commits, and SQLite retains full synchronous durability.
Interruption can repeat work since the last committed transaction; one slow
association can extend the normal interval. In-progress hits are rolled back
with their cursor rather than being saved as complete work.

Association checkpoints apply to certified groups in exhaustive indexed deep
search. Groups without a structural certificate, explicit finite search limits
and raw nomination paths retain their existing retry behavior; an unfinished
bounded search is not promoted to exhaustive coverage.

## When work must repeat

Checkpoint reuse requires the same matching geometry and a matching search
scope. Changes to source identities, logical mappings, acquisition availability,
filesystem allocation restrictions, search tier, ownership or matching policy
can invalidate saved association work. An invalidated checkpoint cannot prove
that an eligible candidate is absent or unique in the new scope.

Current source identity and read permissions remain authoritative. A saved
match does not bypass source checks, physical overlap checks, encoded-byte
proofs or the requested codec validation. All physical alternatives remain
available to ambiguity and reconstruction checks.

Completed batches release their association checkpoint data. A later explicit
retry of completed work can therefore require new associations. Temporary
association databases written by older versions lack durable cursor/coverage
receipts and are not adopted as completed work by the checkpoint implementation.
The first run using checkpoint support starts association receipts from scratch
while retaining compatible case queues, source indexes and structural discovery.

## Reading progress and managing the case

Use `run.log` for the current phase and checkpoint reuse. Association locations,
anchor checks and located extents describe search work; they do not count
successfully reconstructed recordings. Reported counters are not a whole-case
completion percentage when the total amount of search work is unknown.

Keep the case directory, including its `work` directory, when preserving
progress. Each save updates the same association checkpoint database in place;
there are no historical association checkpoint copies to retain or prune. A
transient SQLite rollback journal protects the current transaction and is
removed on commit. Genuine matching locations remain in the current database
because later ambiguity and reconstruction checks need them. Checkpointing
does not impose a total case quota: source indexes, genuine matching
alternatives and reconstructed media can still require substantial space.

A process already running from a frozen zipapp continues using that runtime.
Updating source or merging a change does not add checkpoints to that process.
Use a reviewed new runtime on a subsequent restart to activate new behavior.
