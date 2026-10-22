# -*- coding: utf-8 -*-
"""
Візуалізатор результатів кластера.
Читає JSON-звіт після команди 'report' і генерує HTML-дашборд.

Запуск:
    python visualize_report.py data/reports/BATCH_ID.json
    python visualize_report.py data/reports/BATCH_ID.json --open
"""

import json
import sys
import webbrowser
from collections import Counter
from pathlib import Path
from datetime import datetime


def generate_html(report: dict, output_path: Path) -> Path:
    total_files = report.get("total_files", 0)
    done_files = report.get("files", 0)
    total_words = report.get("words", 0)
    total_lines = report.get("lines", 0)
    total_bytes = report.get("bytes", 0)
    duplicates = report.get("duplicates", [])
    workers = report.get("workers", [])
    results = report.get("results", [])
    unfinished = report.get("unfinished", [])
    is_complete = report.get("complete", False)

    # --- підрахунок частоти слів по всіх файлах ---
    global_words = Counter()
    for r in results:
        for word, count in r.get("top_words", []):
            global_words[word] += count
    top_global = global_words.most_common(25)
    max_word_count = top_global[0][1] if top_global else 1

    # --- розподіл файлів за розміром ---
    size_buckets = {"< 1 КБ": 0, "1-10 КБ": 0, "10-100 КБ": 0, "100 КБ - 1 МБ": 0}
    for r in results:
        b = r.get("bytes", 0)
        if b < 1024:
            size_buckets["< 1 КБ"] += 1
        elif b < 10240:
            size_buckets["1-10 КБ"] += 1
        elif b < 102400:
            size_buckets["10-100 КБ"] += 1
        else:
            size_buckets["100 КБ - 1 МБ"] += 1

    # --- файли за папками ---
    folders = Counter()
    for r in results:
        parts = r.get("path", "").split("/")
        folder = parts[0] if len(parts) > 1 else "(корінь)"
        folders[folder] += 1
    top_folders = folders.most_common(15)
    max_folder = max(f[1] for f in top_folders) if top_folders else 1

    # --- топ-10 найбільших файлів ---
    biggest = sorted(results, key=lambda x: x.get("bytes", 0), reverse=True)[:10]
    max_big = biggest[0]["bytes"] if biggest else 1

    # --- HTML ---
    now = datetime.now().strftime("%d.%m.%Y %H:%M")

    dup_html = ""
    if duplicates:
        for i, group in enumerate(duplicates):
            files_li = "".join(f"<li>{f}</li>" for f in group)
            dup_html += f"""
            <div class="dup-group">
                <div class="dup-title">Група #{i+1} — {len(group)} ідентичних файлів</div>
                <ul>{files_li}</ul>
            </div>"""
    else:
        dup_html = '<p class="muted">Дублікатів не знайдено.</p>'

    unfinished_html = ""
    if unfinished:
        rows = "".join(
            f"<tr><td>{u['path']}</td><td class='tag-{u['state']}'>{u['state']}</td><td>{u.get('error','')}</td></tr>"
            for u in unfinished
        )
        unfinished_html = f"""
        <div class="section">
            <h2>⚠️ Незавершені файли</h2>
            <table><tr><th>Файл</th><th>Стан</th><th>Помилка</th></tr>{rows}</table>
        </div>"""

    word_bars = ""
    for word, count in top_global:
        pct = count / max_word_count * 100
        word_bars += f"""
        <div class="bar-row">
            <span class="bar-label">{word}</span>
            <div class="bar-track"><div class="bar-fill" style="width:{pct:.0f}%"></div></div>
            <span class="bar-value">{count:,}</span>
        </div>"""

    folder_bars = ""
    for folder, count in top_folders:
        pct = count / max_folder * 100
        folder_bars += f"""
        <div class="bar-row">
            <span class="bar-label">{folder}/</span>
            <div class="bar-track"><div class="bar-fill folder-fill" style="width:{pct:.0f}%"></div></div>
            <span class="bar-value">{count}</span>
        </div>"""

    size_bars = ""
    max_size = max(size_buckets.values()) if any(size_buckets.values()) else 1
    for label, count in size_buckets.items():
        pct = count / max_size * 100
        size_bars += f"""
        <div class="bar-row">
            <span class="bar-label">{label}</span>
            <div class="bar-track"><div class="bar-fill size-fill" style="width:{pct:.0f}%"></div></div>
            <span class="bar-value">{count}</span>
        </div>"""

    biggest_rows = ""
    for r in biggest:
        pct = r["bytes"] / max_big * 100
        kb = r["bytes"] / 1024
        biggest_rows += f"""
        <div class="bar-row">
            <span class="bar-label" title="{r['path']}">{r['path'].split('/')[-1]}</span>
            <div class="bar-track"><div class="bar-fill big-fill" style="width:{pct:.0f}%"></div></div>
            <span class="bar-value">{kb:.1f} КБ</span>
        </div>"""

    status_class = "status-ok" if is_complete else "status-warn"
    status_text = "✅ Завершено" if is_complete else "⏳ В процесі"

    html = f"""<!DOCTYPE html>
<html lang="uk">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Cluster — Звіт аналізу корпусу</title>
<style>
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{ font-family: 'Segoe UI', system-ui, -apple-system, sans-serif; background: #0f1117; color: #e0e0e0; line-height: 1.6; }}
.container {{ max-width: 1100px; margin: 0 auto; padding: 24px; }}
header {{ text-align: center; padding: 40px 0 20px; }}
header h1 {{ font-size: 2.2em; color: #fff; letter-spacing: -0.5px; }}
header h1 span {{ color: #00d4aa; }}
header .subtitle {{ color: #888; font-size: 0.95em; margin-top: 6px; }}

.cards {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 16px; margin: 28px 0; }}
.card {{ background: #1a1d27; border-radius: 12px; padding: 20px; text-align: center; border: 1px solid #2a2d3a; }}
.card .value {{ font-size: 2em; font-weight: 700; color: #00d4aa; }}
.card .label {{ font-size: 0.85em; color: #999; margin-top: 4px; text-transform: uppercase; letter-spacing: 0.5px; }}

.section {{ background: #1a1d27; border-radius: 12px; padding: 24px; margin: 20px 0; border: 1px solid #2a2d3a; }}
.section h2 {{ font-size: 1.2em; margin-bottom: 16px; color: #fff; }}

.bar-row {{ display: flex; align-items: center; margin: 6px 0; gap: 10px; }}
.bar-label {{ width: 140px; text-align: right; font-size: 0.85em; color: #ccc; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; flex-shrink: 0; }}
.bar-track {{ flex: 1; height: 22px; background: #252830; border-radius: 6px; overflow: hidden; }}
.bar-fill {{ height: 100%; border-radius: 6px; background: linear-gradient(90deg, #00d4aa, #00b894); transition: width 0.8s ease; min-width: 2px; }}
.folder-fill {{ background: linear-gradient(90deg, #6c5ce7, #a29bfe); }}
.size-fill {{ background: linear-gradient(90deg, #fdcb6e, #e17055); }}
.big-fill {{ background: linear-gradient(90deg, #e17055, #d63031); }}
.bar-value {{ width: 70px; font-size: 0.8em; color: #999; flex-shrink: 0; }}

.dup-group {{ background: #252830; border-radius: 8px; padding: 14px 18px; margin: 10px 0; border-left: 3px solid #e17055; }}
.dup-title {{ color: #e17055; font-weight: 600; margin-bottom: 6px; font-size: 0.95em; }}
.dup-group ul {{ list-style: none; padding: 0; }}
.dup-group li {{ color: #ccc; font-size: 0.85em; padding: 2px 0; }}
.dup-group li::before {{ content: "📄 "; }}

.workers {{ display: flex; flex-wrap: wrap; gap: 10px; margin-top: 12px; }}
.worker-tag {{ background: #252830; border: 1px solid #00d4aa; color: #00d4aa; padding: 6px 16px; border-radius: 20px; font-size: 0.85em; }}

.status {{ display: inline-block; padding: 4px 14px; border-radius: 20px; font-size: 0.85em; font-weight: 600; }}
.status-ok {{ background: #00d4aa22; color: #00d4aa; }}
.status-warn {{ background: #fdcb6e22; color: #fdcb6e; }}

.grid-2 {{ display: grid; grid-template-columns: 1fr 1fr; gap: 20px; }}
@media (max-width: 768px) {{ .grid-2 {{ grid-template-columns: 1fr; }} }}

table {{ width: 100%; border-collapse: collapse; margin-top: 10px; }}
th, td {{ padding: 8px 12px; text-align: left; border-bottom: 1px solid #2a2d3a; font-size: 0.85em; }}
th {{ color: #999; text-transform: uppercase; font-size: 0.75em; letter-spacing: 0.5px; }}
.tag-failed {{ color: #e17055; }}
.tag-pending {{ color: #fdcb6e; }}
.tag-cancelled {{ color: #999; }}

footer {{ text-align: center; color: #555; font-size: 0.8em; padding: 30px 0 10px; }}
.muted {{ color: #666; font-style: italic; }}
</style>
</head>
<body>
<div class="container">

<header>
    <h1>📊 <span>Cluster</span> — Звіт аналізу</h1>
    <div class="subtitle">Пакет: {report.get('batch', '?')} &nbsp;|&nbsp; {now} &nbsp;|&nbsp; <span class="{status_class} status">{status_text}</span></div>
</header>

<div class="cards">
    <div class="card"><div class="value">{done_files}</div><div class="label">Файлів оброблено</div></div>
    <div class="card"><div class="value">{total_words:,}</div><div class="label">Слів знайдено</div></div>
    <div class="card"><div class="value">{total_lines:,}</div><div class="label">Рядків</div></div>
    <div class="card"><div class="value">{total_bytes / 1024:.0f} КБ</div><div class="label">Обсяг даних</div></div>
    <div class="card"><div class="value">{len(duplicates)}</div><div class="label">Груп дублікатів</div></div>
    <div class="card"><div class="value">{len(workers)}</div><div class="label">Воркерів</div></div>
</div>

<div class="grid-2">
    <div class="section">
        <h2>📝 Топ-25 найчастіших слів</h2>
        {word_bars}
    </div>
    <div class="section">
        <h2>📁 Файли за категоріями</h2>
        {folder_bars}
        <br>
        <h2 style="margin-top:20px">📏 Розподіл за розміром</h2>
        {size_bars}
    </div>
</div>

<div class="section">
    <h2>📊 Найбільші файли (Топ-10)</h2>
    {biggest_rows}
</div>

<div class="section">
    <h2>🔍 Виявлені дублікати (ідентичний SHA-256)</h2>
    <p class="muted" style="margin-bottom:12px">Кластер паралельно обчислив SHA-256 хеш кожного файлу і автоматично знайшов файли з ідентичним вмістом — навіть якщо вони мають різні назви.</p>
    {dup_html}
</div>

{unfinished_html}

<div class="section">
    <h2>💻 Воркери, що брали участь</h2>
    <div class="workers">
        {"".join(f'<span class="worker-tag">🖥️ {w}</span>' for w in workers) if workers else '<span class="muted">Немає даних</span>'}
    </div>
</div>

<footer>
    Cluster — Розподілена система обчислень для локальної мережі (LAN)<br>
    Випускна робота учня 11 класу — Пятковський Вадим
</footer>

</div>
</body>
</html>"""

    target = output_path.with_suffix(".html")
    target.write_text(html, encoding="utf-8")
    return target


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Використання: python visualize_report.py <шлях_до_report.json> [--open]")
        print("Приклад:       python visualize_report.py data/reports/abc123.json --open")
        sys.exit(1)

    report_path = Path(sys.argv[1])
    if not report_path.exists():
        print(f"Файл не знайдено: {report_path}")
        sys.exit(1)

    with report_path.open(encoding="utf-8") as f:
        data = json.load(f)

    html_file = generate_html(data, report_path)
    print(f"HTML-дашборд збережено: {html_file.resolve()}")

    if "--open" in sys.argv:
        webbrowser.open(str(html_file.resolve()))
        print("Відкрито у браузері.")
