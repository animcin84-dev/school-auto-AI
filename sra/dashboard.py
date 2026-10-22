"""Локальный read-only дашборд для школы (AUDIT §E P3; разрешено 2026-10-22).

Принципы:
- ТОЛЬКО ЧТЕНИЕ: ни один маршрут не пишет в хранилище и не исполняет
  действий; POST и прочие методы отклоняются (405);
- стандартная библиотека: http.server, без зависимостей;
- по умолчанию привязан к 127.0.0.1 (правило репозитория: локальные превью
  не выставляются наружу); смена адреса — явным флагом --bind;
- HTML-экранирование ВСЕГО пользовательского текста (notes — недоверенный);
- на каждой странице баннер среды и природы данных: SYNTHETIC-хранилище
  помечено на каждом экране — синтетика не выдаётся за реальные измерения;
- недоступная секция показывается своей причиной, а не числом-заглушкой.

Запуск: python -m sra dashboard --db school.sqlite3 --environment demo
"""
from __future__ import annotations

import html
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import enums
from .store import EvidenceStore

_PAGE = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<title>School Resource Autopilot — {title}</title>
<style>
 body {{ font-family: system-ui, sans-serif; margin: 0; background: #f7f7f5; color: #222; }}
 header {{ background: #1f3a5f; color: #fff; padding: 12px 20px; }}
 header .env {{ background: {env_color}; border-radius: 4px; padding: 2px 8px;
               font-weight: bold; margin-left: 10px; font-size: 0.9em; }}
 main {{ max-width: 980px; margin: 20px auto; padding: 0 12px; }}
 table {{ border-collapse: collapse; width: 100%; background: #fff; margin: 8px 0; }}
 th, td {{ border: 1px solid #ccc; padding: 6px 10px; text-align: left; }}
 th {{ background: #eef2f7; }}
 .ok {{ color: #1a7f37; font-weight: bold; }}
 .bad {{ color: #b42318; font-weight: bold; }}
 .warn {{ color: #9a6700; font-weight: bold; }}
 .banner {{ background: #fff3cd; border: 1px solid #e0c36a; padding: 8px 12px;
           border-radius: 4px; margin: 10px 0; }}
 .card {{ background: #fff; border: 1px solid #ddd; border-radius: 6px;
         padding: 14px 18px; margin: 12px 0; }}
 nav a {{ margin-right: 14px; }}
 footer {{ color: #777; font-size: 0.85em; padding: 16px 20px; }}
</style></head><body>
<header><b>School Resource Autopilot</b>
 <span class="env">СРЕДА: {environment}</span>
 <span class="env">ДАННЫЕ: {data_nature}</span></header>
<main>
<div class="banner">Только чтение: дашборд не исполняет действий и не меняет
данные. Решения — за бракеражной комиссией и администрацией школы.
{data_note}</div>
{body}
</main>
<footer>Локальный read-only дашборд ({bind}:{port}); источники и происхождение
каждого числа — в JSON-отчётах соответствующих команд CLI.</footer>
</body></html>"""

_ENV_COLORS = {"DEMO": "#b45309", "OPEN": "#1d4ed8", "REAL": "#15803d"}


def _esc(x) -> str:
    return html.escape(str(x), quote=True)


def _nature(store) -> tuple[str, str]:
    counts = store.stats().get("evidence_status_counts", {})
    if not counts:
        return "ПУСТО", "В хранилище нет записей."
    if set(counts) == {enums.SYNTHETIC}:
        return enums.SYNTHETIC, ("ВСЕ записи SYNTHETIC (синтетика): числа "
                                 "проверяют код, не описывают реальную школу.")
    if enums.SYNTHETIC in counts:
        return "СМЕСЬ", "В хранилище смесь природы данных — смотрите статусы записей."
    return "/".join(sorted(counts)), "Реальные данные; происхождение — в карточках записей."


def _schools(store) -> list[str]:
    return [r["school_id"] for r in
            store._conn.execute("SELECT school_id FROM schools ORDER BY school_id")]


def _html_digest(store, school_id: str) -> str:
    from .digest import build_digest
    from .explain import explain_digest
    d = build_digest(store, school_id)
    w = d.get("window") or {}
    rows = [f"<p>Окно: {_esc(w.get('date_from'))} … {_esc(w.get('date_to'))}</p>"]
    wm = d.get("window_metric") or {}
    if wm.get("status") == "OK":
        rows.append(f"<p>Отходы за окно: <b>{_esc(wm['plate_waste_per_diner_g'])} г "
                    f"на едока</b> (Σ {_esc(wm['plate_waste_g_total'])} г / "
                    f"Σ едоков {_esc(wm['diners_total'])})</p>")
    else:
        rows.append(f"<p class='warn'>Метрика окна недоступна: "
                    f"{_esc(wm.get('reason'))}</p>")
    q = d.get("quality") or {}
    rows.append("<h3>Качество данных</h3><table><tr><th>Проверка</th><th>Статус</th>"
                "<th>Примечание</th></tr>")
    for c in q.get("checks") or []:
        cls = "ok" if c.get("ok") else "bad"
        rows.append(f"<tr><td>{_esc(c.get('name'))}</td>"
                    f"<td class='{cls}'>{'OK' if c.get('ok') else 'ПРОБЛЕМА'}</td>"
                    f"<td>{_esc(c.get('message') or '')}</td></tr>")
    rows.append("</table>")
    n = d.get("norms") or {}
    rows.append("<h3>Порции vs Санправила РК</h3>")
    if n.get("status") == "UNAVAILABLE":
        rows.append(f"<p class='warn'>{_esc(n.get('reason'))}</p>")
    else:
        rows.append("<table><tr><th>Блюдо</th><th>Норма, г</th><th>Медиана, г</th>"
                    "<th>Вне нормы</th><th>Статус</th></tr>")
        for dish in n.get("dishes") or []:
            if dish.get("status") == "UNAVAILABLE":
                rows.append(f"<tr><td>{_esc(dish['dish'])}</td>"
                            f"<td>{dish['norm_g'][0]}–{dish['norm_g'][1]}</td>"
                            "<td>—</td><td>—</td><td>нет измерений</td></tr>")
                continue
            cls = "ok" if dish["status"] == "OK" else "warn"
            rows.append(f"<tr><td>{_esc(dish['dish'])}</td>"
                        f"<td>{dish['norm_g'][0]}–{dish['norm_g'][1]}</td>"
                        f"<td>{_esc(dish.get('median_g'))}</td>"
                        f"<td>{_esc(dish.get('share_out_pct'))}%</td>"
                        f"<td class='{cls}'>{_esc(dish['status'])}</td></tr>")
        rows.append("</table>")
    rows.append("<h3>Алерты</h3>")
    for a in d.get("alerts", []):
        cls = "bad" if a.get("severity") == "WARN" else ""
        v = (f": {_esc(a.get('value_pct'))}% при пороге "
             f"{_esc(a.get('threshold_pct'))}%"
             if a.get("value_pct") is not None else "")
        rows.append(f"<p class='{cls}'>[{_esc(a.get('severity'))}] "
                    f"{_esc(a.get('code'))}{v}"
                    + (f" — {_esc(a['reason'])}" if a.get("reason") else "")
                    + "</p>")
    if not d.get("alerts"):
        rows.append("<p>Алертов нет.</p>")
    ml = d.get("ml_status") or {}
    rows.append(f"<h3>Прогнозы (ML)</h3><p>diners: {_esc(ml.get('diners_sufficiency'))}; "
                f"composite: {_esc(ml.get('composite_sufficiency'))}</p>")
    for warn in ml.get("warnings", []):
        rows.append(f"<p class='warn'>предупреждение: {_esc(warn)}</p>")
    rows.append(f"<p><i>{_esc(explain_digest(d).splitlines()[-1])}</i></p>")
    return "".join(rows)


def _html_ml_status(store, school_id: str) -> str:
    from .status import ml_status
    st = ml_status(store, school_id)
    rows = ["<table>",
            f"<tr><th>Сессий (с menu_id)</th><td>{st['sessions']} "
            f"({st['sessions_with_menu_id']})</td></tr>",
            f"<tr><th>Наблюдений (с отходами)</th><td>{st['observations']} "
            f"({st['observations_with_plate_waste']})</td></tr>",
            f"<tr><th>Период</th><td>{_esc(st['date_from'])} … "
            f"{_esc(st['date_to'])}</td></tr>",
            f"<tr><th>diners: данных достаточно?</th><td>"
            f"{_esc(st['diners_sufficiency'])}</td></tr>",
            f"<tr><th>composite: данных достаточно?</th><td>"
            f"{_esc(st['composite_sufficiency'])}</td></tr>",
            f"<tr><th>dataset_version</th><td>{st['dataset_version']}</td></tr>",
            "</table>"]
    cards = st.get("latest_cards") or []
    rows.append("<h3>Модели-карточки (последние версии)</h3>")
    if cards:
        rows.append("<table><tr><th>Модель</th><th>Версия</th><th>MAE</th></tr>")
        for c in cards:
            m = (c.get("metrics") or {})
            mae = m.get("mae_diners_mean", m.get("mae_mean"))
            rows.append(f"<tr><td>{_esc(c['model_id'])}</td><td>v{c['version']}</td>"
                        f"<td>{_esc(mae)}</td></tr>")
        rows.append("</table>")
    else:
        rows.append("<p>Карточек нет: backtest'ы ещё не запускались "
                    "(forecast-diners / forecast-composite).</p>")
    promo = st.get("latest_promotion")
    if promo:
        rows.append(f"<p>Последнее решение gate: {_esc(promo['model_id'])} "
                    f"v{promo['version']}.</p>")
    for w in st.get("warnings", []):
        rows.append(f"<p class='warn'>предупреждение: {_esc(w)}</p>")
    return "".join(rows)


def _html_quality(store, school_id: str) -> str:
    from .quality import data_quality_report
    q = data_quality_report(store, school_id)
    if q.get("status") == "DATA_NOT_READY":
        return f"<p class='warn'>{_esc(q.get('reason'))}</p>"
    rows = ["<table><tr><th>Проверка</th><th>Статус</th><th>Детали</th></tr>"]
    for c in q.get("checks", []):
        cls = "ok" if c.get("ok") else "bad"
        det = json.dumps(c.get("details") or {}, ensure_ascii=False)[:300]
        rows.append(f"<tr><td>{_esc(c.get('check'))}</td>"
                    f"<td class='{cls}'>{'OK' if c.get('ok') else 'ПРОБЛЕМА'}</td>"
                    f"<td><code>{_esc(det)}</code></td></tr>")
    rows.append("</table>")
    return "".join(rows)


def _html_norms(store, school_id: str) -> str:
    from .norms import check_portions
    sessions = [s for s in store.fetch_sessions(school_id=school_id)
                if s.meal_type == "LUNCH"]
    obs = [o for o in store.fetch_observations(school_id=school_id)
           if o.session_id in {s.session_id for s in sessions}]
    n = check_portions(obs, age_group="6-11", sessions=sessions, per="total")
    if n.get("status") == "UNAVAILABLE":
        return f"<p class='warn'>{_esc(n.get('reason'))}</p>"
    rows = [f"<p>Семантика порции: {_esc(n.get('portion_semantics'))}</p>",
            "<table><tr><th>Блюдо</th><th>Норма, г</th><th>Медиана</th>"
            "<th>Мин–Макс</th><th>Вне нормы</th><th>Статус</th></tr>"]
    for d_ in n.get("dishes", []):
        if d_.get("status") == "UNAVAILABLE":
            rows.append(f"<tr><td>{_esc(d_['dish'])}</td>"
                        f"<td>{d_['norm_g'][0]}–{d_['norm_g'][1]}</td>"
                        "<td colspan='4'>нет измерений</td></tr>")
            continue
        cls = "ok" if d_["status"] == "OK" else "warn"
        rows.append(f"<tr><td>{_esc(d_['dish'])}</td>"
                    f"<td>{d_['norm_g'][0]}–{d_['norm_g'][1]}</td>"
                    f"<td>{_esc(d_.get('median_g'))} г</td>"
                    f"<td>{_esc(d_.get('min_g'))}–{_esc(d_.get('max_g'))}</td>"
                    f"<td>{_esc(d_.get('share_out_pct'))}%</td>"
                    f"<td class='{cls}'>{_esc(d_['status'])}</td></tr>")
    rows.append("</table><p><i>Порция вне нормы — предупреждение, не ошибка; "
                "решение за бракеражной комиссией.</i></p>")
    return "".join(rows)


def _html_alerts(store, school_id: str) -> str:
    from .monitoring import nutrition_alerts
    alerts = nutrition_alerts(store, school_id)
    if not alerts:
        return "<p>Алертов нет.</p>"
    rows = []
    for a in alerts:
        cls = "bad" if a.get("severity") == "WARN" else ""
        v = (f": {_esc(a.get('value_pct'))}% при пороге "
             f"{_esc(a.get('threshold_pct'))}%"
             if a.get("value_pct") is not None else "")
        rows.append(f"<p class='{cls}'>[{_esc(a.get('severity'))}] "
                    f"{_esc(a.get('code'))}{v}"
                    + (f" — {_esc(a['note'])}" if a.get("note") else "") + "</p>")
    return "".join(rows)


PAGES = {
    "digest": ("Дайджест", _html_digest),
    "ml-status": ("ML-статус", _html_ml_status),
    "quality": ("Качество данных", _html_quality),
    "norms": ("Порции и нормы", _html_norms),
    "alerts": ("Алерты", _html_alerts),
}


def _make_handler(store_path: str, environment: str):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # тише в консоли
            pass

        def _render(self, body: str, title: str, code: int = 200,
                    nature: str = "—", note: str = "") -> None:
            page = _PAGE.format(
                title=_esc(title), environment=environment,
                env_color=_ENV_COLORS.get(environment, "#555"),
                data_nature=nature, data_note=_esc(note), body=body,
                bind=self.server.server_address[0],
                port=self.server.server_address[1])
            payload = page.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("X-Robots-Tag", "noindex")
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):  # noqa: N802
            url = urlparse(self.path)
            qs = parse_qs(url.query)
            school_id = (qs.get("school_id") or [""])[0]
            store = EvidenceStore(store_path, environment)
            try:
                nature, note = _nature(store)
                if url.path == "/":
                    links = "".join(
                        f"<p>— школа <a href='/digest?school_id={_esc(s)}'>"
                        f"{_esc(s)}</a> "
                        f"(<a href='/ml-status?school_id={_esc(s)}'>ML</a>, "
                        f"<a href='/quality?school_id={_esc(s)}'>качество</a>, "
                        f"<a href='/norms?school_id={_esc(s)}'>нормы</a>, "
                        f"<a href='/alerts?school_id={_esc(s)}'>алерты</a>)</p>"
                        for s in _schools(store))
                    body = (f"<div class='card'><h2>Школы в хранилище</h2>"
                            + (links or "<p>Хранилище пусто.</p>") + "</div>")
                    self._render(body, "Обзор", nature=nature, note=note)
                    return
                page = url.path.lstrip("/")
                if page in PAGES:
                    title, fn = PAGES[page]
                    if not school_id:
                        self._render("<p class='bad'>Укажите school_id.</p>",
                                     title, code=400, nature=nature, note=note)
                        return
                    if school_id not in _schools(store):
                        self._render("<p class='bad'>Школа не найдена в этом "
                                     "хранилище.</p>", title, code=404,
                                     nature=nature, note=note)
                        return
                    self._render(f"<div class='card'><h2>{_esc(title)} — "
                                 f"{_esc(school_id)}</h2>"
                                 + fn(store, school_id) + "</div>", title,
                                 nature=nature, note=note)
                    return
                self._render("<p class='bad'>Страница не найдена.</p>",
                             "404", code=404, nature=nature, note=note)
            finally:
                store.close()

        def do_POST(self):  # noqa: N802
            body = "405: дашборд только читает\n".encode("utf-8")
            self.send_response(405)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(body)

    return Handler


def serve(store_path: str, environment: str, *, port: int = 8765,
          bind: str = "127.0.0.1") -> None:
    """Блокирующий запуск. По правилу репозитория bind=127.0.0.1."""
    httpd = ThreadingHTTPServer((bind, port), _make_handler(store_path, environment))
    host, real_port = httpd.server_address[:2]
    print(f"Дашборд (read-only): http://{host}:{real_port}/  "
          f"[среда {environment}; Ctrl-C для остановки]")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
