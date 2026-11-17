import numpy as np

from neri import report


def test_weighted_quantile():
    x = np.array([1.0, 2.0, 3.0, 4.0])
    assert report.wquant(x, np.ones(4), 0.5) in (2.0, 3.0)
    assert report.wquant(x, np.array([1.0, 1.0, 1.0, 100.0]), 0.5) == 4.0  # a heavy weight drags the median


def test_weights_change_the_estimate_and_unweighted_is_reported():
    x = np.array([0.0] * 50 + [1.0] * 50)
    w = np.array([1.0] * 50 + [9.0] * 50)  # the "1" group stands for 9x more of the population
    e = report.estimate(x, w, "share", np.random.default_rng(1), 200)
    assert abs(e["value"] - 0.9) < 1e-9 and abs(e["unweighted"] - 0.5) < 1e-9
    assert e["ci_lo"] < e["value"] < e["ci_hi"] and e["n"] == 100


def test_bootstrap_interval_brackets_truth_and_handles_nan_and_empty():
    rng = np.random.default_rng(7)
    x = (rng.random(400) < 0.3).astype(float)
    e = report.estimate(x, np.ones(400), "share", np.random.default_rng(2), 300)
    assert e["ci_lo"] < 0.3 < e["ci_hi"]
    assert report.estimate(np.array([np.nan, np.nan]), np.ones(2), "share", np.random.default_rng(1), 10)["n"] == 0
