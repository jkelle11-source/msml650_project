import time
import numpy as np
import os
import joblib
from sklearn.ensemble import IsolationForest
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import (confusion_matrix, f1_score, precision_score, recall_score)

NORMAL = 0
ANOMALY = 1
# minute
WINDOW = 60

def train_isolation_forest(X_train):
    model = IsolationForest(n_estimators=100, contamination="auto", random_state=42, n_jobs=-1,)
    model.fit(X_train)
    return model

def predict_isolation_forest(model, X):
    raw_predictions = model.predict(X)
    predictions = []
    # -1 for anomalies and 1 for normal data
    for prediction in raw_predictions:
        if prediction == -1:
            predictions.append(ANOMALY)
        else:
            predictions.append(NORMAL)
    return np.array(predictions)

def train_supervised_detector(X_train, y_train):
    model = LogisticRegression(max_iter=1000, class_weight="balanced", random_state=42)
    model.fit(X_train, y_train)
    return model

# predicting using supervised
def predict_supervised_detector(model, X):
    predictions = model.predict(X)
    return predictions

# precision, recall, f1, fpr
def precision_helper(y_true, y_pred):
    return precision_score(y_true,y_pred,zero_division=0)

def recall_helper(y_true, y_pred):
    return recall_score(y_true,y_pred,zero_division=0)

def f1_helper(y_true, y_pred):
    return f1_score(y_true,y_pred, zero_division=0)

def fpr_helper(y_true, y_pred):
    matrix = confusion_matrix(y_true, y_pred, labels=[NORMAL, ANOMALY])
    # nomral is 0, from first row
    tn = matrix[0][0]
    fp = matrix[0][1]
    if (fp + tn) > 0:
        # false positives/ all
        fpr = fp / (fp + tn)
    else:
        fpr = 0.0
    return fpr

# to call all metrics together 
def evaluate_metrics(y_true, y_pred):
    precision = precision_helper(y_true, y_pred)
    recall = recall_helper(y_true, y_pred)
    f1 = f1_helper(y_true, y_pred)
    fpr = fpr_helper(y_true, y_pred)
    metrics = {"precision": float(precision), "recall": float(recall), "f1": float(f1), "fpr": float(fpr)}
    return metrics

def isolation_forest_latency(model, X):
    start_time = time.perf_counter()
    predict_isolation_forest(model, X)
    end_time = time.perf_counter()
    latency_ms = (end_time - start_time) * 1000
    return latency_ms

def supervised_latency(model, X):
    start_time = time.perf_counter()
    predict_supervised_detector(model, X)
    end_time = time.perf_counter()
    latency_ms = (end_time - start_time) * 1000
    return latency_ms


def detection_latency(inference_latency_ms):
    inference_seconds = inference_latency_ms / 1000
    detection_latency_seconds = WINDOW + inference_seconds
    return detection_latency_seconds

def metric_difference(unsupervised_metrics, supervised_metrics):
    precision_difference = supervised_metrics["precision"] - unsupervised_metrics["precision"]
    recall_difference = supervised_metrics["recall"] - unsupervised_metrics["recall"]
    f1_difference = supervised_metrics["f1"] - unsupervised_metrics["f1"]
    # reversed since lower FPR is better
    fpr_difference = unsupervised_metrics["fpr"] - supervised_metrics["fpr"]
    differences = {"precision": precision_difference,"recall": recall_difference, "f1": f1_difference, "fpr": fpr_difference}
    return differences

# for loading later
def save_model(model, model_name, model_dir):
    os.makedirs(model_dir, exist_ok=True)
    model_path = os.path.join(model_dir, model_name)
    joblib.dump(model, model_path)
    return model_path

def load_model(model_path):
    model = joblib.load(model_path)
    return model