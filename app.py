"""ChainVote — Blockchain-secured e-voting: anonymous one-person-one-vote ballots mined into a proof-of-work chain,
with live results, countdowns, shareable poll codes and a public chain explorer.
Run:  python app.py   →  http://127.0.0.1:5000   (demo: demo@example.com / Demo@1234)
"""
import hashlib
import json
import random
import secrets
import string
import time
from datetime import datetime, timedelta

from flask import abort, current_app, flash, g, jsonify, redirect, render_template, request, url_for

from core import create_app, execute, login_required, now, query

DIFFICULTY = 3
UI = dict(
    name="ChainVote", short="Blockchain E-Voting", icon="🗳️", accent1="#F72585", accent2="#4CC9F0", home="polls",
    tagline="Create tamper-proof polls in seconds. Every ballot is anonymised, hashed and mined into a blockchain — results update live.",
    hero='Every vote counts. <span class="gradient-text">Every vote is proven.</span>', badge="Web3 democracy",
    features=[("bi-box", "Proof-of-work ledger", "SHA-256 blocks, nonce mining, chain integrity checks"),
              ("bi-incognito", "Anonymous & unique", "Salted voter hashes enforce one vote per person"),
              ("bi-broadcast", "Live results", "Animated tallies, countdown timers and activity feed")],
    float=['⛓️ Block <b>#128</b> mined · nonce 4,213', '📊 <b class="gradient-text">1,204 votes</b> verified on-chain'],
    nav=[("Polls", "polls", "bi-collection"), ("Create Poll", "create", "bi-plus-square"), ("Explorer", "explorer_home", "bi-boxes")],
    search_hint="Search polls…", promo_title="How it works", promo="Your identity is hashed with a server secret — nobody can link a block to you.",
)
SCHEMA = """
CREATE TABLE IF NOT EXISTS polls (
    id INTEGER PRIMARY KEY AUTOINCREMENT, creator_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    code TEXT UNIQUE NOT NULL, title TEXT NOT NULL, description TEXT, options TEXT NOT NULL, ends_at TEXT NOT NULL,
    is_public INTEGER DEFAULT 1, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS blocks (
    id INTEGER PRIMARY KEY AUTOINCREMENT, poll_id INTEGER NOT NULL REFERENCES polls(id) ON DELETE CASCADE,
    idx INTEGER NOT NULL, timestamp TEXT NOT NULL, voter_hash TEXT NOT NULL, choice INTEGER NOT NULL,
    prev_hash TEXT NOT NULL, nonce INTEGER NOT NULL, hash TEXT NOT NULL, mine_ms REAL,
    UNIQUE(poll_id, voter_hash), UNIQUE(poll_id, idx));
"""


# ---------------------------------------------------------------- blockchain core
def sha(s):
    return hashlib.sha256(s.encode()).hexdigest()


def header(idx, ts, voter, choice, prev, nonce):
    return f"{idx}|{ts}|{voter}|{choice}|{prev}|{nonce}"


def mine(idx, ts, voter, choice, prev):
    t0, nonce = time.perf_counter(), 0
    h = sha(header(idx, ts, voter, choice, prev, nonce))
    while not h.startswith("0" * DIFFICULTY):
        nonce += 1
        h = sha(header(idx, ts, voter, choice, prev, nonce))
    return nonce, h, (time.perf_counter() - t0) * 1000


def add_block(poll_id, voter, choice, ts=None):
    last = query("SELECT idx, hash FROM blocks WHERE poll_id=? ORDER BY idx DESC LIMIT 1", (poll_id,), one=True)
    idx, prev = (last["idx"] + 1, last["hash"]) if last else (0, "0" * 64)
    ts = ts or now()
    nonce, h, ms = mine(idx, ts, voter, choice, prev)
    execute("INSERT INTO blocks(poll_id,idx,timestamp,voter_hash,choice,prev_hash,nonce,hash,mine_ms) VALUES(?,?,?,?,?,?,?,?,?)",
            (poll_id, idx, ts, voter, choice, prev, nonce, h, ms))
    return dict(idx=idx, hash=h, nonce=nonce, mine_ms=round(ms, 1))


def voter_hash(uid, poll_id):
    return sha(f"{current_app.config['SECRET_KEY']}:{uid}:{poll_id}")


def validate(poll_id):
    blocks = query("SELECT * FROM blocks WHERE poll_id=? ORDER BY idx", (poll_id,))
    bad = []
    for i, b in enumerate(blocks):
        h = sha(header(b["idx"], b["timestamp"], b["voter_hash"], b["choice"], b["prev_hash"], b["nonce"]))
        if h != b["hash"] or not h.startswith("0" * DIFFICULTY) or (i and b["prev_hash"] != blocks[i - 1]["hash"]):
            bad.append(b["idx"])
    return not bad, bad, blocks


def new_code():
    while True:
        c = "".join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(6))
        if not query("SELECT 1 FROM polls WHERE code=?", (c,), one=True):
            return c


def create_poll(uid, title, desc, options, hours, public=True):
    code = new_code()
    pid = execute("INSERT INTO polls(creator_id,code,title,description,options,ends_at,is_public) VALUES(?,?,?,?,?,?,?)",
                  (uid, code, title, desc, json.dumps(options), (datetime.now() + timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S"), int(public)))
    add_block(pid, "GENESIS", -1)
    return pid, code


def seed_demo(uid):
    rng = random.Random(11)
    demos = [("Best language to learn AI in 2026?", "Vote for the language you'd recommend to a beginner.", ["Python", "JavaScript", "Julia", "R", "Rust"], 72, [52, 9, 4, 7, 6]),
             ("Next workshop topic for the student club", "Results decide the October workshop.", ["Generative AI", "Cybersecurity", "Blockchain", "Data Analytics"], 30, [21, 14, 8, 11]),
             ("Should the campus hackathon be 24h or 48h?", "", ["24 hours", "48 hours", "Two weekends"], 6, [17, 23, 5])]
    for title, desc, opts, hrs, votes in demos:
        pid, _ = create_poll(uid, title, desc, opts, hrs)
        start = datetime.now() - timedelta(hours=8)
        ballots = [i for i, n in enumerate(votes) for _ in range(n)]
        rng.shuffle(ballots)
        for k, choice in enumerate(ballots):
            ts = (start + timedelta(minutes=k * 7 + rng.randint(0, 5))).strftime("%Y-%m-%d %H:%M:%S")
            add_block(pid, sha(f"demo-voter-{title}-{k}"), choice, ts)


app = create_app(__name__, UI, SCHEMA, seed_demo=seed_demo)


def poll_or_404(code):
    p = query("SELECT * FROM polls WHERE code=?", (code.upper(),), one=True)
    if not p:
        abort(404)
    p["options"] = json.loads(p["options"])
    p["ended"] = datetime.strptime(p["ends_at"], "%Y-%m-%d %H:%M:%S") < datetime.now()
    return p


def results(p, uid=None):
    rows = query("SELECT choice, COUNT(*) n FROM blocks WHERE poll_id=? AND choice>=0 GROUP BY choice", (p["id"],))
    counts = [0] * len(p["options"])
    for r in rows:
        counts[r["choice"]] = r["n"]
    total = sum(counts)
    recent = query("SELECT idx, timestamp, substr(hash,1,18) hash, nonce, mine_ms FROM blocks WHERE poll_id=? ORDER BY idx DESC LIMIT 8", (p["id"],))
    for b in recent:
        b["ago"] = int((datetime.now() - datetime.strptime(b["timestamp"], "%Y-%m-%d %H:%M:%S")).total_seconds())
    voted = uid and query("SELECT 1 FROM blocks WHERE poll_id=? AND voter_hash=?", (p["id"], voter_hash(uid, p["id"])), one=True)
    ok, bad, _ = validate(p["id"])
    return dict(counts=counts, total=total, pct=[round(100 * c / total, 1) if total else 0 for c in counts], recent=recent,
                voted=bool(voted), valid=ok, height=(recent[0]["idx"] + 1) if recent else 0, ended=p["ended"])


# ---------------------------------------------------------------- pages
@app.route("/polls")
@login_required
def polls():
    rows = query("SELECT p.*, u.name creator, (SELECT COUNT(*) FROM blocks b WHERE b.poll_id=p.id AND b.choice>=0) votes "
                 "FROM polls p JOIN users u ON u.id=p.creator_id WHERE p.is_public=1 OR p.creator_id=? ORDER BY p.created_at DESC", (g.user["id"],))
    for r in rows:
        r["options"] = json.loads(r["options"])
        r["ended"] = datetime.strptime(r["ends_at"], "%Y-%m-%d %H:%M:%S") < datetime.now()
    stats = dict(polls=len(rows), votes=sum(r["votes"] for r in rows), active=sum(not r["ended"] for r in rows),
                 blocks=query("SELECT COUNT(*) n FROM blocks", one=True)["n"])
    return render_template("polls.html", rows=rows, st=stats, page_title="Polls")


@app.route("/create", methods=["GET", "POST"])
@login_required
def create():
    if request.method == "POST":
        title = request.form.get("title", "").strip()[:150]
        opts = [o.strip()[:80] for o in request.form.getlist("option") if o.strip()]
        hours = max(1, min(24 * 30, int(request.form.get("hours", 24) or 24)))
        if len(title) < 4 or len(opts) < 2 or len(set(opts)) != len(opts):
            flash("Add a title and at least two different options.", "danger")
            return redirect(url_for("create"))
        _, code = create_poll(g.user["id"], title, request.form.get("description", "").strip()[:500], opts, hours, bool(request.form.get("public")))
        flash(f"Poll created! Share code {code} — genesis block mined ⛏️", "success")
        return redirect(url_for("poll", code=code))
    return render_template("create.html", page_title="Create Poll")


@app.route("/poll/<code>")
@login_required
def poll(code):
    p = poll_or_404(code)
    return render_template("poll.html", p=p, r=results(p, g.user["id"]), page_title=p["title"][:40])


@app.route("/vote")
@login_required
def latest_poll():
    first = query("SELECT code FROM polls WHERE is_public=1 OR creator_id=? ORDER BY created_at DESC, id DESC LIMIT 1", (g.user["id"],), one=True)
    return redirect(url_for("poll", code=first["code"])) if first else redirect(url_for("create"))


@app.route("/explorer")
@login_required
def explorer_home():
    first = query("SELECT code FROM polls WHERE is_public=1 OR creator_id=? ORDER BY created_at DESC LIMIT 1", (g.user["id"],), one=True)
    return redirect(url_for("explorer", code=first["code"])) if first else redirect(url_for("create"))


@app.route("/explorer/<code>")
@login_required
def explorer(code):
    p = poll_or_404(code)
    ok, bad, blocks = validate(p["id"])
    allp = query("SELECT code, title FROM polls WHERE is_public=1 OR creator_id=? ORDER BY created_at DESC", (g.user["id"],))
    return render_template("explorer.html", p=p, ok=ok, bad=bad, blocks=blocks[::-1][:25], n=len(blocks), allp=allp, difficulty=DIFFICULTY,
                           page_title="Chain Explorer")


@app.post("/join")
@login_required
def join():
    code = request.form.get("code", "").strip().upper()
    if query("SELECT 1 FROM polls WHERE code=?", (code,), one=True):
        return redirect(url_for("poll", code=code))
    flash("No poll found with that code.", "danger")
    return redirect(url_for("polls"))


# ---------------------------------------------------------------- API
@app.get("/api/poll/<code>")
@login_required
def api_results(code):
    p = poll_or_404(code)
    return jsonify(results(p, g.user["id"]))


@app.post("/api/poll/<code>/vote")
@login_required
def api_vote(code):
    p = poll_or_404(code)
    if p["ended"]:
        return jsonify(error="Voting has closed for this poll"), 400
    choice = request.get_json(force=True).get("choice")
    if not isinstance(choice, int) or not 0 <= choice < len(p["options"]):
        return jsonify(error="Invalid option"), 400
    vh = voter_hash(g.user["id"], p["id"])
    if query("SELECT 1 FROM blocks WHERE poll_id=? AND voter_hash=?", (p["id"], vh), one=True):
        return jsonify(error="You have already voted — one person, one vote."), 409
    blk = add_block(p["id"], vh, choice)
    return jsonify(ok=True, block=blk, receipt=vh[:16])


@app.post("/api/poll/<code>/close")
@login_required
def api_close(code):
    p = poll_or_404(code)
    if p["creator_id"] != g.user["id"]:
        return jsonify(error="Only the creator can close this poll"), 403
    execute("UPDATE polls SET ends_at=? WHERE id=?", (now(), p["id"]))
    return jsonify(ok=True)


if __name__ == "__main__":
    app.run(debug=True)
