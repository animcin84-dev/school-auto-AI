#!/bin/sh
# ML-окружение для sra (не входит в git; пересоздаёт .venv)
python3 -m venv .venv
.venv/bin/pip install -r requirements-ml.txt
