"""Input-ref validation after the ``inputs/``-prefix restriction was widened.

new-api may now reference a previous task's RESULT in place, instead of copying
those bytes into ``inputs/`` first (image-to-image, keyframe / reference-to-video
where the caller passes back a product URL we issued; see new-api
``docs/own-url-nfs-fastpath.md``). That removed one check, so these tests pin
down what did NOT change:

- **Containment** still holds, just against the storage root instead of
  ``<root>/inputs``. Absolute paths and ``..`` escapes are still rejected.
- **Tenant binding** (``parent.name == user_id``, checked on the realpath) is now
  the ONLY thing separating tenants. It must reject a cross-tenant ref in both
  layouts, and must not be fooled by a symlink that reads as this user.
- **Existence** is still required.

The widening itself is the first test: a result-layout ref must be accepted, or
the whole zero-copy path silently 400s and new-api falls back to copying.
"""

import os

import pytest

from gpustack.api.exceptions import BadRequestException
from gpustack.routes.videos import _validate_input_ref

USER = 42
# <feature>-<model>/YYYY/MM/DD/<user_id>/<task_id>.<ext> — a task RESULT.
RESULT_REF = f"i2i-qwen-image-edit/2026/09/05/{USER}/task-abc.png"
# inputs/<task_type>-<model>/YYYY/MM/DD/<user_id>/<gid>-<field>.<ext> — an upload.
INPUT_REF = f"inputs/i2v-wan/2026/09/05/{USER}/gid-image.png"


def _place(root, ref, content=b"\x89PNG\r\n\x1a\n"):
    """Materialize a file at <root>/<ref> and return its absolute path."""
    abs_path = os.path.join(root, ref)
    os.makedirs(os.path.dirname(abs_path), exist_ok=True)
    with open(abs_path, "wb") as f:
        f.write(content)
    return abs_path


@pytest.mark.parametrize("ref", [RESULT_REF, INPUT_REF], ids=["result", "input"])
def test_accepts_both_layouts(tmp_path, ref):
    """The point of the change: a product path is a valid input ref, and the
    original inputs/ layout keeps working unchanged."""
    root = str(tmp_path)
    _place(root, ref)
    assert _validate_input_ref(ref, root, USER, "image") == os.path.join(root, ref)


@pytest.mark.parametrize(
    "ref",
    [
        "/etc/passwd",
        "../../etc/passwd",
        # 6 个 ``..``, 不是 5: 前缀 i2i-x/2026/09/05/<user> 正好 5 段, 5 个 ``..``
        # 只会消回 root 自身(normpath -> "etc/passwd"), 那样测到的是"文件不存在"
        # 而非穿越。第 6 个才真的走出 root。
        f"i2i-x/2026/09/05/{USER}/../../../../../../etc/passwd",
        "",
        "   ",
        None,
        123,
    ],
)
def test_rejects_escapes_and_junk(tmp_path, ref):
    """Containment and shape checks survive the widening."""
    with pytest.raises(BadRequestException):
        _validate_input_ref(ref, str(tmp_path), USER, "image")


@pytest.mark.parametrize(
    "ref",
    [
        f"i2i-qwen-image-edit/2026/09/05/{USER + 1}/victim.png",
        f"inputs/i2v-wan/2026/09/05/{USER + 1}/gid-image.png",
    ],
    ids=["result", "input"],
)
def test_rejects_cross_tenant_in_both_layouts(tmp_path, ref):
    """Tenant binding is now the only separator — it has to hold for the result
    layout too, not just for inputs/."""
    root = str(tmp_path)
    _place(root, ref)
    with pytest.raises(BadRequestException):
        _validate_input_ref(ref, root, USER, "image")


def test_rejects_escape_outside_root_even_when_tenant_dir_matches(tmp_path):
    """The one case that isolates the containment check.

    The other escape attempts above are already stopped by isabs or by the tenant
    binding (``/etc/passwd``'s parent is ``etc``, not the user id), so they pass
    even with containment removed. This ref escapes the root while its parent dir
    IS the request user's id and the file really exists — so isabs, tenant binding
    and isfile all say yes, and only containment can reject it. Without it, a
    caller reads any file on the host that happens to sit in a directory named
    after their user id.
    """
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside" / str(USER)
    outside.mkdir(parents=True)
    (outside / "secret.png").write_bytes(b"\x89PNG\r\n\x1a\n")

    with pytest.raises(BadRequestException):
        _validate_input_ref(f"../outside/{USER}/secret.png", str(root), USER, "image")


def test_symlink_into_other_tenant_is_rejected(tmp_path):
    """A symlink sitting in this user's dir but pointing at another tenant's file
    reads as this user in the raw ref and stays under the root; only resolving
    the realpath before the tenant check catches it."""
    root = str(tmp_path)
    victim = _place(root, f"i2i-x/2026/09/05/{USER + 1}/victim.png")

    link_ref = f"i2i-x/2026/09/05/{USER}/looks-like-mine.png"
    link_path = os.path.join(root, link_ref)
    os.makedirs(os.path.dirname(link_path), exist_ok=True)
    os.symlink(victim, link_path)

    with pytest.raises(BadRequestException):
        _validate_input_ref(link_ref, root, USER, "image")


def test_rejects_missing_file(tmp_path):
    with pytest.raises(BadRequestException):
        _validate_input_ref(RESULT_REF, str(tmp_path), USER, "image")


def test_rejects_directory(tmp_path):
    """A ref naming a directory passes containment and tenant binding; only the
    isfile check stops it from reaching the engine."""
    root = str(tmp_path)
    dir_ref = f"i2i-x/2026/09/05/{USER}/adir"
    os.makedirs(os.path.join(root, dir_ref), exist_ok=True)
    with pytest.raises(BadRequestException):
        _validate_input_ref(dir_ref, root, USER, "image")
