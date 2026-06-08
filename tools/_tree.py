#!/usr/bin/env python3
import pathlib


ROOT = pathlib.Path(__file__).resolve().parent.parent
OUTPUT = ROOT / "tools" / "_tree.md"
EXCLUDE_DIRS = {".git", ".pytest_cache", ".venv", ".vscode", "__pycache__"}
EXCLUDE_EXTS = {".db", ".db-shm", ".db-wal", ".log", ".pyc", ".sqlite"}
TEE = "\u251c"
LAST = "\u2514"
VERTICAL = "\u2502"
HORIZONTAL = "\u2500"
INFO_PLACEHOLDER = "&nbsp;\u2014 [info placeholder]"


def html_indent(text):
    return text.replace(" ", "&nbsp;")


def visible_items(path):
    items = []
    for item in path.iterdir():
        if item == OUTPUT:
            continue
        if item.is_dir() and item.name in EXCLUDE_DIRS:
            continue
        if item.is_file() and item.suffix in EXCLUDE_EXTS:
            continue
        items.append(item)
    return sorted(items, key=lambda item: (item.is_dir(), item.name.lower()))


def padded_label(prefix, connector, name, width):
    label = f"{prefix}{connector}{name}"
    tail = f"{connector}{name}"
    return html_indent(prefix) + tail.ljust(max(len(tail), width - len(prefix)))


def file_line(lines, prefix, connector, name, width):
    lines.append(f"<code>{padded_label(prefix, connector, name, width)}</code>{INFO_PLACEHOLDER}<br>")


def directory_summary(lines, prefix, connector, name, is_top_level):
    if is_top_level:
        branch = f"{TEE}{HORIZONTAL * 6} " if connector.startswith(TEE) else f"{LAST}{HORIZONTAL * 6} "
        label = html_indent("  ") + f"{branch}{name}/"
    else:
        summary_prefix = prefix[2:] if prefix.startswith("  ") else prefix
        label = html_indent(summary_prefix) + f"{connector}{name}/"
    lines.append("<details>")
    lines.append(f"<summary><code>{label}</code>{INFO_PLACEHOLDER}</summary>")


def render_directory(lines, path, prefix="", depth=0):
    try:
        items = visible_items(path)
    except PermissionError:
        return

    file_width = max(
        (
            len(f"{prefix}{LAST + HORIZONTAL * 2 + ' ' if index == len(items) - 1 else TEE + HORIZONTAL * 2 + ' '}{item.name}")
            for index, item in enumerate(items)
            if item.is_file()
        ),
        default=0,
    )

    for index, item in enumerate(items):
        is_last = index == len(items) - 1
        connector = LAST + HORIZONTAL * 2 + " " if is_last else TEE + HORIZONTAL * 2 + " "

        if item.is_dir():
            directory_summary(lines, prefix, connector, item.name, depth == 0)
            child_prefix = prefix + ("    " if is_last else VERTICAL + "   ")
            render_directory(lines, item, child_prefix, depth + 1)
            lines.append("</details>")
        else:
            file_line(lines, prefix, connector, item.name, file_width)


def main():
    lines = [
        "<details>",
        "<summary><strong>CC-Deal-Finder/</strong></summary>",
    ]
    render_directory(lines, ROOT, prefix="    ")
    lines.append("</details>")
    OUTPUT.write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
