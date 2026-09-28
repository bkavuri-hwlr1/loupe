from __future__ import annotations

import importlib.util
import io
import json
import os
import stat
import tarfile
import tomllib
import zipfile
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "macos_release", Path(__file__).resolve().parents[2] / "scripts/macos_release.py"
)
assert _SPEC is not None and _SPEC.loader is not None
release = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(release)


def test_tag_and_runtime_must_match_package(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text('[project]\nversion = "1.2.3rc1"\n')
    package = tmp_path / "src/llm_cli"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text('__version__ = "1.2.3rc1"\n')
    assert release.release_version("v1.2.3rc1", tmp_path) == "1.2.3rc1"
    with pytest.raises(ValueError, match="tag must match"):
        release.release_version("v1.2.3", tmp_path)
    (package / "__init__.py").write_text('__version__ = "1.2.2"\n')
    with pytest.raises(ValueError, match="versions disagree"):
        release.release_version("v1.2.3rc1", tmp_path)


@pytest.mark.parametrize(
    "member",
    [
        "docs/internal.md",
        ".env",
        "llm_cli/.env",
        "llm_cli/token.json",
        "../llm_cli/a.py",
    ],
)
def test_private_or_unsafe_wheel_members_block_release(
    tmp_path: Path, member: str
) -> None:
    wheel = tmp_path / "application.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("llm_cli/__init__.py", "")
        archive.writestr(member, "private")
    with pytest.raises(ValueError, match="wheel member"):
        release.validate_application_wheel(wheel, "1.2.3")


def test_wheel_symlinks_block_release(tmp_path: Path) -> None:
    wheel = tmp_path / "application.whl"
    member = zipfile.ZipInfo("llm_cli/leak.py")
    member.create_system = 3
    member.external_attr = (stat.S_IFLNK | 0o777) << 16
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(member, "../../secret.py")
    with pytest.raises(ValueError, match="Unsafe wheel member"):
        release.validate_application_wheel(wheel, "1.2.3")


def test_archive_is_reproducible_and_contains_no_owner_paths(tmp_path: Path) -> None:
    source = tmp_path / "private-checkout-name"
    source.mkdir()
    (source / "README.md").write_text("public")
    first, second = tmp_path / "one.tar.gz", tmp_path / "two.tar.gz"
    release.archive_bundle(source, first)
    os.utime(source / "README.md", (12345, 12345))
    release.archive_bundle(source, second)
    assert first.read_bytes() == second.read_bytes()
    with tarfile.open(first) as archive:
        member = archive.getmember("README.md")
        assert member.uid == member.gid == member.mtime == 0
        assert member.uname == member.gname == ""
        assert archive.getnames() == ["README.md"]
    (source / "linked").symlink_to(source / "README.md")
    with pytest.raises(ValueError, match="Symlinks"):
        release.archive_bundle(source, second)


def test_prepare_requires_both_verified_architectures(tmp_path: Path) -> None:
    assets = tmp_path / "assets"
    assets.mkdir()
    tap = tmp_path / "tap"
    version = tomllib.loads((release.ROOT / "pyproject.toml").read_text())["project"][
        "version"
    ]
    for architecture in release.ARCHITECTURES:
        asset = assets / f"loupe-{version}-macos-{architecture}.tar.gz"
        manifest = json.dumps(
            {"version": version, "architecture": architecture}
        ).encode()
        with tarfile.open(asset, "w:gz") as archive:
            member = tarfile.TarInfo("manifest.json")
            member.size = len(manifest)
            archive.addfile(member, io.BytesIO(manifest))
        (assets / f"SHA256SUMS-{architecture}").write_text(
            f"{release.sha256(asset)}  {asset.name}\n"
        )
    release.prepare(f"v{version}", assets, tap)
    formula = (tap / "Formula/loupe.rb").read_text()
    assert f'version "{version}"' in formula
    assert "@ARM64_SHA256@" not in formula
    assert "@X86_64_SHA256@" not in formula
    assert "MagnifioSearchEngine/loupe" not in formula
    assert not (tap / "src").exists()
    (assets / "SHA256SUMS-arm64").write_text("tampered")
    with pytest.raises(ValueError, match="Checksum mismatch"):
        release.prepare(f"v{version}", assets, tap)
