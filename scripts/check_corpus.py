from pathlib import Path

from ticketmind.knowledge.corpus import (
    build_case_text,
    load_historical_cases,
)


def main() -> None:
    project_root = Path(__file__).resolve().parents[1]
    corpus_path = (
        project_root
        / "data"
        / "synthetic"
        / "v2"
        / "historical_cases.jsonl"
    )

    cases = load_historical_cases(corpus_path)

    assert len(cases) == 12
    assert len({case.source_id for case in cases}) == 12

    for case in cases:
        print(f"{case.source_id} | {case.request.subject}")

    print("\n第一条案例的检索文本：")
    print(build_case_text(cases[0]))


if __name__ == "__main__":
    main()