import os
import sqlite3
from datetime import datetime

from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request
from groq import Groq

load_dotenv()

app = Flask(__name__)

DB_PATH = os.environ.get("DB_PATH", "words.db")


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS words (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            english TEXT NOT NULL,
            german TEXT NOT NULL,
            example_sentence TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()


init_db()

client = Groq(api_key=os.environ.get("GROQ_API_KEY"))


def translate_word(word):
    prompt = f"""You are a German-English dictionary assistant.

Given the word: "{word}"

1. Detect if it's English or German.
2. Provide the translation (English→German or German→English).
3. Provide an example sentence IN GERMAN using the German word.

Respond in EXACTLY this JSON format, no extra text:
{{"english": "the english word", "german": "the german word", "example_sentence": "A German sentence using the word"}}"""

    response = client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.3,
        max_tokens=200,
    )

    import json
    text = response.choices[0].message.content.strip()
    # Extract JSON from the response (handle markdown code blocks)
    if "```" in text:
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    return json.loads(text)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/search", methods=["POST"])
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
    conn.execute(
        "INSERT INTO words (english, german, example_sentence, created_at) VALUES (?, ?, ?, ?)",
        (result["english"], result["german"], result["example_sentence"], datetime.utcnow().isoformat()),
    )
    conn.commit()
    word_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.close()

    result["id"] = word_id
    result["created_at"] = datetime.utcnow().isoformat()
    return jsonify(result)


@app.route("/api/words")
def get_words():
    conn = get_db()
    rows = conn.execute("SELECT * FROM words ORDER BY created_at DESC").fetchall()
    conn.close()
    return jsonify([dict(row) for row in rows])


@app.route("/api/words/<int:word_id>", methods=["DELETE"])
def delete_word(word_id):
    conn = get_db()
    conn.execute("DELETE FROM words WHERE id = ?", (word_id,))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


if __name__ == "__main__":
    app.run(debug=True, port=8080)
