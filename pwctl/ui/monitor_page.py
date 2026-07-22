"""Monitor page: service health, CPU/RAM, xruns, pw-top and logs."""

from __future__ import annotations

import collections

import gi

gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from ..backend import prefs, stats, system
from .widgets import async_call, group, page_scroller, pill, state_style

UNIT_LABELS = {'pipewire.service': 'PipeWire',
               'wireplumber.service': 'WirePlumber',
               'pipewire-pulse.service': 'PipeWire-Pulse'}

HISTORY = 60          # samples kept for the sparklines (~3 min at 3 s)


class Sparkline(Gtk.DrawingArea):
    """Tiny inline history graph for one metric."""

    def __init__(self, width=120, height=26):
        super().__init__(content_width=width, content_height=height)
        self.values = collections.deque(maxlen=HISTORY)
        self.max_hint = 1.0
        self.set_draw_func(self._draw)

    def push(self, value):
        self.values.append(max(0.0, value))
        self.queue_draw()

    def _draw(self, _a, cr, w, h):
        if len(self.values) < 2:
            return
        fg = self.get_style_context().get_color()
        top = max(self.max_hint, max(self.values)) or 1.0
        step = w / (HISTORY - 1)
        n = len(self.values)
        cr.move_to(w - (n - 1) * step, h - self.values[0] / top * (h - 3) - 1)
        for i, v in enumerate(self.values):
            cr.line_to(w - (n - 1 - i) * step, h - v / top * (h - 3) - 1)
        cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.85)
        cr.set_line_width(1.6)
        cr.stroke_preserve()
        cr.line_to(w, h)
        cr.line_to(w - (n - 1) * step, h)
        cr.close_path()
        cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.12)
        cr.fill()


class MonitorPage:
    def __init__(self, window):
        self.window = window
        self._timer = None
        self._busy = False
        self._prev_procs = {}
        self._last_xruns = None
        self._log_cursor = None
        self._log_paused = False

        # ---- services ----
        svc = group('Services')
        self.svc_rows = {}
        for unit, label in UNIT_LABELS.items():
            row = Adw.ActionRow(title=label, subtitle=unit)
            p = pill('…', 'dim')
            cpu = Gtk.Label(width_chars=7, xalign=1)
            cpu.add_css_class('numeric-value')
            ram = Gtk.Label(width_chars=8, xalign=1)
            ram.add_css_class('dim-label')
            spark = Sparkline()
            spark.max_hint = 10.0
            restart = Gtk.Button(icon_name='view-refresh-symbolic',
                                 tooltip_text=f'Restart {label}',
                                 valign=Gtk.Align.CENTER)
            restart.add_css_class('flat')
            restart.connect('clicked', self._restart, unit, label)
            for wdg in (spark, cpu, ram, p, restart):
                row.add_suffix(wdg)
            svc.add(row)
            self.svc_rows[unit] = {'pill': p, 'cpu': cpu, 'ram': ram,
                                   'spark': spark}

        # ---- graph health ----
        health = group('Graph health')
        self.xrun_row = Adw.ActionRow(
            title='Xruns (dropouts)',
            subtitle='Total processing errors reported by all nodes')
        self.xrun_label = Gtk.Label()
        self.xrun_label.add_css_class('numeric-value')
        self.xrun_spark = Sparkline()
        self.xrun_row.add_suffix(self.xrun_spark)
        self.xrun_row.add_suffix(self.xrun_label)
        health.add(self.xrun_row)

        self.load_row = Adw.ActionRow(
            title='DSP load',
            subtitle='Busiest node: share of the quantum spent processing')
        self.load_label = Gtk.Label()
        self.load_label.add_css_class('numeric-value')
        self.load_spark = Sparkline()
        self.load_spark.max_hint = 1.0
        self.load_row.add_suffix(self.load_spark)
        self.load_row.add_suffix(self.load_label)
        health.add(self.load_row)

        # ---- notifications ----
        notif = group('System notifications',
                      'Desktop notifications while the app is running.')
        for key, title, subtitle in (
                ('notify_links', 'Broken or disconnected links',
                 'Notify when a link between two live nodes disappears '
                 '(e.g. a device vanished mid-stream).'),
                ('notify_services', 'Service failures',
                 'Notify when PipeWire, WirePlumber or PipeWire-Pulse '
                 'enters the failed state.'),
                ('notify_xruns', 'Audio dropouts (xruns)',
                 'Notify when new xruns are detected while this page is '
                 'monitoring.')):
            row = Adw.SwitchRow(title=title, subtitle=subtitle)
            row.set_active(bool(prefs.get(key)))
            row.connect('notify::active',
                        lambda r, _p, k=key: prefs.save(**{k: r.get_active()}))
            notif.add(row)

        # ---- pw-top ----
        top = group('Node activity (pw-top)',
                    'Live per-node timing: WAIT/BUSY times, quantum, rate '
                    'and error counts. Follower nodes are indented.')
        self.top_view = Gtk.TextView(editable=False, monospace=True,
                                     left_margin=12, right_margin=12,
                                     top_margin=8, bottom_margin=8)
        self.top_view.add_css_class('card')
        top_sw = Gtk.ScrolledWindow(min_content_height=220,
                                    max_content_height=320,
                                    vexpand=False)
        top_sw.set_child(self.top_view)
        top.add(top_sw)

        # ---- logs ----
        logs = group('Logs', 'journalctl --user for the PipeWire stack.')
        controls = Adw.ActionRow(title='Follow log')
        self.log_pause = Gtk.Switch(valign=Gtk.Align.CENTER, active=True,
                                    tooltip_text='Keep appending new lines')
        self.log_pause.connect(
            'notify::active',
            lambda s, _p: setattr(self, '_log_paused', not s.get_active()))
        self.err_only = Gtk.ToggleButton(label='Warnings+',
                                         valign=Gtk.Align.CENTER,
                                         tooltip_text='Only show warnings '
                                                      'and errors')
        self.err_only.add_css_class('flat')
        self.err_only.connect('toggled', self._reset_log)
        clear = Gtk.Button(label='Clear view', valign=Gtk.Align.CENTER)
        clear.add_css_class('flat')
        clear.connect('clicked', self._clear_log)
        controls.add_suffix(self.err_only)
        controls.add_suffix(clear)
        controls.add_suffix(self.log_pause)
        logs.add(controls)
        self.log_view = Gtk.TextView(editable=False, monospace=True,
                                     left_margin=12, right_margin=12,
                                     top_margin=8, bottom_margin=8,
                                     wrap_mode=Gtk.WrapMode.WORD_CHAR)
        self.log_view.add_css_class('card')
        log_sw = Gtk.ScrolledWindow(min_content_height=260,
                                    max_content_height=380, vexpand=False)
        log_sw.set_child(self.log_view)
        self._log_sw = log_sw
        logs.add(log_sw)

        self.widget = page_scroller(svc, health, notif, top, logs, width=980)
        self.widget.connect('map', self._on_map)
        self.widget.connect('unmap', self._on_unmap)

    # ---------------------------------------------------------------- poll --
    def _on_map(self, *_a):
        self.refresh()
        if not self._timer:
            self._timer = GLib.timeout_add_seconds(3, self._tick)

    def _on_unmap(self, *_a):
        if self._timer:
            GLib.source_remove(self._timer)
            self._timer = None

    def _tick(self):
        self.refresh()
        return True

    def refresh(self):
        if self._busy:
            return
        self._busy = True
        cursor = None if self._log_paused else self._log_cursor
        prio = '4' if self.err_only.get_active() else None

        def collect():
            snap = stats.health_snapshot()
            log_text, new_cursor = ('', self._log_cursor)
            if not self._log_paused:
                log_text, new_cursor = stats.journal_tail(
                    cursor=cursor, lines=120, priority=prio)
            return snap, log_text, new_cursor
        async_call(collect, self._apply)

    def _apply(self, result, error):
        self._busy = False
        if error or not result:
            return
        snap, log_text, new_cursor = result

        for unit, widgets in self.svc_rows.items():
            state = snap.states.get(unit, 'unknown')
            p = widgets['pill']
            p.set_label(state)
            for c in list(p.get_css_classes()):
                if c.startswith('pill-'):
                    p.remove_css_class(c)
            p.add_css_class(f'pill-{state_style(state)}')

            cur = snap.procs.get(unit)
            prev = self._prev_procs.get(unit)
            if cur and cur.ok:
                widgets['ram'].set_label(f'{cur.rss_bytes / 1048576:.1f} MB')
                if prev:
                    pct = stats.cpu_percent(prev, cur)
                    widgets['cpu'].set_label(f'{pct:.1f}%')
                    widgets['spark'].push(pct)
            else:
                widgets['cpu'].set_label('—')
                widgets['ram'].set_label('—')
            self._prev_procs[unit] = cur

        self.xrun_label.set_label(str(snap.xruns))
        if self._last_xruns is not None:
            delta = snap.xruns - self._last_xruns
            self.xrun_spark.push(max(0, delta))
            if delta > 0:
                self.xrun_row.set_subtitle(
                    f'+{delta} new since last sample')
                if prefs.get('notify_xruns'):
                    self.window.notify_user(
                        'Audio dropouts detected',
                        f'{delta} new xrun(s) in the PipeWire graph')
            else:
                self.xrun_row.set_subtitle(
                    'Total processing errors reported by all nodes')
        self._last_xruns = snap.xruns

        self.load_label.set_label(f'{snap.load * 100:.0f}%')
        self.load_spark.push(snap.load)

        lines = [f'{"":1} {"ID":>4} {"QUANT":>6} {"RATE":>6} {"WAIT":>8} '
                 f'{"BUSY":>8} {"B/Q":>5} {"ERR":>4}  NAME']
        for r in snap.top:
            name = r.name if r.is_driver else f'  + {r.name}'
            lines.append(f'{r.state:1} {r.id:>4} {r.quantum:>6} {r.rate:>6} '
                         f'{r.wait:>8} {r.busy:>8} {r.b_q:>5} '
                         f'{r.errors:>4}  {name}')
        self.top_view.get_buffer().set_text('\n'.join(lines))

        if log_text:
            buf = self.log_view.get_buffer()
            end = buf.get_end_iter()
            if buf.get_char_count():
                buf.insert(end, '\n')
                end = buf.get_end_iter()
            buf.insert(end, log_text)
            # trim to the last ~800 lines
            if buf.get_line_count() > 900:
                start = buf.get_start_iter()
                cut = buf.get_iter_at_line(buf.get_line_count() - 800)[1]
                buf.delete(start, cut)
            GLib.idle_add(self._scroll_log_end)
        self._log_cursor = new_cursor

    def _scroll_log_end(self):
        adj = self._log_sw.get_vadjustment()
        adj.set_value(adj.get_upper())
        return False

    def _reset_log(self, *_a):
        self._clear_log()
        self._log_cursor = None
        self.refresh()

    def _clear_log(self, *_a):
        self.log_view.get_buffer().set_text('')

    def _restart(self, _b, unit, label):
        self.window.toast(f'Restarting {label}…')

        def done(result, error):
            rc = result[0] if result else 1
            self.window.toast(f'{label} restarted' if not error and rc == 0
                              else f'{label} restart failed')
        async_call(lambda: system.restart_unit(unit), done)
