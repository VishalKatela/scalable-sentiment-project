from transformers import pipeline
from lime.lime_text import LimeTextExplainer
import numpy as np

pipe = pipeline(
    "sentiment-analysis",
    model="cardiffnlp/twitter-xlm-roberta-base-sentiment-multilingual",
    device=-1,
    top_k=None,
    truncation=True,
    max_length=512
)

text = "यह फिल्म बहुत खराब थी, मुझे बिल्कुल पसंद नहीं आई।"
print("Pipeline output:", pipe(text))

def predict_proba(texts):
    rows = []
    for t in texts:
        scores = {x['label'].lower(): x['score'] for x in pipe(t)[0]}
        p = scores.get('positive', 0)
        n = scores.get('negative', 0)
        total = p + n
        if total == 0: total = 1
        rows.append([n/total, p/total])
    return np.array(rows)

explainer = LimeTextExplainer(class_names=["negative", "positive"], split_expression=r'\s+')
exp = explainer.explain_instance(text, predict_proba, num_features=10, num_samples=100)
print("LIME with \s+:")
for w, wt in exp.as_list():
    print(w, wt)
