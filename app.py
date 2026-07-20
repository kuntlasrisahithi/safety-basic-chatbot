
# ============================================================
# BLOCK 1
# ============================================================
import re
import os
import difflib
from flask import Flask, request, jsonify, render_template


# ============================================================
# BLOCK 2
# ============================================================
def tokenize(text: str):
    """Lowercase + strip punctuation -> list of words."""
    return re.findall(r"[a-z0-9]+", text.lower())


def and_gate(query_tokens: set, row_keywords: set) -> int:
    """
    AND gate: counts how many of the row's keywords are ALSO
    present in the query. Every matching keyword must satisfy
    (token IN query) AND (token IN row_keywords).
    """
    return len(query_tokens & row_keywords)


def or_gate(query_tokens: set, row_keywords: set) -> bool:
    """
    OR gate: True if the row and the query share ANY token at all.
    Used as a cheap pre-filter before scoring.

    FIX: the original implementation compared the size of the union
    against the sum of the two set sizes. That is mathematically
    equivalent to "the sets are not disjoint", but only by accident -
    it's confusing and easy to get subtly wrong. Written directly here
    as an actual overlap check.
    """
    return len(query_tokens & row_keywords) > 0


def xor_gate(score_a: int, score_b: int) -> bool:
    """
    XOR gate: True only when exactly ONE of the two candidate rows
    has a non-zero score. Used to break ties between two categories
    that both look plausible - if only one side is truly non-zero,
    XOR flags it as the unambiguous winner.
    """
    a = score_a > 0
    b = score_b > 0
    return (a and not b) or (b and not a)


def compact(text: str) -> str:
    """Collapse to a single lowercase alnum string, e.g. 'first aid' -> 'firstaid'."""
    return re.sub(r"[^a-z0-9]", "", text.lower())


def fuzzy_bonus(query: str, row: dict) -> int:
    """
    FIX: brand-new fallback matching layer.

    The exact-token AND/OR gates miss two very common real-world cases:
      1. Concatenated words: user types "firstaid" instead of "first aid".
         No single query token equals "first" or "aid", so overlap is 0.
      2. Small typos: "extinguiser" instead of "extinguisher".

    fuzzy_bonus() catches these by comparing a "compacted" (space/punct
    stripped) version of the query against a compacted version of the
    row's keywords + category, using difflib's similarity ratio, and by
    checking substring containment either direction. This never runs
    instead of the exact match - it only adds a small bonus/fallback
    score, so precise matches still win.
    """
    q_compact = compact(query)
    row_compact = compact(row["keywords"] + " " + row["category"])

    if not q_compact or not row_compact:
        return 0

    # Substring containment (handles "firstaid" inside "bandageinjurymedicalfirstaid")
    if q_compact in row_compact or any(
        q_compact in compact(kw) or compact(kw) in q_compact
        for kw in row["keyword_tokens"]
        if len(kw) >= 4
    ):
        return 2

    # Fuzzy ratio against each individual keyword (catches typos)
    for kw in row["keyword_tokens"]:
        if len(kw) < 4:
            continue
        ratio = difflib.SequenceMatcher(None, compact(kw), q_compact).ratio()
        if ratio >= 0.8:
            return 1

    return 0


QUESTION_WORDS = {"what", "why", "when", "where", "who", "how", "can", "should", "does", "is", "are"}


def question_word_bonus(query_tokens: set, row: dict) -> int:
    """
    FIX: brand-new disambiguation layer.

    Within one category (e.g. all 15 PPE rows), the row's keyword list
    only differs by ONE extra word - e.g. row "Why is PPE important?"
    adds "important", row "When should PPE be worn?" adds "worn". But
    real questions use the interrogative itself ("why", "when") rather
    than that extra word, so "why ppe" and "what is ppe" both scored
    identically (every row shares only the base "ppe" token) and always
    fell back to whichever row happened to be first in the CSV.

    This compares the interrogative word(s) in the query against the
    interrogative word(s) actually used in the row's own stored
    question text, and rewards a match. This is the signal that truly
    distinguishes "why" rows from "what" rows from "when" rows.
    """
    query_qwords = query_tokens & QUESTION_WORDS
    if not query_qwords:
        return 0
    row_qwords = set(tokenize(row["question"])) & QUESTION_WORDS
    return 3 if query_qwords & row_qwords else 0


def score_row(query_tokens: set, query: str, row: dict) -> int:
    """
    Combines the gates into one relevance score for a single row:
      - AND gate gives the base overlap score (keyword hits)
      - OR gate is used as a gatekeeper: if there is no overlap at
        all, we fall back to fuzzy_bonus() instead of auto-disqualifying
      - A small bonus is added if the row's category name itself
        appears in the query (extra AND condition)
      - A bonus is added if the query's interrogative word (why/what/
        when/...) matches the interrogative word actually used in the
        row's own question text (breaks same-category ties)

    FIX: previously, if or_gate/and_gate found zero overlap the row was
    hard-disqualified (return 0), with no fallback. Now we try fuzzy
    matching before giving up, so typos and concatenated words like
    "firstaid" still surface the right answer.
    """
    row_keywords = set(row["keyword_tokens"])
    overlap = and_gate(query_tokens, row_keywords)

    if overlap == 0 and not or_gate(query_tokens, row_keywords):
        fb = fuzzy_bonus(query, row)
        return fb  # 0 if truly nothing matches, else a small fallback score

    category_tokens = set(tokenize(row["category"]))
    category_bonus = 2 if and_gate(query_tokens, category_tokens) > 0 else 0
    qword_bonus = question_word_bonus(query_tokens, row)

    return overlap + category_bonus + qword_bonus


def rank_rows(query: str, data: list) -> list:
    """
    Scores every row in the centralised dataset and returns them
    sorted best-first. Uses xor_gate() to detect a clean single
    winner vs. a genuine tie between two categories.
    """
    query_tokens = set(tokenize(query))
    scored = [(score_row(query_tokens, query, row), row) for row in data]
    scored = [pair for pair in scored if pair[0] > 0]
    scored.sort(key=lambda pair: pair[0], reverse=True)

    if len(scored) >= 2:
        top_score, second_score = scored[0][0], scored[1][0]
        is_clean_winner = xor_gate(top_score, second_score) or top_score != second_score
        scored[0][1]["_clean_winner"] = bool(is_clean_winner)
    elif len(scored) == 1:
        scored[0][1]["_clean_winner"] = True

    return scored


# ============================================================
# BLOCK 3
# ============================================================
CSV_PATH = os.path.join(os.path.dirname(__file__), "data", "safety.csv")


def load_dataset(path: str) -> list:
  
    import csv
    rows = []
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for r in reader:
            keywords_raw = r.get("keywords", "")
            rows.append({
                "id": r.get("id"),
                "category": r.get("category", ""),
                "keywords": keywords_raw,
                "keyword_tokens": tokenize(keywords_raw),  # FIX: word-level tokens
                "question": r.get("question", ""),
                "answer": r.get("answer", ""),
            })
    return rows


CENTRAL_DATA = load_dataset(CSV_PATH)  # loaded once, shared by every request


# ============================================================
# BLOCK 4
# ============================================================
def retrieve(query: str, top_k: int = 3) -> list:
    """RETRIEVAL step: pull the top_k most relevant rows for a query."""
    ranked = rank_rows(query, CENTRAL_DATA)
    return ranked[:top_k]


def generate_answer(query: str, retrieved: list) -> dict:
    """
    GENERATION step: since there is no external LLM, "generation"
    here means deterministically composing a response from the
    retrieved rows - the RAG pattern without a neural generator.
    """
    if not retrieved:
        return {
            "answer": "I don't have information on that yet. Try asking about "
                      "PPE, Fire Safety, First Aid, Electrical Safety, Permit to "
                      "Work, Emergency Response, or Incident Reporting.",
            "sources": [],
            "confidence": "none",
        }

    top_score, top_row = retrieved[0]
    confidence = "high" if top_row.get("_clean_winner") else "medium"

    primary_answer = top_row["answer"]

    extra_context = []
    seen_answers = {primary_answer}
    for score, row in retrieved[1:]:
        if row["answer"] not in seen_answers and score >= top_score - 1:
            extra_context.append(row["answer"])
            seen_answers.add(row["answer"])

    full_answer = primary_answer
    if extra_context:
        full_answer += " Related: " + " ".join(extra_context)

    return {
        "answer": full_answer,
        "sources": [
            {"id": row["id"], "category": row["category"], "question": row["question"]}
            for _, row in retrieved
        ],
        "confidence": confidence,
    }


GREETINGS = {"hi", "hello", "hey", "yo", "sup", "hiya", "howdy", "greetings"}
GREETING_FILLER = {"there", "claude", "assistant", "team"}


def is_greeting(query: str) -> bool:
    """
    True for short greeting-only messages ("hi", "hey there"), not for
    real questions that happen to start with a greeting word
    ("hi, what is ppe" should still be answered, not greeted).
    """
    tokens = tokenize(query)
    if not tokens or len(tokens) > 3:
        return False
    if not any(t in GREETINGS for t in tokens):
        return False
    return all(t in GREETINGS or t in GREETING_FILLER for t in tokens)


def answer_query(query: str) -> dict:
    if is_greeting(query):
        return {
            "answer": "Hi! I'm your local safety assistant. Ask me about PPE, "
                      "Fire Safety, First Aid, Electrical Safety, Permit to Work, "
                      "Emergency Response, or Incident Reporting.",
            "sources": [],
            "confidence": "greeting",
        }
    retrieved = retrieve(query, top_k=3)
    return generate_answer(query, retrieved)


# ============================================================
# BLOCK 5
# ============================================================
app = Flask(__name__)


@app.route("/")
def home():
    categories = sorted(set(row["category"] for row in CENTRAL_DATA))
    return render_template("index.html", categories=categories, total_entries=len(CENTRAL_DATA))


@app.route("/api/ask", methods=["POST"])
def api_ask():
    payload = request.get_json(force=True, silent=True) or {}
    query = (payload.get("query") or "").strip()
    if not query:
        return jsonify({"error": "Empty query"}), 400
    result = answer_query(query)
    return jsonify(result)


if __name__ == "__main__":
    print(f"Loaded {len(CENTRAL_DATA)} entries from {CSV_PATH}")
    app.run(host="127.0.0.1", port=5000, debug=True)