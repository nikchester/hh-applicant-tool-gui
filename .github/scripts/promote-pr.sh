#!/usr/bin/env bash
# Открывает PR dev → main или обновляет уже открытый.
# Запускается из .github/workflows/promote-pr.yml. Ничего не мерджит.
# Нужны: git-клон с origin/main и origin/dev, gh с GH_TOKEN и GH_REPO.
set -euo pipefail

base=main
head=dev
limit=60

for ref in "origin/$base" "origin/$head"; do
  if ! git rev-parse --verify --quiet "$ref" >/dev/null; then
    echo "Нет $ref: нужен checkout с fetch-depth: 0" >&2
    exit 1
  fi
done

count=$(git rev-list --count "origin/$base..origin/$head")
if [ "$count" -eq 0 ]; then
  echo "В $head нет коммитов, которых нет в $base: PR не нужен"
  exit 0
fi

oldest=$(git log "origin/$base..origin/$head" --format=%cs | tail -1)
no_merges=$(git rev-list --count --no-merges "origin/$base..origin/$head")

body=$(mktemp)
trap 'rm -f "$body"' EXIT
{
  echo "Автоматический PR: в \`$head\` есть коммиты, которых нет в \`$base\`."
  echo
  echo "- Коммитов: $count"
  echo "- Самый старый: $oldest"
  echo
  echo "Слияние вручную, обычным merge-коммитом (не squash): так \`dev\` и \`main\` не расходятся."
  echo
  echo "## Что войдёт (последние $limit без merge-коммитов)"
  # Темы в обратных кавычках: @имя и #номер внутри кода не упоминают людей и не связывают issue.
  git log -n "$limit" --no-merges --format='%h %s' "origin/$base..origin/$head" \
    | sed "s/\`/'/g; s/^/- \`/; s/\$/\`/"
  if [ "$no_merges" -gt "$limit" ]; then
    echo "- … и ещё $((no_merges - limit))"
  fi
} > "$body"

title="Промоут: dev → main (коммитов: $count)"
existing=$(gh pr list --base "$base" --head "$head" --state open --json number --jq '.[0].number // empty')
if [ -n "$existing" ]; then
  gh pr edit "$existing" --title "$title" --body-file "$body"
  echo "Обновлён PR #$existing: $title"
else
  gh pr create --base "$base" --head "$head" --title "$title" --body-file "$body"
  echo "Создан PR: $title"
fi
