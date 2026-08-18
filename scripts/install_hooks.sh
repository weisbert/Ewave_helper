#!/bin/sh
# 装 pre-commit 闸门（hook 不在 git 里，克隆后要跑一次）
set -e
ROOT=$(git rev-parse --show-toplevel)
cat > "$ROOT/.git/hooks/pre-commit" <<'HOOK'
#!/bin/sh
exec sh "$(git rev-parse --show-toplevel)/scripts/redzone_scan.sh" --staged
HOOK
chmod +x "$ROOT/.git/hooks/pre-commit"
echo "installed: .git/hooks/pre-commit -> scripts/redzone_scan.sh --staged"
