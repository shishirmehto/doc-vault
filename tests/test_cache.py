"""The Redis caches: embeddings in the gateway, first answers in chat. An in-memory double stands in for Redis,
and a real client on a refused port or an unresolvable host for an outage. Fake provider, real Postgres."""

from __future__ import annotations

import dataclasses
import logging
import socket
import struct
from datetime import timedelta
from uuid import UUID

import pytest
import redis

from docvault import chat, documents, prompts
from docvault.ai import ProviderError
from docvault.cache import Cache, CacheStats, Counts
from docvault.config import Settings
from docvault.context import build_ctx
from docvault.domain import DocumentId, Purpose, VaultError
from docvault.fake_ai import FakeChat, FakeEmbedder

from .conftest import FACTS, POLICY, SAMPLES, RecordingJobs, scalar
from .test_ai import OWNER, ScriptedChat, gateway, rows
from .test_ask_stream import LIMIT_QUESTION, events
from .test_budget import NOW, capped, spent
from .test_chat import ask, ledger, normalised, ready
from .test_documents import FailingEmbedder, RecordingEmbedder, with_embedder
from .test_insights import post, with_chat
from .test_versions import TERMS, created, upload

FOLLOW_UP = "Do defence costs reduce it?"
NO_CALLS = {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}


def cache_warnings(caplog) -> list[tuple[str, str]]:
    return [(record.levelname, record.getMessage()) for record in caplog.records if record.name == "docvault.cache"]


class MemoryRedis:
    """The redis.Redis methods Cache calls, in memory, with bytes out as the real client returns them.
    While `down`, every call fails as an unreachable server does; `calls` counts the calls that reached it."""

    def __init__(self) -> None:
        self.values: dict[str, bytes] = {}
        self.ttls: dict[str, int] = {}
        self.hashes: dict[str, dict[bytes, bytes]] = {}
        self.down = False
        self.calls = 0

    def reach(self) -> None:
        self.calls += 1
        if self.down:
            raise redis.ConnectionError("Error 111 connecting to cache:6379. Connection refused.")

    def mget(self, keys):
        self.reach()
        return [self.values.get(key) for key in keys]

    def hgetall(self, name):
        self.reach()
        return dict(self.hashes.get(name, {}))

    def ping(self):
        self.reach()
        return True

    def pipeline(self, transaction=True):
        return MemoryPipeline(self)


class MemoryPipeline:
    def __init__(self, server: MemoryRedis) -> None:
        self.server, self.commands = server, []

    def set(self, key, value, ex=None):
        self.commands.append(lambda: (self.server.values.__setitem__(key, value), self.server.ttls.__setitem__(key, ex)))

    def hsetnx(self, name, key, value):
        self.commands.append(lambda: self.server.hashes.setdefault(name, {}).setdefault(key.encode(), value.encode()))

    def hincrby(self, name, key, amount):
        def run():
            fields = self.server.hashes.setdefault(name, {})
            fields[key.encode()] = str(int(fields.get(key.encode(), b"0")) + amount).encode()
        self.commands.append(run)

    def execute(self):
        self.server.reach()
        for command in self.commands:
            command()


def with_cache(ctx, cache: Cache):
    """ctx whose gateway holds `cache`, which the answer cache reads through ctx.cache."""
    return dataclasses.replace(ctx, ai=dataclasses.replace(ctx.ai, cache=cache))


def memory_cache(ctx, client) -> tuple[Cache, MemoryRedis]:
    """Points the client's app at ctx with a cache on a fresh MemoryRedis."""
    server = MemoryRedis()
    cache = Cache(server, lambda: NOW)
    client.app.state.ctx = with_cache(ctx, cache)
    return cache, server


def float32(vector: list[float]) -> list[float]:
    return list(struct.unpack(f"<{len(vector)}f", struct.pack(f"<{len(vector)}f", *vector)))


def new_chat(client, *doc_ids) -> str:
    return client.post("/chats", json={"document_ids": [str(doc_id) for doc_id in doc_ids]}).json()["id"]


# ------------------------------------------------------------------ settings and the cache itself


@pytest.mark.parametrize(("value", "parsed"), [("", None), ("redis://cache:6379/0", "redis://cache:6379/0")])
def test_an_empty_cache_url_is_no_cache(monkeypatch, value, parsed):
    monkeypatch.setenv("CACHE_URL", value)

    assert Settings(_env_file=None).cache_url == parsed


def test_inside_the_skip_window_no_call_reaches_redis_and_after_it_the_next_call_tries_again(caplog):
    server, now = MemoryRedis(), [NOW]
    cache = Cache(server, clock=lambda: now[0])
    cache.count("answer", 1, 0)
    server.down = True
    probed_down = cache.probe()
    warned_by_probe = cache_warnings(caplog)

    failed = cache.get_many(["k"])
    server.down = False
    now[0] = NOW + timedelta(seconds=59)
    within = (cache.get_many(["k"]), cache.stats(), cache.probe())
    cache.set_many({"k": b"v"}, 60)
    cache.count("answer", 1, 0)
    calls_within = server.calls
    now[0] = NOW + timedelta(seconds=61)
    after = (cache.get_many(["k"]), cache.stats())

    assert (probed_down, warned_by_probe, failed) == ("unreachable", [], [None])
    assert within == ([None], None, "ok")
    assert calls_within == 4   # the first count, the failed probe, the failed lookup and the probe inside the window
    assert after == ([None], CacheStats(Counts(0, 0), Counts(1, 0), NOW))
    assert cache_warnings(caplog) == [("WARNING", "cache unreachable; skipping the cache for 60 s: "
                                                  "Error 111 connecting to cache:6379. Connection refused.")]


@pytest.mark.parametrize(("url", "probed"), [(None, "off"), ("redis://127.0.0.1:1/0", "unreachable")])
def test_the_context_builds_its_cache_from_cache_url(ctx, url, probed):
    built = build_ctx(ctx.settings.model_copy(update={"cache_url": url}), RecordingJobs())
    try:
        assert built.cache.probe() == probed
    finally:
        built.db.close()


# ------------------------------------------------------------------ embeddings


def test_a_second_embed_of_the_same_texts_comes_from_the_cache_with_no_ledger_row(db):
    embedder = RecordingEmbedder()
    server = MemoryRedis()
    ai = dataclasses.replace(gateway(db, ScriptedChat([]), embedder=embedder), cache=Cache(server, lambda: NOW))
    texts = ["Each occurrence limit", "General aggregate limit", "Products completed operations"]

    first = ai.embed(texts, purpose=Purpose.EMBED_QUERY, owner=OWNER)
    second = ai.embed(texts, purpose=Purpose.EMBED_QUERY, owner=OWNER)

    assert second == [float32(vector) for vector in first]
    assert first == FakeEmbedder().embed(texts, timeout_s=1)[0]
    assert embedder.batches == [texts]
    assert rows(db, "purpose, model") == [("embed_query", "fake-hash")]
    assert set(server.ttls.values()) == {30 * 24 * 3600}
    assert all(key.startswith("docvault:emb:fake-hash:1536:") for key in server.values)


def test_a_batch_paid_for_before_a_later_batch_failed_is_not_embedded_again(db):
    cache = Cache(MemoryRedis(), lambda: NOW)
    texts = ["Each occurrence limit", "General aggregate limit", "Products completed operations",
             "Personal advertising injury"]
    failing = FailingEmbedder(good_calls=1, error=ProviderError("server_error", retryable=True))
    ai = dataclasses.replace(gateway(db, ScriptedChat([]), embedder=failing, embed_batch_size=2), cache=cache)
    with pytest.raises(VaultError):
        ai.embed(texts, purpose=Purpose.EMBED_CHUNKS, owner=OWNER)
    retried = RecordingEmbedder()

    vectors = dataclasses.replace(ai, embedder=retried).embed(texts, purpose=Purpose.EMBED_CHUNKS, owner=OWNER)

    assert retried.batches == [texts[2:]]
    assert vectors == [*map(float32, FakeEmbedder().embed(texts[:2], timeout_s=1)[0]),
                       *FakeEmbedder().embed(texts[2:], timeout_s=1)[0]]
    assert rows(db, "ok, error_code") == [(True, None), (False, "server_error"), (True, None)]


def test_only_the_misses_go_to_the_provider_each_once_in_batches_and_the_order_is_kept(db):
    embedder = RecordingEmbedder()
    cache = Cache(MemoryRedis(), lambda: NOW)
    ai = dataclasses.replace(gateway(db, ScriptedChat([]), embedder=embedder, embed_batch_size=2), cache=cache)
    a, b, c, d, e = ("Each occurrence limit", "General aggregate limit", "Products completed operations",
                     "Personal advertising injury", "Medical expense limit")
    ai.embed([b, d], purpose=Purpose.EMBED_CHUNKS, owner=OWNER)

    mixed = ai.embed([a, b, c, a, d, e], purpose=Purpose.EMBED_CHUNKS, owner=OWNER)

    fresh = dict(zip([a, b, c, d, e], FakeEmbedder().embed([a, b, c, d, e], timeout_s=1)[0], strict=True))
    assert mixed == [fresh[a], float32(fresh[b]), fresh[c], fresh[a], float32(fresh[d]), fresh[e]]
    assert embedder.batches == [[b, d], [a, c], [e]]
    assert len(rows(db, "id")) == 3
    assert cache.stats() == CacheStats(Counts(hits=2, misses=6), Counts(0, 0), NOW)


# ------------------------------------------------------------------ answers


def test_every_input_to_a_first_answer_is_in_its_cache_key(ctx, monkeypatch):
    class OtherModel(FakeChat):
        models = {"main": "gpt-6.1-terra", "small": "fake-chat"}

    class StricterGate(FakeEmbedder):
        min_similarity = 0.5

    a, b = DocumentId(UUID(int=1)), DocumentId(UUID(int=2))
    docs = {a: b"\x01" * 32, b: b"\x02" * 32}

    def key(context=ctx, scope=docs, question=LIMIT_QUESTION) -> str:
        return chat.answer_key(context, scope, question)

    def setting(**update) -> object:
        return dataclasses.replace(ctx, settings=ctx.settings.model_copy(update=update))

    variants = {
        "a document's bytes": key(scope={a: b"\x03" * 32, b: docs[b]}),
        "the documents": key(scope={a: docs[a]}),
        "the question": key(question="What is the general aggregate limit?"),
        "the main model": key(context=with_chat(ctx, OtherModel())),
        "its reasoning effort": key(context=setting(openai_chat_reasoning_effort="high")),
        "the embedding space": key(context=with_embedder(ctx, FakeEmbedder(dimensions=256))),
        "the gate's min_similarity": key(context=with_embedder(ctx, StricterGate())),
        "retrieval_k": key(context=setting(retrieval_k=4)),
        "retrieval_per_doc_min": key(context=setting(retrieval_per_doc_min=1)),
    }
    with monkeypatch.context() as patched:
        patched.setattr(prompts, "ANSWER_VERSION", "answer-v99")
        variants["the answer prompt"] = key()
    with monkeypatch.context() as patched:
        patched.setattr(chat, "ANSWER_CACHE_FORMAT", 99)
        variants["the cache format"] = key()

    base = key()
    assert [name for name, variant in variants.items() if variant == base] == []
    assert len(set(variants.values())) == len(variants)
    assert key(scope={b: docs[b], a: docs[a]}, question="  what IS the each   OCCURRENCE limit?") == base
    assert (base[:16], len(base)) == ("docvault:answer:", 16 + 64)


def test_the_same_first_question_in_a_new_chat_is_answered_from_the_cache_with_no_model_call(client, ctx):
    cache, server = memory_cache(ctx, client)
    policy = ready(ctx, POLICY)

    first = ask(client, new_chat(client, policy), LIMIT_QUESTION)
    again_chat = new_chat(client, policy)
    again = ask(client, again_chat, LIMIT_QUESTION)
    respelled = ask(client, new_chat(client, policy), "what is the EACH   occurrence limit?")

    same = ("status", "answer", "citations", "suggestions")
    assert (first["cached"], again["cached"], respelled["cached"]) == (False, True, True)
    assert (first["status"], len(first["citations"]), first["suggestions"]) == ("answered", 1, [
        "What is the Policy Number?", "What is the Named Insured?", "What is the Mailing Address?"])
    assert {k: again[k] for k in same} == {k: first[k] for k in same} == {k: respelled[k] for k in same}
    assert (again["question"], again["interpreted_as"], again["usage"]) == (LIMIT_QUESTION, LIMIT_QUESTION, NO_CALLS)
    assert respelled["question"] == "what is the EACH   occurrence limit?"
    assert ledger(ctx) == {first["id"]: ["embed_query", "answer"]}
    assert client.get(f"/chats/{again_chat}/messages").json()["turns"] == [again]
    assert cache.stats() == CacheStats(embedding=Counts(hits=0, misses=1), answer=Counts(hits=2, misses=1), counted_since=NOW)
    assert [ttl for key, ttl in server.ttls.items() if key.startswith("docvault:answer:")] == [30 * 24 * 3600]


def test_a_follow_up_is_never_looked_up_or_stored(client, ctx):
    cache, _ = memory_cache(ctx, client)
    policy = ready(ctx, POLICY)
    first_chat = new_chat(client, policy)
    ask(client, first_chat, LIMIT_QUESTION)
    follow_up = ask(client, first_chat, FOLLOW_UP)

    asked_first = ask(client, new_chat(client, policy), FOLLOW_UP)
    other_chat = new_chat(client, policy)
    ask(client, other_chat, "What is the general aggregate limit?")
    limit_as_follow_up = ask(client, other_chat, LIMIT_QUESTION)

    assert (follow_up["cached"], asked_first["cached"], limit_as_follow_up["cached"]) == (False, False, False)
    # The follow-up's query vector is the first chat's, from the embedding cache.
    assert (ledger(ctx)[asked_first["id"]], ledger(ctx)[limit_as_follow_up["id"]]) == (
        ["embed_query", "answer"], ["condense", "answer"])
    assert cache.stats().answer == Counts(hits=0, misses=3)


def test_a_chat_on_version_2_does_not_get_the_answer_given_on_version_1(client, ctx):
    memory_cache(ctx, client)
    v1 = created(upload(ctx, TERMS.format("$2,500 per occurrence"), "terms.txt"))
    v2 = created(upload(ctx, TERMS.format("$5,000 per occurrence"), "terms.txt"))
    for doc in (v1, v2):
        documents.run_index(ctx, doc.id)
    question = "What is the property damage deductible?"

    on_v1 = ask(client, new_chat(client, v1.id), question)
    on_v2 = ask(client, new_chat(client, v2.id), question)

    assert (v2.version.number, on_v1["status"], on_v2["status"]) == (2, "answered", "answered")
    assert [(turn["cached"], turn["citations"][0]["quote"]) for turn in (on_v1, on_v2)] == [
        (False, "Property Damage Deductible: $2,500 per occurrence"),
        (False, "Property Damage Deductible: $5,000 per occurrence")]
    assert ledger(ctx) == {on_v1["id"]: ["embed_query", "answer"], on_v2["id"]: ["answer"]}   # the same query vector


@pytest.mark.parametrize(("question", "reason", "calls"), [
    ("Is flood damage covered?", "no_evidence", [["embed_query"], None]),
    ("Roofing?", "model_declined", [["embed_query", "answer"], ["answer"]]),
], ids=["no_evidence", "model_declined"])
def test_a_first_question_that_was_not_answered_is_asked_afresh(client, ctx, question, reason, calls):
    cache, server = memory_cache(ctx, client)
    policy = ready(ctx, POLICY)

    turns = [ask(client, new_chat(client, policy), question) for _ in range(2)]

    assert [(turn["reason"], turn["cached"]) for turn in turns] == [(reason, False)] * 2
    assert [ledger(ctx).get(turn["id"]) for turn in turns] == calls   # the query vector is cached the second time
    assert (cache.stats().answer, [key for key in server.values if key.startswith("docvault:answer:")]) == (
        Counts(hits=0, misses=2), [])


def test_a_streamed_cache_hit_is_one_delta_without_markers_then_the_turn(client, ctx):
    memory_cache(ctx, client)
    policy = ready(ctx, POLICY)
    first = ask(client, new_chat(client, policy), LIMIT_QUESTION)

    response = client.post(f"/chats/{new_chat(client, policy)}/messages/stream", json={"question": LIMIT_QUESTION})

    limit = normalised(FACTS["policy-each-occurrence-limit"]["text"])
    sent = events(response.text)
    assert [event for event, _ in sent] == ["delta", "turn"]
    assert sent[0][1] == {"text": limit}
    assert (first["answer"], sent[1][1]["answer"], sent[1][1]["cached"], sent[1][1]["usage"]) == (
        f"{limit} [1]", f"{limit} [1]", True, NO_CALLS)


def test_over_the_spending_cap_a_cache_hit_still_answers_and_a_miss_gets_429(client, ctx):
    memory_cache(ctx, client)
    policy = ready(ctx, POLICY)
    first = ask(client, new_chat(client, policy), LIMIT_QUESTION)
    spent(ctx.db, "5.00", NOW - timedelta(hours=1))
    client.app.state.ctx = capped(client.app.state.ctx, "5")
    miss = "What is the general aggregate limit?"

    plain_hit = client.post(f"/chats/{new_chat(client, policy)}/messages", json={"question": LIMIT_QUESTION})
    streamed_hit = client.post(f"/chats/{new_chat(client, policy)}/messages/stream", json={"question": LIMIT_QUESTION})
    plain_miss = client.post(f"/chats/{new_chat(client, policy)}/messages", json={"question": miss})
    streamed_miss = client.post(f"/chats/{new_chat(client, policy)}/messages/stream", json={"question": miss})

    assert (plain_hit.status_code, plain_hit.json()["cached"], plain_hit.json()["answer"]) == (
        200, True, first["answer"])
    assert [(event, data.get("cached")) for event, data in events(streamed_hit.text)] == [("delta", None),
                                                                                         ("turn", True)]
    assert [(r.status_code, r.headers["content-type"], r.json()["error"]["code"]) for r in (plain_miss, streamed_miss)
            ] == [(429, "application/json", "quota_exceeded")] * 2
    assert ledger(ctx) == {first["id"]: ["embed_query", "answer"]}
    assert scalar(ctx.db, "SELECT count(*) FROM chat_turns") == 3


@pytest.mark.parametrize("url", ["redis://127.0.0.1:1/0", "redis://cache:6379/0"], ids=["refused", "unresolvable"])
def test_an_unreachable_cache_changes_nothing_but_one_warning_a_minute(client, ctx, monkeypatch, caplog, url):
    resolve = socket.getaddrinfo

    def no_cache_host(host, *args, **kwargs):
        if host == "cache":
            raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")
        return resolve(host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", no_cache_host)
    now = [NOW]
    ctx = with_cache(ctx, Cache.from_url(url, clock=lambda: now[0]))
    client.app.state.ctx = ctx
    caplog.set_level(logging.WARNING, logger="docvault.cache")

    doc_id = post(client, SAMPLES / POLICY)
    documents.run_index(ctx, doc_id)
    policy_chat = new_chat(client, doc_id)
    first = ask(client, policy_chat, LIMIT_QUESTION)
    follow_up = ask(client, policy_chat, FOLLOW_UP)
    warned_within = len(cache_warnings(caplog))
    now[0] = NOW + timedelta(seconds=61)
    again = ask(client, new_chat(client, doc_id), LIMIT_QUESTION)

    assert client.get(f"/documents/{doc_id}").json()["status"] == "ready"
    assert [(turn["status"], turn["cached"]) for turn in (first, follow_up, again)] == [("answered", False)] * 3
    assert ledger(ctx) == {first["id"]: ["embed_query", "answer"], follow_up["id"]: ["condense", "embed_query", "answer"],
                           again["id"]: ["embed_query", "answer"]}
    assert scalar(ctx.db, "SELECT count(*) FROM llm_calls WHERE purpose = 'embed_chunks'") == 3   # 6 chunks, 2 a batch
    assert (warned_within, [(level, message.split(": ")[0]) for level, message in cache_warnings(caplog)]) == (
        1, [("WARNING", "cache unreachable; skipping the cache for 60 s")] * 2)
    assert client.get("/metrics/processing").json()["cache"] is None


# ------------------------------------------------------------------ metrics and health


def test_processing_metrics_show_the_cache_counts_since_the_first_one(client, ctx):
    off = client.get("/metrics/processing").json()["cache"]
    cache, server = memory_cache(ctx, client)
    before_any = client.get("/metrics/processing").json()["cache"]
    cache.count("embedding", hits=3, misses=2)
    cache.count("answer", hits=1, misses=4)
    counted = client.get("/metrics/processing").json()["cache"]
    server.down = True
    down = client.get("/metrics/processing").json()["cache"]

    zero = {"hits": 0, "misses": 0}
    assert (off, before_any, down) == (None, {"embedding": zero, "answer": zero, "counted_since": None}, None)
    assert counted == {"embedding": {"hits": 3, "misses": 2}, "answer": {"hits": 1, "misses": 4},
                       "counted_since": "2030-01-15T21:30:00Z"}


def test_health_reports_the_cache_and_never_fails_because_of_it(client, ctx, monkeypatch):
    monkeypatch.setattr(redis.Redis, "from_url", lambda *args, **kwargs: MemoryRedis())   # the broker is up
    off = client.get("/health")
    _, server = memory_cache(ctx, client)
    up = client.get("/health")
    server.down = True
    down = client.get("/health")

    assert [(r.status_code, r.json()["redis"], r.json()["cache"]) for r in (off, up, down)] == [
        (200, "ok", "off"), (200, "ok", "ok"), (200, "ok", "unreachable")]
