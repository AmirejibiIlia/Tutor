import json
import os
from datetime import datetime
from functools import wraps
from urllib.parse import quote

import bcrypt
import psycopg2
import psycopg2.extras
from dotenv import load_dotenv
import httpx
from flask import Flask, Response, jsonify, redirect, render_template, request, session, url_for
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
            difficulty TEXT DEFAULT 'new',
            ipa TEXT,
            gender_label TEXT,
            notes TEXT,
            created_at TIMESTAMP NOT NULL
        )
    """)
    migrations = [
        "ALTER TABLE words ADD COLUMN IF NOT EXISTS user_id INTEGER REFERENCES users(id) ON DELETE CASCADE",
        "ALTER TABLE words ADD COLUMN IF NOT EXISTS word_type TEXT",
        "ALTER TABLE words ADD COLUMN IF NOT EXISTS gender_article TEXT",
        "ALTER TABLE words ADD COLUMN IF NOT EXISTS plural TEXT",
        "ALTER TABLE words ADD COLUMN IF NOT EXISTS verb_forms TEXT",
        "ALTER TABLE words ADD COLUMN IF NOT EXISTS sentence_translation TEXT",
        "ALTER TABLE words ADD COLUMN IF NOT EXISTS difficulty TEXT DEFAULT 'new'",
        "ALTER TABLE words ADD COLUMN IF NOT EXISTS ipa TEXT",
        "ALTER TABLE words ADD COLUMN IF NOT EXISTS gender_label TEXT",
        "ALTER TABLE words ADD COLUMN IF NOT EXISTS notes TEXT",
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
    prompt = f"""You are a professional linguist writing for a high-quality German-English learning dictionary.

The user typed: "{word}"

CRITICAL — INPUT HANDLING:
- The user may type a single word, a phrase, or even a full sentence.
- The input may contain spelling mistakes, grammar errors, or awkward phrasing in EITHER language. DO NOT copy their mistakes.
- Your job is to understand what the user MEANS, then produce the CORRECT dictionary entry.
- If the input is a phrase or sentence (e.g. "can I have a water please"), identify the KEY WORD or PHRASE to create an entry for, and use word_type "phrase" if it's a full expression.
- ALWAYS produce grammatically perfect German and natural idiomatic English, regardless of how the user typed it.
- Example: if user types "can I have a water, please?" → german should be "Könnte ich bitte ein Wasser haben?" (correct grammar), english should be "Could I have some water, please?" (natural English).

LANGUAGE ACCURACY:
- Detect if the input is English or German and translate accordingly.
- The "german" field must use correct German spelling: nouns CAPITALIZED (Blume, Hund), verbs in lowercase infinitive (gehen, sprechen), everything else lowercase. For phrases, use proper sentence capitalization.
- The "english" field must be natural, idiomatic English — avoid awkward literal translations.
- If a word has multiple common meanings, use the most common one.

WORD TYPE:
- word_type must be one of: "noun", "verb", "adjective", "adverb", "preposition", "conjunction", "pronoun", "particle", "interjection", "numeral", "phrase"

GRAMMAR DETAILS:
- For NOUNS: provide gender_article (der/die/das), gender_label (m/f/n), and plural form (capitalized). The "german" field = just the capitalized noun.
- For VERBS: provide verb_forms as "Präteritum, Partizip II" (e.g. "ging, ist gegangen"). The "german" field = infinitive.
- For all other types: set gender_article, gender_label, plural, and verb_forms to null.

PRONUNCIATION:
- ipa: IPA phonetic transcription of the German word (e.g. "/ˈbluːmə/" for Blume). Always include.

EXAMPLE:
- example_sentence: one natural, everyday German sentence using the word. Not overly formal.
- sentence_translation: natural English translation of that sentence.

LEARNING NOTES:
- notes: a short helpful note for learners (irregular plural, special usage, common mistakes, related words, etc.). Keep it to 1-2 sentences. Set to null if nothing noteworthy.

Respond in EXACTLY this JSON format, no extra text:
{{"english": "flower", "german": "Blume", "word_type": "noun", "gender_article": "die", "gender_label": "f", "plural": "Blumen", "verb_forms": null, "ipa": "/ˈbluːmə/", "example_sentence": "Ich habe ihr Blumen zum Geburtstag geschenkt.", "sentence_translation": "I gave her flowers for her birthday.", "notes": "Regular plural. Also used figuratively: 'die Blume des Lebens' (the flower of life)."}}"""

    response = client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.3,
        max_tokens=500,
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


@app.route("/stats")
@login_required
def stats_page():
    return render_template("stats.html")


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


@app.route("/api/tts")
def tts():
    text = request.args.get("text", "")
    lang = request.args.get("lang", "de")
    if not text:
        return "No text", 400
    url = f"https://translate.google.com/translate_tts?ie=UTF-8&tl={lang}&client=tw-ob&q={quote(text)}"
    resp = httpx.get(url, headers={"User-Agent": "Mozilla/5.0"}, follow_redirects=True)
    return Response(resp.content, content_type="audio/mpeg")


@app.route("/api/ask", methods=["POST"])
@login_required
def ask_about_word():
    data = request.get_json()
    question = data.get("question", "").strip()
    context = data.get("context", {})

    if not question:
        return jsonify({"error": "No question provided"}), 400

    german = context.get("german", "")
    english = context.get("english", "")
    word_type = context.get("word_type", "")

    prompt = f"""You are a friendly, knowledgeable German language tutor.

The student is looking at this dictionary entry:
- German: {german}
- English: {english}
- Type: {word_type}

The student asks: "{question}"

Answer helpfully and concisely. Include German examples with English translations when relevant.
Use this format for examples: "German sentence" — "English translation"
Keep your answer short (2-5 sentences max) but informative. If they ask for variations (formal, informal, friendly, etc.), provide them clearly."""

    try:
        response = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.5,
            max_tokens=300,
        )
        answer = response.choices[0].message.content.strip()
        return jsonify({"answer": answer})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


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
        """INSERT INTO words (user_id, english, german, word_type, gender_article, gender_label, plural, verb_forms, example_sentence, sentence_translation, ipa, notes, difficulty, created_at)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
        (
            session["user_id"],
            result["english"],
            result["german"],
            result.get("word_type"),
            result.get("gender_article"),
            result.get("gender_label"),
            result.get("plural"),
            result.get("verb_forms"),
            result["example_sentence"],
            result.get("sentence_translation"),
            result.get("ipa"),
            result.get("notes"),
            "new",
            datetime.utcnow(),
        ),
    )
    word_id = cur.fetchone()[0]
    conn.commit()
    cur.close()
    conn.close()

    result["id"] = word_id
    result["difficulty"] = "new"
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


@app.route("/api/words/<int:word_id>/difficulty", methods=["PATCH"])
@login_required
def set_difficulty(word_id):
    data = request.get_json()
    difficulty = data.get("difficulty", "new")
    if difficulty not in ("new", "hard", "medium", "easy"):
        return jsonify({"error": "Invalid difficulty"}), 400
    conn = get_db()
    cur = conn.cursor()
    cur.execute("UPDATE words SET difficulty = %s WHERE id = %s AND user_id = %s", (difficulty, word_id, session["user_id"]))
    conn.commit()
    cur.close()
    conn.close()
    return jsonify({"ok": True})


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


@app.route("/api/stats")
@login_required
def get_stats():
    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    uid = session["user_id"]

    # Total count
    cur.execute("SELECT COUNT(*) as total FROM words WHERE user_id = %s", (uid,))
    total = cur.fetchone()["total"]

    # By type
    cur.execute("SELECT COALESCE(word_type, 'other') as word_type, COUNT(*) as count FROM words WHERE user_id = %s GROUP BY word_type ORDER BY count DESC", (uid,))
    by_type = [dict(r) for r in cur.fetchall()]

    # By difficulty
    cur.execute("SELECT COALESCE(difficulty, 'new') as difficulty, COUNT(*) as count FROM words WHERE user_id = %s GROUP BY difficulty", (uid,))
    by_difficulty = {r["difficulty"]: r["count"] for r in cur.fetchall()}

    # Words per day (last 30 days)
    cur.execute("""
        SELECT DATE(created_at) as day, COUNT(*) as count
        FROM words WHERE user_id = %s AND created_at > NOW() - INTERVAL '30 days'
        GROUP BY DATE(created_at) ORDER BY day
    """, (uid,))
    daily = [{"day": r["day"].isoformat(), "count": r["count"]} for r in cur.fetchall()]

    # Streak: consecutive days with at least 1 word
    cur.execute("""
        SELECT DISTINCT DATE(created_at) as day
        FROM words WHERE user_id = %s ORDER BY day DESC
    """, (uid,))
    days = [r["day"] for r in cur.fetchall()]
    streak = 0
    from datetime import date, timedelta
    today = date.today()
    for i, d in enumerate(days):
        expected = today - timedelta(days=i)
        if d == expected:
            streak += 1
        elif i == 0 and d == today - timedelta(days=1):
            # Allow if today has no words yet but yesterday does
            streak += 1
            today = today - timedelta(days=1)
        else:
            break

    cur.close()
    conn.close()

    # Milestones
    milestones = [
        {"target": 1, "label": "First Word", "icon": "seed"},
        {"target": 10, "label": "Getting Started", "icon": "sprout"},
        {"target": 25, "label": "Quarter Century", "icon": "leaf"},
        {"target": 50, "label": "Half Century", "icon": "tree"},
        {"target": 100, "label": "Century", "icon": "star"},
        {"target": 250, "label": "Enthusiast", "icon": "fire"},
        {"target": 500, "label": "Scholar", "icon": "book"},
        {"target": 1000, "label": "Master", "icon": "crown"},
    ]
    for m in milestones:
        m["reached"] = total >= m["target"]

    return jsonify({
        "total": total,
        "by_type": by_type,
        "by_difficulty": by_difficulty,
        "daily": daily,
        "streak": streak,
        "milestones": milestones,
    })


if __name__ == "__main__":
    app.run(debug=True, port=8080)
