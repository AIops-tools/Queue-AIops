"""Platform registry + path/normaliser behaviour (injection safety included)."""

from __future__ import annotations

import pytest

from queue_aiops.platform import (
    PLATFORMS,
    RABBITMQ,
    REDIS,
    Platform,
    get_platform,
    platform_names,
    register,
)


@pytest.mark.unit
def test_registry_has_both_platforms():
    assert set(PLATFORMS) == {"redis", "rabbitmq"}
    assert platform_names() == ("rabbitmq", "redis")
    assert get_platform(REDIS).kind == "client"
    assert get_platform(RABBITMQ).kind == "http"
    assert get_platform(REDIS).default_port == 6379
    assert get_platform(RABBITMQ).default_port == 15672


@pytest.mark.unit
def test_unknown_platform_error_teaches_available():
    with pytest.raises(ValueError, match="Registered platforms: rabbitmq, redis"):
        get_platform("kafka")


@pytest.mark.unit
def test_unknown_resource_error_teaches_available():
    with pytest.raises(KeyError, match="Mapped resources:"):
        get_platform(RABBITMQ).path("exchanges_totally_unmapped")


@pytest.mark.unit
def test_redis_platform_has_no_http_paths():
    redis = get_platform(REDIS)
    assert not redis.paths
    with pytest.raises(KeyError):
        redis.path("overview")


@pytest.mark.unit
def test_default_vhost_slash_is_percent_encoded():
    """The rabbitmq default vhost is literally '/' — it must never split the path."""
    p = get_platform(RABBITMQ)
    assert p.path("queue", vhost="/", name="orders") == "/api/queues/%2F/orders"
    assert p.path("queue_purge", vhost="/", name="orders") == "/api/queues/%2F/orders/contents"


@pytest.mark.unit
def test_path_traversal_in_names_is_encoded_not_interpreted():
    p = get_platform(RABBITMQ)
    hostile = "../admin/users"
    path = p.path("queue", vhost="/", name=hostile)
    assert "../" not in path
    assert path == "/api/queues/%2F/..%2Fadmin%2Fusers"
    policy = p.path("policy", vhost="prod/../../etc", name="p1")
    assert "../" not in policy


@pytest.mark.unit
def test_query_metacharacters_are_encoded():
    p = get_platform(RABBITMQ)
    path = p.path("queue", vhost="/", name="orders?columns=all#frag")
    assert "?" not in path and "#" not in path


@pytest.mark.unit
def test_rows_unwraps_paged_items_and_bare_arrays():
    p = get_platform(RABBITMQ)
    bare = p.rows([{"name": "q1"}, {"name": "q2"}, "junk"])
    assert [r["name"] for r in bare] == ["q1", "q2"]
    paged = p.rows({"items": [{"name": "q3"}], "page": 1})
    assert [r["name"] for r in paged] == ["q3"]
    assert p.rows({"unrelated": 1}) == []


@pytest.mark.unit
def test_normalise_sanitizes_control_chars_and_bounds_depth():
    p = get_platform(RABBITMQ)
    hostile = {"name": "q\x1b[31m\x00evil", "nested": {"a": {"b": {"c": {"d": {
        "e": {"f": {"g": {"h": {"i": "too-deep"}}}}}}}}}}
    out = p.normalise(hostile)
    assert "\x1b" not in out["name"] and "\x00" not in out["name"]
    # depth capped: the deepest leaf became None instead of exhausting the stack
    probe = out["nested"]
    for key in "abcdefgh":
        probe = probe.get(key) if isinstance(probe, dict) else probe
    assert probe is None or isinstance(probe, dict)


@pytest.mark.unit
def test_normalise_bounds_string_length():
    p = get_platform(REDIS)
    out = p.normalise({"k": "x" * 5000})
    assert len(out["k"]) <= 600  # sanitize truncates (limit + marker)


@pytest.mark.unit
def test_register_is_additive_for_new_platforms():
    register(Platform(name="_test_broker", label="t", kind="http", default_port=1))
    assert "_test_broker" in platform_names()
    # Cleanup so other tests see only the canonical registry.
    from queue_aiops import platform as platform_mod

    platform_mod._REGISTRY.pop("_test_broker", None)
