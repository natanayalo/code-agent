# PR #430 split: github ci

pull_request metadata selects the current head. A check_run/check_suite for a different or missing head cannot overwrite current CI state. Advancing the PR head clears prior CI status to pending. This deliberately changes the old behavior where any check could select the head: a check for an unseen commit awaits the corresponding PR metadata event/redelivery. Revert to roll back; there is no migration. This forms part of the PR #430 split.
