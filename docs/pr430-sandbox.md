# PR #430 split: sandbox

Credentials are staged in task-private paths and mounted read-only. Legacy shell children run without root privileges/capabilities and use a task-private egress proxy when networking is requested. Proxy UID/GID matches audit ownership; symlink and hard-link audit aliases are rejected. Compose binds individual provider credential files with create_host_path disabled. The worker remains a trusted Docker broker; shared-daemon host isolation and real credential refresh are not established by synthetic tests. Revert this PR before dependent Telegram security changes if rollback is needed; do not activate deployment as part of this split.

Extraction additionally reproduced a proxy startup race: persistent shell tasks could connect before the proxy listener bound port 8080 (ConnectionRefusedError). Proxy startup now waits at most ten seconds inside the owned container; failed readiness uses existing cleanup and fails closed. This is an intentional correction to the pinned source.

The combined gate also reproduced a source regression in Poetry setup: its fixed POETRY_VIRTUALENVS_IN_PROJECT=true setting was rejected as unscoped. Only that exact non-secret key/value pair is now accepted; arbitrary values and other unscoped environment remain rejected, with regression tests.
