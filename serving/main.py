from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel

import csv
import os
import random
import numpy as np

# ---------------------------------------------------------------------------
# Lazy-load heavy models so the server starts fast
# ---------------------------------------------------------------------------
_en_pipe = None
_multi_pipe = None


def get_en_pipeline():
    """RoBERTa — English only, highest accuracy."""
    global _en_pipe
    if _en_pipe is None:
        from transformers import pipeline
        _en_pipe = pipeline(
            "sentiment-analysis",
            model="cardiffnlp/twitter-roberta-base-sentiment-latest",
            device=-1,
            truncation=True,
            max_length=512,
            top_k=None,          # return ALL label scores so we can pick correctly
        )
    return _en_pipe


def get_multi_pipeline():
    """XLM-RoBERTa multilingual — much better than mBERT for Hindi/Arabic/Japanese/Spanish."""
    global _multi_pipe
    if _multi_pipe is None:
        from transformers import pipeline
        _multi_pipe = pipeline(
            "sentiment-analysis",
            model="cardiffnlp/twitter-xlm-roberta-base-sentiment-multilingual",
            device=-1,
            truncation=True,
            max_length=512,
            top_k=None,          # return ALL label scores
        )
    return _multi_pipe


# ---------------------------------------------------------------------------
# Language detection
# ---------------------------------------------------------------------------
def detect_language(text: str) -> str:
    try:
        from langdetect import detect
        return detect(text)
    except Exception:
        return "en"


# ---------------------------------------------------------------------------
# Shared prediction helper
# ---------------------------------------------------------------------------
def _raw_predict(text: str):
    """
    Returns (label, confidence, lang_code, model_name).
    Uses top_k=None so we compare positive vs negative directly —
    prevents 'neutral' from masking the true sentiment.
    """
    lang = detect_language(text)

    if lang == "en":
        pipe = get_en_pipeline()
        model_name = "RoBERTa (EN)"
    else:
        pipe = get_multi_pipeline()
        model_name = "XLM-RoBERTa (multilingual)"

    # top_k=None → list of [{label, score}, ...] sorted by score desc
    all_scores = pipe(text)[0]
    scores = {item["label"].lower(): item["score"] for item in all_scores}

    # Always compare positive vs negative directly, ignore neutral
    pos = scores.get("positive", 0.0)
    neg = scores.get("negative", 0.0)
    total = pos + neg
    if total == 0: total = 1.0

    if pos >= neg:
        label = "positive"
        confidence = pos / total
    else:
        label = "negative"
        confidence = neg / total

    return label, confidence, lang, model_name


# ---------------------------------------------------------------------------
# LIME-compatible wrapper
# ---------------------------------------------------------------------------
def predict_proba(texts: list) -> np.ndarray:
    """Returns Nx2 array [p_negative, p_positive] — LIME-compatible."""
    rows = []
    for text in texts:
        label, confidence, _, _ = _raw_predict(text)
        if label == "positive":
            rows.append([1 - confidence, confidence])
        else:
            rows.append([confidence, 1 - confidence])
    return np.array(rows)


# ---------------------------------------------------------------------------
# Built-in LLM narrative — ALWAYS works, no API key needed
# ---------------------------------------------------------------------------
def generate_local_narrative(review: str, label: str, confidence: float, lang: str) -> str:
    """Constructs a film-critic style narrative from model outputs — no external API."""
    pct = int(confidence * 100)

    if label == "positive":
        templates = [
            f"The review radiates clear enthusiasm with {pct}% certainty. "
            f"The language carries strong markers of appreciation and satisfaction — "
            f"this reviewer found genuine value in what they watched.",
            f"With {pct}% confidence the sentiment is warm and approving. "
            f"The reviewer's word choices signal enjoyment and recommendation; "
            f"this film clearly delivered on its promises.",
            f"The overall tone leans distinctly positive ({pct}% confidence). "
            f"It reads as a genuine endorsement — the kind of review that sends audiences to the cinema.",
        ]
        if confidence > 0.92:
            stars = "⭐⭐⭐⭐⭐"
        elif confidence > 0.78:
            stars = "⭐⭐⭐⭐"
        else:
            stars = "⭐⭐⭐"
    else:
        templates = [
            f"The review carries unmistakable negative signals at {pct}% confidence. "
            f"The tone points to disappointment — this film failed to meet the reviewer's expectations.",
            f"With {pct}% certainty, this is a discouraging review. "
            f"The language conveys frustration and dissatisfaction; "
            f"the viewing experience clearly left the reviewer cold.",
            f"The overall sentiment here is negative ({pct}% confidence). "
            f"The reviewer's words signal displeasure — not a film they would recommend.",
        ]
        if confidence > 0.90:
            stars = "⭐"
        elif confidence > 0.75:
            stars = "⭐⭐"
        else:
            stars = "⭐⭐"

    if lang != "en":
        lang_note = f" (Review detected in language: {lang.upper()})"
    else:
        lang_note = ""

    narrative = random.choice(templates) + lang_note
    return narrative + f"\n\nSuggested rating: {stars}"


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------
app = FastAPI(title="Reel Verdict API", version="2.1")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class ReviewRequest(BaseModel):
    review: str


# ---------------------------------------------------------------------------
# /predict
# ---------------------------------------------------------------------------
@app.post("/predict")
def predict(req: ReviewRequest):
    label, confidence, lang, model_name = _raw_predict(req.review)
    return {
        "sentiment": label,
        "confidence": round(confidence, 4),
        "language": lang,
        "model": model_name,
    }


# ---------------------------------------------------------------------------
# /explain  (LIME word weights)
# ---------------------------------------------------------------------------
@app.post("/explain")
def explain(req: ReviewRequest):
    from lime.lime_text import LimeTextExplainer
    # split_expression=r'\s+' prevents LIME from breaking Hindi/Arabic characters apart
    explainer = LimeTextExplainer(class_names=["negative", "positive"], split_expression=r'\s+')
    exp = explainer.explain_instance(
        req.review,
        predict_proba,
        num_features=10,
        num_samples=100,
    )
    word_weights = [{"word": w, "weight": round(wt, 4)} for w, wt in exp.as_list()]
    return {"review": req.review, "word_weights": word_weights}


# ---------------------------------------------------------------------------
# /llm-explain  — always works; Groq is an optional upgrade
# ---------------------------------------------------------------------------
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")


@app.post("/llm-explain")
def llm_explain(req: ReviewRequest):
    label, confidence, lang, model_name = _raw_predict(req.review)

    # --- Try Groq first if API key is set ---
    if GROQ_API_KEY:
        try:
            from groq import Groq
            client = Groq(api_key=GROQ_API_KEY)
            prompt = (
                f'Movie review: "{req.review}"\n\n'
                f"Sentiment model verdict: {label} ({confidence * 100:.1f}% confidence)\n"
                f"Detected language: {lang}\n\n"
                f"In 2-3 sentences, explain in plain English why this review leans {label}. "
                f"Then on a new line, suggest a star rating out of 5 (e.g. ⭐⭐⭐). "
                f"Be concise and insightful, like a film critic."
            )
            chat = client.chat.completions.create(
                model="qwen/qwen3.8-27b",
                messages=[{"role": "user", "content": prompt}],
                max_tokens=200,
            )
            narrative = chat.choices[0].message.content.strip()
            return {"available": True, "narrative": narrative, "source": "Groq Qwen 3.8 27B"}
        except Exception as e:
            print(f"[llm-explain] Groq call failed: {e}")  # visible in uvicorn logs

    # --- Always-working local narrative fallback ---
    narrative = generate_local_narrative(req.review, label, confidence, lang)
    return {
        "available": True,
        "narrative": narrative,
        "source": "Built-in critic",
    }


# ---------------------------------------------------------------------------
# /stats
# ---------------------------------------------------------------------------
COMPARISON_PATHS = [
    "../bda/artifacts/model_comparison.csv",
    "artifacts/model_comparison.csv",
]


@app.get("/stats")
def stats():
    for path in COMPARISON_PATHS:
        if os.path.exists(path):
            with open(path, newline="") as f:
                rows = list(csv.DictReader(f))
            return {"available": True, "models": rows}
    return {"available": False, "models": []}


# ---------------------------------------------------------------------------
# Static frontend
# ---------------------------------------------------------------------------
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
def serve_frontend():
    return FileResponse("static/index.html")