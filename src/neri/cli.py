"""neri CLI. Commands map 1:1 to milestones in docs/KICKOFF.md; stubs are filled in milestone by milestone."""
import typer

app = typer.Typer(no_args_is_help=True, add_completion=False)


def _todo(m: str) -> None:
    typer.echo(f"not implemented yet — see docs/KICKOFF.md {m}")
    raise typer.Exit(2)


@app.command()
def env():
    """M0: check gh auth, rate limits, data dirs."""
    from . import config, gh

    ok = True
    auth, line = gh.auth_ok()
    typer.echo(f"gh auth: {'ok' if auth else 'FAIL'} ({line})")
    ok &= auth
    if auth:
        try:
            for k, v in gh.rate_limits().items():
                typer.echo(f"rate {k:8s} {v['remaining']}/{v['limit']} (reset in {v['reset_in_s']}s)")
        except Exception as e:  # network/HTTP errors; message only, never headers
            typer.echo(f"rate limits: FAIL ({type(e).__name__})")
            ok = False
    created = config.ensure_dirs()
    typer.echo(f"data dirs: ok ({len(config.DATA_DIRS)} dirs, {len(created)} created)")
    snap = config.load()["snapshot"]["date"]
    typer.echo(f"snapshot_date: {snap or 'MISSING'}")
    ok &= bool(snap)
    typer.echo("ENV OK" if ok else "ENV FAIL")
    raise typer.Exit(0 if ok else 1)


@app.command()
def enumerate(summary: bool = False, max_minutes: float = typer.Option(None, help="time box"),
              checkpoint: int = typer.Option(None, help="freeze a whole-bucket prefix of ~N candidates")):
    """M1: code-search candidates, size-partitioned."""
    from . import enumerate as en

    if checkpoint:
        typer.echo(en.checkpoint(checkpoint))
        return
    if summary:
        typer.echo("\n".join(en.summary()))
        return
    en.run(max_minutes=max_minutes, log=lambda m: typer.echo(m, err=False))


@app.command()
def sample(checkpoint: str = typer.Option(None, help="use data/raw/checkpoints/c<N>.parquet, e.g. 1000")):
    """M2: split, metadata, filters, stratified sample (explore only gets metadata)."""
    from . import sample as sm

    typer.echo("\n".join(sm.run(checkpoint)))


@app.command()
def fetch(summary: bool = False, checkpoint: str = typer.Option(None), limit: int = typer.Option(None),
          skills: bool = typer.Option(False, help="fetch .claude/skills/*/SKILL.md for explore repos that have them"),
          truncated: bool = typer.Option(False, help="resolve flagged paths in truncated-tree repos with per-directory walks"),
          subdirs: bool = typer.Option(False, help="fetch manifests from directories that agent files `cd` into")):
    """M3: trees, agent files, imports, manifests (explore only)."""
    from . import fetch as fe

    if summary:
        typer.echo("\n".join(fe.summary()))
        return
    if skills:
        fe.run_skills(checkpoint, log=typer.echo)
        return
    if truncated:
        fe.resolve_truncated(checkpoint, log=typer.echo)
        return
    if subdirs:
        fe.run_subdirs(checkpoint, log=typer.echo)
        return
    fe.run(checkpoint, limit, log=typer.echo)


@app.command()
def features(summary: bool = False, checkpoint: str = typer.Option(None),
             table: str = typer.Option(None, help="with --summary: 'commands' or 'skills' census table"),
             include_dev: bool = typer.Option(False, help="include the c5000 DEV repos (tuning only; never for reported numbers)")):
    """M4: deterministic features A1-A5, A7 (+ commands census and skills)."""
    from . import features as ft

    if table:
        typer.echo("\n".join(ft.table(checkpoint, table, include_dev)))
    else:
        typer.echo("\n".join(ft.summary(checkpoint, include_dev) if summary else ft.run(checkpoint)))


@app.command()
def validate(checkpoint: str = typer.Option(None), round: int = 1, score: bool = False, tiers: bool = False,
             kappa: bool = False, indep: bool = False):
    """M5: build wrongness validation sheet (or --score the labels)."""
    from . import validate as va

    if kappa:
        typer.echo("\n".join(va.kappa(round)))
    else:
        typer.echo("\n".join(va.score(round, "_indep" if indep else "")) if score else va.build(checkpoint, round))


@app.command("llm-validate")
def llm_validate():
    """M6: Batch-API topic label validation (budget-capped)."""
    _todo("M6")


@app.command()
def history(summary: bool = False):
    """M7: file version history on subsample + model timeline."""
    _todo("M7")


@app.command()
def report(checkpoint: str = typer.Option(None), bootstrap: int = typer.Option(None, help="override resamples (default config)")):
    """M8: reports/numbers.json (weighted + unweighted, bootstrap CIs, non-dev only) + raw figures in figures/explore/."""
    from . import report as rp

    out = rp.build(checkpoint, bootstrap)
    figs = rp.figures(checkpoint)
    typer.echo("\n".join(rp.summary(out)))
    typer.echo("figures: " + ", ".join(figs))


@app.command()
def explore(checkpoint: str = typer.Option(None, help="default: latest checkpoint with features")):
    """Write reports/explorer.html (LOCAL ONLY, gitignored): distributions + sortable per-repo table."""
    from . import explorer

    typer.echo(explorer.write(checkpoint))


@app.command()
def view(checkpoint: str = typer.Option(None), only_nondev: bool = typer.Option(False, help="skip the c5000 DEV repos")):
    """Create data/view/<owner>__<repo>/<path> symlinks to fetched blobs for the explore sample (local only)."""
    from . import view as vw

    typer.echo("\n".join(vw.run(checkpoint, include_dev=not only_nondev)))


@app.command("freeze-pointers")
def freeze_pointers(force: bool = False):
    """Write the frozen candidate pointer list + manifest to data/public/ (no file text)."""
    from . import pointers

    typer.echo("\n".join(pointers.freeze(force)))


@app.command()
def strata(checkpoint: str = typer.Option("50000")):
    """Write strata.json: non-dev population sizes N_h of the established strata and the long tail."""
    from . import sample as sm

    t = sm.build_strata(checkpoint, log=lambda m: None)
    typer.echo(f"established non-dev N_h={t['established']['N_nondev']:,} {t['established']['N_nondev_by_stratum']}; long tail non-dev N_h={t['long_tail']['N_nondev']:,}")


@app.command()
def longtail(checkpoint: str = "50000", poll: int = 120, now: bool = typer.Option(False, help="do not wait for the pipeline marker")):
    """Long-tail stratum (stars 0-9): draw ~2,500 repos, then fetch/features for them (waits for the checkpoint pipeline)."""
    from . import longtail as lt

    lt.run(checkpoint, poll, wait=not now)


@app.command()
def chain():
    """Unattended post-1 chain: enumeration -> freeze -> sample -> fetch -> features -> report -> explorer -> view."""
    from . import chain as ch

    ch.run()


@app.command()
def pipeline(checkpoints: str = "25000,50000", poll: int = 120):
    """Background: for each checkpoint N, wait for N candidates, then run sample -> fetch -> features."""
    from . import pipeline as pp

    pp.run([int(x) for x in checkpoints.split(",")], poll)


@app.command()
def check(path: str = ".", json: bool = False):
    """M9: lint a local repo's CLAUDE.md / AGENTS.md."""
    _todo("M9")


if __name__ == "__main__":
    app()


@app.command("longtail-boost")
def longtail_boost(cutoff: str = typer.Option("17:00", help="local HH:MM hard stop"), finalize: bool = typer.Option(False, help="only rewrite longtail.parquet to the completed prefix")):
    """Fetch ALL eligible non-dev long-tail repos in seeded random order (resumable); then --finalize before features/report."""
    from . import boost
    if finalize:
        typer.echo("\n".join(boost.finalize()))
        return
    boost.run(cutoff)
    typer.echo("\n".join(boost.finalize()))


@app.command("fetch-readmes")
def fetch_readmes(workers: int = 12):
    """Fetch root README* of every explore repo at its pinned sha (raw only; resumable)."""
    from . import readmes
    readmes.run(workers, log=typer.echo)


@app.command("fetch-settings")
def fetch_settings(workers: int = 12):
    """D1: fetch committed .claude/settings*.json for explore repos (raw only)."""
    from . import readmes
    readmes.run_settings(workers, log=typer.echo)


@app.command("age-baseline")
def age_baseline_cmd():
    """B: counts of all active public repos by creation-age band (repo search total_count)."""
    from . import agebase
    agebase.run(typer.echo)


@app.command("or-label")
def or_label():
    """Blind second labeler on OpenRouter (non-Anthropic model) for link_check / agentsmd_check rows. Hard $ cap."""
    from . import orlabel
    typer.echo(str(orlabel.run(typer.echo)))


@app.command("extras")
def extras_cmd():
    """README overlap, CLAUDE-vs-AGENTS drift, package-manager check, essentials scorecard inputs (deterministic)."""
    from . import postx, postz
    typer.echo("\n".join(postx.run()))
    typer.echo("\n".join(postz.run()))  # round 3: signature, hooks, co-presence
    typer.echo(postz.matrix())


@app.command("render-draft")
def render_draft():
    """Render docs/post1-draft.md placeholders from reports/numbers.json."""
    from . import render
    typer.echo("\n".join(render.render()))


@app.command("post-boost")
def post_boost():
    """After the long-tail boost: finalize prefix, then features + report + figures + widget + cmd check + public export + draft render."""
    from . import boost, chain, render
    msg = boost.finalize()
    typer.echo("\n".join(msg))
    import time as _t
    with open("JOURNAL.md", "a") as f:
        f.write(f"\n## {_t.strftime('%Y-%m-%d %H:%M')} long-tail boost finished\n- {msg[0]}. Features, report, figures, widget, cmd check, public export and draft render re-run automatically (data/log/post_boost.log).\n")
    chain.step_features()
    chain.step_report()
    typer.echo("\n".join(render.render()))
    from . import carousel, config as _c
    d = _c.ROOT / "docs"
    typer.echo("\n".join(render.render(d / "linkedin-post.md", d / "linkedin-post.rendered.md")))
    typer.echo(carousel.build())


@app.command("popular-full")
def popular_full(finalize: bool = typer.Option(False, help="only rewrite explore.parquet")):
    """Fetch ALL eligible non-dev stars>=10 repos (resumable), then extend explore.parquet to the full population."""
    from . import popfull
    if not finalize:
        popfull.run()
    typer.echo(popfull.finalize())
