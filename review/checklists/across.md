### Checklist: across repositories

1. **Ownership.** Dotfiles owns executables, Agentbot owns agent-facing
   integration (each repository's `AGENTS.md` says so). Look for one product
   doing the other's job: Dotfiles writing agent configuration, Agentbot
   installing a binary, or a vendor installer that one of them runs writing
   files the other owns.
2. **Shared code.** Both consumers load `dotfiles-shared`; a function either
   one calls must exist with the same signature in the version the
   `CONTRACT` pins. Look for a consumer that copies shared code instead.
3. **The four clients.** Claude Code, Codex, Cursor CLI and the Antigravity
   CLI should get the same skills, MCP servers, policy and safety settings,
   except where the docs record a reason. List any client that is missing
   something the others have, or has something they do not.
4. **Update path.** `dotfiles full-update` runs the Dotfiles update and then
   Agentbot's; check that a failure in one is reported and does not leave the
   other half-applied, and that the order is right (an executable updated
   before its configuration is refreshed).
5. **Docs that cross repositories.** Commands, paths and flags one
   repository's docs say about another must still be true.
