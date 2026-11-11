from __future__ import annotations

import runpy
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SAMPLES = ROOT / "samples"


def test_generator_reproduces_the_committed_samples_byte_for_byte(tmp_path):
    runpy.run_path(str(ROOT / "scripts" / "make_samples.py"))["main"](tmp_path)

    generated = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    committed = {p.name: p.read_bytes() for p in SAMPLES.iterdir()}
    assert sorted(generated) == ["facts.yaml", "sample-cgl-policy.pdf",
                                 "sample-endorsement-additional-insured.docx", "sample-renewal-quote.pdf"]
    assert generated == committed
