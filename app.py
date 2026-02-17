import json
import os
from datetime import datetime
from functools import wraps

import bcrypt
import psycopg2
import psycopg2.extras
from dotenv import load_dotenv
from flask import Flask, jsonify, redirect, render_template, request, session, url_for
from groq import Groq

load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-me")

DATABASE_URL = os.environ.get("DATABASE_URL")


def get_db():
    conn = psycopg2.connect(DATABASE_URL)
    return conn


def init_db():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            created_at TIMESTAMP NOT NULL
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS words (
            id SERIAL PRIMARY KEY,
            user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
            english TEXT NOT NULL,
            german TEXT NOT NULL,
            word_type TEXT,
            gender_article TEXT,
            plural TEXT,
            verb_forms TEXT,
            example_sentence TEXT NOT NULL,
            sentence_translation TEXT,
            created_at TIMESTAMP NOT NULL
        )
    """)
    # Migrate old table: add columns that may be missing
    migrations = [
        "ALTER TABLE words ADD COLUMN IF NOT EXISTS user_id INTEGER REFERENCES users(id) ON DELETE CASCADE",
        "ALTER TABLE words ADD COLUMN IF NOT EXISTS word_type TEXT",
        "ALTER TABLE words ADD COLUMN IF NOT EXISTS gender_article TEXT",
        "ALTER TABLE words ADD COLUMN IF NOT EXISTS plural TEXT",
        "ALTER TABLE words ADD COLUMN IF NOT EXISTS verb_forms TEXT",
        "ALTER TABLE words ADD COLUMN IF NOT EXISTS sentence_translation TEXT",
    ]
    for sql in migrations:
        cur.execute(sql)
    conn.commit()
    cur.close()
    conn.close()


init_db()

client = Groq(api_key=os.environ.get("GROQ_API_KEY"))


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            if request.path.startswith("/api/"):
                return jsonify({"error": "Login required"}), 401
            return redirect(url_for("login_page"))
        return f(*args, **kwargs)
    return decorated


def translate_word(word):
    prompt = f"""You are a German-English dictionary assistant.

Given the word: "{word}"

1. Detect if it's English or German.
2. Provide the translation (English→German or German→English).
3. Determine the word type (noun, verb, adjective, adverb, preposition, etc.).
4. If it's a NOUN: provide the German article (der/die/das) and the plural form.
5. If it's a VERB: provide the Präteritum and Partizip II forms (e.g. "ging, gegangen").
6. Provide an example sentence IN GERMAN using the German word.
7. Provide the English translation of that example sentence.

Respond in EXACTLY this JSON format, no extra text:
{{"english": "the english word", "german": "the german word", "word_type": "noun", "gender_article": "der/die/das or null", "plural": "plural form or null", "verb_forms": "Präteritum, Partizip II or null", "example_sentence": "German sentence", "sentence_translation": "English translation of the sentence"}}

For non-nouns, set gender_article and plural to null.
For non-verbs, set verb_forms to null."""

    response = client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.3,
        max_tokens=300,
    )

    text = response.choices[0].message.content.strip()
    if "```" in text:
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    return json.loads(text)


# --- Pages ---

@app.route("/")
@login_required
def index():
    return render_template("index.html")


@app.route("/login")
def login_page():
    if "user_id" in session:
        return redirect(url_for("index"))
    return render_template("login.html")


@app.route("/register")
def register_page():
    if "user_id" in session:
        return redirect(url_for("index"))
    return render_template("register.html")


# --- Auth API ---

@app.route("/api/register", methods=["POST"])
def register():
    data = request.get_json()
    username = data.get("username", "").strip()
    password = data.get("password", "")

    if not username or not password:
        return jsonify({"error": "Username and password required"}), 400
    if len(password) < 4:
        return jsonify({"error": "Password must be at least 4 characters"}), 400

    password_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()

    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute(
            "INSERT INTO users (username, password_hash, created_at) VALUES (%s, %s, %s) RETURNING id",
            (username, password_hash, datetime.utcnow()),
        )
        user_id = cur.fetchone()[0]
        conn.commit()
    except psycopg2.errors.UniqueViolation:
        conn.rollback()
        cur.close()
        conn.close()
        return jsonify({"error": "Username already taken"}), 409
    cur.close()
    conn.close()

    session["user_id"] = user_id
    session["username"] = username
    return jsonify({"ok": True, "username": username})


@app.route("/api/login", methods=["POST"])
def login():
    data = request.get_json()
    username = data.get("username", "").strip()
    password = data.get("password", "")

    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT * FROM users WHERE username = %s", (username,))
    user = cur.fetchone()
    cur.close()
    conn.close()

    if not user or not bcrypt.checkpw(password.encode(), user["password_hash"].encode()):
        return jsonify({"error": "Invalid username or password"}), 401

    session["user_id"] = user["id"]
    session["username"] = user["username"]
    return jsonify({"ok": True, "username": user["username"]})


@app.route("/api/logout", methods=["POST"])
def logout():
    session.clear()
    return jsonify({"ok": True})


@app.route("/api/me")
def me():
    if "user_id" not in session:
        return jsonify({"logged_in": False}), 401
    return jsonify({"logged_in": True, "username": session["username"]})


# --- Words API ---

@app.route("/api/search", methods=["POST"])
@login_required
def search():
    data = request.get_json()
    word = data.get("word", "").strip()
    if not word:
        return jsonify({"error": "No word provided"}), 400

    try:
        result = translate_word(word)
    except Exception as e:
        return jsonify({"error": f"Translation failed: {str(e)}"}), 500

    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        """INSERT INTO words (user_id, english, german, word_type, gender_article, plural, verb_forms, example_sentence, sentence_translation, created_at)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
        (
            session["user_id"],
            result["english"],
            result["german"],
            result.get("word_type"),
            result.get("gender_article"),
            result.get("plural"),
            result.get("verb_forms"),
            result["example_sentence"],
            result.get("sentence_translation"),
            datetime.utcnow(),
        ),
    )
    word_id = cur.fetchone()[0]
    conn.commit()
    cur.close()
    conn.close()

    result["id"] = word_id
    result["created_at"] = datetime.utcnow().isoformat()
    return jsonify(result)


@app.route("/api/words")
@login_required
def get_words():
    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT * FROM words WHERE user_id = %s ORDER BY created_at DESC", (session["user_id"],))
    rows = cur.fetchall()
    cur.close()
    conn.close()
    for row in rows:
        row["created_at"] = row["created_at"].isoformat()
    return jsonify(rows)


@app.route("/api/words/<int:word_id>", methods=["DELETE"])
@login_required
def delete_word(word_id):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("DELETE FROM words WHERE id = %s AND user_id = %s", (word_id, session["user_id"]))
    conn.commit()
    cur.close()
    conn.close()
    return jsonify({"ok": True})


if __name__ == "__main__":
    app.run(debug=True, port=8080)
