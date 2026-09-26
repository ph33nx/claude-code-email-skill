---
name: email
description: Read and answer a real mailbox over IMAP and SMTP from the terminal - search it, list new mail, read one conversation trimmed to what a reply needs, save a markdown reply to the mailbox Drafts folder threaded under the message it answers, list drafts, and send a draft. Use when asked to check mail, find someone's or a ticket's emails, read a thread, draft or save a reply, or send an email. Never send without the user's explicit word for that message.
---

# email

`M="python3 ${CLAUDE_SKILL_DIR}/mail.py"` (in an agent that does not expand that variable: the folder holding this SKILL.md), a compact wrapper over the himalaya CLI (v2). Output is short by default; `-v` widens it. Setup, accounts and gotchas: the repository README.

## Commands

Every command takes `-a NAME` (account; default account when omitted) and `-v` (full dates, full Cc lists, From and threading headers), before or after the command.

| Need | Command |
|---|---|
| New since last run | `$M new` (first run: unread). `--unanswered`, `--since YYYY-MM-DD`, `--all` (show senders in `~/.config/mail/<account>.ignore`) |
| Find | `$M search <query>`: `from:X`, `to:X`, `subject:X`, `body:X`, an address, a ticket id, or text. `--limit N` (20), `--mailbox sent` |
| One conversation | `$M thread <inbox:UID \| sent:UID \| ticket \| address>`, oldest first. `--last N`, `--cap N` (1000 chars a message), `--full` |
| One message | `$M read <box:UID>`. `--cap N`, `--full` |
| Save a reply | `$M draft reply.md --reply <box:UID>` (stdin when no file). Reply-all. `--dry-run` saves nothing; `--replace drafts:UID` swaps a revision in; `--no-cc`, `--to`, `--cc`, `--subject` override; `--force` (see rules) |
| Save a new message | `$M draft msg.md --to A --subject S` |
| Drafts waiting | `$M drafts` |
| Send | `$M send drafts:UID` |

## Output

```
09-24 18:08 *inbox:1820 Jane Doe <jane@example.com> Re: TICKET-123 | Subject
09-25 15:39  sent:260 to jane@example.com Re: TICKET-123 | Subject
(11 new, 51 hidden)
--- 09-24 18:08 inbox:1820 jane@example.com -> support@example.com +4 cc
```

`*` unread, `A` answered, blank read and unanswered. UTC. `box:UID` is the id every command takes. `draft` prints `drafts:UID` then To, Cc, Subject. `send` prints one line.

## Rules

- **Never send without the user's "send" for that exact message.** "Save" means `draft`. Never batch-send.
- **Write the reply once, into a file the user reviews; revise with small edits, then `draft --replace`.** Never reprint it in chat. Markdown in the file arrives as rich text.
- **Reply to the LATEST message of the conversation.** Recipients come from the message answered, so an older one re-adds people since dropped. `draft --reply` refuses when a later message exists and names it; `--force` only on the user's word. After our own last message, `--reply sent:UID` follows up.
- **Read the To and Cc lines `draft` prints** before telling the user it is saved.
- **Never append to Sent by hand**: `send` files the copy and flags the answered message `A`.
- **Reading never marks mail read.**
- `search`/`thread` by address match From and To only; a form that carries the address in its body: `body:` or the ticket id.
- Exit 1 prints `error: ...`; report it, do not loop.
