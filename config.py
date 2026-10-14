import argparse
import ast
from pathlib import Path


def extract_imports(py_file: Path, root: Path):
    """Return formatted import statements from one Python file."""
    try:
        source = py_file.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError) as exc:
        return [f"# ERROR reading file: {exc}"]

    try:
        tree = ast.parse(source, filename=str(py_file))
    except SyntaxError as exc:
        return [f"# ERROR parsing file at line {exc.lineno}: {exc.msg}"]

    imports = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.append((node.lineno, ast.get_source_segment(source, node) or "import"))
        elif isinstance(node, ast.ImportFrom):
            imports.append((node.lineno, ast.get_source_segment(source, node) or "from ... import ..."))

    # Keep source order, including multiple imports on the same line if present.
    imports.sort(key=lambda item: item[0])
    relative_path = py_file.relative_to(root)
    result = [f"# File: {relative_path}"]
    if not imports:
        result.append("# No import statements found.")
    else:
        result.extend(statement for _, statement in imports)
    return result


def main():
    parser = argparse.ArgumentParser(
        description="Recursively collect import statements from Python files."
    )
    parser.add_argument(
        "project",
        nargs="?",
        default=".",
        help="Project directory to scan (defaults to the current directory).",
    )
    parser.add_argument(
        "--output",
        default="python_imports.txt",
        help="Output text filename (default: python_imports.txt).",
    )
    args = parser.parse_args()

    root = Path(args.project).expanduser().resolve()
    if not root.is_dir():
        parser.error(f"Project directory does not exist: {root}")

    output = Path(args.output).expanduser()
    if not output.is_absolute():
        output = root / output
    output = output.resolve()

    ignored_dirs = {
        ".git", ".venv", "venv", "env", "__pycache__",
        "node_modules", ".mypy_cache", ".pytest_cache",
    }

    python_files = sorted(
        path for path in root.rglob("*.py")
        if not any(part in ignored_dirs for part in path.relative_to(root).parts)
        and path.resolve() != output
        and path.resolve() != Path(__file__).resolve()
    )

    lines = [
        f"Python import report for: {root}",
        f"Python files scanned: {len(python_files)}",
        "=" * 72,
        "",
    ]

    for py_file in python_files:
        lines.extend(extract_imports(py_file, root))
        lines.append("")

    try:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    except OSError as exc:
        raise SystemExit(f"Could not write output file {output}: {exc}")

    print(f"Scanned {len(python_files)} Python file(s).")
    print(f"Import report saved to: {output}")


if __name__ == "__main__":
    main()
