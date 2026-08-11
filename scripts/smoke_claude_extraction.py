"""Live smoke check for the ClaudeCodeAdapter extraction path (OC-32).

NOT part of the test suite: it calls the real `claude` CLI once, on a tiny
synthetic CV, and prints the validated extraction. Run manually:

    uv run python scripts/smoke_claude_extraction.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packages"))

from adapters.models.claude_code import ClaudeCodeAdapter
from domain.extraction import CvExtractionService
from prompts import load_prompt

SYNTHETIC_CV = """\
Jane Placeholder
Software Engineer

Experience
Acme Widgets, Backend Engineer, 2021-2023
- Built the order-processing service in Python, handling 50k orders/day.
- Contributed to the migration from a monolith to services.

Education
B.Sc. Computer Science, Example University, 2017-2021
"""


def main() -> None:
    service = CvExtractionService(ClaudeCodeAdapter(), load_prompt("cv_extraction.md"))
    extraction = service.extract(SYNTHETIC_CV)
    print(f"experiences ({len(extraction.experiences)}):")
    for e in extraction.experiences:
        print(f"  [{e.kind}] {e.title} @ {e.org} ({e.start_date} - {e.end_date})")
    print(f"facts ({len(extraction.facts)}):")
    for f in extraction.facts:
        print(f"  [{f.fact_type}] (exp {f.experience_index}) {f.statement}")


if __name__ == "__main__":
    main()
