# SPDX-License-Identifier: MIT
.PHONY: test check

test:
	PYTHONPATH=src python3 -W error::ResourceWarning -m unittest discover -s tests -v

check: test
	python3 -m compileall -q src
