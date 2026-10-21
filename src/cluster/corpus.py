import base64
from collections import Counter
import hashlib
import re

MAX_FILE_BYTES = 1024 * 1024


def analyze(payload):
    raw = base64.b64decode(payload["content"])
    if len(raw) > MAX_FILE_BYTES:
        raise ValueError("File exceeds the 1 MiB limit")

    text = raw.decode("utf-8")
    words_list = re.findall(r"[^\W\d_]+", text.lower())
    words = Counter(words_list)

    return {
        "path": payload["path"],
        "sha256": hashlib.sha256(raw).hexdigest(),
        "bytes": len(raw),
        "lines": len(text.splitlines()),
        "words": len(words_list),
        "top_words": words.most_common(20),
    }


def summarize(rows):
    groups = {}
    for row in rows:
        groups.setdefault(row["sha256"], []).append(row["path"])

    duplicates = [paths for paths in groups.values() if len(paths) > 1]

    return {
        "files": len(rows),
        "bytes": sum(row["bytes"] for row in rows),
        "lines": sum(row["lines"] for row in rows),
        "words": sum(row["words"] for row in rows),
        "duplicates": duplicates,
        "results": rows,
    }
