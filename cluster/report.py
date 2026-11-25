import csv
import html
import json
from collections import Counter
from datetime import datetime
from pathlib import Path


def export_reports(report: dict, reports_dir: Path, batch_id: str) -> Path:
    reports_dir.mkdir(parents=True, exist_ok=True)
    json_path = reports_dir / f"{batch_id}.json"
    csv_path = reports_dir / f"{batch_id}.csv"
    html_path = reports_dir / f"{batch_id}.html"

    json_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    with csv_path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(["File", "Bytes", "Lines", "Words", "SHA-256", "Worker", "Output"])
        for r in report.get("results", []):
            writer.writerow([
                r.get("path", ""),
                r.get("bytes", ""),
                r.get("lines", ""),
                r.get("words", ""),
                r.get("sha256", ""),
                r.get("worker", ""),
                r.get("output", ""),
            ])

    html_path.write_text(_build_html(report), encoding="utf-8")
    return json_path


def _build_html(report: dict) -> str:
    results = report.get("results", [])
    duplicates = report.get("duplicates", [])
    workers = report.get("workers", [])
    is_map = any("output" in r for r in results)

    top_words = Counter()
    for r in results:
        for w, c in r.get("top_words", []):
            top_words[w] += c

    words_rows = "".join(
        f"<tr><td>{html.escape(w)}</td><td>{c}</td></tr>"
        for w, c in top_words.most_common(15)
    )
    map_rows = "".join(
        f"<tr><td>{html.escape(r.get('path', ''))}</td><td>{html.escape(r.get('worker', ''))}</td><td><pre>{html.escape(r.get('output', ''))}</pre></td></tr>"
        for r in results
    )
    dup_html = "".join(
        f"<div class='card'><b>Group #{i+1}</b> ({len(g)} files):<ul>"
        + "".join(f"<li>{html.escape(p)}</li>" for p in g)
        + "</ul></div>"
        for i, g in enumerate(duplicates)
    ) or "<p class='muted'>No duplicates found.</p>"

    worker_rows = "".join(
        f"<tr><td>{html.escape(str(w))}</td></tr>" for w in workers
    ) or "<tr><td class='muted'>No data</td></tr>"

    now = datetime.now().strftime("%d.%m.%Y %H:%M")
    extra_section = (
        f"<h2>Task Outputs</h2><table><tr><th>File</th><th>Worker</th><th>Output</th></tr>{map_rows}</table>"
        if is_map
        else f"<h2>Top Words</h2><table><tr><th>Word</th><th>Count</th></tr>{words_rows}</table><h2>Duplicates</h2>{dup_html}"
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>cluster report - {html.escape(str(report.get('batch', '')))}</title>
<style>
  body {{ font-family: 'Segoe UI', sans-serif; background: #0e0e10; color: #ececef; margin: 0; padding: 28px; }}
  .wrap {{ max-width: 980px; margin: 0 auto; }}
  h1 {{ color: #d97757; margin-bottom: 4px; }}
  .muted {{ color: #8a8a93; }}
  .grid {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; margin: 20px 0; }}
  .stat {{ background: #161619; border: 1px solid #27272d; border-radius: 16px; padding: 14px; }}
  .stat .num {{ font-size: 24px; font-weight: bold; color: #ececef; }}
  .card {{ background: #161619; border: 1px solid #27272d; border-radius: 16px; padding: 14px; margin-bottom: 12px; }}
  table {{ width: 100%; border-collapse: collapse; background: #161619; border: 1px solid #27272d; border-radius: 12px; overflow: hidden; }}
  th, td {{ padding: 8px 12px; border-bottom: 1px solid #27272d; text-align: left; vertical-align: top; }}
  th {{ color: #d97757; }}
  pre {{ margin: 0; white-space: pre-wrap; font-family: Consolas, monospace; font-size: 12px; }}
</style>
</head>
<body>
<div class="wrap">
  <h1>cluster report</h1>
  <div class="muted">Batch: {html.escape(str(report.get('batch', '')))} | {now}</div>
  <div class="grid">
    <div class="stat"><div class="num">{report.get('files', 0)}/{report.get('total_files', 0)}</div><div>Files</div></div>
    <div class="stat"><div class="num">{report.get('words', 0):,}</div><div>Words</div></div>
    <div class="stat"><div class="num">{report.get('lines', 0):,}</div><div>Lines</div></div>
    <div class="stat"><div class="num">{len(duplicates)}</div><div>Duplicates</div></div>
  </div>
  <h2>Workers</h2>
  <table><tr><th>Node</th></tr>{worker_rows}</table>
  {extra_section}
</div>
</body>
</html>"""
