import polars as pl

from neri import fetch, strata


def est_frame():
    rows = []
    for i in range(30):  # stratum A: 30 sampled (10 are dev), stratum B: 6 sampled (none dev)
        rows.append({"owner": f"a{i}", "repo": "r", "star_stratum": "10-99", "type_prelim": "CLAUDE only", "sample_prob": 0.5})
    for i in range(6):
        rows.append({"owner": f"b{i}", "repo": "r", "star_stratum": "100-999", "type_prelim": "CLAUDE only", "sample_prob": 1.0})
    return pl.DataFrame(rows)


def test_sum_of_weights_over_nondev_rows_equals_nondev_population_per_stratum():
    est = est_frame()
    dev = pl.DataFrame({"owner": [f"a{i}" for i in range(10)], "repo": ["r"] * 10})
    n_nd = {"10-99|CLAUDE only": 400, "100-999|CLAUDE only": 6}  # non-dev populations
    out = strata.apply_nondev_probs(est, dev, n_nd)
    nd = out.join(dev, on=["owner", "repo"], how="anti").with_columns(ws=1.0 / pl.col("sample_prob"))
    got = dict(nd.group_by(pl.concat_str(["star_stratum", "type_prelim"], separator="|")).agg(pl.col("ws").sum()).iter_rows())
    assert abs(got["10-99|CLAUDE only"] - 400) < 1e-6 and abs(got["100-999|CLAUDE only"] - 6) < 1e-6


def test_overall_weights_do_not_over_represent_the_established_stratum():
    est = strata.apply_nondev_probs(est_frame(), None, {"10-99|CLAUDE only": 30, "100-999|CLAUDE only": 6})
    tail = strata.draw_longtail(pl.DataFrame({"owner": [f"t{i}" for i in range(900)], "repo": ["r"] * 900, "stars": [0] * 900}), 100, 1)
    w_est, w_tail = (1.0 / est["sample_prob"]).sum(), (1.0 / tail["sample_prob"]).sum()
    assert abs(w_est - 36) < 1e-6 and abs(w_tail - 900) < 1e-6  # each stratum's weights add up to its own population
    assert abs(w_tail / (w_est + w_tail) - 900 / 936) < 1e-9    # so the tail is ~96% of the overall population, not ~74% of the sample


def lookup():
    buckets = {f"CLAUDE.md|{lo}|{hi}": {"id": f"CLAUDE.md|{lo}|{hi}", "name": "CLAUDE.md", "lo": lo, "hi": hi}
               for lo, hi in ((200, 999), (1000, 4999), (5000, 102400))}
    return strata.bucket_lookup(buckets)


def f(name="CLAUDE.md", size=500, root=True, symlink=False):
    return {"name": name, "size": size, "root": root, "symlink": symlink}


def test_inclusion_probability_counts_every_agent_file_in_its_own_bucket():
    p = {"CLAUDE.md": 0.1}
    one = strata.repo_incl_prob([f(size=500)], p, lookup())
    three = strata.repo_incl_prob([f(size=500), f(size=2000, root=False), f(size=9000, root=False)], p, lookup())
    same_bucket = strata.repo_incl_prob([f(size=500), f(size=600, root=False)], p, lookup())
    assert abs(one - 0.1) < 1e-9 and abs(three - (1 - 0.9 ** 3)) < 1e-9
    assert abs(same_bucket - 0.1) < 1e-9  # two files in ONE bucket are sampled together: a single chance
    assert strata.repo_incl_prob([f(size=500, symlink=True), f(size=50)], p, lookup()) == 0  # nothing in range: unweightable
    assert abs(strata.repo_incl_prob([f(size=500)], p, lookup(), extra=2) - (1 - 0.9 ** 3)) < 1e-9  # files beyond the fetch cap


def test_nested_only_repo_gets_a_positive_inclusion_probability():
    assert abs(strata.repo_incl_prob([f(size=2000, root=False)], {"CLAUDE.md": 0.06}, lookup()) - 0.06) < 1e-9


def test_choose_primary_is_shallowest_then_largest_and_prefers_claude_md():
    files = [{"path": "a/b/CLAUDE.md", "name": "CLAUDE.md", "size": 9000}, {"path": "docs/CLAUDE.md", "name": "CLAUDE.md", "size": 300},
             {"path": "x/CLAUDE.md", "name": "CLAUDE.md", "size": 800}, {"path": "AGENTS.md", "name": "AGENTS.md", "size": 5000}]
    assert fetch.choose_primary(files)["path"] == "x/CLAUDE.md"  # depth 1, larger than docs/CLAUDE.md; ignores the root-depth AGENTS.md
    assert fetch.choose_primary([{"path": "p/AGENTS.md", "name": "AGENTS.md", "size": 1}])["path"] == "p/AGENTS.md"  # fallback
