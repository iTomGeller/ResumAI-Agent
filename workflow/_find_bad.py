from pathlib import Path
import ast
import re

p = Path("tests/test_runtime_executor.py")
text = p.read_text(encoding="utf-8-sig")
lines = text.splitlines()
# Find likely broken string literals: odd number of unescaped quotes
for i, line in enumerate(lines, 1):
    if "?" in line and ('"' in line or "'" in line):
        try:
            ast.parse(line)
        except SyntaxError:
            print(f"{i}: {line[:120]!r}")

print("--- full parse ---")
try:
    ast.parse(text)
    print("ok")
except SyntaxError as e:
    print(e.lineno)
    # write line to file to avoid console encoding issues
    Path("_bad_line.txt").write_text(f"{e.lineno}\n{e.text}", encoding="utf-8")
