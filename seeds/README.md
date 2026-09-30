# seeds/

Evolvable data the agent may rewrite through `agent/evolution` (always via a shadow worktree,
the full test suite and the simulator, then a fast-forward merge you can see in `git log`).

* `copy_variants.json`: extra landing-page headline arms for the copy bandit
  (`tools/copy_bandit.py` validates every entry when it loads them).

Only `*.json` files here can evolve.
