"""Build public, self-contained wheel bundles from the private source tree.

Run with `uv run --locked python scripts/macos_release.py --help`.
Only this script's output directory is handed to the public tap.
"""

from __future__ import annotations

import argparse
import ast
import gzip
import hashlib
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile
import tomllib
import zipfile
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
TAP = "MagnifioSearchEngine/homebrew-tap"
ARCHITECTURES = ("arm64", "x86_64")
VERSION_PATTERN = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+(?:(?:a|b|rc)[0-9]+)?")


def release_version(tag: str, root: Path = ROOT) -> str:
    version = tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]
    if not VERSION_PATTERN.fullmatch(version) or tag != f"v{version}":
        raise ValueError(f"Release tag must match the package version: v{version}")
    tree = ast.parse((root / "src/llm_cli/__init__.py").read_text())
    versions = [
        ast.literal_eval(node.value)
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "__version__"
            for target in node.targets
        )
    ]
    if versions != [version]:
        raise ValueError("Package and runtime versions disagree")
    return str(version)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def validate_application_wheel(wheel: Path, version: str) -> None:
    """Fail closed if a public wheel includes anything outside application files."""
    metadata = f"llm_coord-{version}.dist-info"
    metadata_files = {"METADATA", "WHEEL", "RECORD", "entry_points.txt"}
    with zipfile.ZipFile(wheel) as archive:
        for item in archive.infolist():
            path = PurePosixPath(item.filename)
            if (
                path.is_absolute()
                or ".." in path.parts
                or stat.S_ISLNK(item.external_attr >> 16)
            ):
                raise ValueError(f"Unsafe wheel member: {path}")
            if item.is_dir():
                continue
            application_file = (
                path.parts[0] == "llm_cli"
                and all(not part.startswith(".") for part in path.parts)
                and (path.suffix == ".py" or path.name == "py.typed")
            )
            metadata_file = (
                len(path.parts) == 2
                and path.parts[0] == metadata
                and path.name in metadata_files
            )
            if not application_file and not metadata_file:
                raise ValueError(f"Unexpected public wheel member: {path}")


def archive_bundle(source: Path, destination: Path) -> None:
    """Create reproducible archives without local owner names or paths."""
    with (
        destination.open("wb") as output,
        gzip.GzipFile(fileobj=output, mode="wb", mtime=0, filename="") as compressed,
        tarfile.open(fileobj=compressed, mode="w") as archive,
    ):
        for path in sorted(source.rglob("*")):
            if path.is_symlink():
                raise ValueError(f"Symlinks are not release inputs: {path}")
            if not path.is_file():
                continue
            info = tarfile.TarInfo(path.relative_to(source).as_posix())
            info.size = path.stat().st_size
            info.mode = 0o644
            with path.open("rb") as content:
                archive.addfile(info, content)


def build(tag: str, output: Path) -> None:
    version = release_version(tag)
    architecture = platform.machine()
    if platform.system() != "Darwin" or architecture not in ARCHITECTURES:
        raise ValueError("Build each bundle on a native Apple Silicon or Intel Mac")
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="magnifio-build-") as temporary:
        work = Path(temporary)
        bundle = work / "bundle"
        wheels = bundle / "wheels"
        wheels.mkdir(parents=True)
        env = {**os.environ, "SOURCE_DATE_EPOCH": "315532800"}
        subprocess.run(
            ["uv", "build", "--wheel", "--no-sources", "--out-dir", str(wheels)],
            cwd=ROOT,
            env=env,
            check=True,
        )
        application = next(wheels.glob("llm_coord-*.whl"))
        validate_application_wheel(application, version)
        requirements = work / "requirements.txt"
        subprocess.run(
            [
                "uv",
                "export",
                "--locked",
                "--extra",
                "openai",
                "--extra",
                "anthropic",
                "--no-dev",
                "--no-emit-project",
                "--no-header",
                "--no-annotate",
                "--output-file",
                str(requirements),
            ],
            cwd=ROOT,
            check=True,
            stdout=subprocess.DEVNULL,
        )
        # Hash checking prevents the bundle from drifting from uv.lock. A missing
        # compatible wheel fails the release rather than invoking a compiler.
        subprocess.run(
            [
                "uv",
                "tool",
                "run",
                "--python",
                "3.14",
                "--from",
                "pip==26.1.2",
                "pip",
                "download",
                "--require-hashes",
                "--only-binary=:all:",
                "--platform",
                f"macosx_15_0_{architecture}",
                "--python-version",
                "3.14",
                "--implementation",
                "cp",
                "--abi",
                "cp314",
                "--requirement",
                str(requirements),
                "--dest",
                str(wheels),
            ],
            cwd=work,
            check=True,
        )
        for source, target in (
            (ROOT / "packaging/README.md", bundle / "README.md"),
            (ROOT / "packaging/homebrew/smoke_test.py", bundle / "smoke_test.py"),
        ):
            shutil.copyfile(source, target)
        manifest = {
            "version": version,
            "architecture": architecture,
            "python": "3.14",
            "minimum_macos": "15",
            "wheels": {
                wheel.name: sha256(wheel) for wheel in sorted(wheels.glob("*.whl"))
            },
        }
        (bundle / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        # Prove the bundle runs without the source tree or a developer venv.
        venv = work / "installed"
        subprocess.run(
            ["uv", "venv", "--python", "3.14", str(venv)], cwd=work, check=True
        )
        python = venv / "bin/python"
        subprocess.run(
            [
                "uv",
                "pip",
                "install",
                "--python",
                str(python),
                "--no-index",
                "--no-deps",
                *map(str, sorted(wheels.glob("*.whl"))),
            ],
            cwd=work,
            check=True,
        )
        for _ in range(2):
            subprocess.run(
                [
                    str(python),
                    str(bundle / "smoke_test.py"),
                    str(venv / "bin"),
                    version,
                    str(work / "retained-profile"),
                ],
                cwd=work,
                check=True,
            )
        destination = output / f"magnifio-{version}-macos-{architecture}.tar.gz"
        archive_bundle(bundle, destination)
        (output / f"SHA256SUMS-{architecture}").write_text(
            f"{sha256(destination)}  {destination.name}\n"
        )
        print(destination)


def prepare(tag: str, assets: Path, tap: Path) -> None:
    version = release_version(tag)
    template = (ROOT / "packaging/homebrew/magnifio.rb.in").read_text()
    values = {
        "VERSION": version,
        "ROOT_URL": f"https://github.com/{TAP}/releases/download/{tag}",
    }
    for architecture in ARCHITECTURES:
        asset = assets / f"magnifio-{version}-macos-{architecture}.tar.gz"
        checksum = sha256(asset)
        expected = (assets / f"SHA256SUMS-{architecture}").read_text()
        if expected != f"{checksum}  {asset.name}\n":
            raise ValueError(f"Checksum mismatch: {asset.name}")
        with tarfile.open(asset) as archive:
            manifest_file = archive.extractfile("manifest.json")
            if manifest_file is None:
                raise ValueError("Missing bundle manifest")
            manifest = json.load(manifest_file)
        if manifest["version"] != version or manifest["architecture"] != architecture:
            raise ValueError(f"Bundle does not match release: {asset.name}")
        values[f"{architecture.upper()}_SHA256"] = checksum
    for key, value in values.items():
        template = template.replace(f"@{key}@", value)
    if re.search(r"@[A-Z0-9_]+@", template):
        raise ValueError("Unfilled formula template")
    (tap / "Formula").mkdir(parents=True, exist_ok=True)
    (tap / "Formula/magnifio.rb").write_text(template)
    (tap / "release-assets").mkdir(parents=True, exist_ok=True)
    for architecture in ARCHITECTURES:
        for name in (
            f"magnifio-{version}-macos-{architecture}.tar.gz",
            f"SHA256SUMS-{architecture}",
        ):
            shutil.copyfile(assets / name, tap / "release-assets" / name)
    (tap / "release-assets/tag").write_text(tag + "\n")
    shutil.copyfile(ROOT / "packaging/README.md", tap / "README.md")
    (tap / ".github/workflows").mkdir(parents=True, exist_ok=True)
    shutil.copyfile(
        ROOT / "packaging/homebrew/publish.yml", tap / ".github/workflows/publish.yml"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    builder = commands.add_parser("build")
    builder.add_argument("--tag", required=True)
    builder.add_argument("--output", required=True, type=Path)
    preparer = commands.add_parser("prepare")
    preparer.add_argument("--tag", required=True)
    preparer.add_argument("--assets", required=True, type=Path)
    preparer.add_argument("--tap", required=True, type=Path)
    arguments = parser.parse_args()
    if arguments.command == "build":
        build(arguments.tag, arguments.output.resolve())
    else:
        prepare(arguments.tag, arguments.assets.resolve(), arguments.tap.resolve())


if __name__ == "__main__":
    main()
