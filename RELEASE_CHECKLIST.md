# Release Checklist

Use this checklist for each public release.

## 1) Prep

- [ ] Pull latest `master`
- [ ] Confirm working tree is clean (`git status`)
- [ ] Decide next version (e.g. `v0.3.0`)

## 2) Quick Validation

- [ ] Create/activate virtual environment
- [ ] Install/update dependencies from `requirements.txt`
- [ ] Launch app (`python main.py`)
- [ ] Smoke test core flow:
  - [ ] Scan local mod folder
  - [ ] Look up mods
  - [ ] Check for updates
  - [ ] Verify at least one update action/path works (direct or browser fallback)

## 3) Docs + Notes

- [ ] Update `README.md` if setup/behavior changed
- [ ] Add short release notes (what changed, known limitations)
- [ ] Confirm `PROJECT_NOTES.md` next-steps are still accurate

## 4) Version + Tag

- [ ] Commit final release changes
- [ ] Create annotated tag (example):
  - `git tag -a v0.3.0 -m "v0.3.0"`
- [ ] Push branch and tags:
  - `git push origin master`
  - `git push origin --tags`

## 5) GitHub Release

- [ ] Create a GitHub Release from the new tag
- [ ] Paste release notes summary
- [ ] Mark as pre-release if not fully stable

## 6) Post-Release

- [ ] Verify release page and tag are visible
- [ ] Open a follow-up issue for any deferred WIP items
- [ ] Start next dev cycle (bump notes/changelog as needed)
