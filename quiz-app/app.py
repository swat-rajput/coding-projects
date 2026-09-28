"""Quiz app: serves 20 random questions per quiz, never repeating answered ones.

Run:  python3 app.py   then open http://127.0.0.1:5050
"""
import json
import os
import random
import sqlite3
from datetime import datetime

from flask import Flask, g, jsonify, request, send_from_directory

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "quiz.db")
QUESTIONS_JSON = os.path.join(BASE_DIR, "questions_rebalanced.json")
QUIZ_SIZE = 20

app = Flask(__name__, static_folder="static")

SCHEMA = """
CREATE TABLE IF NOT EXISTS questions (
    id          TEXT PRIMARY KEY,
    domain      TEXT NOT NULL,
    question    TEXT NOT NULL,
    answers     TEXT NOT NULL,   -- JSON object {"A": "...", ...}
    correct     TEXT NOT NULL,   -- JSON list, e.g. ["B"] or ["A","C"]
    explanation TEXT
);
CREATE TABLE IF NOT EXISTS attempts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT NOT NULL,
    finished_at TEXT
);
CREATE TABLE IF NOT EXISTS attempt_questions (
    attempt_id  INTEGER NOT NULL REFERENCES attempts(id),
    position    INTEGER NOT NULL,
    question_id TEXT NOT NULL REFERENCES questions(id),
    selected    TEXT,            -- JSON list, NULL until answered
    is_correct  INTEGER,
    answered_at TEXT,
    PRIMARY KEY (attempt_id, position)
);
-- A question is "used" once answered; it is not served again until reset.
CREATE TABLE IF NOT EXISTS answered (
    question_id TEXT PRIMARY KEY REFERENCES questions(id),
    answered_at TEXT NOT NULL
);
"""


def now():
    return datetime.now().isoformat(timespec="seconds")


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    """Create tables and upsert questions from the JSON file."""
    db = sqlite3.connect(DB_PATH)
    db.executescript(SCHEMA)
    with open(QUESTIONS_JSON, encoding="utf-8") as f:
        questions = json.load(f)["questions"]
    for q in questions:
        correct = q["correct_answer"]
        if isinstance(correct, str):
            correct = [correct]
        db.execute(
            """INSERT INTO questions (id, domain, question, answers, correct, explanation)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET domain=excluded.domain, question=excluded.question,
                 answers=excluded.answers, correct=excluded.correct, explanation=excluded.explanation""",
            (q["id"], q["domain"], q["question"], json.dumps(q["answers"]),
             json.dumps(sorted(correct)), q.get("explanation", "")),
        )
    db.commit()
    db.close()
    print(f"Loaded {len(questions)} questions into {DB_PATH}")


def pool_stats(db):
    total = db.execute("SELECT COUNT(*) FROM questions").fetchone()[0]
    answered = db.execute("SELECT COUNT(*) FROM answered").fetchone()[0]
    return {"total": total, "answered": answered, "remaining": total - answered}


def active_attempt(db):
    return db.execute(
        "SELECT id FROM attempts WHERE finished_at IS NULL ORDER BY id DESC LIMIT 1"
    ).fetchone()


def question_payload(row, reveal):
    data = {
        "position": row["position"],
        "id": row["question_id"],
        "domain": row["domain"],
        "question": row["question"],
        "answers": json.loads(row["answers"]),
        "multi": len(json.loads(row["correct"])) > 1,
        "selected": json.loads(row["selected"]) if row["selected"] else None,
    }
    if reveal:
        data["correct"] = json.loads(row["correct"])
        data["is_correct"] = bool(row["is_correct"])
        data["explanation"] = row["explanation"]
    return data


def attempt_state(db, attempt_id):
    rows = db.execute(
        """SELECT aq.*, q.domain, q.question, q.answers, q.correct, q.explanation
           FROM attempt_questions aq JOIN questions q ON q.id = aq.question_id
           WHERE aq.attempt_id = ? ORDER BY aq.position""",
        (attempt_id,),
    ).fetchall()
    attempt = db.execute("SELECT * FROM attempts WHERE id = ?", (attempt_id,)).fetchone()
    return {
        "attempt_id": attempt_id,
        "finished": attempt["finished_at"] is not None,
        "questions": [question_payload(r, reveal=r["selected"] is not None) for r in rows],
        "stats": pool_stats(db),
    }


@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.get("/api/status")
def status():
    db = get_db()
    active = active_attempt(db)
    return jsonify({
        "stats": pool_stats(db),
        "active_attempt": attempt_state(db, active["id"]) if active else None,
        "history": [dict(r) for r in db.execute(
            """SELECT a.id, a.started_at, a.finished_at,
                      SUM(aq.is_correct) AS score, COUNT(aq.selected) AS answered
               FROM attempts a JOIN attempt_questions aq ON aq.attempt_id = a.id
               WHERE a.finished_at IS NOT NULL
               GROUP BY a.id HAVING COUNT(aq.selected) > 0
               ORDER BY a.id DESC LIMIT 10""")],
    })


@app.post("/api/quiz")
def start_quiz():
    db = get_db()
    # Abandon any unfinished quiz; its unanswered questions go back into the pool.
    db.execute("UPDATE attempts SET finished_at = ? WHERE finished_at IS NULL", (now(),))
    ids = [r[0] for r in db.execute(
        "SELECT id FROM questions WHERE id NOT IN (SELECT question_id FROM answered)")]
    if not ids:
        db.commit()
        return jsonify({"error": "All questions have been answered. Reset progress to start over."}), 409
    chosen = random.sample(ids, min(QUIZ_SIZE, len(ids)))
    cur = db.execute("INSERT INTO attempts (started_at) VALUES (?)", (now(),))
    attempt_id = cur.lastrowid
    db.executemany(
        "INSERT INTO attempt_questions (attempt_id, position, question_id) VALUES (?, ?, ?)",
        [(attempt_id, i, qid) for i, qid in enumerate(chosen)],
    )
    db.commit()
    return jsonify(attempt_state(db, attempt_id))


@app.post("/api/quiz/<int:attempt_id>/answer")
def answer(attempt_id):
    db = get_db()
    body = request.get_json(force=True)
    position = int(body["position"])
    selected = sorted(set(body.get("selected") or []))
    if not selected:
        return jsonify({"error": "Select an answer."}), 400
    row = db.execute(
        """SELECT aq.*, q.correct FROM attempt_questions aq JOIN questions q ON q.id = aq.question_id
           JOIN attempts a ON a.id = aq.attempt_id
           WHERE aq.attempt_id = ? AND aq.position = ? AND a.finished_at IS NULL""",
        (attempt_id, position),
    ).fetchone()
    if row is None:
        return jsonify({"error": "Question not found in an active quiz."}), 404
    if row["selected"] is not None:
        return jsonify({"error": "Already answered."}), 409
    is_correct = selected == json.loads(row["correct"])
    ts = now()
    db.execute(
        """UPDATE attempt_questions SET selected = ?, is_correct = ?, answered_at = ?
           WHERE attempt_id = ? AND position = ?""",
        (json.dumps(selected), int(is_correct), ts, attempt_id, position),
    )
    db.execute("INSERT OR IGNORE INTO answered (question_id, answered_at) VALUES (?, ?)",
               (row["question_id"], ts))
    db.commit()
    return jsonify(attempt_state(db, attempt_id))


@app.post("/api/quiz/<int:attempt_id>/finish")
def finish(attempt_id):
    db = get_db()
    db.execute("UPDATE attempts SET finished_at = ? WHERE id = ? AND finished_at IS NULL",
               (now(), attempt_id))
    db.commit()
    return jsonify(attempt_state(db, attempt_id))


@app.post("/api/reset")
def reset():
    """Clear the answered-question tracking so every question is available again."""
    db = get_db()
    db.execute("DELETE FROM answered")
    db.execute("UPDATE attempts SET finished_at = ? WHERE finished_at IS NULL", (now(),))
    db.commit()
    return jsonify({"stats": pool_stats(db)})


if __name__ == "__main__":
    init_db()
    app.run(debug=False, port=int(os.environ.get("PORT", 5050)))
