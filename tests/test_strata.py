import numpy as np
import polars as pl

from neri import report, strata


def pop(n=100):
    return pl.DataFrame({"owner": [f"o{i}" for i in range(n)], "repo": [f"r{i}" for i in range(n)], "stars": [i % 10 for i in range(n)]})


def test_draw_longtail_is_seeded_sized_and_carries_population_size():
    a, b = strata.draw_longtail(pop(), 20, 7), strata.draw_longtail(pop(), 20, 7)
    assert a.height == 20 and a["N_h"][0] == 100 and abs(a["sample_prob"][0] - 0.2) < 1e-9 and set(a["stratum"]) == {"long_tail"}
    assert sorted(a["owner"]) == sorted(b["owner"])  # deterministic
    assert sorted(strata.draw_longtail(pop(), 20, 8)["owner"]) != sorted(a["owner"])  # another seed, another draw
    assert strata.draw_longtail(pop(10), 50, 1).height == 10  # fewer repos than the target: take all, p = 1
    assert strata.draw_longtail(pop(10), 50, 1)["sample_prob"][0] == 1.0


def test_estimate_diff_recovers_a_known_difference():
    rng = np.random.default_rng(3)
    x1 = (rng.random(500) < 0.2).astype(float)
    x2 = (rng.random(500) < 0.5).astype(float)
    d = report.estimate_diff(x1, np.ones(500), x2, np.ones(500), "share", np.random.default_rng(1), 300)
    assert d["ci_lo"] < 0.3 < d["ci_hi"] and d["ci_lo"] > 0 and d["n"] == [500, 500]
    assert report.estimate_diff(np.array([]), np.array([]), x2, np.ones(500), "share", rng, 10)["value"] is None


def test_overall_weights_restore_population_proportions():
    # a stratum sampled at p=0.1 must weigh 10x a stratum sampled at p=1 so the weighted overall share matches the population
    x = np.array([1.0] * 10 + [0.0] * 10)  # first 10 repos (tail, p=.1) all "yes"; next 10 (established, p=1) all "no"
    w = np.array([10.0] * 10 + [1.0] * 10)
    assert abs(report._stat("share", x, w) - 100 / 110) < 1e-9
