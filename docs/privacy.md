# Privacy

The bridge runs on the Mac and stores its queue, normalized message text, and downloaded images locally. Those files can contain sensitive material.

Recommended practice:

- Use a dedicated runtime directory with owner-only permissions.
- Allow exactly one sender ID.
- Delete old media and message logs on a schedule appropriate to your risk model.
- Review Git staging before every commit.
- Do not place runtime data inside a synced or public repository.
- Review generated text for sensitive data before relying on automatic replies.

The bridge returns the final answer for the injected turn. Codex analysis, tool traces, and local approval prompts are not intentionally forwarded.
