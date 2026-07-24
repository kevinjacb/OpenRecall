# Investigation: extraction-worker listeners block the worker's main loop

**Date:** 2026-07-22
**Status:** Root cause analysis complete. No fix yet.
**Symptom:** "Tasks aren't being completed successfully." With P3 proactive
listeners registered, the extraction worker's main `_run` loop is
serialised — each session blocks the loop for the full listener duration
(planner timeout = 2s, plus LLM/WS roundtrips). Sessions queue up faster
than they're processed; the enqueuer fills to its 1024 capacity and starts
dropping new sessions (`EXTRACTION_QUEUE_OVERFLOW_TOTAL`).

## Phase 1 — Root cause: the dispatch path hits a serialised no-loop fallback

The production call chain is:

```
run_gateway.main_loop
  → worker.start()                              # binds the worker's loop
  → worker.add_listener(proactive_engine.on_session_completion)
  → worker._run                                 # event-loop coroutine
      while not stop:
          session = await enqueuer.get()
          await asyncio.to_thread(self._safe_process, session_id)   # ← worker thread
              _safe_process → process_session → _dispatch_listeners
                  try: asyncio.get_running_loop()
                  except RuntimeError: running_loop = None
                  for listener in self._listeners:
                      if running_loop is not None:
                          task = running_loop.create_task(listener(completion))  # not this branch
                      else:
                          asyncio.run(listener(completion))                      # ← THIS BRANCH
```

`_safe_process` runs on a worker thread. The worker thread has no
running event loop. `asyncio.get_running_loop()` raises `RuntimeError`,
`running_loop` is set to `None`, and the dispatch falls into the
no-loop branch.

The no-loop branch is:

```python
try:
    asyncio.run(listener(completion))
except Exception:
    self._metrics.increment(Metrics.EXTRACTION_LISTENER_FAILURE_TOTAL, ...)
    log.exception("extraction_listener_failed", ...)
```

`asyncio.run(listener(completion))`:
1. Creates a new event loop on the worker thread.
2. Drives `listener(completion)` to completion on that loop.
3. Tears the loop down.
4. **Returns when the listener returns — the worker thread is blocked
   for the full listener duration.**

`listener(completion)` is the proactive engine's
`on_session_completion` (see `agent/proactive.py:85-148`):
- Builds a `PlannerContext`.
- `await asyncio.wait_for(self._planner.plan(ctx), timeout=2.0)` —
  **2-second planner timeout.**
- On `RETURN`, `await self._ws_sender.send_proactive(...)`.

The planner's `plan` itself does `await self._llm.reason(prompt)`
which calls `await asyncio.to_thread(self._chat.complete, ...)` —
the LLM call goes back to the default executor on yet another worker
thread. The new loop in `_dispatch_listeners` awaits the future.
**The worker thread is blocked waiting for the executor to finish.**

The ws_sender's `send_proactive` enqueues into `ProactiveOutbox` and
calls `signal()` on the `asyncio.Event`. The Event was created at
script top-level, so it's "lazy" and binds to whatever loop first
uses it. **The first use is in this no-loop dispatch, so the Event
binds to the throwaway loop in the worker thread.** The drain task
on the main event loop calls `await proactive_outbox.wait()` and
**this may or may not work depending on which loop the Event is
bound to when the drain task calls `wait()`.** Empirically, the
drain task does receive the signal (the Event is "lazy" and binds
at first interaction), but the architecture is fragile.

## Phase 2 — Empirical confirmation

A 3-session test with a 2-second listener each:

```
3 sessions, 2s listener each:
  total elapsed: 6.01s
  listener calls: [('s0', t=0.00), ('s1', t=2.00), ('s2', t=4.00)]
  expected (parallel): ~2s; serial: ~6s
```

The listeners run serially. The default executor has many threads,
but the worker's main `_run` loop is `await to_thread` for each
session in turn. While the worker thread is busy with `asyncio.run`
on the listener (a 2s planner timeout), the next `to_thread` is not
even scheduled.

The enqueuer's capacity is 1024 (see `run_gateway.py:135`). At one
extraction per 2s (worst case with planner timeouts), the enqueuer
fills in ~33 minutes. In practice the planner is faster than 2s
on success, but the listener path is now on the critical path of
**every** completed session.

## Phase 3 — Why the no-loop branch exists

The dispatch was added in M4.3 to handle the reconcile-on-start
path (`worker.start()` calls `asyncio.to_thread(self._safe_reconcile)`
on the main thread, but the reconcile sweep also runs
`process_session` for each session, which needs to dispatch
listeners). The M4.3 commit explicitly notes that **before M4.3, the
queue loop was the only caller of `process_session` and always had
a loop, so the broken fallback was untested-and-unused.**

The M4.3 fix is in the comments at lines 286-298 of `extraction_worker.py`:
> "No-loop fallback: `process_session` is also called from
> `:meth:`start`'s reconcile sweep, which runs on a worker thread
> via `asyncio.to_thread` — there is no event loop on that thread.
> In that case we run the listener on a transient loop in the same
> thread."

The fallback was correct for the **reconcile sweep** (one-shot
best-effort catch-up at startup), but it does not generalize to
the **queue loop's** steady-state call to `_safe_process` from a
worker thread. The same `process_session` is used for both paths,
but the listener-dispatch contract is different:

- **Reconcile sweep**: one-shot, runs at startup, best-effort,
  blocking is acceptable.
- **Queue loop**: steady-state, every event-triggered session
  completion, blocking is **fatal** because it stalls the worker.

The fallback path was sized for reconcile, but it now serves the
queue loop too.

## Phase 4 — The fix

The dispatch should use the **main event loop** (the one bound to
the enqueuer via `worker.start()`) for queue-loop calls. The loop
is reachable via `self._enqueuer.bound_loop`. The change:

```python
def _dispatch_listeners(self, completion: SessionCompletion) -> None:
    # Prefer the bound loop (the main event loop) — set by
    # ExtractionWorker.start() via enqueuer.bind_loop(...). This is
    # the loop where ws_sender's Event lives, where the planner's
    # async I/O should run, and where the drain task is waiting.
    target_loop = self._enqueuer.bound_loop

    # No-loop fallback: reconcile sweep, run from a thread that has
    # no loop. Best-effort — same as today, but only used by the
    # reconcile path now, not the queue path.
    if target_loop is None:
        for listener in list(self._listeners):
            try:
                asyncio.run(listener(completion))
            except Exception:
                self._metrics.increment(
                    Metrics.EXTRACTION_LISTENER_FAILURE_TOTAL,
                    tags={"session_id": completion.session_id},
                )
                log.exception(
                    "extraction_listener_failed",
                    extra={"session_id": completion.session_id},
                )
        return

    # Main-loop dispatch: schedule each listener as a fire-and-forget
    # task on the bound loop. The worker thread is unblocked
    # immediately; the listener runs on the main loop in parallel
    # with the next session's processing.
    for listener in list(self._listeners):
        try:
            target_loop.call_soon_threadsafe(
                self._schedule_listener, listener, completion,
            )
        except RuntimeError:
            # Loop is closed (server shutdown). Drop with a counter.
            self._metrics.increment(
                Metrics.EXTRACTION_LISTENER_FAILURE_TOTAL,
                tags={"session_id": completion.session_id, "reason": "loop_closed"},
            )

def _schedule_listener(self, listener, completion) -> None:
    """Runs on the main event loop. Schedules the listener as a task
    and attaches a done-callback for exception counting."""
    task = asyncio.create_task(listener(completion))

    def _safe(t: asyncio.Task, sid: str = completion.session_id) -> None:
        try:
            t.result()
        except Exception:
            self._metrics.increment(
                Metrics.EXTRACTION_LISTENER_FAILURE_TOTAL,
                tags={"session_id": sid},
            )
            log.exception(
                "extraction_listener_failed",
                extra={"session_id": sid},
            )
    task.add_done_callback(_safe)
```

This change:

1. **Unblocks the worker thread** — `call_soon_threadsafe` returns
   immediately; the worker thread proceeds to the next session
   without waiting for the listener.
2. **Uses the correct loop** — the listener runs on the main event
   loop, where the planner's `asyncio.to_thread`, the ws_sender's
   Event, and the drain task all live.
3. **Preserves the no-loop fallback** for the reconcile sweep,
   which is genuinely best-effort and one-shot.
4. **No new threading hazards** — `call_soon_threadsafe` is
   thread-safe and the only cross-thread primitive used.

## What the test should look like

The current test (`test_listener_runs_when_process_session_called_from_a_thread`)
verifies the no-loop fallback path. The regression guard for this
bug is a **production-path test**:

```python
@pytest.mark.asyncio
async def test_listener_does_not_block_worker_thread():
    """The worker thread must not block on the listener. Enqueue N
    sessions, each with a slow listener; the worker should drain the
    queue in O(1) time (i.e. the listeners are offloaded, not
    serialised on the worker thread).
    """
    # ... build worker with a 2s listener ...
    # ... enqueue 3 sessions ...
    # ... call worker._run for ~3s total ...
    # ... assert all 3 listeners were invoked (not just one) ...
    # ... assert no EXTRACTION_QUEUE_OVERFLOW_TOTAL ...
```

## What needs user input before fixing

1. **Is this the bug the user is observing?** The user's phrasing
   was "tasks aren't being completed successfully, check how the
   tasks are ingested carefully." This analysis matches that
   description (sessions aren't being processed because the listener
   blocks the worker), but the user may have a different concrete
   failure in mind. Worth confirming with one symptom (queue
   overflow counter, slow first-N-minutes behavior, etc.) before
   committing to the fix.

2. **Is the no-loop fallback still needed?** It is, for the
   reconcile path. The fix preserves it but only as a fallback.

3. **Should the fix land in the same commit as the test?** I would
   recommend yes — the test is the regression guard for the bug.
   Without the test, the next refactor of `_dispatch_listeners`
   would silently re-introduce the bug.
