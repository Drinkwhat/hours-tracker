#!/usr/bin/env python3
"""Work-hours log: automatic sessions (Claude Code + git commits) and manual entries in hours.csv.

  python3 hours.py sync                                         # log past days not yet recorded
  python3 hours.py add my-project 1.5 meeting "client call" [--date 2026-09-26]
  python3 hours.py test
"""
import argparse, csv, datetime as dt, glob, json, os, re, subprocess

HOME = os.path.expanduser("~")
ROOT = os.environ.get("HOURS_ROOT", f"{HOME}/Desktop")  # every project lives in a folder under ROOT
HERE = os.path.dirname(os.path.abspath(__file__))
CSV = os.path.join(HERE, "hours.csv")
COLS = ["date", "start", "end", "hours", "project", "type", "source", "notes"]
# commit author emails that count as "me"; defaults to the global git identity
EMAILS = set(filter(None, os.environ.get("HOURS_EMAILS", "").split(","))) or {
    subprocess.run(["git", "config", "--global", "user.email"], capture_output=True, text=True).stdout.strip()}
# Heuristics (gap, pad, min length) tuned on Sep 2026: commits alone capture ~40% of the real time.
CLAUDE_RULE = dict(gap=3600, pad=900, min_len=0)   # days with Claude Code prompts: dense data, tight rules
GIT_RULE = dict(gap=7200, pad=0, min_len=3600)     # commit-only days: sparse data, each session is at least 1h


def sessions(ts, gap, pad, min_len):
    """Group timestamps (s) into sessions [(start, end)]: start moved back by pad, length at least min_len."""
    out = []
    for t in sorted(set(ts)):
        if out and t - out[-1][1] <= gap:
            out[-1][1] = t
        else:
            out.append([t - pad, t])
    return [(min(a, b - min_len), b) for a, b in out]


def claude_events():
    """Timestamps of the prompts typed into Claude Code, per project folder."""
    ev = {}
    prefix = re.sub(r"[^A-Za-z0-9]", "-", ROOT)  # Claude Code names its project dirs after the path
    for d in glob.glob(f"{HOME}/.claude/projects/{prefix}*"):
        m = re.match(re.escape(prefix) + r"-?(.*?)(--claude-worktrees-.*)?$", os.path.basename(d))
        for f in glob.glob(f"{d}/*.jsonl"):
            for line in open(f, errors="ignore"):
                try:
                    o = json.loads(line)
                except ValueError:
                    continue
                c = o.get("message", {}).get("content") if isinstance(o.get("message"), dict) else None
                # only real user prompts: tool results are logged as "user" messages too
                if o.get("type") != "user" or "timestamp" not in o or (
                        isinstance(c, list) and any(isinstance(x, dict) and x.get("type") == "tool_result" for x in c)):
                    continue
                t = dt.datetime.fromisoformat(o["timestamp"].replace("Z", "+00:00")).timestamp()
                ev.setdefault(m.group(1) or os.path.basename(ROOT).lower(), []).append(int(t))
    return ev


def git_events():
    """Timestamps of my commits, per repository under ROOT."""
    ev = {}
    for g in glob.glob(f"{ROOT}/*/.git"):
        repo = os.path.dirname(g)
        out = subprocess.run(["git", "-C", repo, "log", "--all", "--no-merges", "--format=%ae|%at"],
                             capture_output=True, text=True).stdout.split()
        ts = [int(t) for e, t in (l.split("|") for l in out) if e in EMAILS]
        if ts:
            ev.setdefault(os.path.basename(repo), []).extend(ts)
    return ev


def read_rows():
    if not os.path.exists(CSV):
        return []
    with open(CSV, newline="") as f:
        return list(csv.DictReader(f))


def write_rows(rows):
    rows.sort(key=lambda r: (r["date"], r["start"], r["project"]))
    with open(CSV, "w", newline="") as f:
        w = csv.DictWriter(f, COLS)
        w.writeheader()
        w.writerows(rows)
    # data for dashboard.html (a file:// page cannot fetch the CSV, but it can load a <script>)
    with open(os.path.join(HERE, "hours-data.js"), "w") as f:
        f.write("window.HOURS = " + json.dumps(rows, ensure_ascii=False) + ";\n")


def sync():
    rows = read_rows()
    done = {(r["date"], r["project"]) for r in rows if r["source"].startswith("auto")}
    # rows moved by hand to another project keep "from <original project>" in notes, so sync won't recreate them
    done |= {(r["date"], r["notes"][5:].split(":")[0]) for r in rows
             if r["source"].startswith("auto") and r["notes"].startswith("from ")}
    today = dt.date.today().isoformat()
    by_day = {}  # (day, project) -> {"claude": [...], "git": [...]}
    for src, ev in (("claude", claude_events()), ("git", git_events())):
        for proj, ts in ev.items():
            for t in ts:
                day = dt.date.fromtimestamp(t).isoformat()
                by_day.setdefault((day, proj), {"claude": [], "git": []})[src].append(t)
    added = 0
    for (day, proj), ev in by_day.items():
        # only closed days never written before: Claude Code drops its logs after ~30 days, saved rows stay
        if day >= today or (day, proj) in done:
            continue
        source, rule = ("auto-claude", CLAUDE_RULE) if ev["claude"] else ("auto-git", GIT_RULE)
        for a, b in sessions(ev["claude"] + ev["git"], **rule):
            s, e = dt.datetime.fromtimestamp(a), dt.datetime.fromtimestamp(b)
            rows.append({"date": day, "start": s.strftime("%H:%M"), "end": e.strftime("%H:%M"),
                         "hours": f"{(b - a) / 3600:.2f}", "project": proj, "type": "development",
                         "source": source, "notes": ""})
            added += 1
    write_rows(rows)
    print(f"sync: +{added} sessions -> {CSV}")


def add(a):
    rows = read_rows()
    rows.append({"date": a.date, "start": "", "end": "", "hours": f"{a.hours:.2f}", "project": a.project,
                 "type": a.type, "source": "manual", "notes": a.notes})
    write_rows(rows)
    print(f"added: {a.date} {a.project} {a.hours}h {a.type}")


def test():
    h = 3600
    assert sessions([], **CLAUDE_RULE) == []
    assert sessions([0, h, 3 * h], **CLAUDE_RULE) == [(-900, h), (3 * h - 900, 3 * h)]
    # commit-only: a lone commit = 1h, commits 2h apart stay in the same session
    assert sessions([0, 2 * h, 5 * h], **GIT_RULE) == [(0, 2 * h), (4 * h, 5 * h)]
    print("ok")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("sync"); sub.add_parser("test")
    ad = sub.add_parser("add")
    ad.add_argument("project"); ad.add_argument("hours", type=float)
    ad.add_argument("type", nargs="?", default="meeting"); ad.add_argument("notes", nargs="?", default="")
    ad.add_argument("--date", default=dt.date.today().isoformat())
    a = p.parse_args()
    {"sync": sync, "test": test, "add": lambda: add(a)}[a.cmd]()
