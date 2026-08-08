"""Session-level state and indexes.

This package owns two related pieces of server-side session state:

* :class:`SessionIndex` — the in-memory derived per-session summary used to
  answer the HTTP ``/sessions`` routes. It is fed by ``GatewayCore`` after
  every successful ``EventStore.append``.
* :class:`SessionLifecycle` — a small registry of currently-open sessions
  (one per ``GatewayCore`` in the WS handler). It is the source of truth
  for ``/status``'s ``activeSessions`` count.

The two are deliberately separate: the index knows about *past* sessions
(durable-derived data), the lifecycle knows about *current* connections
(transient liveness). Conflating them is a future YAGNI.
"""
