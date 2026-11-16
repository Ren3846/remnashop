"""Map validation in migration a026 (uuid -> numeric panel id)."""

import hashlib
import importlib.util
from pathlib import Path
from uuid import uuid4

import pytest

_PATH = Path("src/infrastructure/database/migrations/versions/a026_user_remna_id_to_bigint.py")
_spec = importlib.util.spec_from_file_location("a026", _PATH)
assert _spec and _spec.loader
a026 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(a026)


def _write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str, sha: str = "") -> None:
    f = tmp_path / "map.csv"
    f.write_text(body)
    monkeypatch.setenv("REMNA_ID_MAP_PATH", str(f))
    monkeypatch.setenv("REMNA_ID_MAP_SHA256", sha or hashlib.sha256(f.read_bytes()).hexdigest())


def test_valid_map_loads_exact_pairs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    u1, u2 = uuid4(), uuid4()
    _write(tmp_path, monkeypatch, f"uuid,remna_id\n{u1},1\n{u2},9007199254740993\n")
    m = a026._load_map()
    assert m == {u1: 1, u2: 9007199254740993}  # > 2**53: no float precision loss


@pytest.mark.parametrize(
    "body",
    [
        "uuid,remna_id\n{u},1\n{u},2\n",  # duplicate uuid
        "uuid,remna_id\n{u},1\n{v},1\n",  # duplicate id
        "uuid,remna_id\n{u},0\n",  # out of range
        "uuid,id\n{u},1\n",  # wrong header
    ],
)
def test_invalid_map_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str
) -> None:
    _write(tmp_path, monkeypatch, body.format(u=uuid4(), v=uuid4()))
    with pytest.raises(a026.RemnaIdMigrationError):
        a026._load_map()


def test_checksum_mismatch_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write(tmp_path, monkeypatch, f"uuid,remna_id\n{uuid4()},1\n", sha="0" * 64)
    with pytest.raises(a026.RemnaIdMigrationError, match="checksum"):
        a026._load_map()


def test_missing_inputs_are_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("REMNA_ID_MAP_PATH", raising=False)
    with pytest.raises(a026.RemnaIdMigrationError):
        a026._load_map()


def test_allowlist_parsing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REMNA_ID_MAP_ALLOW_UNRESOLVED", " 12, 15 ,")
    assert a026._allowed_unresolved() == {12, 15}


# --- step 2: join 2.7.4 export with the 3.x panel by shortUuid --------------------------

from src.cli.remna_id_map import MapBuildError, build  # noqa: E402


def _v2(*rows: tuple[str, str, str]) -> list[dict[str, str]]:
    return [{"uuid": u, "short_uuid": s, "username": n} for u, s, n in rows]


def test_build_joins_by_short_uuid() -> None:
    v2 = _v2(("u1", "s1", "alice"), ("u2", "s2", "bob"))
    v3 = [(10, "s2", "bob"), (11, "s1", "alice"), (12, "s3", "new_user")]
    assert build(v2, v3) == [("u1", 11), ("u2", 10)]


@pytest.mark.parametrize(
    "v3",
    [
        [(10, "s1", "alice")],  # bob missing
        [(10, "s1", "alice"), (11, "s2", "mallory")],  # username changed
        [(10, "s1", "alice"), (11, "s1", "alice"), (12, "s2", "bob")],  # dup shortUuid
    ],
)
def test_build_refuses_partial_or_ambiguous_maps(v3: list[tuple[int, str, str]]) -> None:
    with pytest.raises(MapBuildError):
        build(_v2(("u1", "s1", "alice"), ("u2", "s2", "bob")), v3)
