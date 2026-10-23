# Security

## Supported version

Only the latest tagged experimental release receives fixes.

## Reporting a vulnerability

Open a GitHub Security Advisory for issues that could expose credentials, local files, message history, or remote-control access. Do not include real tokens, user IDs, thread IDs, local paths, or private photos in a public issue.

## Local secrets

Keep `config.json`, the credential file, runtime state, logs, and downloaded media outside the repository. The included `.gitignore` is a guardrail, not a substitute for reviewing staged files before every push.
