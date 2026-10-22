# -*- coding: utf-8 -*-
from collections import Counter
from datetime import datetime
from pathlib import Path


def generate_html_report(report: dict, target_html: Path) -> Path:
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

    global_words = Counter()
    for r in results:
        for word, count in r.get("top_words", []):
            global_words[word] += count
    top_global = global_words.most_common(25)
    max_word_count = top_global[0][1] if top_global else 1

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

    folders = Counter()
    for r in results:
        parts = r.get("path", "").replace("\\", "/").split("/")
        folder = parts[0] if len(parts) > 1 else "(корінь)"
        folders[folder] += 1
    top_folders = folders.most_common(15)
    max_folder = max(f[1] for f in top_folders) if top_folders else 1

    biggest = sorted(results, key=lambda x: x.get("bytes", 0), reverse=True)[:10]
    max_big = biggest[0]["bytes"] if biggest else 1

    now = datetime.now().strftime("%d.%m.%Y %H:%M:%S")

    dup_html = ""
    if duplicates:
        for i, group in enumerate(duplicates):
            files_li = "".join(f"<li>{f}</li>" for f in group)
            dup_html += f"""
            <div class="dup-group">
                <div class="dup-title">Група #{i+1} — {len(group)} ідентичних файлів за хешем SHA-256:</div>
                <ul>{files_li}</ul>
            </div>"""
    else:
        dup_html = '<p class="muted">Дублікатів серед оброблених файлів не виявлено.</p>'

    unfinished_html = ""
    if unfinished:
        rows = "".join(
            f"<tr><td>{u['path']}</td><td class='tag-{u['state']}'>{u['state']}</td><td>{u.get('error','')}</td></tr>"
            for u in unfinished
        )
        unfinished_html = f"""
        <div class="section">
            <h2>⚠️ Незавершені або скасовані файли</h2>
            <table><tr><th>Файл</th><th>Стан</th><th>Причина</th></tr>{rows}</table>
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
        name = r["path"].replace("\\", "/").split("/")[-1]
        biggest_rows += f"""
        <div class="bar-row">
            <span class="bar-label" title="{r['path']}">{name}</span>
            <div class="bar-track"><div class="bar-fill big-fill" style="width:{pct:.0f}%"></div></div>
            <span class="bar-value">{kb:.1f} КБ</span>
        </div>"""

    status_class = "status-ok" if is_complete else "status-warn"
    status_text = "✅ Завершено на 100%" if is_complete else f"⏳ Оброблено {done_files}/{total_files}"

    html = f"""<!DOCTYPE html>
<html lang="uk">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Cluster — Звіт обчислень {report.get('batch', '')}</title>
<style>
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{ font-family: 'Segoe UI', system-ui, -apple-system, sans-serif; background: #0c0e14; color: #dcdcdc; line-height: 1.6; padding-bottom: 50px; }}
.container {{ max-width: 1100px; margin: 0 auto; padding: 24px; }}
header {{ text-align: center; padding: 36px 0 20px; }}
header h1 {{ font-size: 2.3em; color: #ffffff; letter-spacing: -0.5px; }}
header h1 span {{ color: #00e5b3; }}
header .subtitle {{ color: #888ea8; font-size: 0.95em; margin-top: 8px; }}

.cards {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 14px; margin: 26px 0; }}
.card {{ background: #161922; border-radius: 12px; padding: 18px; text-align: center; border: 1px solid #232734; }}
.card .value {{ font-size: 2em; font-weight: 700; color: #00e5b3; }}
.card .label {{ font-size: 0.82em; color: #888ea8; margin-top: 4px; text-transform: uppercase; letter-spacing: 0.5px; }}

.section {{ background: #161922; border-radius: 12px; padding: 24px; margin: 20px 0; border: 1px solid #232734; }}
.section h2 {{ font-size: 1.25em; margin-bottom: 16px; color: #ffffff; display: flex; align-items: center; gap: 8px; }}

.bar-row {{ display: flex; align-items: center; margin: 6px 0; gap: 12px; }}
.bar-label {{ width: 140px; text-align: right; font-size: 0.85em; color: #cbd0e0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; flex-shrink: 0; }}
.bar-track {{ flex: 1; height: 20px; background: #222634; border-radius: 6px; overflow: hidden; }}
.bar-fill {{ height: 100%; border-radius: 6px; background: linear-gradient(90deg, #00b894, #00e5b3); transition: width 0.6s ease; min-width: 2px; }}
.folder-fill {{ background: linear-gradient(90deg, #5b54d6, #8c7ae6); }}
.size-fill {{ background: linear-gradient(90deg, #e17055, #f39c12); }}
.big-fill {{ background: linear-gradient(90deg, #d63031, #e84118); }}
.bar-value {{ width: 75px; font-size: 0.82em; color: #888ea8; flex-shrink: 0; }}

.dup-group {{ background: #1e222e; border-radius: 8px; padding: 14px 18px; margin: 10px 0; border-left: 4px solid #e17055; }}
.dup-title {{ color: #e17055; font-weight: 600; margin-bottom: 6px; font-size: 0.95em; }}
.dup-group ul {{ list-style: none; padding: 0; }}
.dup-group li {{ color: #cfd4e6; font-size: 0.88em; padding: 2px 0; }}
.dup-group li::before {{ content: "📄 "; }}

.workers {{ display: flex; flex-wrap: wrap; gap: 10px; margin-top: 10px; }}
.worker-tag {{ background: #1e222e; border: 1px solid #00e5b3; color: #00e5b3; padding: 6px 16px; border-radius: 20px; font-size: 0.88em; font-weight: 500; }}

.status {{ display: inline-block; padding: 4px 14px; border-radius: 20px; font-size: 0.85em; font-weight: 600; }}
.status-ok {{ background: #00e5b322; color: #00e5b3; }}
.status-warn {{ background: #f39c1222; color: #f39c12; }}

.grid-2 {{ display: grid; grid-template-columns: 1fr 1fr; gap: 20px; }}
@media (max-width: 768px) {{ .grid-2 {{ grid-template-columns: 1fr; }} }}

table {{ width: 100%; border-collapse: collapse; margin-top: 10px; }}
th, td {{ padding: 10px 12px; text-align: left; border-bottom: 1px solid #232734; font-size: 0.86em; }}
th {{ color: #888ea8; text-transform: uppercase; font-size: 0.75em; letter-spacing: 0.5px; }}
.tag-failed {{ color: #e17055; }}
.tag-pending {{ color: #f39c12; }}
.tag-cancelled {{ color: #7f8c8d; }}

footer {{ text-align: center; color: #636b83; font-size: 0.85em; padding: 36px 0 10px; }}
.muted {{ color: #7f8c8d; font-style: italic; }}
</style>
</head>
<body>
<div class="container">

<header>
    <h1>📊 <span>Cluster</span> — Підсумковий звіт</h1>
    <div class="subtitle">Ідентифікатор пакета: <b>{report.get('batch', '?')}</b> &nbsp;|&nbsp; Час експорту: {now} &nbsp;|&nbsp; <span class="{status_class} status">{status_text}</span></div>
</header>

<div class="cards">
    <div class="card"><div class="value">{done_files} / {total_files}</div><div class="label">Файлів опрацьовано</div></div>
    <div class="card"><div class="value">{total_words:,}</div><div class="label">Всього слів</div></div>
    <div class="card"><div class="value">{total_lines:,}</div><div class="label">Рядків тексту</div></div>
    <div class="card"><div class="value">{total_bytes / 1024:.1f} КБ</div><div class="label">Обсяг даних</div></div>
    <div class="card"><div class="value">{len(duplicates)}</div><div class="label">Груп дублікатів</div></div>
    <div class="card"><div class="value">{len(workers)}</div><div class="label">Вузлів кластера</div></div>
</div>

<div class="grid-2">
    <div class="section">
        <h2>📝 Топ-25 найуживаніших слів</h2>
        {word_bars}
    </div>
    <div class="section">
        <h2>📁 Розподіл файлів за темами</h2>
        {folder_bars}
        <br>
        <h2 style="margin-top:16px">📏 Розподіл за розміром</h2>
        {size_bars}
    </div>
</div>

<div class="section">
    <h2>📊 Топ-10 найбільших файлів</h2>
    {biggest_rows}
</div>

<div class="section">
    <h2>🔍 Автоматично виявлені дублікати (за криптографічним хешем SHA-256)</h2>
    <p class="muted" style="margin-bottom:12px">Кожен вузол кластера паралельно розраховував SHA-256 хеш-суму отриманого файлу. Система миттєво згрупувала документи з однаковим вмістом незалежно від їхньої назви та шляху.</p>
    {dup_html}
</div>

{unfinished_html}

<div class="section">
    <h2>💻 Обчислювальні вузли (Workers), які брали участь у розрахунку</h2>
    <div class="workers">
        {"".join(f'<span class="worker-tag">🖥️ {w}</span>' for w in workers) if workers else '<span class="muted">Немає зафіксованих вузлів</span>'}
    </div>
</div>

<footer>
    <b>Cluster</b> — Система розподілених обчислень для локальної мережі (LAN)<br>
    Випускна робота учня 11 класу — Пятковський Вадим
</footer>

</div>
</body>
</html>"""

    target_html.write_text(html, encoding="utf-8")
    return target_html
