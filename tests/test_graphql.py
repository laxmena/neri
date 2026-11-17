import httpx

from neri import sample


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_retries_empty_body_5xx_and_secondary_rate_limit(monkeypatch):
    monkeypatch.setattr(sample.time, "sleep", lambda s: None)
    calls = []

    def handler(req):
        n = len(calls); calls.append(n)
        if n == 0:
            return httpx.Response(200, content=b"")
        if n == 1:
            return httpx.Response(502, text="<html>Bad Gateway</html>")
        if n == 2:
            return httpx.Response(403, headers={"retry-after": "0"}, text='{"message":"secondary rate limit"}')
        return httpx.Response(200, json={"data": {"r0": {"nameWithOwner": "a/b"}}})

    logs = []
    out = sample._fetch_batch(_client(handler), [("a", "b")], logs.append)
    assert out == {("a", "b"): {"nameWithOwner": "a/b"}} and len(calls) == 4
    assert any("non-JSON/empty" in l for l in logs) and any("502" in l for l in logs) and any("rate limit" in l for l in logs)


def test_failing_batch_is_split_and_bad_repo_is_not_cached(monkeypatch):
    monkeypatch.setattr(sample.time, "sleep", lambda s: None)

    def handler(req):
        q = req.content.decode()
        if "bad" in q:
            return httpx.Response(502, text="boom")
        return httpx.Response(200, json={"data": {f"r{j}": {"nameWithOwner": "ok"} for j in range(4)}})

    out = sample._fetch_batch(_client(handler), [("a", "x"), ("a", "bad")], lambda m: None)
    assert ("a", "bad") not in out and ("a", "x") in out
