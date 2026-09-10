#!/usr/bin/env python3
"""管理対象だけをホームへコピーする。既存の配置は変更前に退避する。"""

import argparse
import filecmp
import shutil
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def targets(home):
    for name in (".gemini/ANTIGRAVITY.md", ".codex/AGENTS.md", ".claude/CLAUDE.md"):
        yield ROOT / "rules/AGENTS.md", home / name
    yield ROOT / "claude-code/settings.json", home / ".claude/settings.json"
    yield ROOT / "claude-code/hooks/circuit-breaker.py", home / ".claude/hooks/circuit-breaker.py"
    for skill in sorted((ROOT / "skills").iterdir()):
        if skill.is_dir() and (skill / "SKILL.md").is_file():
            for base in (".agents/skills", ".claude/skills", ".gemini/antigravity-cli/skills", ".gemini/config/skills"):
                yield skill, home / base / skill.name


def identical(source, destination):
    if destination.is_symlink() or not destination.exists():
        return False
    if source.is_dir():
        if not destination.is_dir():
            return False
        left = {p.name for p in source.iterdir()}
        right = {p.name for p in destination.iterdir()}
        return left == right and all(identical(source / n, destination / n) for n in left)
    return destination.is_file() and filecmp.cmp(source, destination, shallow=False)


def deploy(home, apply=False):
    pairs = list(targets(home))
    # 親ディレクトリ経由で意図しない場所へ書き込まない。
    for source, destination in pairs:
        if not source.exists():
            raise ValueError(f"原本がありません: {source}")
        for parent in destination.parents:
            if parent == home:
                break
            if parent.is_symlink():
                raise ValueError(f"配布先の親がリンクです: {parent}")
    changes = [(s, d) for s, d in pairs if not identical(s, d)]
    backup = None
    for source, destination in changes:
        print(f"{'COPY' if apply else 'DIFF'} {source.relative_to(ROOT)} -> {destination}")
        if not apply:
            continue
        if destination.exists() or destination.is_symlink():
            if backup is None:
                backup_root = home / ".agent-shell-setting-backups"
                backup_root.mkdir(parents=True, exist_ok=True)
                backup = Path(tempfile.mkdtemp(prefix="deploy-", dir=backup_root))
                print(f"BACKUP {backup}")
            saved = backup / destination.relative_to(home)
            saved.parent.mkdir(parents=True, exist_ok=True)
            # リンク自体を退避し、リンク先は書き換えない。
            destination.rename(saved)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(source, destination)
        else:
            shutil.copy2(source, destination)
    if apply:
        assert all(identical(s, d) for s, d in pairs), "コピー後の照合に失敗しました"
    print(f"{len(changes)} target(s) {'copied' if apply else 'differ'}")
    return 0 if apply or not changes else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="退避してからコピーする（既定は差分確認のみ）")
    parser.add_argument("--home", type=Path, default=Path.home(), help="配布先ホーム（検証にも使用）")
    args = parser.parse_args()
    raise SystemExit(deploy(args.home.resolve(), args.apply))
