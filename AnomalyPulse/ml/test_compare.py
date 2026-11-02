"""Tests for the comparison report (M5-3). Run from AnomalyPulse/:  python3 -m pytest ml/test_compare.py -q"""
import pytest

from ml import compare

CLASSES = ["NORMAL", "TRAFFIC_SPIKE", "CASCADING_FAILURE"]
N, A, C = "NORMAL", "TRAFFIC_SPIKE", "CASCADING_FAILURE"
TRUTH = [N, N, A, A, C, C]
PRED = [N, A, A, N, C, A]


def test_accuracy_and_confusion_matrix():
    assert compare.accuracy(TRUTH, PRED) == 0.5
    assert compare.confusion(TRUTH, PRED, CLASSES) == [[1, 1, 0], [1, 1, 0], [0, 1, 1]]


def test_per_class_scores_and_macro_f1_match_a_hand_calculation():
    per = compare.per_class(compare.confusion(TRUTH, PRED, CLASSES), CLASSES)
    assert per[A]["precision"] == pytest.approx(1 / 3) and per[A]["recall"] == 0.5
    assert per[A]["f1"] == pytest.approx(0.4)
    assert per[C]["precision"] == 1.0 and per[C]["f1"] == pytest.approx(2 / 3)
    assert compare.macro_f1(per) == pytest.approx((0.5 + 0.4 + 2 / 3) / 3)


def test_anomaly_view_treats_everything_but_normal_as_an_anomaly():
    anomaly = compare.evaluate_classifier(TRUTH, PRED, CLASSES)["anomaly"]
    assert anomaly["precision"] == 0.75 and anomaly["recall"] == 0.75


def test_unknown_class_is_refused():
    with pytest.raises(ValueError):
        compare.confusion([N], ["MYSTERY"], CLASSES)


def test_length_mismatch_is_refused():
    with pytest.raises(ValueError):
        compare.accuracy([N, N], [N])


def test_cascade_margin_is_exact_recall_minus_the_best_model0_recall():
    report = compare.compare(TRUTH, {"xgboost": PRED}, {"previous_window": [False] * 6}, CLASSES)
    cascade = report["cascade"]
    assert cascade["windows"] == 2
    assert cascade["model0_recall"] == {"previous_window": 0.0}
    assert cascade["classifier_recall"]["xgboost"] == {"exact": 0.5, "flagged": 1.0}
    assert cascade["margin"]["xgboost"] == 0.5


def test_the_margin_is_taken_against_the_stronger_model0():
    # one variant flags no cascade window, the other flags one of the two
    flags = {"previous_window": [False] * 6, "earlier_mean_3": [False, False, False, False, True, False]}
    cascade = compare.compare(TRUTH, {"xgboost": PRED}, flags, CLASSES)["cascade"]
    assert cascade["model0_recall"] == {"previous_window": 0.0, "earlier_mean_3": 0.5}
    assert cascade["margin"]["xgboost"] == 0.0


def test_no_cascade_windows_gives_no_cascade_section():
    truth = [N, A]
    report = compare.compare(truth, {"x": truth}, {"previous_window": [False, True]}, CLASSES)
    assert report["cascade"] is None


def test_markdown_names_every_model_and_the_cascade_margin():
    report = compare.compare(TRUTH, {"xgboost": PRED, "random_forest": TRUTH},
                             {"previous_window": [False] * 6, "earlier_mean_3": [False, False, True, False, False, True]},
                             CLASSES)
    text = compare.render_markdown(report, CLASSES)
    for needle in ("Model 0 (previous_window)", "Model 0 (earlier_mean_3)", "xgboost",
                   "random_forest", "Confusion matrix", "Cascade margin over Model 0"):
        assert needle in text
