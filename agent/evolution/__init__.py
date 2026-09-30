"""Autonomous self-evolution (Phase 11): diagnose → patch in a shadow worktree → test → merge → canary.

* ``diagnostics``: finds code-level bottlenecks in telemetry and proposes patches.
* ``evolver``: scope boundaries and the worktree/test/fast-forward workflow.
* ``hot_reload``: graceful re-exec that keeps the webhook socket, the canary and rollback.
* ``task``: the ``evolve_code`` engine task and its gates.
* ``log``: the audit log, ``data/evolution_log.db``.

Off unless ``ENABLE_AUTONOMOUS_CODE_EVOLUTION=true``.
"""
