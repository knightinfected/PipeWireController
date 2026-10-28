"""The Equalizer page's right side: the selected equalizer or microphone.

`EqDetail` is one equalizer in full, from the top: where its sound goes
(the route banner), the curve and its bands (`eq_editor.EqEditor`, moved
out of its dialog), a summary (source, bands, preamp, headroom), Listen
(volume, and Compare at the same loudness), the apps playing through it,
and its output.  `MicDetail` is a clean microphone: where it is used, a
before/after level, its source and its processing.

Each is a small document: what you change is kept by Save (Create for a
new one) and dropped by Revert (Discard).  An equalizer plays its curve as
you edit it (the session); everything else applies when saved.  Live
actions (the running switch, volume, mute, Compare, sending an app,
default output) never wait for Save.

`paint(data)` takes one delivery from the page (`enhance_page.collect`) and
updates in place; nothing here rebuilds under the pointer except the Apps
rows, and those only when the set of apps changes.
"""

from __future__ import annotations

import gi

gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Adw, Gio, GLib, Gtk, Pango  # noqa: E402

from ..backend import balance, enhance, eqsession, prefs, pw, rules  # noqa: E402
from ..backend import eqcurve as E  # noqa: E402
from .device_list import MiniMeter  # noqa: E402
from .eq_editor import LAYOUTS, STEREO, EqEditor  # noqa: E402
from .volume import make_volume  # noqa: E402
from .widgets import LOCAL_GRACE, async_call, combine_devices, esc, \
    icon_button  # noqa: E402

DRAFT = '__new__'          # the key of a detail that is not created yet
NOUNS = {'eq': 'equalizer', 'mic': 'clean microphone'}
MINUS = '−'


def _now():
    return GLib.get_monotonic_time() / 1e6


def _db(value, unit=' dB', plus=True):
    """+1.5 dB / −4.0 dB, with a real minus sign."""
    if abs(value) < 0.05:
        return f'0.0{unit}'
    sign = ('+' if plus else '') if value > 0 else MINUS
    return f'{sign}{abs(value):.1f}{unit}'


def _label(text='', css=(), wrap=False, xalign=0.0):
    lbl = Gtk.Label(label=text, xalign=xalign, wrap=wrap)
    if wrap:
        lbl.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
    else:
        lbl.set_ellipsize(Pango.EllipsizeMode.END)
    for c in css:
        lbl.add_css_class(c)
    return lbl


def _group(title, suffix=None):
    g = Adw.PreferencesGroup(title=esc(title))
    if suffix is not None:
        g.set_header_suffix(suffix)
    return g


def route_of(node_name, nodes, streams, limit=8):
    """The devices a published sink's sound goes through, in order: its
    `.out` stream's link, then that one's if it is another equalizer."""
    by_id = {n.id: n for n in nodes}
    out_names = {s.props.get('node.name'): s for s in streams}
    path, seen, cur = [], {node_name}, node_name
    for _ in range(limit):
        out = out_names.get(f'{cur}.out')
        nxt = by_id.get(out.target_id) if out is not None and \
            out.target_id is not None else None
        if nxt is None or nxt.name in seen:
            break
        path.append(nxt)
        seen.add(nxt.name)
        if not nxt.name.startswith('pwctl.eq.'):
            break
        cur = nxt.name
    return path


def apps_into(node, data):
    """Real app streams playing into `node`."""
    if node is None:
        return []
    return [s for s in data['streams']
            if s.is_playback and s.is_app and s.target_id == node.id]


def raw_mic(enh, data):
    """The microphone a clean microphone really records from: its capture
    stream's link, else the one it is set to."""
    cap = next((s for s in data['streams'] if s.props.get('node.name')
                == f'{enh.node_name}.capture'), None)
    if cap is not None and cap.target_id is not None:
        node = next((n for n in data['nodes'] if n.id == cap.target_id),
                    None)
        if node is not None:
            return node
    want = enh.params.get('source_target')
    return next((n for n in data['nodes'] if want and n.name == want), None)


# ----------------------------------------------------------- the frame --

class _Top:
    """The pane's bar: back (narrow), the name and its state, the document
    buttons, the menu."""

    def __init__(self, page):
        self.widget = Gtk.Box(spacing=6)
        self.widget.add_css_class('eqp-top')
        self.back = Gtk.Button(icon_name='go-previous-symbolic',
                               valign=Gtk.Align.CENTER,
                               tooltip_text='Back to the list')
        self.back.add_css_class('flat')
        self.back.connect('clicked', lambda _b: page.show_list())
        self.widget.append(self.back)
        names = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1,
                        hexpand=True, valign=Gtk.Align.CENTER)
        self.title = _label(css=('eqp-title',))
        self.status = _label(css=('caption', 'dim-label'))
        names.append(self.title)
        names.append(self.status)
        self.widget.append(names)
        self.revert = Gtk.Button(valign=Gtk.Align.CENTER)
        self.save = Gtk.Button(valign=Gtk.Align.CENTER)
        self.save.add_css_class('suggested-action')
        self.widget.append(self.revert)
        self.widget.append(self.save)
        self.menu = Gtk.MenuButton(icon_name='view-more-symbolic',
                                   valign=Gtk.Align.CENTER,
                                   tooltip_text='More')
        self.menu.add_css_class('flat')
        self.widget.append(self.menu)

    def set_collapsed(self, collapsed):
        self.back.set_visible(collapsed)


class _Banner:
    """Where the sound goes, in one sentence, and the one action that
    changes it."""

    def __init__(self):
        self.widget = Gtk.Box(spacing=10)
        self.widget.add_css_class('eqp-route')
        self.icon = Gtk.Image(valign=Gtk.Align.START)
        col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8,
                      hexpand=True)
        self.text = _label(wrap=True)
        # under the sentence, not beside it: beside, a narrow pane squeezed
        # the sentence into a column one word wide
        self.button = Gtk.Button(halign=Gtk.Align.START, visible=False)
        self.button.add_css_class('eqp-route-action')
        col.append(self.text)
        col.append(self.button)
        self.widget.append(self.icon)
        self.widget.append(col)
        self._handler = None
        self._style = None

    def show(self, style, icon, text, action=None, on_action=None):
        if self._style != style:
            if self._style:
                self.widget.remove_css_class(self._style)
            self.widget.add_css_class(style)
            self._style = style
        self.icon.set_from_icon_name(icon)
        self.text.set_label(text)
        if self._handler is not None:
            self.button.disconnect(self._handler)
            self._handler = None
        self.button.set_visible(bool(action))
        if action:
            self.button.set_label(action)
            self._handler = self.button.connect('clicked',
                                                lambda _b: on_action())


class _Tile:
    def __init__(self, title, action=None, on_action=None):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        head = Gtk.Box(spacing=4)
        self.title = _label(title, css=('eqp-tile-title',))
        self.title.set_hexpand(True)
        head.append(self.title)
        self.action = None
        if action:
            self.action = Gtk.Button(label=action, visible=False,
                                     valign=Gtk.Align.CENTER)
            self.action.add_css_class('eqp-tile-action')
            self.action.connect('clicked', lambda _b: on_action())
            head.append(self.action)
        self.value = _label(css=('eqp-tile-value',))
        self.sub = _label(css=('eqp-tile-sub',))
        box.append(head)
        box.append(self.value)
        box.append(self.sub)
        self.widget = box
        self.widget.add_css_class('eqp-tile')
        self._tone = None

    def set(self, value, sub='', tone=None):
        self.value.set_label(value)
        self.sub.set_label(sub)
        self.sub.set_visible(bool(sub))
        if tone != self._tone:
            for t in ('good', 'warn'):
                self.value.remove_css_class(t)
            if tone:
                self.value.add_css_class(tone)
            self._tone = tone


class _Detail:
    """What both kinds share: the frame, the menu, the document."""

    kind = ''

    def __init__(self, page, enh):
        self.page = page
        self.window = page.window
        self.enh = enh
        self.draft = enh is None
        self.key = DRAFT if enh is None else enh.id
        self._busy = False
        self._updating = False
        self._data = None
        self._node = None
        self.top = _Top(page)
        self.top.revert.connect('clicked', lambda _b: self.revert())
        self.top.save.connect('clicked', lambda _b: self.save())
        self.top.revert.set_label('Discard' if self.draft else 'Revert')
        self.top.save.set_label('Create' if self.draft else 'Save')
        self.top.menu.set_visible(not self.draft)
        self.banner = _Banner()
        self.column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL,
                              spacing=18, margin_top=14, margin_bottom=36,
                              margin_start=14, margin_end=14)
        self.column.append(self.banner.widget)

    def _frame(self, breakpoints=()):
        clamp = Adw.Clamp(maximum_size=780, tightening_threshold=600,
                          child=self.column)
        self.scroller = Gtk.ScrolledWindow(
            hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True,
            child=clamp)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.append(self.top.widget)
        box.append(self.scroller)
        # The pane's own breakpoints, so the editor reflows to the width
        # it is given, beside the list or alone.  They go with the pane.
        self.widget = Adw.BreakpointBin(width_request=320,
                                        height_request=200, child=box)
        self.widget.add_css_class('eqp-detail')
        for bp in breakpoints:
            self.widget.add_breakpoint(bp)
        self.box = box
        self.set_collapsed(self.page.collapsed)

    def set_collapsed(self, collapsed):
        self.top.set_collapsed(collapsed)

    def _menu(self, items):
        group = Gio.SimpleActionGroup()
        menu = Gio.Menu()
        section = Gio.Menu()
        self._menu_items = {}
        for name, label, fn in items:
            if name is None:
                menu.append_section(None, section)
                section = Gio.Menu()
                continue
            act = Gio.SimpleAction.new(name, None)
            act.connect('activate', lambda *_a, f=fn: f())
            group.add_action(act)
            self._menu_items[name] = [section, section.get_n_items(), label]
            section.append(label, f'eqp.{name}')
        menu.append_section(None, section)
        self.top.widget.insert_action_group('eqp', group)
        self.top.menu.set_menu_model(menu)
        self._menu_group = group

    def _enable(self, name, on):
        act = self._menu_group.lookup_action(name)
        if act is not None:
            act.set_enabled(on)

    def _relabel(self, name, label):
        item = getattr(self, '_menu_items', {}).get(name)
        if item is None or item[2] == label:
            return
        section, index, _old = item
        section.remove(index)
        section.insert(index, label, f'eqp.{name}')
        item[2] = label

    # -- a volume (or level) slider with its percentage and mute ----------
    def _clear_volume(self):
        child = self._vol_box.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self._vol_box.remove(child)
            child = nxt
        self._vol = None
        self._vol_serial = None

    def _build_volume(self, node, meter=True, mute_tip='Mute'):
        levels = balance.Levels(node.channels, node.channel_map)
        self._pct = Gtk.Label(width_chars=5, xalign=1)
        self._pct.add_css_class('dev-pct')

        def changed(value):
            if self._updating:
                return
            self._vol_ts = _now()
            self._pct.set_label(f'{round(value * 100)}%')
            values = levels.set_master(value)
            async_call(lambda: pw.set_channel_volumes(
                node.id, values, serial=node.serial,
                media_class=node.media_class))
        ctl = make_volume(self.page.volume_style, changed, compact=True)
        ctl.widget.set_hexpand(True)
        if meter:
            ctl.set_meter(node.serial)
        mute = Gtk.ToggleButton(icon_name='audio-volume-muted-symbolic',
                                valign=Gtk.Align.CENTER, active=node.muted,
                                tooltip_text=mute_tip)
        mute.add_css_class('flat')

        def muted(btn):
            if self._updating:
                return
            self._vol_ts = _now()
            async_call(lambda: pw.set_mute(node.id, btn.get_active()))
        mute.connect('toggled', muted)
        self._vol_box.append(ctl.widget)
        self._vol_box.append(self._pct)
        self._vol_box.append(mute)
        self._vol = ctl
        self._mute = mute
        self._vol_serial = node.serial
        self._vol_ts = 0.0

    def _paint_slider(self, row, node, off_text, **build):
        if node is None:
            if self._vol is not None:
                self._clear_volume()
            if off_text is not None:
                row.set_subtitle(off_text)
            row.set_sensitive(False)
            return
        if off_text is not None:
            row.set_subtitle('')
        row.set_sensitive(True)
        if self._vol is None or self._vol_serial != node.serial:
            self._clear_volume()
            self._build_volume(node, **build)
        if _now() - self._vol_ts < LOCAL_GRACE:
            return
        self._updating = True
        value = node.volume if node.volume is not None else 1.0
        self._vol.set_value(value)
        self._pct.set_label(f'{round(value * 100)}%')
        if self._mute.get_active() != node.muted:
            self._mute.set_active(node.muted)
        self._updating = False

    def _rename(self):
        dlg = Adw.AlertDialog(heading=f'Rename the {NOUNS[self.kind]}',
                              body='Apps and the Devices page show this '
                                   'name. Renaming restarts it for a '
                                   'moment.')
        entry = Gtk.Entry(text=self.enh.name, activates_default=True)
        dlg.set_extra_child(entry)
        dlg.add_response('cancel', 'Cancel')
        dlg.add_response('ok', 'Rename')
        dlg.set_response_appearance('ok', Adw.ResponseAppearance.SUGGESTED)
        dlg.set_default_response('ok')

        def resp(_d, r):
            name = entry.get_text().strip()
            if r == 'ok' and name and name != self.enh.name:
                self.page.rename(self, name)
        dlg.connect('response', resp)
        dlg.present(self.window)

    def node_of(self, data):
        if self.enh is None:
            return None
        return next((n for n in data['nodes']
                     if n.name == self.enh.node_name), None)

    # the document (each kind fills these in)
    @property
    def dirty(self):
        return False

    @property
    def title(self):
        return self.enh.name if self.enh is not None else ''

    def revert(self):
        pass

    def save(self, then=None):
        pass

    def discard(self):
        """Leaving without saving: put back whatever plays."""

    def close(self):
        """The thing itself is gone (deleted): stop, touch nothing."""

    def paint(self, data):
        pass

    def _paint_top(self):
        pass


# ============================================================ equalizer ==

class EqDetail(_Detail):
    kind = 'eq'

    def __init__(self, page, enh=None, curve=None, name='', notes=None,
                 origin=None):
        super().__init__(page, enh)
        self._vol_box = None
        self._vol = None
        self._vol_serial = None
        self._vol_ts = 0.0
        self._apps_sig = None
        self._targets_sig = None
        self._sinks = []
        self._missing = None
        self.origin = origin or ({'kind': 'made', 'name': ''}
                                 if self.draft else None)

        s = eqsession.EqSession(enh, run_async=async_call,
                                on_status=self._on_status)
        if curve is not None and enh is None:
            s.curve = curve.clamped()
        s.match_level = bool(prefs.get('eq_match_level'))
        self.session = s
        params = enh.params if enh is not None else {}
        self.saved_settings = {
            'name': enh.name if enh is not None else name,
            'target': params.get('target', ''),
            'positions': list(params.get('positions') or ['FL', 'FR'])}
        self.settings = dict(self.saved_settings,
                             positions=list(self.saved_settings['positions']))

        self.editor = EqEditor(self.window, s, others=self._others)
        self.editor.sel = 0 if (curve is not None and curve.bands) else None
        self.editor.on_changed = self._changed
        self.editor.on_loaded = self._loaded
        self.editor.on_save = lambda: self.save()

        if self.draft:
            self.column.append(self._name_group())
        self.column.append(self.editor)
        self.column.append(self._tiles())
        self.column.append(self.editor.inspector)
        if not self.draft:
            self.column.append(self._listen_group())
            self.column.append(self._apps_group())
        self.column.append(self._output_group())

        narrow = Adw.Breakpoint.new(Adw.BreakpointCondition.parse(
            'max-width: 560sp'))
        narrowest = Adw.Breakpoint.new(Adw.BreakpointCondition.parse(
            'max-width: 440sp'))
        self.editor.add_breakpoints(narrow, narrowest)
        # four tiles fit down to ~400 px; below, two by two (not 3 + 1)
        narrowest.add_setter(self.tiles, 'max-children-per-line', 2)
        self._frame((narrow, narrowest))
        self.editor.shortcuts_for(self.box)
        if not self.draft:
            self._menu([
                ('default', 'Make it the default output',
                 lambda: self.page.make_default(self)),
                ('running', 'Switch off', lambda: self.page.set_running(
                    self.enh, not self.enh.enabled)),
                (None, None, None),
                ('rename', 'Rename…', self._rename),
                ('duplicate', 'Duplicate', self._duplicate),
                (None, None, None),
                ('delete', 'Delete…', lambda: self.page.delete(self))])
        self.editor.show_notes(list(notes or []) + (
            ['Some values were outside what the equalizer can play '
             f'({E.GAIN_MAX:+.0f} dB, Q {E.Q_MAX:g}) and were brought '
             'inside it. Saving keeps the new values.']
            if s.capped else []))
        if self.draft:
            self._paint_banner(None)
        self.editor.render()
        if not self.draft:
            s.start()

    # -------------------------------------------------------- sections --
    def _name_group(self):
        g = _group('New equalizer')
        self.name_row = Adw.EntryRow(title='Name')
        self.name_row.set_text(self.settings['name'])
        self.name_row.connect('changed', self._name_changed)
        g.add(self.name_row)
        return g

    def _name_changed(self, row):
        self.settings['name'] = row.get_text()
        self._paint_top()
        self.page.detail_changed(self)

    def _tiles(self):
        self.tiles = Gtk.FlowBox(homogeneous=True, min_children_per_line=2,
                                 max_children_per_line=4,
                                 selection_mode=Gtk.SelectionMode.NONE,
                                 column_spacing=8, row_spacing=8)
        self.tiles.add_css_class('eqp-tiles')
        self.t_source = _Tile('Source')
        self.t_bands = _Tile('Bands')
        self.t_pre = _Tile('Preamp')
        self.t_head = _Tile('Headroom', 'Fix',
                            lambda: self.editor.auto_gain())
        for t in (self.t_source, self.t_bands, self.t_pre, self.t_head):
            self.tiles.append(Gtk.FlowBoxChild(child=t.widget,
                                               focusable=False))
        return self.tiles

    def _listen_group(self):
        g = _group('Listen')
        self.vol_row = Adw.ActionRow(title='Volume', title_lines=1)
        self._vol_box = Gtk.Box(spacing=6, hexpand=True,
                                valign=Gtk.Align.CENTER)
        self.vol_row.add_suffix(self._vol_box)
        g.add(self.vol_row)
        self.match_row = Adw.SwitchRow(title='Compare at the same loudness')
        self.match_row.set_active(self.session.match_level)
        self.match_row.connect('notify::active', self._match_toggled)
        g.add(self.match_row)
        return g

    def _apps_group(self):
        self.apps_note = _label(css=('caption', 'dim-label'))
        self.apps = _group('Apps', self.apps_note)
        self._app_rows = []
        return self.apps

    def _output_group(self):
        g = _group('Output')
        self.target_row = Adw.ComboRow(
            title='Sends to',
            subtitle='Pick another equalizer to stack both curves.')
        self.target_row.set_model(Gtk.StringList.new(['Follow default']))
        self.target_row.connect('notify::selected', self._target_picked)
        g.add(self.target_row)
        combine = Adw.ActionRow(
            title='Send to several devices',
            subtitle='Speakers and HDMI at the same time.')
        btn = Gtk.Button(label='Combine…', valign=Gtk.Align.CENTER)
        btn.connect('clicked', self._combine)
        combine.add_suffix(btn)
        combine.set_activatable_widget(btn)
        g.add(combine)
        self.layout_row = Adw.ComboRow(
            title='Channel layout', subtitle='Must match what plays into it.',
            model=Gtk.StringList.new([l[1] for l in LAYOUTS]))
        self._updating = True
        self.layout_row.set_selected(self._layout_index(
            self.settings['positions']))
        self._updating = False
        self.layout_row.connect('notify::selected', self._layout_picked)
        g.add(self.layout_row)
        return g

    @staticmethod
    def _layout_index(positions):
        return next((i for i, l in enumerate(LAYOUTS)
                     if list(l[2]) == list(positions)), STEREO)

    # ------------------------------------------------------- the edit --
    def _others(self):
        return [e for e in self.page.items if e.kind == 'eq'
                and (self.enh is None or e.id != self.enh.id)]

    def _loaded(self, kind, name):
        if not self.draft:
            return
        self.origin = {'kind': kind, 'name': name}
        if kind == 'file' and not self.settings['name'].strip():
            self.name_row.set_text(name.rsplit('.', 1)[0])

    def _changed(self):
        self._paint_tiles()
        self._paint_top()
        if not self.draft:
            self._paint_match()
        self.page.detail_changed(self)

    def _on_status(self, _status):
        self.editor.render()
        self._paint_top()

    def _match_toggled(self, row, _p):
        on = row.get_active()
        self.session.set_match_level(on)
        prefs.save(eq_match_level=on)
        self._paint_match()

    def _target_picked(self, row, _p):
        if self._updating:
            return
        i = row.get_selected()
        names = [''] + [n.name for n in self._sinks]
        if self._missing:
            names.append(self._missing)
        if 0 <= i < len(names):
            self.settings['target'] = names[i]
        self._paint_settings()

    def _layout_picked(self, row, _p):
        if self._updating:
            return
        self.settings['positions'] = list(LAYOUTS[row.get_selected()][2])
        self._paint_settings()

    def _combine(self, _b):
        def created(node_name, message):
            if not node_name:
                self.window.toast(message)
                return
            self._sinks.append(pw.AudioNode(
                id=-1, name=node_name, description=message,
                media_class='Audio/Sink'))
            self.settings['target'] = node_name
            self._fill_targets()
            self._paint_settings()
            self.window.toast(f'“{message}” created. Save to send '
                              'the equalizer there.')
        combine_devices(self.window, list(self._sinks), created,
                        name_hint=self.title or 'Combined output')

    # ------------------------------------------------------ the document --
    @property
    def title(self):
        if self.draft:
            return self.settings['name'].strip() or 'New equalizer'
        return self.enh.name

    @property
    def settings_dirty(self):
        keys = ('target', 'positions')
        return any(self.settings[k] != self.saved_settings[k] for k in keys)

    @property
    def dirty(self):
        if self.draft:
            return True
        return self.session.dirty or self.settings_dirty

    def revert(self):
        if self.draft:
            self.page.discard_draft()
            return
        if self.session.dirty:
            self.editor.revert()
        self.settings = dict(self.saved_settings,
                             positions=list(self.saved_settings['positions']))
        self._updating = True
        self.layout_row.set_selected(self._layout_index(
            self.settings['positions']))
        self._updating = False
        self._fill_targets()
        self.editor.render()

    def save(self, then=None):
        if self._busy or not self.dirty:
            if then:
                then()
            return
        s = self.session
        if s.status == eqsession.RESTARTING:
            self.window.toast('Still updating — try again in a moment')
            return
        if self.draft:
            self._create(then)
            return
        s.set_listen(eqsession.EDIT)
        enh = self.enh
        old = dict(enh.params)
        params = {**old, **s.params(), 'target': self.settings['target'],
                  'positions': list(self.settings['positions'])}
        settings = dict(self.settings,
                        positions=list(self.settings['positions']))
        self._busy = True
        self._paint_top()

        def work():
            enh.params = params
            ok, err = enhance.apply(enh)
            stored = next((e for e in enhance.list_enhancements()
                           if e.id == enh.id), None)
            return ok, err, stored is not None and stored.params == params

        def done(result, error):
            self._busy = False
            ok, err, kept = result if result else (False, str(error), False)
            if kept:
                s.mark_saved()
                self.saved_settings = settings
            else:
                enh.params = old
            if ok:
                self.window.toast(f'“{enh.name}” saved')
            else:
                self.window.toast(f'Failed: {err or "unknown error"}')
            s.rebind()
            self.editor.render()
            self.page.refresh()
            if ok and then:
                then()
        async_call(work, done)

    def _create(self, then=None):
        name = self.settings['name'].strip()
        if not name:
            self.window.toast('Give the equalizer a name')
            self.name_row.grab_focus()
            return
        params = {**self.session.params(),
                  'target': self.settings['target'],
                  'positions': list(self.settings['positions']),
                  'origin': self.origin}
        enh = enhance.new_enhancement(name, 'eq', params=params)
        enh.enabled = True
        self._busy = True
        self._paint_top()

        def done(result, error):
            self._busy = False
            ok, err = result if result else (False, str(error))
            self.window.toast(f'“{name}” is ready' if ok
                              else f'Failed: {err or "unknown error"}')
            if not enh.meta_path.exists():
                self._paint_top()       # nothing was made: keep the draft
                return
            self.page.created(enh)
            if then:
                then()
        async_call(lambda: enhance.apply(enh), done)

    def discard(self):
        work = self.session.cancel_work()
        if work is not None:
            async_call(work)

    def close(self):
        self.session.close()

    def _duplicate(self):
        params = dict(self.enh.params)
        if self.dirty:
            params.update(self.session.params())
            params['target'] = self.settings['target']
            params['positions'] = list(self.settings['positions'])
        self.page.duplicate(self, params)

    # -------------------------------------------------------- painting --
    def _paint_top(self):
        s = self.session
        top = self.top
        top.title.set_label(self.title)
        if self.draft:
            status = 'New — it starts when you create it'
        elif self._busy:
            status = 'Saving…'
        elif s.status == eqsession.RESTARTING:
            status = 'Updating the equalizer…'
        elif s.status == eqsession.FAILED:
            status = 'Could not update it live — changes apply when you save'
        elif s.status == eqsession.LIVE:
            status = ('Not saved — you are hearing your changes'
                      if self.dirty else
                      'Live — you hear changes as you make them')
        else:
            status = ('Not saved — changes apply when you save'
                      if self.dirty else 'Not running')
        top.status.set_label(status)
        top.status.set_tooltip_text(status)
        show = self.dirty
        top.revert.set_visible(show)
        top.save.set_visible(show)
        top.save.set_sensitive(not self._busy
                               and s.status != eqsession.RESTARTING)
        if not self.draft:
            enh = self.enh
            self._enable('default', enh.enabled and self._node is not None
                         and not self._node.is_default)
            self._paint_running_label()
            dup = 'Duplicate with your changes' if self.dirty \
                else 'Duplicate'
            self._relabel('duplicate', dup)

    def _paint_running_label(self):
        self._relabel('running', 'Switch off' if self.enh.enabled
                      else 'Switch on')

    def _paint_tiles(self):
        curve = self.session.curve
        origin = enhance.origin_of(self.enh) if not self.draft else (
            (enhance.ORIGINS.get(self.origin.get('kind')),
             self.origin.get('name', '')) if self.origin else None)
        self.t_source.widget.get_parent().set_visible(bool(origin))
        if origin:
            self.t_source.set(origin[0], origin[1])
            self.t_source.widget.set_tooltip_text(
                ' · '.join(x for x in origin if x))
        n = len(curve.bands)
        off = sum(1 for b in curve.bands if not b.on)
        self.t_bands.set(str(n), f'{off} off' if off else '')
        self.t_pre.set(_db(curve.preamp))
        head = -curve.peak_db()
        clips = head < -0.05
        if clips:
            self.t_head.set(_db(head), 'It can clip', 'warn')
            self.t_head.widget.set_tooltip_text(
                'The loudest point goes past full scale. Fix (Auto gain) '
                'lowers the preamp just enough.')
        else:
            self.t_head.set(_db(head, plus=False), '', 'good')
            self.t_head.widget.set_tooltip_text(
                'How far the loudest point stays under full scale')
        self.t_head.action.set_visible(clips)

    def _paint_match(self):
        lvl = self.session.curve.level_db()
        self.match_row.set_subtitle(
            f'Saved and Bypassed play at this curve’s loudness '
            f'({_db(lvl)}, estimated for pink noise), so neither wins by '
            'being louder.' if self.match_row.get_active() else
            'Off: Bypassed plays at full level, which can sound better '
            'only because it is louder.')

    def _paint_settings(self):
        tgt = self.settings['target'] != self.saved_settings['target']
        lay = self.settings['positions'] != self.saved_settings['positions']
        when = ' Applies when you save.' if not self.draft else ''
        self.target_row.set_subtitle(
            'Pick another equalizer to stack both curves.'
            + (when if tgt else ''))
        self.layout_row.set_subtitle('Must match what plays into it.'
                                     + (when if lay else ''))
        self._paint_top()
        self.page.detail_changed(self)

    def paint(self, data):
        """One delivery from the page: live state, never the edit."""
        self._data = data
        self._fill_targets()
        if self.draft:
            return
        fresh = next((e for e in data['items'] if e.id == self.enh.id),
                     None)
        if fresh is not None:
            self.enh.enabled = fresh.enabled
        node = self.node_of(data)
        self._node = node
        self._paint_banner(node)
        self._paint_volume(node)
        self._paint_apps(node)
        self._paint_top()
        self._follow(node)

    def _follow(self, node):
        """Something outside the session started, stopped or restarted
        the unit: find it again (never mid-restart of the session's own)."""
        s = self.session
        if s._restart_busy or self._busy:
            return
        if node is not None and s.status in (eqsession.LIVE,
                                             eqsession.FAILED) and \
                node.id != s._node_id:
            s.rebind()
        elif node is None and s.status == eqsession.LIVE:
            s.rebind()
        elif node is not None and s.status == eqsession.OFF and \
                self.enh.enabled:
            s.rebind()

    def _paint_banner(self, node):
        b = self.banner
        if self.draft:
            b.show('new', 'list-add-symbolic',
                   'New. It becomes an output of its own when you create '
                   'it: whatever plays into it is shaped by this curve, '
                   'then sent on to the device below.')
            return
        data = self._data
        enh = self.enh
        state = (data or {}).get('states', {}).get(enh.id, '')
        if not enh.enabled:
            b.show('off', 'media-playback-pause-symbolic',
                   'Not running, so nothing can play through it.',
                   'Switch on', lambda: self.page.set_running(enh, True))
            return
        if node is None:
            if state == 'failed':
                b.show('failed', 'dialog-warning-symbolic',
                       'It failed to start.', 'Try again',
                       lambda: self.page.set_running(enh, True,
                                                     again=True))
            else:
                b.show('off', 'content-loading-symbolic', 'Starting…')
            return
        path = route_of(enh.node_name, data['nodes'], data['streams'])
        names = ' → '.join([enh.name] + [n.description or n.name
                                         for n in path])
        if not path:
            tail = ', but it is not connected to an output yet.'
        else:
            tail = '.'
        if node.is_default:
            b.show('default', 'object-select-symbolic',
                   f'Default output — everything you hear goes '
                   f'{names}{tail}')
        else:
            b.show('info', 'dialog-information-symbolic',
                   f'Only apps you send here are equalized: {names}{tail}',
                   'Make default output', lambda: self.page.make_default(self))

    def _paint_volume(self, node):
        self._paint_slider(self.vol_row, node, 'Switch it on to listen',
                           mute_tip='Mute this equalizer')

    def _paint_apps(self, node):
        data = self._data
        here = apps_into(node, data)
        bound = data.get('bound', {})
        default = node is not None and node.is_default
        ids = {s.id for s in here}
        others = [s for s in data['streams'] if s.is_playback and s.is_app
                  and s.id not in ids]
        sig = (node.id if node is not None else None, default,
               tuple((s.id, s.name, s.media, s.icon,
                      self.enh.node_name in bound.get(s.id, ()))
                     for s in here), bool(others))
        self.apps_note.set_label('plus anything that follows the default'
                                 if default else '')
        if sig == self._apps_sig:
            return
        self._apps_sig = sig
        for r in self._app_rows:
            self.apps.remove(r)
        self._app_rows = []

        def add(row):
            self.apps.add(row)
            self._app_rows.append(row)

        if node is None:
            row = Adw.ActionRow(title='Switch it on to send apps here')
            row.set_sensitive(False)
            add(row)
            return
        for s in here:
            title, detail = _stream_label(s)
            row = Adw.ActionRow(title=title, subtitle=detail,
                                title_lines=1, subtitle_lines=1)
            row.add_prefix(Gtk.Image.new_from_icon_name(
                s.icon or 'multimedia-player-symbolic'))
            kept = self.enh.node_name in bound.get(s.id, ())
            keep = Gtk.ToggleButton(
                icon_name='pwctl-pin-symbolic', active=kept,
                valign=Gtk.Align.CENTER, css_classes=['flat'],
                tooltip_text='Keep sending this app here, including after '
                             'a restart')
            keep.connect('toggled', lambda b, st=s: self.page.keep_app(
                st, self.enh.node_name, b.get_active()))
            row.add_suffix(keep)
            row.add_suffix(icon_button(
                'window-close-symbolic', 'Stop sending this app here',
                lambda *_, st=s: self.page.unroute(self.enh, st)))
            add(row)
        if not here:
            row = Adw.ActionRow(
                title='Nothing is playing through it',
                subtitle=('Apps that play to the default output will.'
                          if default else
                          'Send an app here, or make it the default '
                          'output.'))
            row.add_prefix(Gtk.Image.new_from_icon_name(
                'audio-volume-muted-symbolic'))
            row.set_sensitive(False)
            add(row)
        send = Adw.ButtonRow(title='Send an app here…')
        send.add_css_class('eqp-send')
        send.set_sensitive(bool(others))
        if not others:
            send.set_tooltip_text('No other app is playing right now')
        send.connect('activated', self._send_popover)
        add(send)

    def _send_popover(self, row):
        data = self._data
        node = self._node
        if data is None or node is None:
            return
        here = {s.id for s in apps_into(node, data)}
        candidates = [s for s in data['streams'] if s.is_playback
                      and s.is_app and s.id not in here]
        box = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        box.add_css_class('boxed-list')
        pop = Gtk.Popover(child=box, width_request=320)
        pop.set_parent(row)
        pop.connect('closed', lambda p: GLib.idle_add(p.unparent))
        for s in candidates:
            title, detail = _stream_label(s)
            item = Adw.ActionRow(title=title, subtitle=detail,
                                 activatable=True, title_lines=1,
                                 subtitle_lines=1)
            item.add_prefix(Gtk.Image.new_from_icon_name(
                s.icon or 'multimedia-player-symbolic'))
            item.connect('activated', lambda *_a, st=s: (
                pop.popdown(), self.page.route(self.enh, node, st)))
            box.append(item)
        pop.popup()

    def _fill_targets(self):
        data = self._data
        if data is None:
            return
        sinks = enhance.eq_targets(self.enh,
                                   [n for n in data['nodes'] if n.is_sink],
                                   self.page.items, data['edges'])
        # a combined output made here but not delivered yet stays listed
        for extra in self._sinks:
            if extra.id == -1 and all(n.name != extra.name for n in sinks):
                sinks.append(extra)
        want = self.settings['target']
        missing = want if want and all(n.name != want
                                       for n in sinks) else None
        sig = (tuple((n.name, n.description) for n in sinks), missing, want)
        if sig == self._targets_sig:
            return
        self._targets_sig = sig
        self._sinks = sinks
        self._missing = missing
        labels = ['Follow default'] + [n.description or n.name
                                       for n in sinks]
        if missing:
            labels.append(f'{missing} (not connected)')
        self._updating = True
        self.target_row.set_model(Gtk.StringList.new(labels))
        if not want:
            sel = 0
        elif missing:
            sel = len(labels) - 1
        else:
            sel = next(i + 1 for i, n in enumerate(sinks) if n.name == want)
        self.target_row.set_selected(sel)
        self._updating = False
        self._paint_settings()


def _stream_label(s):
    # Both halves land in Adw row titles, which are Pango markup — and a
    # media.name is whatever an app decided to call what it is playing.
    detail = s.media or s.binary or ''
    if detail and detail.strip().lower() == s.name.strip().lower():
        detail = ''
    return esc(s.name), esc(detail) or 'Playing'


# ================================================================== mic ==

class MicDetail(_Detail):
    kind = 'mic'

    # (label, params-key, subtitle, advanced)
    TOGGLES = [
        ('Noise suppression', 'noise_suppression',
         'Remove steady background noise (fans, hiss, hum).', False),
        ('Automatic gain control', 'gain_control',
         'Even out the microphone level automatically.', False),
        ('High-pass filter', 'high_pass_filter',
         'Cut low-frequency rumble below speech.', False),
        ('Voice activity detection', 'voice_detection',
         'Detect speech to gate the processing.', False),
        ('Extended filter', 'extended_filter',
         'Cancel echo with a longer tail (larger rooms).', True),
        ('Delay-agnostic mode', 'delay_agnostic',
         'Track a variable speaker-to-mic delay.', True),
        ('Transient suppression', 'transient_suppression',
         'Damp keyboard clicks and other short transients.', True),
    ]

    def __init__(self, page, enh=None):
        super().__init__(page, enh)
        params = enh.params if enh is not None else {}
        saved = {'name': enh.name if enh is not None else 'Clean Microphone',
                 'source_target': params.get('source_target', ''),
                 'monitor_mode': bool(params.get('monitor_mode', True))}
        for _l, key, _s, _a in self.TOGGLES:
            saved[key] = bool(params.get(key, enhance.WEBRTC_DEFAULTS[key]))
        self.saved_settings = saved
        self.settings = dict(saved)
        self._sources = []
        self._sources_sig = None
        self._vol = None
        self._vol_serial = None
        self._vol_ts = 0.0
        self._vol_box = None

        if not self.draft:
            self.column.append(self._listen_group())
        self.column.append(self._mic_group())
        proc = _group('Processing')
        adv = _group('Advanced processing')
        self.toggle_rows = {}
        for label, key, subtitle, is_adv in self.TOGGLES:
            row = Adw.SwitchRow(title=label, subtitle=subtitle)
            row.set_active(self.settings[key])
            row.connect('notify::active', self._toggled, key)
            (adv if is_adv else proc).add(row)
            self.toggle_rows[key] = row
        self.column.append(proc)
        self.column.append(adv)
        self.adv_group = adv
        self.set_advanced(bool(getattr(self.window, 'advanced', False)))
        self._frame()
        if not self.draft:
            self._menu([
                ('default', 'Make it the default input',
                 lambda: self.page.make_default(self)),
                ('running', 'Switch off', lambda: self.page.set_running(
                    self.enh, not self.enh.enabled)),
                (None, None, None),
                ('rename', 'Rename…', self._rename),
                (None, None, None),
                ('delete', 'Delete…', lambda: self.page.delete(self))])
        if self.draft:
            self._paint_banner(None)
        self._paint_top()

    def set_advanced(self, on):
        # a saved advanced option stays in view even with Advanced off
        shown = on or any(self.settings[k] != enhance.WEBRTC_DEFAULTS[k]
                          for _l, k, _s, a in self.TOGGLES if a)
        self.adv_group.set_visible(shown)

    def _listen_group(self):
        g = _group('Listen')
        self.before_row = Adw.ActionRow(title='Before',
                                        subtitle='The real microphone')
        self.before = MiniMeter()
        self.before.area.set_size_request(120, -1)
        self.before.area.set_valign(Gtk.Align.CENTER)
        self.before_row.add_suffix(self.before.area)
        g.add(self.before_row)
        self.after_row = Adw.ActionRow(title='After',
                                       subtitle='What apps hear')
        self.after = MiniMeter()
        self.after.area.set_size_request(120, -1)
        self.after.area.set_valign(Gtk.Align.CENTER)
        self.after_row.add_suffix(self.after.area)
        g.add(self.after_row)
        self.level_row = Adw.ActionRow(title='Level', title_lines=1)
        self._vol_box = Gtk.Box(spacing=6, hexpand=True,
                                valign=Gtk.Align.CENTER)
        self.level_row.add_suffix(self._vol_box)
        g.add(self.level_row)
        return g

    def _mic_group(self):
        g = _group('Microphone')
        if self.draft:
            self.name_row = Adw.EntryRow(title='Name')
            self.name_row.set_text(self.settings['name'])
            self.name_row.connect('changed', self._name_changed)
            g.add(self.name_row)
        self.source_row = Adw.ComboRow(
            title='Source microphone',
            subtitle='The real microphone to clean up.')
        self.source_row.set_model(Gtk.StringList.new(['Follow default']))
        self.source_row.connect('notify::selected', self._source_picked)
        g.add(self.source_row)
        self.monitor_row = Adw.SwitchRow(
            title='Reference from system output',
            subtitle='Use whatever is playing on your speakers or headphones '
                     'as the echo reference. Turn off only if you route the '
                     'echo reference yourself.')
        self.monitor_row.set_active(self.settings['monitor_mode'])
        self.monitor_row.connect('notify::active', self._toggled,
                                 'monitor_mode')
        g.add(self.monitor_row)
        return g

    def _name_changed(self, row):
        self.settings['name'] = row.get_text()
        self._paint_top()
        self.page.detail_changed(self)

    def _toggled(self, row, _p, key):
        if self._updating:
            return
        self.settings[key] = row.get_active()
        self._paint_top()
        self.page.detail_changed(self)

    def _source_picked(self, row, _p):
        if self._updating:
            return
        i = row.get_selected()
        names = [''] + [n.name for n in self._sources]
        if 0 <= i < len(names):
            self.settings['source_target'] = names[i]
        self._paint_top()
        self.page.detail_changed(self)

    @property
    def title(self):
        if self.draft:
            return self.settings['name'].strip() or 'New clean microphone'
        return self.enh.name

    @property
    def dirty(self):
        if self.draft:
            return True
        return any(self.settings[k] != v for k, v in
                   self.saved_settings.items() if k != 'name')

    def revert(self):
        if self.draft:
            self.page.discard_draft()
            return
        self.settings = dict(self.saved_settings)
        self._updating = True
        for key, row in self.toggle_rows.items():
            row.set_active(self.settings[key])
        self.monitor_row.set_active(self.settings['monitor_mode'])
        self._sources_sig = None
        self._updating = False
        self._fill_sources()
        self._paint_top()
        self.page.detail_changed(self)

    def save(self, then=None):
        if self._busy or not self.dirty:
            if then:
                then()
            return
        name = self.settings['name'].strip()
        if not name:
            self.window.toast('Give the microphone a name')
            return
        params = {k: v for k, v in self.settings.items() if k != 'name'}
        settings = dict(self.settings)
        if self.draft:
            enh = enhance.new_enhancement(name, 'mic', params=params)
            enh.enabled = True
        else:
            enh = self.enh
            old = dict(enh.params)
            enh.params = {**old, **params}
        self._busy = True
        self._paint_top()

        def done(result, error):
            self._busy = False
            ok, err = result if result else (False, str(error))
            self.window.toast((f'“{name}” is ready' if self.draft
                               else f'“{name}” saved') if ok
                              else f'Failed: {err or "unknown error"}')
            if self.draft and not enh.meta_path.exists():
                self._paint_top()       # nothing was made: keep the draft
                return
            if self.draft:
                self.page.created(enh)
            else:
                self.saved_settings = settings
                self._paint_top()
                self.page.refresh()
            if then:
                then()
        async_call(lambda: enhance.apply(enh), done)

    def _paint_top(self):
        top = self.top
        top.title.set_label(self.title)
        if self.draft:
            status = 'New — it starts when you create it'
        elif self._busy:
            status = 'Saving…'
        elif self.dirty:
            status = 'Not saved — changes apply when you save'
        elif not self.enh.enabled:
            status = 'Not running'
        else:
            status = 'Running'
        top.status.set_label(status)
        show = self.dirty
        top.revert.set_visible(show)
        top.save.set_visible(show)
        top.save.set_sensitive(not self._busy)
        if not self.draft:
            node = self._node
            self._enable('default', self.enh.enabled and node is not None
                         and not node.is_default)
            self._relabel('running', 'Switch off' if self.enh.enabled
                          else 'Switch on')

    def paint(self, data):
        self._data = data
        self._fill_sources()
        if self.draft:
            return
        fresh = next((e for e in data['items'] if e.id == self.enh.id),
                     None)
        if fresh is not None:
            self.enh.enabled = fresh.enabled
        node = self.node_of(data)
        self._node = node
        self._paint_banner(node)
        self._paint_meters(node)
        self._paint_level(node)
        self._paint_top()

    def _paint_banner(self, node):
        b = self.banner
        if self.draft:
            b.show('new', 'list-add-symbolic',
                   'New. It becomes an input of its own when you create '
                   'it: your microphone with echo and background noise '
                   'taken out, for calls and streaming.')
            return
        enh = self.enh
        state = (self._data or {}).get('states', {}).get(enh.id, '')
        if not enh.enabled:
            b.show('off', 'media-playback-pause-symbolic',
                   'Not running, so no app can pick it.', 'Switch on',
                   lambda: self.page.set_running(enh, True))
        elif node is None:
            if state == 'failed':
                b.show('failed', 'dialog-warning-symbolic',
                       'It failed to start.', 'Try again',
                       lambda: self.page.set_running(enh, True,
                                                     again=True))
            else:
                b.show('off', 'content-loading-symbolic', 'Starting…')
        elif node.is_default:
            b.show('default', 'object-select-symbolic',
                   'Default input — apps that record hear the clean '
                   'microphone.')
        else:
            b.show('info', 'dialog-information-symbolic',
                   'Pick it as the input in the app that needs it, or make '
                   'it the default for every app.', 'Make default input',
                   lambda: self.page.make_default(self))

    def _raw_mic(self, data):
        return raw_mic(self.enh, data)

    def _paint_meters(self, node):
        data = self._data
        raw = self._raw_mic(data) if node is not None else None
        # A meter records: only while the device runs anyway, so looking
        # at this page never opens the microphone (UI.md §9, MiniMeter).
        for meter, row, n, what in (
                (self.before, self.before_row, raw, 'The real microphone'),
                (self.after, self.after_row, node, 'What apps hear')):
            running = n is not None and n.state == 'running'
            meter.set(n.serial if n is not None else None, running)
            if n is None:
                row.set_subtitle(what)
            elif running:
                row.set_subtitle(esc(n.description or n.name))
            else:
                row.set_subtitle(f'{esc(n.description or n.name)} · idle '
                                 '— the level shows while an app records')

    def _paint_level(self, node):
        # No meter on this slider: metering a source records from it, which
        # would open the microphone; the After row meters only while an
        # app already records.
        self._paint_slider(self.level_row, node, None, meter=False,
                           mute_tip='Mute the clean microphone')

    def _fill_sources(self):
        data = self._data
        if data is None:
            return
        sources = [n for n in data['nodes'] if not n.is_sink
                   and not n.name.startswith('pwctl.mic.')]
        sig = tuple((n.name, n.description) for n in sources)
        want = self.settings['source_target']
        if sig == self._sources_sig:
            return
        self._sources_sig = sig
        self._sources = sources
        names = ['Follow default'] + [n.description or n.name
                                      for n in sources]
        self._updating = True
        self.source_row.set_model(Gtk.StringList.new(names))
        self.source_row.set_selected(next(
            (i + 1 for i, n in enumerate(sources) if n.name == want), 0))
        self._updating = False
