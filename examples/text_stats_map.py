import re
import sys
from pathlib import Path

path = Path(sys.argv[1])
text = path.read_text(encoding="utf-8", errors="replace")
words = re.findall(r"\w+", text)
lines = text.splitlines()
print(f"{path.name}: {len(lines)} lines, {len(words)} words, {len(text)} chars")
