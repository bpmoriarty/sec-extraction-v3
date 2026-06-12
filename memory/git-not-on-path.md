---
name: git-not-on-path
description: On this corporate machine git is installed but not on the PowerShell PATH
metadata:
  type: project
---

On the Morningstar corporate machine, `git` is NOT on the default PowerShell PATH —
calling `git` directly fails with `CommandNotFoundException`. The executable lives at
`C:\Users\bmoriar\AppData\Local\Programs\Git\cmd\git.exe` (installed via winget,
git 2.54.0).

**Why:** winget installed it but didn't persist the PATH entry for non-interactive shells.

**How to apply:** prepend it for the session before any git command:
`$env:Path = "C:\Users\bmoriar\AppData\Local\Programs\Git\cmd;" + $env:Path`
Then `git ...` works normally (credentials are saved via Git Credential Manager;
`master` tracks `origin/master`).
