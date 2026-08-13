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
from .widgets import async_call, esc, group, icon_button, page_scroller

MATCH_KEYS = [
    ('application.name', 'Application name'),
    ('application.process.binary', 'Process binary'),
    ('node.name', 'Node name'),
]

# (media.class, label, subtitle, row icon). An app can hold one rule per
# direction, so a VoIP client can have both its speaker and its microphone
# set — matching on media.class is what keeps the two rules apart (#7).
DIRECTIONS = [
    ('', 'Both directions',
     'One rule covering everything the app plays and records.',
     'application-x-executable-symbolic'),
    ('Stream/Output/Audio', 'Playback',
     'What the app plays. Choose an output device.',
     'audio-speakers-symbolic'),
    ('Stream/Input/Audio', 'Recording',
     'What the app captures. Choose an input device.',
     'audio-input-microphone-symbolic'),
]

CLOCK_MASTER_PRIORITY = 20000


class PolicyPage:
    def __init__(self, window):
        self.window = window
        self._nodes = []

        self.apps = group(
            'Per-application rules',
            'Route an app to a fixed device, keep it where you put it, or '
            'stop it from connecting automatically. Each rule covers one '
            'direction, so an app can have both a speaker and a microphone. '
            'Applied when the app opens its next stream (restart '
            'PipeWire-Pulse for Pulse apps).')
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
        match = dict(rule.get('match') or {})
        props = rule.get('props') or {}
        direction = match.pop('media.class', '')
        key, value = next(iter(match.items()), ('?', '?'))
        # The dialog calls this "Process binary"; the row said
        # application.process.binary. Same vocabulary in both places.
        key_label = next((t for k, t in MATCH_KEYS if k == key), key)
        _mc, dir_label, _sub, icon = next(
            (d for d in DIRECTIONS if d[0] == direction), DIRECTIONS[0])
        bits = []
        if props.get('target.object'):
            # Stored as a node name; show the description the rest of the app
            # uses, falling back to the raw name if the device is unplugged.
            target = props['target.object']
            bits.append('→ ' + esc(next(
                (n.description for n in self._nodes if n.name == target),
                target)))
        if props.get('node.autoconnect') is False:
            bits.append('no auto-connect')
        if props.get('node.dont-reconnect'):
            bits.append('never moved automatically')
        # Direction leads the subtitle: with one rule per direction, two rows
        # can carry the same app name and that is the only thing telling them
        # apart.
        row = Adw.ActionRow(title=esc(value),
                            subtitle=dir_label + ' · ' + esc(key_label) +
                                     ' · ' + (' · '.join(bits) or
                                              'no actions'),
                            title_lines=1, subtitle_lines=1)
        row.add_prefix(Gtk.Image.new_from_icon_name(icon))
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
        row.set_title(esc(node.description))
        row.set_subtitle(('Output · ' if node.is_sink else 'Input · ')
                         + esc(node.name)
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
        match = dict((rule or {}).get('match') or {})
        props = (rule or {}).get('props') or {}
        # What the rule matched before this edit — needed so changing the app
        # name or the direction replaces the rule instead of leaving the old
        # one behind next to the new one.
        self._old_match = dict(match) or None
        self._updating = False

        g = Adw.PreferencesGroup(title='Match')
        self.key_row = Adw.ComboRow(
            title='Match on',
            model=Gtk.StringList.new([t for _k, t in MATCH_KEYS]))
        g.add(self.key_row)
        self.value_row = Adw.EntryRow(title='Value (exact match)')
        g.add(self.value_row)
        self.direction_row = Adw.ComboRow(
            title='Applies to',
            subtitle=DIRECTIONS[1][2],
            model=Gtk.StringList.new([d[1] for d in DIRECTIONS]))
        # New rules default to playback: it is the common case, and it lets
        # the device list below show only outputs instead of every endpoint.
        self.direction_row.set_selected(1)
        g.add(self.direction_row)

        direction = match.pop('media.class', '')
        if match:
            key, value = next(iter(match.items()))
            idx = next((i for i, (k, _t) in enumerate(MATCH_KEYS)
                        if k == key), 0)
            self.key_row.set_selected(idx)
            self.value_row.set_text(value)
            # An existing rule keeps whatever direction it was saved with —
            # rules written before this existed matched both, and silently
            # narrowing one on edit would change what it does.
            self.direction_row.set_selected(
                next((i for i, d in enumerate(DIRECTIONS)
                      if d[0] == direction), 0))
        self.direction_row.connect('notify::selected', self._direction_changed)

        self.running_row = Adw.ComboRow(
            title='…or pick a running app',
            subtitle='Fills the fields from a live stream')
        self.running_row.connect('notify::selected', self._pick_running)
        g.add(self.running_row)
        self._streams = []

        a = Adw.PreferencesGroup(title='Actions')
        self.target_row = Adw.ComboRow(title='Play on')
        self.target_row.connect('notify::selected', self._target_picked)
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

        self._all_nodes = []
        self._target_nodes = []
        self._want_target = props.get('target.object', '')

        def collect():
            return pw.list_audio_nodes(), pw.list_streams()
        async_call(collect, self._loaded)

    def _loaded(self, result, error):
        if error or result is None:
            return
        nodes, streams = result
        self._all_nodes = nodes
        self._fill_targets()
        self._streams = streams
        self.running_row.set_model(Gtk.StringList.new(
            ['—'] + [s.name for s in streams]))

    # ------------------------------------------------------------ direction --
    def _direction(self) -> str:
        return DIRECTIONS[self.direction_row.get_selected()][0]

    def _direction_changed(self, row, _p):
        self.direction_row.set_subtitle(DIRECTIONS[row.get_selected()][2])
        self._fill_targets()

    def _fill_targets(self):
        """Rebuild the device list for the current direction.

        A playback rule offered every endpoint could be pointed at a
        microphone, which is the other half of what made this dialog read as
        incomplete — you were asked for one device without being told which
        end of the app it was for.
        """
        want = self._direction()
        if want == rules.DIR_PLAYBACK:
            nodes = [n for n in self._all_nodes
                     if n.media_class == 'Audio/Sink']
            title = 'Play on'
        elif want == rules.DIR_RECORDING:
            nodes = [n for n in self._all_nodes
                     if n.media_class == 'Audio/Source']
            title = 'Record from'
        else:
            nodes = list(self._all_nodes)
            title = 'Play on / record from'
        self._target_nodes = nodes
        self.target_row.set_title(title)
        idx = next((i + 1 for i, n in enumerate(nodes)
                    if n.name == self._want_target), 0)
        self._updating = True
        try:
            self.target_row.set_model(Gtk.StringList.new(
                ['(keep default)'] + [n.description for n in nodes]))
            self.target_row.set_selected(idx)
        finally:
            self._updating = False

    def _target_picked(self, row, _p):
        # Remembered by name, so flipping direction and back keeps the choice
        # even though the list in between held different devices.
        if self._updating:
            return
        idx = row.get_selected()
        self._want_target = (self._target_nodes[idx - 1].name
                             if 0 < idx <= len(self._target_nodes) else '')

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
        # A live stream knows its own direction, so the rule starts on the
        # right one instead of the default.
        self.direction_row.set_selected(1 if s.is_playback else 2)

    def _save(self, _b):
        key = MATCH_KEYS[self.key_row.get_selected()][0]
        value = self.value_row.get_text().strip()
        if not value:
            self.window.toast('Enter a match value')
            return
        match = {key: value}
        if self._direction():
            match['media.class'] = self._direction()
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
            rules.upsert_app_rule(match, props, self._old_match)
            return True
        self.close()
        async_call(work, lambda r, e: (
            self.window.toast('Rule saved — applies to new streams'
                              if not e else f'Failed: {e}'),
            self.window.flag_restart('pulse'),
            self.page.refresh()))
