.PHONY: setup test demo close eval judge

setup:
	uv sync
	@# Build the sandbox image once; retry, because Docker Hub pulls can be flaky.
	@docker image inspect linus-sandbox >/dev/null 2>&1 || \
	  for i in 1 2 3; do docker build -q -t linus-sandbox sandbox/ && break || sleep 5; done
	@docker image inspect linus-sandbox >/dev/null

test:
	uv run pytest -q tests

demo:
	uv run linus demo

close:
	uv run linus close

eval:
	uv run linus eval

# One command for judges: build everything, run the full close on the demo
# month (16 PRs, 11 attacks) and score it against ground truth.
judge: setup test
	uv run linus doctor || true
	uv run linus demo
	uv run linus eval --label judge
	@echo
	@echo "Re-verify any decision without LLM calls:"
	@echo "  uv run linus receipt .linus/runs/<run-id>/receipts/pr-106.json"
