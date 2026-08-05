import json, subprocess, sys, textwrap
from pathlib import Path
sys.path.insert(0, "src")
from faraday_review.canonical import canonical_bytes, digest, bundle_digest

def test_key_order_irrelevant():
    a = {"b": 1, "a": {"z": [3, 2], "y": 1}}
    b = {"a": {"y": 1, "z": [3, 2]}, "b": 1}
    assert canonical_bytes(a) == canonical_bytes(b)

def test_list_order_significant():
    assert canonical_bytes({"x": [1, 2]}) != canonical_bytes({"x": [2, 1]})

def test_nan_rejected():
    try:
        canonical_bytes({"x": float("nan")}); assert False
    except ValueError:
        pass

def test_unicode_stable():
    assert canonical_bytes({"k": "é"}) == '{"k":"é"}\n'.encode("utf-8")

def test_bundle_digest_ignores_line_endings(tmp_path: Path):
    for name, data in (("crlf", b"a: 1\r\nb: 2\r\n"), ("lf", b"a: 1\nb: 2\n")):
        d = tmp_path / name; d.mkdir()
        (d / "policy.yaml").write_bytes(data)
    assert bundle_digest(tmp_path / "crlf") == bundle_digest(tmp_path / "lf")

def test_bundle_digest_detects_content_change(tmp_path: Path):
    d1 = tmp_path / "a"; d1.mkdir(); (d1 / "p.yaml").write_text("threshold: 3\n")
    d2 = tmp_path / "b"; d2.mkdir(); (d2 / "p.yaml").write_text("threshold: 4\n")
    assert bundle_digest(d1) != bundle_digest(d2)

def test_bundle_digest_detects_added_file(tmp_path: Path):
    d1 = tmp_path / "a"; d1.mkdir(); (d1 / "p.yaml").write_text("x: 1\n")
    before = bundle_digest(d1)
    (d1 / "q.yaml").write_text("y: 2\n")
    assert bundle_digest(d1) != before

SEED_PROBE = textwrap.dedent('''
    import sys, json
    sys.path.insert(0, "src")
    from faraday_review.canonical import digest
    obj = {"providers": sorted({"a","b","c"}), "reasons": sorted({"z","y","x"}),
           "nested": {"k": sorted({"q","p"})}}
    print(digest(obj))
''')

def test_digest_stable_across_hash_seeds():
    out = set()
    for seed in ("0", "1", "7", "19", "43", "101"):
        p = subprocess.run([sys.executable, "-c", SEED_PROBE],
                           capture_output=True, text=True,
                           env={"PYTHONHASHSEED": seed, "PATH": "/usr/bin:/bin"})
        assert p.returncode == 0, p.stderr
        out.add(p.stdout.strip())
    assert len(out) == 1, out
