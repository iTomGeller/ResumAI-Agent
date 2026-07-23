from pathlib import Path
import ast

p = Path("tests/test_runtime_executor.py")
raw = p.read_bytes()
print("starts_with_bom", raw.startswith(b"\xef\xbb\xbf"))
text = raw.decode("utf-8-sig")
try:
    ast.parse(text)
    print("syntax_ok")
except SyntaxError as e:
    print("syntax_error", e.lineno)
    print(repr(e.text))
