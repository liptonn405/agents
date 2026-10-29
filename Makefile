.PHONY: help check test-analyst test-developer

help:
	@echo "make check           - validate structure and run available DeveloperAgent tests"
	@echo "make test-analyst    - run AnalystAgent unit tests"
	@echo "make test-developer  - run DeveloperAgent unit tests"

check:
	python3 scripts/check_repo.py

test-analyst:
	cd agents/analyst && python3 -m unittest discover -s tests -v

test-developer:
	@test -f agents/developer/main.py || { echo "DeveloperAgent is not present on this branch"; exit 1; }
	cd agents/developer && python3 -m unittest discover -s tests -v
