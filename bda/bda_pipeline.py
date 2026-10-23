"""
bda_pipeline.py

Everything from the BDA half of the project in one file, run as
subcommands instead of separate scripts. This does exactly what the
six separate files did before -- nothing new, just merged for
convenience.

Commands:
    python bda_pipeline.py batch           --input data/IMDB_Dataset.csv
    python bda_pipeline.py train-lstm       --input data/IMDB_Dataset.csv --csv
    python bda_pipeline.py evaluate-lstm    --input data/IMDB_Dataset.csv --csv
    python bda_pipeline.py compare          --lstm_accuracy 0.87
    python bda_pipeline.py stream           --model_path artifacts/spark_logistic_regression_pipeline
    python bda_pipeline.py simulate-stream  --input data/IMDB_Dataset.csv
    python bda_pipeline.py kafka-produce    --input data/IMDB_Dataset.csv
    python bda_pipeline.py kafka-consume    --model_path artifacts/spark_logistic_regression_pipeline

Run `python bda_pipeline.py <command> --help` to see each command's options.
"""

import argparse
import json
import os
import pickle
import time
import uuid


# ---------------------------------------------------------------------------
# batch: Spark batch pipeline -- clean data, train LR + NB, save PipelineModel
# ---------------------------------------------------------------------------

def cmd_batch(args):
    from pyspark.sql import SparkSession
    from pyspark.sql.functions import col, lower, regexp_replace, trim
    from pyspark.ml import Pipeline
    from pyspark.ml.feature import HashingTF, IDF, StopWordsRemover, StringIndexer, Tokenizer
    from pyspark.ml.classification import LogisticRegression, NaiveBayes
    from pyspark.ml.evaluation import MulticlassClassificationEvaluator

    spark = (
        SparkSession.builder.appName("ScalableSentimentBatch")
        .config("spark.sql.shuffle.partitions", "8")
        .getOrCreate()
    )

    df = spark.read.csv(args.input, header=True, inferSchema=True, escape='"', multiLine=True)

    df_clean = df.withColumn("review", lower(col("review")))
    df_clean = df_clean.withColumn("review", regexp_replace(col("review"), "<br />", " "))
    df_clean = df_clean.withColumn("review", regexp_replace(col("review"), "[^a-zA-Z\\s]", ""))
    df_clean = df_clean.withColumn("review", trim(col("review")))
    df_clean = df_clean.dropDuplicates(["review"]).na.drop()
    print(f"Rows after cleaning: {df_clean.count()}")

    df_clean.write.mode("overwrite").parquet(f"{args.output_dir}/clean_reviews.parquet")

    train_df, test_df = df_clean.randomSplit([0.8, 0.2], seed=42)

    tokenizer = Tokenizer(inputCol="review", outputCol="words")
    remover = StopWordsRemover(inputCol="words", outputCol="filtered_words")
    hashing_tf = HashingTF(inputCol="filtered_words", outputCol="raw_features", numFeatures=args.num_features)
    idf = IDF(inputCol="raw_features", outputCol="features")
    indexer = StringIndexer(inputCol="sentiment", outputCol="label")
    evaluator = MulticlassClassificationEvaluator(labelCol="label", predictionCol="prediction", metricName="accuracy")

    results = {}
    os.makedirs(args.output_dir, exist_ok=True)

    for name, clf in [
        ("logistic_regression", LogisticRegression(featuresCol="features", labelCol="label", maxIter=20)),
        ("naive_bayes", NaiveBayes(featuresCol="features", labelCol="label", modelType="multinomial")),
    ]:
        pipeline = Pipeline(stages=[tokenizer, remover, hashing_tf, idf, indexer, clf])

        start = time.time()
        model = pipeline.fit(train_df)
        train_time = time.time() - start

        preds = model.transform(test_df)
        acc = evaluator.evaluate(preds)

        model_path = f"{args.output_dir}/spark_{name}_pipeline"
        model.write().overwrite().save(model_path)

        results[name] = {"accuracy": acc, "train_time_seconds": round(train_time, 2), "path": model_path}
        print(f"{name}: accuracy={acc:.4f} train_time={train_time:.2f}s -> saved to {model_path}")

    with open(f"{args.output_dir}/batch_metrics.json", "w") as f:
        json.dump(results, f, indent=2)

    print(f"Wrote comparison metrics to {args.output_dir}/batch_metrics.json")
    spark.stop()


# ---------------------------------------------------------------------------
# stream: watch a folder for new review CSVs, score them with the saved
# Spark pipeline the instant they arrive
# ---------------------------------------------------------------------------

def cmd_stream(args):
    from pyspark.sql import SparkSession
    from pyspark.sql.functions import col, lower, regexp_replace, trim
    from pyspark.sql.types import StringType, StructField, StructType
    from pyspark.ml import PipelineModel

    spark = SparkSession.builder.appName("ScalableSentimentStreaming").getOrCreate()
    model = PipelineModel.load(args.model_path)

    schema = StructType(
        [
            StructField("review", StringType(), True),
            StructField("sentiment", StringType(), True),
        ]
    )

    stream_df = spark.readStream.schema(schema).option("header", True).csv(args.stream_dir)

    clean = stream_df.withColumn("review", lower(col("review")))
    clean = clean.withColumn("review", regexp_replace(col("review"), "<br />", " "))
    clean = clean.withColumn("review", regexp_replace(col("review"), "[^a-zA-Z\\s]", ""))
    clean = clean.withColumn("review", trim(col("review")))

    predictions = model.transform(clean)

    query = (
        predictions.select("review", "prediction", "probability")
        .writeStream
        .outputMode("append")
        .format("console")
        .option("truncate", False)
        .option("checkpointLocation", args.checkpoint_dir)
        .start()
    )

    print(f"Streaming query started -- watching {args.stream_dir} for new review files.")
    query.awaitTermination()


# ---------------------------------------------------------------------------
# simulate-stream: drop sample reviews into the watched folder, one at a
# time with a delay, to fake a live feed for the demo
# ---------------------------------------------------------------------------

def cmd_simulate_stream(args):
    import pandas as pd

    os.makedirs(args.stream_dir, exist_ok=True)

    df = pd.read_csv(args.input)
    sample_reviews = df.sample(args.n, random_state=args.seed).reset_index(drop=True)

    print(f"Dropping {args.n} reviews into {args.stream_dir}/, one every {args.delay}s...")
    for i in range(len(sample_reviews)):
        row = sample_reviews.iloc[[i]]
        filename = f"{args.stream_dir}/review_{uuid.uuid4().hex[:6]}.csv"
        row.to_csv(filename, index=False)
        print(f"Dropped: {filename}  (true label: {row['sentiment'].values[0]})")
        time.sleep(args.delay)

    print("Done. Check the `stream` command's console output for scored predictions.")


# ---------------------------------------------------------------------------
# kafka-produce: send sample reviews to a Kafka topic, one at a time with
# a delay, simulating a live feed of incoming reviews
# ---------------------------------------------------------------------------

def cmd_kafka_produce(args):
    import json as _json
    import pandas as pd
    from kafka import KafkaProducer

    producer = KafkaProducer(
        bootstrap_servers=args.bootstrap_servers,
        value_serializer=lambda v: _json.dumps(v).encode("utf-8"),
    )

    df = pd.read_csv(args.input)
    sample_reviews = df.sample(args.n, random_state=args.seed).reset_index(drop=True)

    print(f"Sending {args.n} reviews to Kafka topic '{args.topic}', one every {args.delay}s...")
    for i in range(len(sample_reviews)):
        row = sample_reviews.iloc[i]
        message = {"review": row["review"], "sentiment": row["sentiment"]}
        producer.send(args.topic, value=message)
        producer.flush()
        print(f"Sent (true label: {row['sentiment']}): {row['review'][:80]}...")
        time.sleep(args.delay)

    print("Done sending. Check the `kafka-consume` command's console output for scored predictions.")


# ---------------------------------------------------------------------------
# kafka-consume: Spark Structured Streaming reads from Kafka and scores
# each review the instant it arrives, using the saved Spark pipeline
# ---------------------------------------------------------------------------

def cmd_kafka_consume(args):
    from pyspark.sql import SparkSession
    from pyspark.sql.functions import col, from_json, lower, regexp_replace, trim
    from pyspark.sql.types import StringType, StructField, StructType
    from pyspark.ml import PipelineModel

    spark = (
        SparkSession.builder.appName("ScalableSentimentKafkaStreaming")
        .config("spark.jars.packages", "org.apache.spark:spark-sql-kafka-0-10_2.12:3.5.1")
        .getOrCreate()
    )

    model = PipelineModel.load(args.model_path)

    message_schema = StructType(
        [
            StructField("review", StringType(), True),
            StructField("sentiment", StringType(), True),
        ]
    )

    raw = (
        spark.readStream.format("kafka")
        .option("kafka.bootstrap.servers", args.bootstrap_servers)
        .option("subscribe", args.topic)
        .option("startingOffsets", "latest")
        .load()
    )

    parsed = raw.selectExpr("CAST(value AS STRING) as json_str").select(
        from_json(col("json_str"), message_schema).alias("data")
    ).select("data.*")

    clean = parsed.withColumn("review", lower(col("review")))
    clean = clean.withColumn("review", regexp_replace(col("review"), "<br />", " "))
    clean = clean.withColumn("review", regexp_replace(col("review"), "[^a-zA-Z\\s]", ""))
    clean = clean.withColumn("review", trim(col("review")))

    predictions = model.transform(clean)

    query = (
        predictions.select("review", "prediction", "probability")
        .writeStream
        .outputMode("append")
        .format("console")
        .option("truncate", False)
        .option("checkpointLocation", args.checkpoint_dir)
        .start()
    )

    print(f"Kafka consumer started -- listening on topic '{args.topic}' via {args.bootstrap_servers}")
    query.awaitTermination()


# ---------------------------------------------------------------------------
# train-lstm: train the deep model, save it for the serving layer
# ---------------------------------------------------------------------------

def cmd_train_lstm(args):
    import pandas as pd
    from sklearn.model_selection import train_test_split
    from tensorflow.keras.preprocessing.text import Tokenizer as KerasTokenizer
    from tensorflow.keras.preprocessing.sequence import pad_sequences
    from tensorflow.keras.models import Sequential
    from tensorflow.keras.layers import Embedding, LSTM, Dense, Dropout

    df = pd.read_csv(args.input) if args.csv else pd.read_parquet(args.input)

    X = df["review"].values
    y = df["sentiment"].map({"positive": 1, "negative": 0}).values

    X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.2, random_state=42)

    tokenizer = KerasTokenizer(num_words=args.max_words, oov_token="<OOV>")
    tokenizer.fit_on_texts(X_train)

    X_train_seq = pad_sequences(tokenizer.texts_to_sequences(X_train), maxlen=args.max_len, padding="post", truncating="post")
    X_test_seq = pad_sequences(tokenizer.texts_to_sequences(X_test), maxlen=args.max_len, padding="post", truncating="post")

    model = Sequential(
        [
            Embedding(input_dim=args.max_words, output_dim=128, input_length=args.max_len),
            LSTM(64, return_sequences=False),
            Dropout(0.5),
            Dense(32, activation="relu"),
            Dense(1, activation="sigmoid"),
        ]
    )
    model.compile(optimizer="adam", loss="binary_crossentropy", metrics=["accuracy"])
    model.summary()

    model.fit(X_train_seq, y_train, validation_split=0.1, epochs=args.epochs, batch_size=args.batch_size)

    loss, acc = model.evaluate(X_test_seq, y_test)
    print(f"Test accuracy: {acc:.4f}")

    os.makedirs(args.output_dir, exist_ok=True)
    model.save(f"{args.output_dir}/sentiment_lstm_model.keras")
    with open(f"{args.output_dir}/keras_tokenizer.pkl", "wb") as f:
        pickle.dump(tokenizer, f)
    with open(f"{args.output_dir}/config.pkl", "wb") as f:
        pickle.dump({"MAX_LEN": args.max_len, "MAX_WORDS": args.max_words}, f)

    print(f"Saved model + tokenizer + config to {args.output_dir}/")


# ---------------------------------------------------------------------------
# evaluate-lstm: classification report + confusion matrix on the test set
# ---------------------------------------------------------------------------

def cmd_evaluate_lstm(args):
    import pandas as pd
    import matplotlib.pyplot as plt
    import seaborn as sns
    from sklearn.metrics import classification_report, confusion_matrix
    from sklearn.model_selection import train_test_split
    from tensorflow.keras.models import load_model
    from tensorflow.keras.preprocessing.sequence import pad_sequences

    df = pd.read_csv(args.input) if args.csv else pd.read_parquet(args.input)
    X = df["review"].values
    y = df["sentiment"].map({"positive": 1, "negative": 0}).values

    # same split as train-lstm (same seed) -> genuine held-out test set
    _, X_test, _, y_test = train_test_split(X, y, test_size=0.2, random_state=42)

    model = load_model(f"{args.model_dir}/sentiment_lstm_model.keras")
    with open(f"{args.model_dir}/keras_tokenizer.pkl", "rb") as f:
        tokenizer = pickle.load(f)
    with open(f"{args.model_dir}/config.pkl", "rb") as f:
        config = pickle.load(f)

    X_test_seq = pad_sequences(tokenizer.texts_to_sequences(X_test), maxlen=config["MAX_LEN"], padding="post", truncating="post")
    y_pred = (model.predict(X_test_seq) > 0.5).astype("int32")

    report = classification_report(y_test, y_pred, target_names=["negative", "positive"])
    print(report)

    os.makedirs(args.output_dir, exist_ok=True)
    with open(f"{args.output_dir}/lstm_classification_report.txt", "w") as f:
        f.write(report)

    cm = confusion_matrix(y_test, y_pred)
    plt.figure(figsize=(5, 4))
    sns.heatmap(cm, annot=True, fmt="d", cmap="Blues", xticklabels=["negative", "positive"], yticklabels=["negative", "positive"])
    plt.xlabel("Predicted")
    plt.ylabel("Actual")
    plt.title("Confusion Matrix - LSTM")
    plt.tight_layout()
    plt.savefig(f"{args.output_dir}/lstm_confusion_matrix.png", dpi=150)
    print(f"Saved report + confusion matrix to {args.output_dir}/")


# ---------------------------------------------------------------------------
# compare: LR vs NB vs LSTM comparison table + bar chart
# ---------------------------------------------------------------------------

def cmd_compare(args):
    import pandas as pd
    import matplotlib.pyplot as plt

    with open(args.batch_metrics) as f:
        batch = json.load(f)

    comparison_df = pd.DataFrame({
        "Model": ["Spark Logistic Regression (TF-IDF)", "Spark Naive Bayes (TF-IDF)", "Keras LSTM (Embeddings)"],
        "Accuracy": [
            batch["logistic_regression"]["accuracy"],
            batch["naive_bayes"]["accuracy"],
            args.lstm_accuracy,
        ],
        "Training Time (s)": [
            batch["logistic_regression"]["train_time_seconds"],
            batch["naive_bayes"]["train_time_seconds"],
            args.lstm_train_time_seconds if args.lstm_train_time_seconds is not None else "not measured",
        ],
        "Framework": ["Spark MLlib (distributed)", "Spark MLlib (distributed)", "TensorFlow/Keras (single-node)"],
        "Explainability": ["Coefficients (linear)", "Feature log-probs", "LIME (post-hoc, model-agnostic)"],
    })

    print(comparison_df.to_string(index=False))

    os.makedirs(args.output_dir, exist_ok=True)
    comparison_df.to_csv(f"{args.output_dir}/model_comparison.csv", index=False)

    accs = comparison_df["Accuracy"].tolist()
    plt.figure(figsize=(6, 4))
    bars = plt.bar(["Logistic Regression", "Naive Bayes", "LSTM"], accs, color=["#4C72B0", "#55A868", "#C44E52"])
    plt.ylim(0, 1)
    plt.ylabel("Accuracy")
    plt.title("Model Comparison: Distributed ML vs Deep Learning")
    for bar, a in zip(bars, accs):
        plt.text(bar.get_x() + bar.get_width() / 2, a + 0.02, f"{a:.3f}", ha="center")
    plt.tight_layout()
    plt.savefig(f"{args.output_dir}/model_comparison.png", dpi=150)

    print(f"Saved {args.output_dir}/model_comparison.csv and {args.output_dir}/model_comparison.png")


# ---------------------------------------------------------------------------
# CLI wiring
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="BDA pipeline: batch, streaming, and LSTM commands in one file")
    sub = parser.add_subparsers(dest="command", required=True)

    p_batch = sub.add_parser("batch", help="Clean data with Spark, train LR + NB, save pipeline")
    p_batch.add_argument("--input", required=True)
    p_batch.add_argument("--output_dir", default="artifacts")
    p_batch.add_argument("--num_features", type=int, default=10000)
    p_batch.set_defaults(func=cmd_batch)

    p_stream = sub.add_parser("stream", help="Watch a folder and score new reviews live")
    p_stream.add_argument("--model_path", required=True)
    p_stream.add_argument("--stream_dir", default="streaming_input")
    p_stream.add_argument("--checkpoint_dir", default="streaming_checkpoint")
    p_stream.set_defaults(func=cmd_stream)

    p_sim = sub.add_parser("simulate-stream", help="Drop sample reviews into the stream folder one at a time")
    p_sim.add_argument("--input", required=True)
    p_sim.add_argument("--stream_dir", default="streaming_input")
    p_sim.add_argument("--n", type=int, default=10)
    p_sim.add_argument("--delay", type=float, default=5.0)
    p_sim.add_argument("--seed", type=int, default=1)
    p_sim.set_defaults(func=cmd_simulate_stream)

    p_kprod = sub.add_parser("kafka-produce", help="Send sample reviews to a Kafka topic")
    p_kprod.add_argument("--input", required=True)
    p_kprod.add_argument("--topic", default="reviews")
    p_kprod.add_argument("--bootstrap_servers", default="localhost:9092")
    p_kprod.add_argument("--n", type=int, default=10)
    p_kprod.add_argument("--delay", type=float, default=5.0)
    p_kprod.add_argument("--seed", type=int, default=1)
    p_kprod.set_defaults(func=cmd_kafka_produce)

    p_kcons = sub.add_parser("kafka-consume", help="Spark reads from Kafka and scores reviews live")
    p_kcons.add_argument("--model_path", required=True)
    p_kcons.add_argument("--topic", default="reviews")
    p_kcons.add_argument("--bootstrap_servers", default="localhost:9092")
    p_kcons.add_argument("--checkpoint_dir", default="kafka_checkpoint")
    p_kcons.set_defaults(func=cmd_kafka_consume)

    p_train = sub.add_parser("train-lstm", help="Train the LSTM model")
    p_train.add_argument("--input", required=True)
    p_train.add_argument("--csv", action="store_true")
    p_train.add_argument("--output_dir", default="../serving")
    p_train.add_argument("--max_words", type=int, default=10000)
    p_train.add_argument("--max_len", type=int, default=200)
    p_train.add_argument("--epochs", type=int, default=5)
    p_train.add_argument("--batch_size", type=int, default=64)
    p_train.set_defaults(func=cmd_train_lstm)

    p_eval = sub.add_parser("evaluate-lstm", help="Classification report + confusion matrix for the LSTM")
    p_eval.add_argument("--input", required=True)
    p_eval.add_argument("--csv", action="store_true")
    p_eval.add_argument("--model_dir", default="../serving")
    p_eval.add_argument("--output_dir", default="artifacts")
    p_eval.set_defaults(func=cmd_evaluate_lstm)

    p_cmp = sub.add_parser("compare", help="LR vs NB vs LSTM comparison table + bar chart")
    p_cmp.add_argument("--batch_metrics", default="artifacts/batch_metrics.json")
    p_cmp.add_argument("--lstm_accuracy", type=float, required=True)
    p_cmp.add_argument("--lstm_train_time_seconds", type=float, default=None)
    p_cmp.add_argument("--output_dir", default="artifacts")
    p_cmp.set_defaults(func=cmd_compare)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
