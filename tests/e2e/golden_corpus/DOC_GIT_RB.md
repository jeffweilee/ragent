# Git Rebase

`git rebase` re-applies a series of commits onto a different base commit.
Compared to `git merge`, rebase produces a linear history without the
extra merge commits, at the cost of rewriting commit hashes for the
moved commits.

## Configuring a Rebase

Common forms:

```bash
git rebase main             # replay current branch onto main
git rebase -i HEAD~5        # interactive rebase of the last 5 commits
git rebase --onto target upstream branch
```

Interactive rebase opens an editor where each commit can be `pick`,
`reword`, `edit`, `squash`, `fixup`, or `drop`. Use `git config
rebase.autoSquash true` so `fixup!` and `squash!` commits line up
automatically.

## Best Practices

- Rebase your own feature branch onto the latest `main` before opening a
  PR; never rebase a branch other people are working on.
- Squash noisy WIP commits before merge so the main-line history reads
  as logical units.
- Use `git pull --rebase` instead of `git pull` to keep local history
  linear when others have pushed.
- Keep `git config push.autoSetupRemote true` so a freshly rebased
  branch pushes cleanly.

## Troubleshooting

- On a conflict, edit the conflicted files, `git add` them, then
  `git rebase --continue`.
- `git rebase --abort` returns to the pre-rebase state.
- If you rebased a published branch, force-push with
  `git push --force-with-lease` so a teammate's intervening commit is
  not silently overwritten.
