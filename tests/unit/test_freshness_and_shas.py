from faraday_review.evidence.freshness import is_fresh, normalize_sha

FULL = "f674025b3d0ea19c7e4f8d52ea6f9031b5c7d9e2"


def test_mixed_case_normalized():
    assert normalize_sha(FULL.upper()) == FULL
    assert is_fresh(FULL.upper(), FULL)


def test_short_sha_rejected_never_prefix_compared():
    assert normalize_sha(FULL[:12]) is None
    assert not is_fresh(FULL[:12], FULL)
    assert not is_fresh(FULL, FULL[:12])


def test_stale_head_not_fresh():
    assert not is_fresh(FULL, "a" * 40)


def test_garbage_never_fresh():
    assert not is_fresh("", "")
    assert not is_fresh("z" * 40, "z" * 40)
