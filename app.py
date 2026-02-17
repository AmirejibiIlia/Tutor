import json
import os
from datetime import datetime

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request
from groq import Groq

load_dotenv()

app = Flask(__name__)

DATABASE_URL = os.environ.get("DATABASE_URL")


def get_db():
    conn = psycopg2.connect(DATABASE_URL)
    return conn


def init_db():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS words (
            id SERIAL PRIMARY KEY,
            english TEXT NOT NULL,
            german TEXT NOT NULL,
            example_sentence TEXT NOT NULL,
            created_at TIMESTAMP NOT NULL
        )
    """)
    conn.commit()
    cur.close()
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

    text = response.choices[0].message.content.strip()
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
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO words (english, german, example_sentence, created_at) VALUES (%s, %s, %s, %s) RETURNING id",
        (result["english"], result["german"], result["example_sentence"], datetime.utcnow()),
    )
    word_id = cur.fetchone()[0]
    conn.commit()
    cur.close()
    conn.close()

    result["id"] = word_id
    result["created_at"] = datetime.utcnow().isoformat()
    return jsonify(result)


@app.route("/api/words")
def get_words():
    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT * FROM words ORDER BY created_at DESC")
    rows = cur.fetchall()
    cur.close()
    conn.close()
    # Convert datetime to string for JSON serialization
    for row in rows:
        row["created_at"] = row["created_at"].isoformat()
    return jsonify(rows)


@app.route("/api/words/<int:word_id>", methods=["DELETE"])
def delete_word(word_id):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("DELETE FROM words WHERE id = %s", (word_id,))
    conn.commit()
    cur.close()
    conn.close()
    return jsonify({"ok": True})


if __name__ == "__main__":
    app.run(debug=True, port=8080)
