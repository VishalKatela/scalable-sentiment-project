# Scalable movie review sentiment analysis

Mini project for **HMCE02T — Scalable ML & Big Data Analytics**.

Built on top of your original notebook. Two additions: real-time
scoring via **Kafka + Spark Structured Streaming**, and a simple web
**frontend** so you can demo predictions + explanations live instead of
scrolling through notebook cells.

## Project layout

```
bda/
  bda_pipeline.py     # everything from the notebook, as subcommands (see below)
  requirements.txt

serving/
  main.py             # FastAPI: /predict and /explain
  static/index.html    # the frontend page
  requirements.txt
```

## `bda_pipeline.py` commands

| Command | What it does |
|---|---|
| `batch` | Spark: clean data, TF-IDF, train LR + Naive Bayes, save pipeline |
| `stream` | Spark: watch a folder, score new reviews the moment they arrive |
| `simulate-stream` | Drop sample reviews into that folder one at a time |
| `kafka-produce` | Send sample reviews to a Kafka topic, one at a time |
| `kafka-consume` | Spark reads from Kafka and scores reviews live |
| `train-lstm` | Train the deep model, save it for the frontend |
| `evaluate-lstm` | Classification report + confusion matrix on the test set |
| `compare` | LR vs NB vs LSTM comparison table + bar chart |

`stream`/`simulate-stream` (folder-based) and `kafka-produce`/`kafka-consume`
do the same job two different ways — Kafka is the one that matches your
syllabus wording exactly; keep the folder-based pair as a fallback if
Kafka setup gives you trouble right before the demo.

## Running it, in order

### 1. Get the dataset

Download the "IMDB Dataset of 50K Movie Reviews" CSV from Kaggle, put
it at `bda/data/IMDB_Dataset.csv`.

### 2. Install dependencies

```bash
cd bda
python -m venv venv
venv\Scripts\activate        # Windows
pip install -r requirements.txt
```

### 3. Run the Spark batch pipeline

```bash
python bda_pipeline.py batch --input data/IMDB_Dataset.csv --output_dir artifacts
```

### 4. Train the LSTM

```bash
python bda_pipeline.py train-lstm --input data/IMDB_Dataset.csv --csv --output_dir ../serving
```

Leaves `sentiment_lstm_model.keras`, `keras_tokenizer.pkl`, and
`config.pkl` inside `serving/` — the frontend needs these three files.

### 5. Evaluate and compare (for your report)

```bash
python bda_pipeline.py evaluate-lstm --input data/IMDB_Dataset.csv --csv --model_dir ../serving
python bda_pipeline.py compare --lstm_accuracy 0.87
```

### 6. Get a Kafka broker running

You need *something* running Kafka before `kafka-produce`/`kafka-consume`
will work. Easiest option — one Docker command, no docker-compose, no
extra services:

```bash
docker run -p 9092:9092 apache/kafka:3.7.0
```

Leave that terminal running. This is just Kafka's own message broker —
it's unrelated to the load-balancer/scaling stuff you dropped earlier,
it's the one thing Kafka itself needs to exist at all.

(No Docker at all? Download Kafka from kafka.apache.org, extract it,
and run `bin\kafka-server-start.bat config\server.properties` from
inside — more manual, same result.)

### 7. Demo the streaming layer

Terminal 1 (the consumer — Spark scoring live):
```bash
python bda_pipeline.py kafka-consume --model_path artifacts/spark_logistic_regression_pipeline
```

Terminal 2 (the producer — sending reviews):
```bash
python bda_pipeline.py kafka-produce --input data/IMDB_Dataset.csv --n 10 --delay 5
```

Watch Terminal 1 print a scored prediction every few seconds as
Terminal 2 sends reviews.

### 8. Run the frontend

```bash
cd ../serving
pip install -r requirements.txt
uvicorn main:app --reload
```

Open `http://localhost:8000` — paste a review, hit "Analyze review",
see the sentiment + which words drove it (via LIME).

## What to say tomorrow, in one paragraph

"I used Spark for distributed data cleaning and TF-IDF feature
extraction, trained Logistic Regression and Naive Bayes as distributed
MLlib baselines, and compared them against a deep LSTM model. I
demonstrated both batch processing and real-time streaming — Kafka
feeds live reviews to a Spark Structured Streaming job that scores them
using the same fitted pipeline as the batch run. The LSTM is served
through a small web app with LIME explaining which words drove each
prediction."

That's CO2 (distributed vs. traditional trade-offs), CO3 (PySpark
pipelines), and CO4 (streaming vs. batch) — the core of the course —
demonstrated with a working, explainable end-to-end example.
