from backend.api import server as srv


def test_per_key_rate_limiter_fingerprints_bucket_keys():
    limiter = srv._PerKeyRateLimiter(max_calls=1, window_seconds=60.0)

    assert limiter.allow("sk-ant-api-key") is True
    assert limiter.allow("sk-ant-api-key") is False
    assert "sk-ant-api-key" not in limiter._buckets
    assert srv._fingerprint("sk-ant-api-key") in limiter._buckets


def test_per_key_rate_limiter_keeps_keys_partitioned():
    limiter = srv._PerKeyRateLimiter(max_calls=1, window_seconds=60.0)

    assert limiter.allow("key-a") is True
    assert limiter.allow("key-b") is True
    assert len(limiter._buckets) == 2