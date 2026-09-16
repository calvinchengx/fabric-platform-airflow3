"""Repo-boundary tests. No Docker, no emulator, no product.

ONE TEST, and the Makefile says why: this platform shipped no tests/ at all
while both sibling airflow3 platforms carry repo-boundary suites, and porting
those is its own change. What is here is the wiring that makes this cell's
nightly able to fail for the right reason, which is the thing that must not be
removable by accident.
"""

from __future__ import annotations

import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent


def test_the_acceptance_run_asserts_the_numbers_and_not_only_the_run():
    """A nightly that proves the DAG RAN proves nothing about the answer.

    G50: across all seven platforms with an acceptance workflow, none compared a
    snapshot against an expected value. This cell was the worst of them -- the
    DAG published no snapshot at all, so there was not even a file to ignore.

    THREE THINGS HAVE TO HOLD TOGETHER and none of them is checkable by reading
    one file: the DAG writes to compose's PRODUCT_SNAPSHOT, `make snapshot`
    copies that same path out, and the acceptance run reads what it copied to.
    """
    raw = (ROOT / ".github" / "workflows" / "acceptance.yml").read_text(encoding="utf-8")
    wf = "\n".join(ln for ln in raw.splitlines() if not ln.lstrip().startswith("#"))
    for needed in ("make snapshot", "scripts/assert_snapshot.py"):
        assert needed in wf, f"the acceptance run never runs `{needed}`"
    core = wf[wf.index("repository: calvinchengx/contoso-data-product\n") :]
    assert re.search(r"ref: [0-9a-f]{40}", core[: core.index("path:")]), (
        "the contoso-data-product checkout is not pinned to a commit"
    )
    assert wf.index("make verify") < wf.index("make snapshot") < wf.index(
        "scripts/assert_snapshot.py"
    ), "verify, then snapshot, then assert -- in that order or the check is empty"

    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    written = re.search(r"^\s*PRODUCT_SNAPSHOT:\s*(\S+)\s*$", compose, re.M)
    assert written, "docker-compose.yml no longer tells the product where to publish"

    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    copied = re.search(r"^SNAPSHOT_IN_WORKER \?= (\S+)$", makefile, re.M)
    assert copied, "the Makefile no longer says what `make snapshot` copies"
    assert copied.group(1) == written.group(1), (
        f"`make snapshot` copies {copied.group(1)} and the product writes "
        f"{written.group(1)} -- the copy would fail, or worse, find a stale file"
    )

    out = re.search(r"^SNAPSHOT_OUT \?= (\S+)$", makefile, re.M)
    assert out, "the Makefile no longer says where `make snapshot` puts it"
    assert re.search(rf"assert_snapshot\.py.*\n.*\n\s+{re.escape(out.group(1))}\s*$",
                     raw, re.M), (
        f"the assert step does not read {out.group(1)}, which is where "
        f"`make snapshot` writes"
    )


# --- digest pins ---------------------------------------------------------------

def _scripts():
    sys.path.insert(0, str(ROOT / "scripts"))


def test_every_image_in_the_compose_file_is_fetched_by_digest():
    """Not a list to keep in step — every `image:` line, whatever it names.

    An allowlist would pass the day someone adds a service and forgets it,
    which is exactly when the pin is missing.
    """
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    for line in compose.splitlines():
        stripped = line.strip()
        if not stripped.startswith("image:"):
            continue
        assert "@${" in stripped and "_DIGEST" in stripped, (
            f"pulled by tag alone: {stripped}")
        assert ":-" not in stripped, (
            f"a default version is a floating pin in fixed clothing: {stripped}")


def test_every_pin_has_both_a_version_and_a_digest():
    _scripts()
    from digests import PINS

    text = (ROOT / "versions.env").read_text(encoding="utf-8")
    for prefix in PINS:
        assert re.search(rf"^{prefix}_VERSION=.+$", text, re.M), prefix
        assert re.search(rf"^{prefix}_DIGEST=sha256:[0-9a-f]{{64}}$", text, re.M), prefix


def test_the_release_script_and_the_pin_list_agree():
    """`set_release.PINS` re-resolves on every release; `digests.PINS` is the
    full set. An image in the first and not the second would be invisible to
    refresh_digests and to the compose check above."""
    _scripts()
    import digests
    import set_release

    assert set(set_release.PINS) <= set(digests.PINS), (
        f"release-tracked but unknown to digests.PINS: "
        f"{sorted(set(set_release.PINS) - set(digests.PINS))}")


def test_a_release_moves_each_sidecar_version_to_what_it_carries(tmp_path, monkeypatch):
    """v0.36.0 moved pysail 0.7.0 -> 0.7.1 and this script left
    SAIL_ENGINE_VERSION at 0.7.0 beside the 0.36.0 digest. The version now
    comes from the release's own pyproject.toml, and every digest from the
    release's own tag."""
    _scripts()
    import set_release

    versions = tmp_path / "versions.env"
    versions.write_text((ROOT / "versions.env").read_text(encoding="utf-8"),
                        encoding="utf-8")
    fake = "sha256:" + "c" * 64
    resolved = []
    pyproject = ('dependencies = ["pysail==8.8.8", "pyspark-client==7.7.7"]\n'
                 'engine = ["pysail==8.8.8"]\n')
    monkeypatch.setattr(set_release, "VERSIONS", versions)
    monkeypatch.setattr(set_release, "digest_of",
                        lambda image, tag: resolved.append(tag) or fake)
    monkeypatch.setattr(set_release, "fetch",
                        lambda url: pyproject if "/v9.9.9/" in url else "")
    monkeypatch.setattr(sys, "argv", ["set_release.py", "9.9.9"])
    assert set_release.main() == 0

    written = versions.read_text(encoding="utf-8")
    assert set(resolved) == {"9.9.9"}, resolved
    assert re.search(r"^FABRIC_EMULATOR_VERSION=9\.9\.9$", written, re.M)
    assert re.search(r"^SAIL_ENGINE_VERSION=8\.8\.8$", written, re.M)
    assert re.search(r"^SPARK_CLIENT_VERSION=7\.7\.7$", written, re.M)
    for prefix in set_release.CARRIES_A_DEPENDENCY_TAG:
        assert re.search(rf"^{prefix}_RELEASE=9\.9\.9$", written, re.M), prefix
        assert re.search(rf"^{prefix}_DIGEST={fake}$", written, re.M), prefix


def test_a_release_with_an_ambiguous_dependency_pin_writes_nothing(tmp_path, monkeypatch):
    import pytest

    _scripts()
    import set_release

    versions = tmp_path / "versions.env"
    original = (ROOT / "versions.env").read_text(encoding="utf-8")
    versions.write_text(original, encoding="utf-8")
    monkeypatch.setattr(set_release, "VERSIONS", versions)
    monkeypatch.setattr(set_release, "digest_of", lambda image, tag: "sha256:" + "d" * 64)
    monkeypatch.setattr(set_release, "fetch",
                        lambda url: '"pysail==0.7.0" "pysail==0.7.1" "pyspark-client==4.2.0"')
    monkeypatch.setattr(sys, "argv", ["set_release.py", "9.9.9"])
    with pytest.raises(SystemExit, match="pysail"):
        set_release.main()
    assert versions.read_text(encoding="utf-8") == original
