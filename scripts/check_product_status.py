from __future__ import annotations

import sys
from pathlib import Path

PRODUCT_INDEX = Path("docs/PRODUCT.md")
PRODUCT_SECTIONS = (
    Path("docs/product/intent.md"),
    Path("docs/product/runtime.md"),
    Path("docs/product/workflow.md"),
    Path("docs/product/interfaces.md"),
    Path("docs/product/operations.md"),
    Path("docs/product/governance.md"),
)


class ProductStatusContractError(Exception):
    pass


def verify_product_status_index(project_root: Path) -> int:
    index = project_root / PRODUCT_INDEX
    if not index.is_file():
        raise ProductStatusContractError(f"{PRODUCT_INDEX} is not a regular file")
    index_content = index.read_text(encoding="utf-8")
    product_directory = project_root / "docs/product"
    actual_sections = set(product_directory.glob("*.md"))
    expected_sections = {project_root / section for section in PRODUCT_SECTIONS}
    if unexpected_sections := actual_sections - expected_sections:
        raise ProductStatusContractError(
            f"{next(iter(sorted(unexpected_sections)))} is not an owned product section"
        )
    for section in PRODUCT_SECTIONS:
        if not (project_root / section).is_file():
            raise ProductStatusContractError(f"{section} is not a regular file")
        link = f"({section.relative_to(PRODUCT_INDEX.parent).as_posix()})"
        if link not in index_content:
            raise ProductStatusContractError(
                f"{section} is not linked by {PRODUCT_INDEX}"
            )
    return len(PRODUCT_SECTIONS)


def main() -> int:
    try:
        section_count = verify_product_status_index(Path.cwd())
    except ProductStatusContractError as error:
        print(f"Product-status gate refused: {error}", file=sys.stderr)
        return 1
    print(f"Product status index: {section_count} owned section(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
