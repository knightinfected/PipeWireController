"""Policy page: per-application rules, auto-connect and default selection.

Per-app rules become stream.rules in the client.conf / pipewire-pulse.conf
drop-ins (they apply when an app opens its next stream).  Default-device
priorities and the clock master become WirePlumber node rules.
"""

from __future__ import annotations

import gi

gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Adw, Gtk  # noqa: E402

from ..backend import pw, rules
from .widgets import async_call, group, icon_button, page_scroller

MATCH_KEYS = [
    ('application.name', 'Application name'),
    ('application.process.binary', 'Process binary'),
    ('node.name', 'Node name'),
]

CLOCK_MASTER_PRIORITY = 20000


class PolicyPage:
    def __init__(self, window):
        self.window = window
        self._nodes = []

        self.apps = group(
            'Per-application rules',
            'Route an app to a fixed device, keep it where you put it, or '
            'stop it from connecting automatically. Applied when the app '
            'opens its next stream (restart PipeWire-Pulse for Pulse apps).')
        add_row = Adw.ActionRow(
            title='Add application rule',
            subtitle='Match by application name, binary or node name')
        add_btn = Gtk.Button(icon_name='list-add-symbolic',
                             valign=Gtk.Align.CENTER)
        add_btn.add_css_class('suggested-action')
        add_btn.connect('clicked', lambda *_: self._open_rule_dialog(None))
        add_row.add_suffix(add_btn)
        add_row.set_activatable_widget(add_btn)
        self.apps.add(add_row)
        self._app_rows = []

        self.priorities = group(
            'Default device selection',
            'WirePlumber picks the available device with the highest '
            'session priority when nothing is chosen explicitly. Raise a '
            'device to make it win automatic selection; ★ on the Dashboard '
            'still overrides manually.')
        self._prio_rows = []

        self.clock = group(
            'Graph clock source',
            'The driver device whose hardware clock paces the whole graph. '
            'Auto lets PipeWire pick; forcing one device helps when another '
            'keeps drifting or resampling.')
        self.clock_row = Adw.ComboRow(title='Preferred clock master')
        self.clock_row.connect('notify::selected', self._clock_changed)
        self.clock.add(self.clock_row)
        self._clock_updating = False

        self.widget = page_scroller(self.apps, self.priorities, self.clock)
        self.widget.connect('map', lambda *_: self.refresh())

    # -------------------------------------------------------------- refresh --
    def refresh(self):
        def collect():
            return pw.list_audio_nodes(), rules.load()
        async_call(collect, self._apply)

    def _apply(self, result, error):
        if error or result is None:
            return
        nodes, data = result
        self._nodes = nodes

        for row in self._app_rows:
            self.apps.remove(row)
        self._app_rows = []
        for i, rule in enumerate(data['apps']):
            row = self._rule_row(i, rule)
            self.apps.add(row)
            self._app_rows.append(row)

        for row in self._prio_rows:
            self.priorities.remove(row)
        self._prio_rows = []
        hw = [n for n in nodes if not n.is_virtual]
        for node in sorted(hw, key=lambda n: (not n.is_sink,
                                              n.description.lower())):
            row = self._priority_row(node, data)
            self.priorities.add(row)
            self._prio_rows.append(row)

        self._clock_updating = True
        try:
            sinks_sources = [n for n in hw]
            names = ['Auto (highest driver priority wins)'] + \
                    [n.description for n in sinks_sources]
            self.clock_row.set_model(Gtk.StringList.new(names))
            master = next(
                (i + 1 for i, n in enumerate(sinks_sources)
                 if (data['nodes'].get(n.name, {}).get('props', {})
                     .get('priority.driver')) == CLOCK_MASTER_PRIORITY), 0)
            self.clock_row.set_selected(master)
            self._clock_nodes = sinks_sources
        finally:
            self._clock_updating = False

    # ------------------------------------------------------------ app rules --
    def _rule_row(self, index, rule):
        match = rule.get('match') or {}
        props = rule.get('props') or {}
        key, value = next(iter(match.items()), ('?', '?'))
        bits = []
        if props.get('target.object'):
            bits.append(f'→ {props["target.object"]}')
        if props.get('node.autoconnect') is False:
            bits.append('no auto-connect')
        if props.get('node.dont-reconnect'):
            bits.append('never moved automatically')
        row = Adw.ActionRow(title=f'{value}',
                            subtitle=f'{key} · ' + (' · '.join(bits) or
                                                    'no actions'),
                            title_lines=1, subtitle_lines=1)
        row.add_prefix(Gtk.Image.new_from_icon_name(
            'application-x-executable-symbolic'))
        row.add_suffix(icon_button(
            'document-edit-symbolic', 'Edit rule',
            lambda *_: self._open_rule_dialog(rule)))
        row.add_suffix(icon_button(
            'user-trash-symbolic', 'Delete rule',
            lambda *_, i=index: self._delete_rule(i)))
        return row

    def _delete_rule(self, index):
        def work():
            rules.delete_app_rule(index)
            return True
        async_call(work, lambda r, e: (self.window.toast('Rule deleted'),
                                       self.window.flag_restart('pulse'),
                                       self.refresh()))

    def _open_rule_dialog(self, rule):
        AppRuleDialog(self.window, self, rule).present(self.window)

    # ----------------------------------------------------------- priorities --
    def _priority_row(self, node, data):
        stored = (data['nodes'].get(node.name, {}).get('props', {})
                  .get('priority.session'))
        current = stored if stored is not None else \
            node.props.get('priority.session', 0)
        row = Adw.SpinRow.new_with_range(0, 5000, 50)
        row.set_title(node.description)
        row.set_subtitle(('Output · ' if node.is_sink else 'Input · ')
                         + node.name
                         + ('' if stored is None else ' · overridden'))
        try:
            row.set_value(float(current))
        except (TypeError, ValueError):
            pass
        row.connect('notify::value', self._priority_changed, node)
        return row

    def _priority_changed(self, row, _p, node):
        value = int(row.get_value())
        base = node.props.get('priority.session', 0)

        def work():
            rules.set_node_rule(node.name, props={
                'priority.session': None if value == base else value})
            return True
        async_call(work, lambda r, e: self.window.flag_restart('wireplumber'))

    def _clock_changed(self, row, _p):
        if self._clock_updating:
            return
        idx = row.get_selected()

        def work():
            for i, node in enumerate(self._clock_nodes):
                want = CLOCK_MASTER_PRIORITY if i == idx - 1 else None
                cur = (rules.load()['nodes'].get(node.name, {})
                       .get('props', {}).get('priority.driver'))
                if cur != want:
                    rules.set_node_rule(node.name,
                                        props={'priority.driver': want})
            return True
        async_call(work, lambda r, e: (
            self.window.toast('Clock master preference saved'),
            self.window.flag_restart('wireplumber')))


class AppRuleDialog(Adw.Dialog):
    def __init__(self, window, page, rule):
        super().__init__(title='Application rule', content_width=520,
                         content_height=560)
        self.window = window
        self.page = page
        self.rule = rule
        match = (rule or {}).get('match') or {}
        props = (rule or {}).get('props') or {}

        g = Adw.PreferencesGroup(title='Match')
        self.key_row = Adw.ComboRow(
            title='Match on',
            model=Gtk.StringList.new([t for _k, t in MATCH_KEYS]))
        g.add(self.key_row)
        self.value_row = Adw.EntryRow(title='Value (exact match)')
        g.add(self.value_row)
        if match:
            key, value = next(iter(match.items()))
            idx = next((i for i, (k, _t) in enumerate(MATCH_KEYS)
                        if k == key), 0)
            self.key_row.set_selected(idx)
            self.value_row.set_text(value)

        self.running_row = Adw.ComboRow(
            title='…or pick a running app',
            subtitle='Fills the fields from a live stream')
        self.running_row.connect('notify::selected', self._pick_running)
        g.add(self.running_row)
        self._streams = []

        a = Adw.PreferencesGroup(title='Actions')
        self.target_row = Adw.ComboRow(title='Play on / record from')
        a.add(self.target_row)
        self.autoconnect_row = Adw.SwitchRow(
            title='Connect automatically',
            subtitle='Off = the stream waits until you patch it manually '
                     '(patchbay or per-stream device menu).')
        self.autoconnect_row.set_active(
            props.get('node.autoconnect') is not False)
        a.add(self.autoconnect_row)
        self.pin_row = Adw.SwitchRow(
            title='Pin to target',
            subtitle='Never follow default-device changes; stay where the '
                     'rule (or you) put it.')
        self.pin_row.set_active(bool(props.get('node.dont-reconnect')))
        a.add(self.pin_row)

        save = Gtk.Button(label='Save rule', halign=Gtk.Align.END,
                          margin_top=12)
        save.add_css_class('suggested-action')
        save.connect('clicked', self._save)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18,
                      margin_top=12, margin_bottom=24,
                      margin_start=18, margin_end=18)
        box.append(g)
        box.append(a)
        box.append(save)
        sw = Gtk.ScrolledWindow(vexpand=True,
                                hscrollbar_policy=Gtk.PolicyType.NEVER)
        sw.set_child(box)
        view = Adw.ToolbarView()
        view.add_top_bar(Adw.HeaderBar())
        view.set_content(sw)
        self.set_child(view)

        self._target_nodes = []
        self._want_target = props.get('target.object', '')

        def collect():
            return pw.list_audio_nodes(), pw.list_streams()
        async_call(collect, self._loaded)

    def _loaded(self, result, error):
        if error or result is None:
            return
        nodes, streams = result
        self._target_nodes = nodes
        names = ['(keep default)'] + [n.description for n in nodes]
        self.target_row.set_model(Gtk.StringList.new(names))
        if self._want_target:
            idx = next((i + 1 for i, n in enumerate(nodes)
                        if n.name == self._want_target), 0)
            self.target_row.set_selected(idx)
        self._streams = streams
        self.running_row.set_model(Gtk.StringList.new(
            ['—'] + [s.name for s in streams]))

    def _pick_running(self, row, _p):
        idx = row.get_selected()
        if idx <= 0 or idx > len(self._streams):
            return
        s = self._streams[idx - 1]
        key_idx, value = 0, s.props.get('application.name')
        if not value and s.binary:
            key_idx, value = 1, s.binary
        if not value:
            key_idx, value = 2, s.props.get('node.name', '')
        self.key_row.set_selected(key_idx)
        self.value_row.set_text(value or '')

    def _save(self, _b):
        key = MATCH_KEYS[self.key_row.get_selected()][0]
        value = self.value_row.get_text().strip()
        if not value:
            self.window.toast('Enter a match value')
            return
        props = {}
        idx = self.target_row.get_selected()
        if idx > 0 and idx <= len(self._target_nodes):
            props['target.object'] = self._target_nodes[idx - 1].name
        if not self.autoconnect_row.get_active():
            props['node.autoconnect'] = False
        if self.pin_row.get_active():
            props['node.dont-reconnect'] = True
        if not props:
            self.window.toast('Choose at least one action')
            return

        def work():
            rules.upsert_app_rule(key, value, props)
            return True
        self.close()
        async_call(work, lambda r, e: (
            self.window.toast('Rule saved — applies to new streams'
                              if not e else f'Failed: {e}'),
            self.window.flag_restart('pulse'),
            self.page.refresh()))
