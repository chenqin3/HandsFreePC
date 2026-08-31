import subprocess
import sys
import tarfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _relative_files(directory: str, pattern: str) -> set[str]:
    root = PROJECT_ROOT / directory
    return {
        path.relative_to(PROJECT_ROOT).as_posix()
        for path in root.rglob(pattern)
        if path.is_file()
    }


def test_sdist_contains_public_install_assets(tmp_path: Path) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "build",
            "--sdist",
            "--no-isolation",
            "--outdir",
            str(tmp_path),
        ],
        cwd=PROJECT_ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr

    archives = list(tmp_path.glob("*.tar.gz"))
    assert len(archives) == 1
    with tarfile.open(archives[0], mode="r:gz") as archive:
        packaged = {
            name.split("/", 1)[1]
            for name in archive.getnames()
            if "/" in name and not name.endswith("/")
        }

    required = {
        "LICENSE",
        "README.md",
        "PRIVACY.md",
        "SECURITY.md",
        "THIRD_PARTY_NOTICES.md",
        "config.example.yaml",
        "handsfree_pc/schemas/desktop_step.schema.json",
        "handsfree_pc/schemas/plan.schema.json",
    }
    required |= _relative_files("docs", "*.md")
    required |= _relative_files("examples", "*.txt")
    required |= _relative_files("scripts", "*.ps1")
    required |= _relative_files("tests/fixtures", "*.json")

    assert required <= packaged, f"missing from sdist: {sorted(required - packaged)}"
    assert "config.local.yaml" not in packaged
    assert not any(
        name.startswith(("logs/", "models/", "recordings/", "runtime/", ".venv/"))
        for name in packaged
    )
