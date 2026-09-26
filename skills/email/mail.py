#!/usr/bin/env python3
"""Token-lean mailbox commands for agents, over the himalaya CLI (v2).

Run:    mail.py [--account NAME] {search,new,thread,read,draft,drafts,send} ...
Does:   IMAP search and reads through `himalaya envelope|message|imap`; drafts are
        markdown rendered to multipart text+HTML and APPENDed to the drafts alias
        with \\Draft; send = `himalaya message send --save sent`, then the draft is
        expunged.
Writes: the drafts and sent mailboxes; a per-account `new` marker under
        $XDG_STATE_HOME/email-skill/.
Pairs:  SKILL.md (usage), README.md (setup and gotchas).

Named mail.py, not email.py: a script named email.py shadows the stdlib package.
"""
import argparse
import concurrent.futures as cf
import email
import email.policy
import html.parser
import json
import os
import re
import subprocess
import sys
import tomllib
from email.message import EmailMessage
from email.utils import formatdate, getaddresses, make_msgid, parsedate_to_datetime
from datetime import datetime, timezone
from pathlib import Path

import markdown  # pip install markdown, or apt python3-markdown

ACCOUNT = None
VERBOSE = False
PAGE = "500"
BODY_CAP = 1000
REPLY_PREFIX = re.compile(r"^\s*((re|fwd?|aw|sv|antw)\s*:\s*)+", re.I)


def die(msg):
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(1)


def him(*args, raw=False, stdin=None):
    cmd = ["himalaya", *(["-a", ACCOUNT] if ACCOUNT else []), *([] if raw else ["--json"]), *args]
    r = subprocess.run(cmd, input=stdin, capture_output=True)
    if r.returncode:
        die((r.stdout or r.stderr).decode(errors="replace").strip()[:500])
    return r.stdout if raw else json.loads(r.stdout or b"null")


def parallel(fn, items):
    with cf.ThreadPoolExecutor(max_workers=8) as ex:
        return list(ex.map(fn, items))


def config():
    """Sender address and mailbox aliases of the selected account, read from himalaya's own config."""
    env = os.environ.get("HIMALAYA_CONFIG")
    paths = [Path(env)] if env else [
        Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "himalaya/config.toml",
        Path.home() / ".config/himalaya/config.toml",
        Path.home() / ".himalayarc",
    ]
    path = next((p for p in paths if p.is_file()), None) or die("no himalaya config found")
    cfg = tomllib.loads(path.read_text())
    accounts = cfg.get("accounts", {})
    name = ACCOUNT or next((n for n, a in accounts.items() if a.get("default")), None)
    acct = accounts.get(name) or die(f"account {name!r} not in {path}")
    aliases = {k.lower(): v for k, v in cfg.get("mailbox", {}).get("alias", {}).items()}
    aliases.update({k.lower(): v for k, v in acct.get("mailbox", {}).get("alias", {}).items()})
    for need in ("sent", "drafts"):
        aliases.get(need) or die(f"account {name!r} needs mailbox.alias.{need}")
    addr = acct.get("email") or acct.get("from") or die(f"account {name!r} needs email")
    return {"name": name, "email": addr, "display": acct.get("display-name") or acct.get("from-name"),
            "native": lambda box: aliases.get(box, box)}


# ── envelopes and ids ───────────────────────────────────────────────

def parse_id(ref, default_box="inbox"):
    box, _, uid = ref.rpartition(":")
    uid.isdigit() or die(f"bad message id {ref!r}, expected BOX:UID like inbox:1831")
    return (box or default_box).lower(), uid


def who(addrs):
    if not addrs:
        return "?"
    a = addrs[0]
    more = f" +{len(addrs) - 1}" if len(addrs) > 1 else ""
    return (f"{a['name']} <{a['email']}>" if a.get("name") else a["email"]) + more


def utc(e):
    try:
        return datetime.fromisoformat(e.get("date") or "").astimezone(timezone.utc)
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)


def envelope_line(box, e):
    flags = {f.get("iana") for f in e.get("flags", [])}
    unseen = "A" if "answered" in flags else " " if "seen" in flags else "*"
    peer = f"to {who(e.get('to'))}" if box in ("sent", "drafts") else who(e.get("from"))
    when = f"{utc(e):%Y-%m-%d %H:%MZ}" if VERBOSE else f"{utc(e):%m-%d %H:%M}"
    return f"{when} {unseen}{box}:{e['id']} {peer} {(e.get('subject') or '')[:200 if VERBOSE else 70]}"


def search_boxes(query, boxes=("inbox", "sent"), size=PAGE):
    """[(box, envelope)] for a shared-DSL query across mailboxes, newest first."""
    q = f"{query} order by date desc"
    found = parallel(lambda b: [(b, e) for e in him("envelope", "search", "-m", b, "-s", size, q)["envelopes"]], boxes)
    return sorted((x for f in found for x in f), key=lambda x: utc(x[1]), reverse=True)


def quote(s):
    return '"' + s.replace('"', "").strip() + '"'


def to_query(q):
    m = re.match(r"^(from|to|subject|body):(.+)$", q, re.S)
    if m:
        return f"{m[1]} {quote(m[2])}"
    if "@" in q and " " not in q:
        return f"from {quote(q)} or to {quote(q)}"
    return f"subject {quote(q)} or body {quote(q)}"


# ── reading ─────────────────────────────────────────────────────────

class HtmlText(html.parser.HTMLParser):
    """HTML to plain text; drops style, script and quoted history (blockquote)."""
    SKIP = {"style", "script", "head", "blockquote"}
    BREAK = {"br", "p", "div", "li", "tr", "h1", "h2", "h3", "h4", "table"}

    def __init__(self):
        super().__init__()
        self.out, self.skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1
        elif tag in self.BREAK:
            self.out.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP:
            self.skip = max(0, self.skip - 1)
        elif tag in self.BREAK:
            self.out.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.out.append(data)


def html_to_text(s):
    p = HtmlText()
    p.feed(s)
    return re.sub(r"[ \t\xa0]+", " ", "".join(p.out))


# A reply header carries a date and, in any client language, ends in "wrote:" or in "<address>:",
# sometimes wrapped over two lines.
QUOTE_HEAD = re.compile(r"^(On .+wrote:|Le .+a écrit ?:|.+\S+@\S+>? ?:)$", re.S)
CUT = re.compile(r"^(-{2,} ?Original Message ?-{2,}|_{10,}|-{5,} ?Forwarded message ?-{5,}|Sent from my .+|Replying to \S+@\S+ on .+)$", re.I)
OUTLOOK_FROM = re.compile(r"^\*?(From|De|Von):\*? ")
OUTLOOK_NEXT = re.compile(r"^\*?(Sent|Date|Envoyé|Gesendet|Enviado):\*? ")


def trim(text):
    """Only what this message said: quoted history, signature and mobile footers cut."""
    lines = text.replace("\r", "").split("\n")
    out = []
    for i, line in enumerate(lines):
        s = line.strip()
        if line.rstrip(" ") == "--" or CUT.match(s):
            break
        nxt = lines[i + 1].strip() if i + 1 < len(lines) else ""
        if any(QUOTE_HEAD.match(x) and re.search(r"\d", x) for x in (s, f"{s} {nxt}")):
            break
        if OUTLOOK_FROM.match(s) and any(OUTLOOK_NEXT.match(x.strip()) for x in lines[i + 1:i + 4]):
            break
        if s.startswith(">"):
            continue
        out.append(line.rstrip())
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()


def body_and_attachments(msg):
    plain = htm = None
    attachments = []
    for part in msg.walk():
        if part.is_multipart():
            continue
        name = part.get_filename()
        if name or part.get_content_disposition() == "attachment":
            size = len(part.get_payload(decode=True) or b"")
            attachments.append(f"{name or 'unnamed'} ({max(1, size // 1024)} KB)")
        elif part.get_content_type() == "text/plain" and plain is None:
            plain = part.get_content()
        elif part.get_content_type() == "text/html" and htm is None:
            htm = part.get_content()
    return (plain if plain is not None else html_to_text(htm or "")), attachments


def read_raw(box, uid):
    return him("message", "read", "-m", box, "--raw", uid, raw=True)


def fetch(ref):
    box, uid = ref
    return box, uid, email.message_from_bytes(read_raw(box, uid), policy=email.policy.default)


def msg_date(m):
    try:
        return parsedate_to_datetime(m["Date"]).timestamp()
    except (TypeError, ValueError):
        return 0


def norm_subject(s):
    return REPLY_PREFIX.sub("", s or "").strip()


def ids_in(m, *headers):
    return {x for h in headers for x in re.findall(r"<[^>]+>", str(m.get(h) or ""))}


def thread_refs(arg, cfg):
    """(box, uid, message) of every message in the conversation, across inbox and sent.
    An address or a ticket id selects every message matching it; BOX:UID follows the links."""
    if not re.match(r"^([a-z]+:)?\d+$", arg, re.I):
        return parallel(fetch, [(b, e["id"]) for b, e in search_boxes(to_query(arg))])
    _, _, anchor = fetch(parse_id(arg))
    seeds = ids_in(anchor, "Message-ID", "In-Reply-To", "References")
    subject = norm_subject(anchor["Subject"])
    # Candidates: same subject (catches replies whose References were cut to the parent),
    # plus anything whose headers cite the thread root (catches a changed subject).
    root = (re.findall(r"<[^>]+>", str(anchor["References"] or "")) or [str(anchor["Message-ID"])])[0].strip("<>")
    jobs = [lambda b=b: [(b, str(h["id"])) for h in him("imap", "search", "-m", cfg["native"](b), "--text", root)["ids"]]
            for b in ("inbox", "sent")]
    if subject:
        jobs.append(lambda: [(b, e["id"]) for b, e in search_boxes(f"subject {quote(subject)}")])
    cands = {ref for refs in parallel(lambda f: f(), jobs) for ref in refs}
    fetched = parallel(fetch, sorted(cands))
    # Keep the connected component around the anchor over Message-ID links.
    known, keep, changed = set(seeds), {}, True
    while changed:
        changed = False
        for b, u, m in fetched:
            links = ids_in(m, "Message-ID", "In-Reply-To", "References")
            if (b, u) not in keep and links & known:
                keep[(b, u)] = (b, u, m)
                known |= links
                changed = True
    return list(keep.values())


def print_messages(msgs, full, cap=BODY_CAP):
    subject = None
    for box, uid, m in msgs:
        text, att = body_and_attachments(m)
        text = text.strip() if full else trim(text)
        if not full and len(text) > cap:
            text = f"{text[:cap]}\n[... {len(text) - cap} chars cut, --cap N or --full]"
        tos = ", ".join(x for _, x in getaddresses([str(m.get("To") or "")]))
        ccs = ", ".join(x for _, x in getaddresses([str(m.get("Cc") or "")]))
        frm = getaddresses([str(m.get("From") or "")])
        when = datetime.fromtimestamp(msg_date(m), timezone.utc)
        date = f"{when:%Y-%m-%d %H:%MZ}" if VERBOSE else f"{when:%m-%d %H:%M}"
        n_cc = len([x for x in ccs.split(", ") if x])
        cc = (f" cc {ccs}" if VERBOSE else f" +{n_cc} cc") if ccs else ""
        print(f"\n--- {date} {box}:{uid} {frm[0][1] if frm else '?'} -> {tos}{cc}")
        if norm_subject(m["Subject"]) != subject:
            subject = norm_subject(m["Subject"])
            print(f"Subject: {m['Subject']}")
        if att:
            print("Attachments: " + ", ".join(att))
        print(text)


def cmd_thread(a, cfg):
    msgs = sorted(thread_refs(a.target, cfg), key=lambda x: msg_date(x[2]))
    if not msgs:
        die("no messages found")
    total = len(msgs)
    if a.last:
        msgs = msgs[-a.last:]
    print(f"{total} messages" + (f", showing last {len(msgs)}" if len(msgs) < total else ""))
    print_messages(msgs, a.full, a.cap)


def cmd_read(a, cfg):
    print_messages([fetch(parse_id(a.id))], a.full, a.cap)


# ── commands ────────────────────────────────────────────────────────

def cmd_search(a, cfg):
    hits = search_boxes(to_query(a.query), [a.mailbox] if a.mailbox else ["inbox", "sent"])
    for b, e in hits[:a.limit]:
        print(envelope_line(b, e))
    if not hits:
        print("(no matches)")
    elif len(hits) > a.limit:
        print(f"({len(hits) - a.limit} more, raise --limit)")


def ignored(e, patterns):
    frm = " ".join(x.get("email", "") for x in e.get("from") or []).lower()
    return any(p in frm for p in patterns)


def cmd_new(a, cfg):
    state = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "email-skill" / f"{cfg['name']}.uid"
    envs = him("envelope", "list", "-m", "inbox", "-s", "100")["envelopes"]
    more = False
    if a.since:
        new = [e for _, e in search_boxes(f"date {a.since} or after {a.since}", ("inbox",))]
    elif state.is_file():
        last = int(state.read_text().strip() or 0)
        new = [e for e in envs if int(e["id"]) > last]
        more = len(new) == len(envs)
    else:
        new = [e for _, e in search_boxes("not flag seen", ("inbox",))]
    # Automated senders (payment, DMARC, CI notices) listed one substring per line, per account.
    ign = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "mail" / f"{cfg['name']}.ignore"
    patterns = [] if a.all or not ign.is_file() else [x.strip().lower() for x in ign.read_text().splitlines() if x.strip() and not x.startswith("#")]
    shown = [e for e in new if not ignored(e, patterns)
             and not (a.unanswered and any(f.get("iana") == "answered" for f in e.get("flags", [])))]
    for e in shown:
        print(envelope_line("inbox", e))
    hidden = len(new) - len(shown)
    print(f"({len(shown)} new" + (f", {hidden} hidden" if hidden else "") + (", more: --since DATE" if more else "") + ")")
    if envs:
        state.parent.mkdir(parents=True, exist_ok=True)
        state.write_text(str(max(int(e["id"]) for e in envs)))


def cmd_drafts(a, cfg):
    envs = him("envelope", "list", "-m", "drafts", "-s", "50")["envelopes"]
    for e in envs:
        print(envelope_line("drafts", e))
    if not envs:
        print("(no drafts)")


def addr_list(values, exclude):
    seen, out = set(exclude), []
    for name, addr in getaddresses(values):
        if addr and addr.lower() not in seen:
            seen.add(addr.lower())
            out.append(email.utils.formataddr((name, addr)))
    return out


def cmd_draft(a, cfg):
    if a.file not in (None, "-") and not Path(a.file).is_file():
        die(f"no such file: {a.file}")
    body = (sys.stdin.read() if a.file in (None, "-") else Path(a.file).read_text()).strip() + "\n"
    me = cfg["email"].lower()
    # 998 is RFC 5322's hard line limit. At the default 78, Python RFC 2047-encodes any
    # Message-ID longer than a line (Outlook's are), and the reply no longer threads.
    msg = EmailMessage(policy=email.policy.SMTP.clone(max_line_length=998))
    if a.reply:
        box, uid = parse_id(a.reply)
        # himalaya builds the RFC 5322 3.6.4 reply headers; the parent supplies reply-all Cc.
        (_, _, parent), native = parallel(lambda f: f(), [
            lambda: fetch((box, uid)),
            lambda: email.message_from_bytes(him("message", "reply", "-m", box, uid, "--body", "", raw=True), policy=email.policy.default),
        ])
        if not a.force and box == "inbox":
            later = [f"{b}:{e['id']}" for b, e in search_boxes(f"subject {quote(norm_subject(parent['Subject']))}", ("inbox",))
                     if utc(e) > datetime.fromtimestamp(msg_date(parent), timezone.utc) and str(e["id"]) != uid]
            later and die(f"a later message exists in this conversation ({', '.join(later)}); reply to the latest, "
                          "whose recipients are current, or pass --force")
        from_me = any(x.lower() == me for _, x in getaddresses([str(parent.get("From") or "")]))
        to = addr_list([str(parent.get("To") or "")] if from_me else [str(native["To"] or "")], [me])
        cc = [] if a.no_cc else addr_list([str(parent.get("Cc") or "")] + ([] if from_me else [str(parent.get("To") or "")]),
                                           [me] + [x.lower() for _, x in getaddresses(to)])
        msg["Subject"] = a.subject or native["Subject"]
        msg["In-Reply-To"] = native["In-Reply-To"]
        msg["References"] = " ".join(re.findall(r"<[^>]+>", str(native["References"])))
    else:
        (a.to and a.subject) or die("a new message needs --to and --subject (or use --reply ID)")
        to, cc = [], []
        msg["Subject"] = a.subject
    to = a.to or to
    cc = a.cc if a.cc is not None else cc
    to or die("no recipient left once your own address is dropped; pass --to")
    msg["From"] = email.utils.formataddr((cfg["display"] or "", cfg["email"]))
    msg["To"] = ", ".join(to)
    if cc:
        msg["Cc"] = ", ".join(cc)
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=cfg["email"].rpartition("@")[2])
    msg.set_content(body)
    html_body = markdown.markdown(body, extensions=["extra", "sane_lists", "nl2br"])
    page = f'<!doctype html><html><body style="font-family:sans-serif;line-height:1.5">{html_body}</body></html>'
    msg.add_alternative(page, subtype="html")
    if a.dry_run:
        print_headers(msg)
        print("(dry run: nothing saved)")
        return
    out = him("message", "add", "-m", "drafts", "-f", "draft", "seen", stdin=bytes(msg))
    print(f"drafts:{out['id']}")
    print_headers(msg)
    if a.replace:
        ok, resp = expunge_draft(parse_id(a.replace, "drafts")[1], cfg)
        print(f"replaced {a.replace}" if ok else f"old draft {a.replace} NOT removed: {resp.strip()[-200:]}")


def print_headers(msg):
    """To, Cc and Subject always (the recipient check reads them); From and threading with -v."""
    keys = ("From", "To", "Cc", "Subject", "In-Reply-To", "References", "Message-ID") if VERBOSE else ("To", "Cc", "Subject")
    for h in keys:
        if msg[h]:
            print(f"{h}: {msg[h]}")


def expunge_draft(uid, cfg):
    """Remove one draft for good (UIDPLUS UID EXPUNGE); True when the server confirmed it."""
    drafts = cfg["native"]("drafts")
    script = f'a1 SELECT "{drafts}"\r\na2 UID STORE {uid} +FLAGS.SILENT (\\Deleted)\r\na3 UID EXPUNGE {uid}'
    resp = him("imap", "raw", "--", script, raw=True).decode(errors="replace")
    return bool(re.search(r"^a3 OK", resp, re.M)), resp


def mark_answered(msg, cfg):
    """Flag the inbox message this reply answers, so listings show it as handled."""
    parent = str(msg["In-Reply-To"] or "").strip().replace('"', "")
    if not parent:
        return
    # HEADER Message-ID matches the message itself; a text search would also hit later replies citing it.
    script = f'a1 SELECT "{cfg["native"]("inbox")}"\r\na2 UID SEARCH HEADER Message-ID "{parent}"'
    resp = him("imap", "raw", "--", script, raw=True).decode(errors="replace")
    for uid in re.findall(r"\d+", (re.search(r"^\* SEARCH([ \d]*)", resp, re.M) or [None, ""])[1]):
        him("flag", "add", "-m", "inbox", "-f", "answered", uid, raw=True)


def cmd_send(a, cfg):
    box, uid = parse_id(a.id, "drafts")
    box == "drafts" or die("send takes a draft id (drafts:UID); save the reply with draft first")
    raw = read_raw(box, uid)
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    msg["To"] or die("draft has no To")
    # Stamp the send time by editing the one header line: re-serialising could refold the rest.
    head, sep, body = raw.partition(b"\r\n\r\n") if b"\r\n\r\n" in raw else raw.partition(b"\n\n")
    head = re.sub(rb"(?mi)^Date:[^\r\n]*", b"Date: " + formatdate(localtime=True).encode(), head, count=1)
    him("message", "send", "--save", "sent", stdin=head + sep + body)
    # Sent copy filed by --save above; now remove the draft and mark what it answered.
    ok, resp = expunge_draft(uid, cfg)
    mark_answered(msg, cfg)
    tail = "filed in Sent, draft removed" if ok else f"filed in Sent, draft NOT removed: {resp.strip()[-200:]}"
    print(f"sent to {msg['To']}" + (f" cc {msg['Cc']}" if msg["Cc"] else "") + f"; {tail}"
          + (f"; {msg['Message-ID']}" if VERBOSE else ""))


def main():
    global ACCOUNT, VERBOSE
    # --account is accepted before or after the command.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--account", "-a", default=argparse.SUPPRESS,
                        help="himalaya account name; the default account when omitted")
    common.add_argument("--verbose", "-v", action="store_true", default=argparse.SUPPRESS,
                        help="full dates, full Cc lists, From and threading headers")
    p = argparse.ArgumentParser(prog="mail.py", description="Agent mailbox commands over himalaya.", parents=[common])
    sub = p.add_subparsers(dest="cmd", required=True)

    def command(name, text):
        return sub.add_parser(name, help=text, parents=[common])

    s = command("search", "one line per hit across inbox and sent")
    s.add_argument("query", help="from:X, to:X, subject:X, body:X, a ticket id, an address, or text")
    s.add_argument("--limit", type=int, default=20)
    s.add_argument("--mailbox", help="one mailbox alias instead of inbox+sent")
    s = command("new", "inbox messages since the last run (first run: unread)")
    s.add_argument("--since", metavar="YYYY-MM-DD")
    s.add_argument("--unanswered", action="store_true", help="hide messages already answered")
    s.add_argument("--all", action="store_true", help="ignore the per-account sender ignore list")
    s = command("thread", "one conversation, oldest first, trimmed")
    s.add_argument("target", help="BOX:UID, a ticket id, or an address")
    s.add_argument("--full", action="store_true", help="no trimming, no cap")
    s.add_argument("--last", type=int, metavar="N", help="only the last N messages")
    s.add_argument("--cap", type=int, default=BODY_CAP, metavar="N", help=f"chars per message (default {BODY_CAP})")
    s = command("draft", "markdown reply to drafts, threaded")
    s.add_argument("file", nargs="?", help="markdown file; stdin when omitted or -")
    s.add_argument("--reply", metavar="BOX:UID", help="message being answered")
    s.add_argument("--to", action="append", help="override or set recipients (repeatable)")
    s.add_argument("--cc", action="append", help="override Cc (repeatable)")
    s.add_argument("--no-cc", action="store_true", help="reply to the sender only")
    s.add_argument("--subject")
    s.add_argument("--dry-run", action="store_true", help="print the recipients and threading headers, save nothing")
    s.add_argument("--replace", metavar="drafts:UID", help="a revised draft: save this one, then remove that one")
    s.add_argument("--force", action="store_true", help="reply to a message even though a later one exists in its conversation")
    s = command("read", "one message, trimmed")
    s.add_argument("id", metavar="BOX:UID")
    s.add_argument("--full", action="store_true", help="no trimming, no cap")
    s.add_argument("--cap", type=int, default=BODY_CAP, metavar="N", help=f"chars per message (default {BODY_CAP})")
    command("drafts", "list drafts")
    s = command("send", "send a draft, file it in sent, remove the draft")
    s.add_argument("id", metavar="drafts:UID")
    a = p.parse_args()
    ACCOUNT = getattr(a, "account", None)
    VERBOSE = getattr(a, "verbose", False)
    cfg = config()
    {"search": cmd_search, "new": cmd_new, "thread": cmd_thread, "read": cmd_read,
     "draft": cmd_draft, "drafts": cmd_drafts, "send": cmd_send}[a.cmd](a, cfg)


if __name__ == "__main__":
    main()
