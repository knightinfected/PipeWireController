"""Signal Paths: build a chain from an app to an output, stage by stage.

Sources on top, mixes below, sends between them.  A source is where audio
enters — an app, a microphone, or everything on the default output — and
carries its own chain; a mix carries a chain of its own and feeds real
devices.  One source and one mix is a straight line, which is what most setups
are; the second dimension only matters when a chain has to split.

Two audiences pull in opposite directions and both are served here.  Someone
who just wants an equalizer on everything should never meet the word "mix":
Quick setup builds the whole arrangement in one click.  Someone running twenty
plugin sinks into three outputs needs every picker to cope with a long list,
so devices, plugins and apps are all chosen through a searchable dialog rather
than a dropdown that becomes unusable past a dozen entries.
"""

from __future__ import annotations

import json

import gi

gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from ..backend import levels, paths, plugins, prefs, pw, virtual
from .volume import make_volume
from .widgets import async_call, confirm, esc, group, icon_button, \
    page_scroller, pick_file, pick_folder, pill, state_style

_PLUGIN_CACHE: dict = {'all': None}

KIND_ICON = {
    'app': 'application-x-executable-symbolic',
    'mic': 'audio-input-microphone-symbolic',
    'everything': 'audio-volume-high-symbolic',
}
STAGE_ICON = {
    'eq': 'audio-x-generic-symbolic',
    'effect': 'applications-multimedia-symbolic',
    'convolver': 'audio-headphones-symbolic',
}
BAND_TYPES = [('PK', 'Peak'), ('LSC', 'Low shelf'), ('HSC', 'High shelf')]


def _adj(lo, hi, val, step, page):
    return Gtk.Adjustment(lower=lo, upper=hi, value=val,
                          step_increment=step, page_increment=page)


def _all_plugins():
    if _PLUGIN_CACHE['all'] is None:
        _PLUGIN_CACHE['all'] = plugins.scan_ladspa() + plugins.scan_lv2()
    return _PLUGIN_CACHE['all']


# --------------------------------------------------------------- pickers --

def search_picker(parent, title, subtitle, items, on_pick, empty=''):
    """A searchable list dialog.

    `items` is a list of (key, title, subtitle) tuples.  Every long list on
    this page goes through here — a machine with twenty sinks and three
    hundred LV2 plugins makes a ComboRow useless, and the search entry is the
    difference between the page scaling and not.
    """
    dlg = Adw.Dialog(title=title, content_width=520, content_height=560)
    search = Gtk.SearchEntry(placeholder_text='Search…', margin_bottom=6)
    listbox = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE,
                          css_classes=['boxed-list'])
    rows = []
    for key, t, sub in items:
        row = Adw.ActionRow(title=esc(t), subtitle=esc(sub),
                            activatable=True, title_lines=1, subtitle_lines=1)
        row.add_suffix(Gtk.Image.new_from_icon_name('go-next-symbolic'))
        row._key = key
        row._hay = f'{t} {sub}'.lower()
        row.connect('activated', lambda r: (dlg.close(), on_pick(r._key)))
        listbox.append(row)
        rows.append(row)

    placeholder = Adw.StatusPage(
        title=empty or 'Nothing to show',
        icon_name='edit-find-symbolic', vexpand=True)
    placeholder.set_visible(not rows)

    def filter_rows(*_a):
        needle = search.get_text().strip().lower()
        for r in rows:
            r.set_visible(needle in r._hay)
    search.connect('search-changed', filter_rows)

    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6,
                  margin_top=12, margin_bottom=12,
                  margin_start=12, margin_end=12)
    if subtitle:
        lbl = Gtk.Label(label=esc(subtitle), xalign=0, wrap=True,
                        css_classes=['dim-label'], margin_bottom=6)
        box.append(lbl)
    box.append(search)
    if rows:
        box.append(listbox)
    else:
        box.append(placeholder)
    sw = Gtk.ScrolledWindow(vexpand=True,
                            hscrollbar_policy=Gtk.PolicyType.NEVER)
    sw.set_child(box)
    view = Adw.ToolbarView()
    view.add_top_bar(Adw.HeaderBar())
    view.set_content(sw)
    dlg.set_child(view)
    dlg.present(parent)
    search.grab_focus()
    return dlg


def prompt_text(parent, heading, body, initial, on_accept, action='Save'):
    dlg = Adw.AlertDialog(heading=heading, body=body)
    entry = Gtk.Entry(text=initial, activates_default=True)
    dlg.set_extra_child(entry)
    dlg.add_response('cancel', 'Cancel')
    dlg.add_response('ok', action)
    dlg.set_response_appearance('ok', Adw.ResponseAppearance.SUGGESTED)
    dlg.set_default_response('ok')
    dlg.connect('response', lambda _d, r: r == 'ok'
                and on_accept(entry.get_text().strip()))
    dlg.present(parent)
    entry.grab_focus()


# ----------------------------------------------------------- stage editor --

class StageDialog(Adw.Window):
    """Edit one stage.  The middle section changes with the stage kind; the
    placement controls (bypass, order, remove) are the same for all of them."""

    def __init__(self, window, page, strip, stage, on_done):
        super().__init__(title=f"{stage['name']}", transient_for=window,
                         modal=True, resizable=True,
                         default_width=560, default_height=680)
        self.window, self.page = window, page
        self.strip, self.stage, self.on_done = strip, stage, on_done
        self._band_rows: list = []
        # Closing the window and finishing through a button both end up here,
        # so the outcome is reported exactly once: without the flag, `close()`
        # inside _save would fire close-request and report a cancel, throwing
        # away the stage that had just been saved.
        self._handled = False
        self.connect('close-request', self._on_close)

        g = group('Stage')
        self.name_row = Adw.EntryRow(title='Name')
        self.name_row.set_text(stage.get('name', ''))
        g.add(self.name_row)

        self.bypass_row = Adw.SwitchRow(
            title='Bypass this stage',
            subtitle='Leaves it in the chain but takes it out of the signal. '
                     'The chain is rebuilt, so audio stops for a moment.')
        self.bypass_row.set_active(bool(stage.get('bypass')))
        g.add(self.bypass_row)

        idx = strip.stages.index(stage)
        move = Adw.ActionRow(title='Position in the chain',
                             subtitle=f'Stage {idx + 1} of {len(strip.stages)}')
        up = icon_button('go-up-symbolic', 'Move earlier',
                         lambda *_: self._move(-1))
        down = icon_button('go-down-symbolic', 'Move later',
                           lambda *_: self._move(1))
        up.set_sensitive(idx > 0)
        down.set_sensitive(idx < len(strip.stages) - 1)
        move.add_suffix(up)
        move.add_suffix(down)
        g.add(move)

        body = self._kind_group()

        save = Gtk.Button(label='Save', halign=Gtk.Align.END, margin_top=12,
                          css_classes=['suggested-action'])
        save.connect('clicked', self._save)
        remove = Gtk.Button(label='Remove from chain', halign=Gtk.Align.START,
                            css_classes=['destructive-action'])
        remove.connect('clicked', self._remove)
        actions = Gtk.Box(spacing=8, margin_top=12)
        actions.append(remove)
        actions.append(Gtk.Box(hexpand=True))
        actions.append(save)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18,
                      margin_top=12, margin_bottom=24,
                      margin_start=18, margin_end=18)
        box.append(g)
        if body:
            box.append(body)
        box.append(actions)
        sw = Gtk.ScrolledWindow(vexpand=True,
                                hscrollbar_policy=Gtk.PolicyType.NEVER)
        clamp = Adw.Clamp(maximum_size=560)
        clamp.set_child(box)
        sw.set_child(clamp)
        view = Adw.ToolbarView()
        view.add_top_bar(Adw.HeaderBar())
        view.set_content(sw)
        self.set_content(view)

    # -- per-kind body ----------------------------------------------------
    def _kind_group(self):
        kind = self.stage.get('kind')
        p = self.stage.setdefault('params', {})
        if kind == 'eq':
            g = group('Bands', 'Each band is one filter. Frequency, gain and '
                               'Q can be changed while audio is playing.')
            add = icon_button('list-add-symbolic', 'Add band',
                              lambda *_: self._add_band())
            g.set_header_suffix(add)
            self.preamp = Adw.SpinRow(
                title='Preamp', subtitle='Overall trim in dB — go negative to '
                'leave headroom for boosted bands.',
                adjustment=_adj(-24, 24, float(p.get('preamp') or 0.0), 0.5, 3),
                digits=1)
            g.add(self.preamp)
            self.bands_group = g
            for b in (p.get('bands') or []):
                self._add_band(b)
            return g
        if kind == 'effect':
            g = group('Plugin')
            self.plug_row = Adw.ActionRow(title='Plugin')
            btn = Gtk.Button(label='Choose…', valign=Gtk.Align.CENTER)
            btn.connect('clicked', lambda *_: self._pick_plugin())
            self.plug_row.add_suffix(btn)
            self.plug_row.set_activatable_widget(btn)
            g.add(self.plug_row)
            self._refresh_plugin_row()
            return g
        if kind == 'convolver':
            g = group('Impulse response',
                      'A WAV or SOFA file. Convolution is the most expensive '
                      'stage kind — expect it to add latency.')
            self.ir_row = Adw.ActionRow(title='File')
            btn = Gtk.Button(label='Browse…', valign=Gtk.Align.CENTER)
            btn.connect('clicked', lambda *_: self._pick_ir())
            self.ir_row.add_suffix(btn)
            self.ir_row.set_activatable_widget(btn)
            g.add(self.ir_row)
            self.gain_row = Adw.SpinRow(
                title='Gain', adjustment=_adj(0, 4, float(p.get('gain') or 1.0),
                                              0.05, 0.5), digits=2)
            g.add(self.gain_row)
            self._refresh_ir_row()
            return g
        return None

    # -- eq bands ---------------------------------------------------------
    def _add_band(self, band=None):
        band = dict(band or {'on': True, 'type': 'PK', 'freq': 1000.0,
                             'gain': 0.0, 'q': 1.0})
        row = Adw.ExpanderRow(show_enable_switch=True)
        row.set_enable_expansion(bool(band.get('on', True)))
        type_row = Adw.ComboRow(
            title='Type',
            model=Gtk.StringList.new([t[1] for t in BAND_TYPES]))
        type_row.set_selected(next(
            (i for i, t in enumerate(BAND_TYPES)
             if t[0] == str(band.get('type', 'PK')).upper()), 0))
        freq = Adw.SpinRow(title='Frequency (Hz)',
                           adjustment=_adj(20, 20000, float(band['freq']),
                                           10, 100), digits=0)
        gain = Adw.SpinRow(title='Gain (dB)',
                           adjustment=_adj(-30, 30, float(band['gain']),
                                           0.5, 3), digits=1)
        q = Adw.SpinRow(title='Q', adjustment=_adj(0.1, 10, float(band['q']),
                                                   0.1, 1), digits=2)
        for r in (type_row, freq, gain, q):
            row.add_row(r)
        rm = icon_button('user-trash-symbolic', 'Remove band',
                         lambda *_: self._drop_band(row))
        row.add_suffix(rm)

        def retitle(*_a):
            t = BAND_TYPES[type_row.get_selected()][1]
            row.set_title(f'{t} · {int(freq.get_value())} Hz')
            row.set_subtitle(f'{gain.get_value():+.1f} dB · Q '
                             f'{q.get_value():.2f}')
        for r in (freq, gain, q):
            r.connect('notify::value', retitle)
        type_row.connect('notify::selected', retitle)
        retitle()
        self.bands_group.add(row)
        self._band_rows.append((row, type_row, freq, gain, q))

    def _drop_band(self, row):
        self._band_rows = [b for b in self._band_rows if b[0] is not row]
        self.bands_group.remove(row)

    def _collect_bands(self):
        out = []
        for row, tr, f, g, q in self._band_rows:
            out.append({'on': row.get_enable_expansion(),
                        'type': BAND_TYPES[tr.get_selected()][0],
                        'freq': round(f.get_value(), 2),
                        'gain': round(g.get_value(), 2),
                        'q': round(q.get_value(), 3)})
        return out

    # -- effect / convolver -----------------------------------------------
    def _refresh_plugin_row(self):
        p = self.stage['params']
        if p.get('plugin'):
            ports = f"{len(p.get('audio_in') or [])} in / " \
                    f"{len(p.get('audio_out') or [])} out"
            self.plug_row.set_subtitle(
                esc(f"{p.get('label') or p['plugin']} — {ports}"))
        else:
            self.plug_row.set_subtitle('No plugin chosen yet')

    def _pick_plugin(self):
        def loaded(all_p, error):
            if error or not all_p:
                self.window.toast('No LADSPA or LV2 plugins found')
                return
            items = []
            for pl in all_p:
                d = pl if isinstance(pl, dict) else vars(pl)
                ports = (f"{len(d.get('audio_in') or [])} in / "
                         f"{len(d.get('audio_out') or [])} out")
                items.append((d, d.get('name') or d.get('label') or '?',
                              f"{d.get('type', '')} · {ports}"))

            def picked(d):
                self.stage['params'].update({
                    'type': d.get('type'), 'plugin': d.get('plugin'),
                    'label': d.get('label'),
                    'audio_in': list(d.get('audio_in') or []),
                    'audio_out': list(d.get('audio_out') or [])})
                if not self.name_row.get_text().strip() or \
                        self.name_row.get_text().strip() == 'effect':
                    self.name_row.set_text(d.get('name') or d.get('label') or '')
                self._refresh_plugin_row()
            search_picker(self, 'Choose a plugin',
                          'Stereo plugins run once per channel pair, mono '
                          'plugins once per channel.', items, picked,
                          empty='No LADSPA or LV2 plugins found')
        async_call(_all_plugins, loaded)

    def _refresh_ir_row(self):
        f = self.stage['params'].get('filename')
        self.ir_row.set_subtitle(esc(f) if f else 'No file chosen yet')

    def _pick_ir(self):
        def got(path):
            self.stage['params']['filename'] = path
            self._refresh_ir_row()
        pick_file(self, 'Choose an impulse response', got)

    # -- actions ----------------------------------------------------------
    def _finish(self, saved):
        self._handled = True
        self.on_done(saved)
        self.close()

    def _on_close(self, *_a):
        if not self._handled:
            self._handled = True
            self.on_done(False)
        return False

    def _move(self, delta):
        i = self.strip.stages.index(self.stage)
        j = max(0, min(len(self.strip.stages) - 1, i + delta))
        if i != j:
            self.strip.stages.insert(j, self.strip.stages.pop(i))
            self._finish(True)

    def _remove(self, _b):
        self.strip.stages = [s for s in self.strip.stages
                             if s.get('id') != self.stage.get('id')]
        self._finish(True)

    def _save(self, _b):
        self.stage['name'] = self.name_row.get_text().strip() or \
            self.stage.get('kind', 'stage')
        self.stage['bypass'] = self.bypass_row.get_active()
        kind = self.stage.get('kind')
        p = self.stage.setdefault('params', {})
        if kind == 'eq':
            p['preamp'] = round(self.preamp.get_value(), 2)
            p['bands'] = self._collect_bands()
        elif kind == 'convolver':
            p['gain'] = round(self.gain_row.get_value(), 3)
            if not p.get('filename'):
                self.window.toast('Choose an impulse response first')
                return
        elif kind == 'effect':
            if not p.get('plugin'):
                self.window.toast('Choose a plugin first')
                return
        self._finish(True)


# ------------------------------------------------------------------ page --

class PathsPage:
    def __init__(self, window):
        self.window = window
        self.volume_style = prefs.get('volume_style') or 'meter'
        self._strips: list = []
        self._nodes: list = []
        self._streams: list = []
        self._vols: dict = {}

        self.quick = group(
            'Quick setup',
            'Each of these builds a complete path in one step. You can take '
            'it apart afterwards — they are ordinary sources and mixes.')
        for title, sub, fn in (
            ('Equalize everything',
             'One equalizer between every app and your current output.',
             self._quick_eq_all),
            ('Put effects on one app',
             'Send a single app through a plugin chain, leaving the rest '
             'of your audio alone.', self._quick_app_fx),
            ('Speakers and a separate stream mix',
             'One chain into your speakers, a second into a virtual output '
             'that OBS or Discord can capture.', self._quick_stream),
        ):
            row = Adw.ActionRow(title=title, subtitle=sub, activatable=True)
            row.add_prefix(Gtk.Image.new_from_icon_name(
                'starred-symbolic'))
            row.add_suffix(Gtk.Image.new_from_icon_name('go-next-symbolic'))
            row.connect('activated', lambda _r, f=fn: f())
            self.quick.add(row)

        self.sources_group = group(
            'Sources',
            'Where audio comes in, and what happens to it before it is sent '
            'on. A source with no sends follows the default output.')
        self.sources_group.set_header_suffix(self._add_button(
            'Add a source', lambda: self._new_strip('source')))

        self.mixes_group = group(
            'Mixes',
            'A chain of its own, feeding real devices. Pick several devices '
            'and they are combined into one output automatically.')
        self.mixes_group.set_header_suffix(self._add_button(
            'Add a mix', lambda: self._new_strip('mix')))

        self.tools_group = group('Sharing')
        imp = Adw.ActionRow(
            title='Import a path',
            subtitle='Load sources and mixes someone exported. They arrive '
                     'switched off so you can look before turning them on.',
            activatable=True)
        imp.add_suffix(Gtk.Image.new_from_icon_name(
            'document-open-symbolic'))
        imp.connect('activated', lambda *_: self._import())
        self.tools_group.add(imp)

        self._rows: list = []
        self.widget = page_scroller(self.quick, self.sources_group,
                                    self.mixes_group, self.tools_group,
                                    width=860)
        self.widget.connect('map', lambda *_: self.refresh())

    def _add_button(self, tooltip, fn):
        b = Gtk.Button(icon_name='list-add-symbolic',
                       valign=Gtk.Align.CENTER, tooltip_text=tooltip)
        b.connect('clicked', lambda *_: fn())
        return b

    # ------------------------------------------------------------ refresh --
    def refresh(self):
        def collect():
            dump = pw.pw_dump()
            nodes = pw.list_audio_nodes(dump)
            streams = [s for s in pw.list_streams(dump) if s.is_playback
                       and not s.props.get('node.name', '').startswith('pwctl.')]
            strips = paths.list_strips()
            states = {s.id: paths.status(s) for s in strips}
            return strips, states, nodes, streams
        async_call(collect, self._apply)

    def _apply(self, payload, error):
        if error or payload is None:
            return
        self._strips, states, self._nodes, self._streams = payload
        self._states = states
        for row in self._rows:
            (self.sources_group if row._role == 'source'
             else self.mixes_group).remove(row)
        self._rows = []
        self._vols = {}

        srcs = paths.sources(self._strips)
        mixes = paths.mixes(self._strips)
        self.quick.set_visible(not self._strips)

        for s in srcs:
            r = self._strip_row(s, mixes)
            self.sources_group.add(r)
            self._rows.append(r)
        if not srcs:
            self.sources_group.add(self._empty_row(
                'No sources yet',
                'Add one, or use Quick setup above.', 'source'))
        for m in mixes:
            r = self._strip_row(m, mixes)
            self.mixes_group.add(r)
            self._rows.append(r)
        if not mixes:
            self.mixes_group.add(self._empty_row(
                'No mixes yet',
                'A source needs somewhere to send its audio.', 'mix'))

    def _empty_row(self, title, sub, role):
        row = Adw.ActionRow(title=title, subtitle=sub, activatable=True)
        row.add_prefix(Gtk.Image.new_from_icon_name('list-add-symbolic'))
        row.connect('activated', lambda *_: self._new_strip(role))
        row._role = role
        self._rows.append(row)
        return row

    # --------------------------------------------------------- strip rows --
    def _node_for(self, strip):
        return next((n for n in self._nodes if n.name == strip.node_name), None)

    def _flow_text(self, strip, mixes):
        stages = strip.active_stages()
        chain = ' › '.join(s.get('name', '?') for s in stages) or 'no processing'
        if strip.role == 'source':
            by_id = {m.id: m for m in mixes}
            dest = ', '.join(by_id[m].name for m in strip.sends if m in by_id)
            dest = dest or 'follows the default output'
        else:
            dest = ', '.join(self._device_label(o) for o in strip.outputs) \
                or 'follows the default output'
        return f'{chain}  →  {dest}'

    def _device_label(self, node_name):
        n = next((x for x in self._nodes if x.name == node_name), None)
        return n.description if n else node_name

    def _strip_row(self, strip, mixes):
        state = self._states.get(strip.id, 'inactive')
        running = strip.enabled and state == 'active'
        row = Adw.ExpanderRow(
            title=esc(strip.name),
            subtitle=esc(self._flow_text(strip, mixes)),
            title_lines=1, subtitle_lines=2)
        row._role = strip.role
        icon = KIND_ICON.get(strip.kind, 'audio-card-symbolic') \
            if strip.role == 'source' else 'audio-speakers-symbolic'
        row.add_prefix(Gtk.Image.new_from_icon_name(icon))
        if running:
            row.add_css_class('enh-active')
            # A running strip shows its chain without being asked, the way a
            # running equalizer does — the chain is the point of the page.
            row.set_expanded(True)

        sw = Gtk.Switch(active=strip.enabled, valign=Gtk.Align.CENTER)
        sw.connect('state-set', self._toggle, strip)
        # ExpanderRow packs suffixes right-to-left, so add in reverse to get
        # the same visual order as a plain row.
        row.add_suffix(sw)
        row.add_suffix(pill(str(len(strip.positions)) + 'ch',
                            'dim-label'))
        if strip.enabled and state != 'active':
            row.add_suffix(pill(state, state_style(state)))

        kids = [self._chain_row(strip)]
        if strip.role == 'source':
            kids.append(self._sends_row(strip, mixes))
            kids.append(self._apps_row(strip))
        else:
            kids.append(self._outputs_row(strip))
        if running:
            vr = self._volume_row(strip)
            if vr:
                kids.append(vr)
        kids.append(self._actions_row(strip))
        for k in kids:
            # Each nested row paints its own background, so the accent edge
            # has to be repeated on every child or it survives only in the
            # gaps between them.
            if running:
                k.add_css_class('enh-inner')
            row.add_row(k)
        return row

    def _chain_row(self, strip):
        """The chain gets a full-width row of its own.

        A twenty-plugin chain has no chance as a row suffix, so this is built
        as a real Gtk.ListBoxRow (a plain widget handed to add_row() would be
        wrapped in one that never gets our styling) with the chips wrapping
        across the whole width.
        """
        row = Gtk.ListBoxRow(activatable=False, selectable=False)
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8,
                        margin_top=12, margin_bottom=12,
                        margin_start=12, margin_end=12)
        head = Gtk.Box(spacing=8)
        head.append(Gtk.Label(label='Signal chain', xalign=0,
                              css_classes=['heading'], hexpand=True))
        add = Gtk.MenuButton(icon_name='list-add-symbolic',
                             valign=Gtk.Align.CENTER,
                             tooltip_text='Add a stage',
                             css_classes=['flat'])
        add.set_popover(self._stage_popover(strip))
        head.append(add)
        outer.append(head)

        flow = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE,
                           max_children_per_line=8, column_spacing=6,
                           row_spacing=6, homogeneous=False,
                           halign=Gtk.Align.START)
        for i, st in enumerate(strip.stages):
            if i:
                flow.append(Gtk.Label(label='›', css_classes=['dim-label'],
                                      valign=Gtk.Align.CENTER))
            chip = Gtk.Button(valign=Gtk.Align.CENTER,
                              css_classes=['pill', 'path-stage'],
                              tooltip_text='Bypassed' if st.get('bypass')
                              else 'Edit this stage')
            content = Gtk.Box(spacing=6)
            content.append(Gtk.Image.new_from_icon_name(
                STAGE_ICON.get(st.get('kind'), 'preferences-other-symbolic')))
            content.append(Gtk.Label(label=esc(st.get('name', '?'))))
            chip.set_child(content)
            if st.get('bypass'):
                chip.add_css_class('path-stage-off')
            chip.connect('clicked', lambda _b, s=st: self._edit_stage(strip, s))
            flow.append(chip)
        if not strip.stages:
            flow.append(Gtk.Label(
                label='No processing — audio passes straight through',
                css_classes=['dim-label'], xalign=0))
        outer.append(flow)
        row.set_child(outer)
        return row

    def _stage_popover(self, strip):
        pop = Gtk.Popover()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2,
                      margin_top=6, margin_bottom=6,
                      margin_start=6, margin_end=6)
        for kind, label, sub in (
            ('eq', 'Equalizer', 'Bands you can move while it plays'),
            ('effect', 'Plugin', 'Any LADSPA or LV2 on this system'),
            ('convolver', 'Convolver', 'An impulse response file'),
        ):
            b = Gtk.Button(css_classes=['flat'])
            inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
            inner.append(Gtk.Label(label=label, xalign=0))
            inner.append(Gtk.Label(label=sub, xalign=0,
                                   css_classes=['dim-label', 'caption']))
            b.set_child(inner)
            b.connect('clicked', lambda _b, k=kind: (pop.popdown(),
                                                     self._add_stage(strip, k)))
            box.append(b)
        pop.set_child(box)
        return pop

    def _sends_row(self, strip, mixes):
        row = Adw.ActionRow(
            title='Sends to',
            subtitle='Pick more than one and they are combined automatically.')
        box = Gtk.Box(spacing=6, valign=Gtk.Align.CENTER)
        for m in mixes:
            tog = Gtk.ToggleButton(label=esc(m.name), active=m.id in strip.sends,
                                   valign=Gtk.Align.CENTER,
                                   css_classes=['pill'])
            tog.connect('toggled', self._toggle_send, strip, m.id)
            box.append(tog)
        if not mixes:
            box.append(Gtk.Label(label='No mixes yet',
                                 css_classes=['dim-label']))
        row.add_suffix(box)
        return row

    def _outputs_row(self, strip):
        row = Adw.ActionRow(
            title='Out to',
            subtitle='Several devices are combined into one output.')
        box = Gtk.Box(spacing=6, valign=Gtk.Align.CENTER)
        for o in strip.outputs:
            chip = Gtk.Button(label=esc(self._device_label(o)),
                              valign=Gtk.Align.CENTER,
                              css_classes=['pill'],
                              tooltip_text='Remove this output')
            chip.connect('clicked', self._drop_output, strip, o)
            box.append(chip)
        add = icon_button('list-add-symbolic', 'Add an output',
                          lambda *_: self._add_output(strip))
        box.append(add)
        row.add_suffix(box)
        return row

    def _apps_row(self, strip):
        here = [s for s in self._streams
                if s.target_id and self._node_for(strip)
                and s.target_id == self._node_for(strip).id]
        row = Adw.ActionRow(
            title='Apps playing here',
            subtitle=esc(', '.join(s.name for s in here)) if here
            else 'Nothing yet — send an app here to hear the chain.')
        btn = Gtk.Button(label='Send an app here', valign=Gtk.Align.CENTER)
        btn.connect('clicked', lambda *_: self._send_app(strip))
        row.add_suffix(btn)
        return row

    def _volume_row(self, strip):
        node = self._node_for(strip)
        if node is None:
            return None
        row = Adw.ActionRow(title='Level', title_lines=1)
        ctl = make_volume(self.volume_style,
                          lambda v, n=node: pw.set_volume(n.id, v),
                          compact=True)
        ctl.set_value(node.volume if node.volume is not None else 1.0)
        if node.serial and node.serial > 0 and not levels.at_capacity():
            ctl.set_meter(node.serial)
        self._vols[strip.id] = ctl
        mute = Gtk.ToggleButton(icon_name='audio-volume-muted-symbolic',
                                active=node.muted, valign=Gtk.Align.CENTER,
                                tooltip_text='Mute')
        mute.connect('toggled', lambda b, n=node: pw.set_mute(n.id,
                                                              b.get_active()))
        row.add_suffix(ctl.widget)
        row.add_suffix(mute)
        return row

    def _actions_row(self, strip):
        row = Adw.ActionRow(title='This strip')
        for icon, tip, fn in (
            ('document-edit-symbolic', 'Rename', self._rename),
            ('document-save-symbolic', 'Export', self._export),
            ('user-trash-symbolic', 'Delete', self._delete),
        ):
            row.add_suffix(icon_button(icon, tip,
                                       lambda *_, f=fn, s=strip: f(s)))
        return row

    # ------------------------------------------------------------ actions --
    def _save_and_apply(self, strip, message=None):
        def done(result, e):
            ok, err = result if result else (False, str(e or ''))
            if message and ok:
                self.window.toast(message)
            elif not ok:
                self.window.toast(f'Failed: {err or "unknown error"}')
            self.refresh()
        paths.save_meta(strip)
        async_call(lambda: paths.apply(strip, self._strips), done)

    def _toggle(self, sw, state, strip):
        if state == strip.enabled:
            return False
        strip.enabled = state
        self._save_and_apply(strip)
        return False

    def _toggle_send(self, btn, strip, mix_id):
        want = btn.get_active()
        if want == (mix_id in strip.sends):
            return
        strip.sends = ([*strip.sends, mix_id] if want
                       else [m for m in strip.sends if m != mix_id])
        self._save_and_apply(strip)

    def _drop_output(self, _b, strip, node_name):
        strip.outputs = [o for o in strip.outputs if o != node_name]
        self._save_and_apply(strip)

    def _add_output(self, strip):
        edges = paths.target_edges(self._strips)
        allowed = paths.output_targets(
            strip, [n for n in self._nodes if n.is_sink], edges)
        items = [(n.name, n.description, n.name) for n in allowed
                 if n.name not in strip.outputs]
        items.append(('__virtual__', 'Create a virtual output…',
                      'A capture sink other apps (OBS, Discord) can record'))

        def picked(key):
            if key == '__virtual__':
                self._new_capture_sink(strip)
                return
            strip.outputs = [*strip.outputs, key]
            self._save_and_apply(strip)
        search_picker(self.window, 'Add an output',
                      'Where this mix sends its audio.', items, picked,
                      empty='No output devices found')

    def _new_capture_sink(self, strip):
        def make(name):
            if not name:
                return
            dev = virtual.new_device(name, 'null-sink',
                                     positions=list(strip.positions))
            dev.enabled = True

            def done(result, e):
                ok, err = result if result else (False, str(e or ''))
                if not ok:
                    self.window.toast(f'Failed: {err}')
                    return
                strip.outputs = [*strip.outputs, dev.node_name]
                self._save_and_apply(
                    strip, f'“{name}” created — pick it as the source in '
                    'your recording app')
            async_call(lambda: virtual.apply(dev), done)
        prompt_text(self.window, 'Create a virtual output',
                    'It appears as a recording source in other apps, so a '
                    'stream mix can be captured without going through your '
                    'speakers.', 'Stream Mix', make, action='Create')

    def _send_app(self, strip):
        node = self._node_for(strip)
        if node is None:
            self.window.toast('Turn this source on first')
            return
        items = [(s.id, s.name, s.media or 'playing')
                 for s in self._streams if s.target_id != node.id]
        if not items:
            self.window.toast('Nothing else is playing right now')
            return

        def picked(stream_id):
            def done(ok, e):
                self.window.toast('Moved' if ok and not e else 'Could not move it')
                self.refresh()
            async_call(lambda: pw.move_stream(stream_id, node.serial), done)
        search_picker(self.window, 'Send an app here',
                      'The app keeps playing; it is just relinked.',
                      items, picked)

    def _add_stage(self, strip, kind):
        stage = paths.new_stage(kind, {'eq': 'Equalizer', 'effect': 'Plugin',
                                       'convolver': 'Convolver'}[kind])
        if kind == 'eq':
            stage['params'] = {'preamp': 0.0, 'bands': [
                {'on': True, 'type': 'PK', 'freq': f, 'gain': 0.0, 'q': 1.0}
                for f in (60, 250, 1000, 4000, 12000)]}
        strip.stages = [*strip.stages, stage]
        self._edit_stage(strip, stage, is_new=True)

    def _edit_stage(self, strip, stage, is_new=False):
        def done(saved):
            if not saved and is_new:
                # Cancelling out of a stage that was created for this dialog
                # leaves nothing behind, so a half-configured plugin never
                # ends up in the chain.
                strip.stages = [s for s in strip.stages
                                if s.get('id') != stage.get('id')]
                if not strip.enabled:
                    paths.save_meta(strip)
                    self.refresh()
                    return
            self._save_and_apply(strip)
        StageDialog(self.window, self, strip, stage, done).present()

    def _new_strip(self, role):
        def make(name):
            if not name:
                return
            kw = {}
            if role == 'source':
                kw['kind'] = 'app'
            strip = paths.new_strip(name, role, **kw)
            strip.enabled = False
            paths.save_meta(strip)
            self.refresh()
            self.window.toast(f'“{name}” added — give it stages, then turn '
                              'it on')
        prompt_text(self.window,
                    'New source' if role == 'source' else 'New mix',
                    'Sources are where audio comes in; mixes feed your '
                    'devices.',
                    'Music' if role == 'source' else 'Speakers',
                    make, action='Add')

    def _rename(self, strip):
        def go(name):
            if name:
                strip.name = name
                self._save_and_apply(strip)
        prompt_text(self.window, 'Rename', 'What this strip is called.',
                    strip.name, go)

    def _delete(self, strip):
        def go():
            def done(_r, _e):
                self.window.toast(f'“{strip.name}” deleted')
                self.refresh()
            async_call(lambda: paths.delete(strip), done)
        confirm(self.window, f'Delete “{strip.name}”?',
                'The chain and anything it created are removed. Apps playing '
                'through it go back to the default output.',
                'Delete', go)

    # ------------------------------------------------------------ sharing --
    def _export(self, strip):
        data = {'format': 'pwctl-signal-path-1',
                'strips': [self._strip_dict(strip)]}
        if strip.role == 'source':
            by_id = {m.id: m for m in paths.mixes(self._strips)}
            data['strips'] += [self._strip_dict(by_id[m])
                               for m in strip.sends if m in by_id]

        def got(folder):
            if not folder:
                return
            p = GLib.build_filenamev([folder, f'{strip.id}.json'])
            try:
                with open(p, 'w') as fh:
                    json.dump(data, fh, indent=2)
                self.window.toast(f'Exported to {p}')
            except OSError as e:
                self.window.toast(f'Could not write it: {e}')
        pick_folder(self.window, 'Choose where to save it', got)

    def _strip_dict(self, strip):
        from dataclasses import asdict
        d = asdict(strip)
        d.pop('enabled', None)
        return d

    def _import(self):
        def got(path):
            if not path:
                return
            try:
                data = json.loads(open(path).read())
            except (OSError, ValueError) as e:
                self.window.toast(f'Could not read it: {e}')
                return
            if data.get('format') != 'pwctl-signal-path-1':
                self.window.toast('That is not an exported signal path')
                return
            remap: dict = {}
            made = []
            for raw in data.get('strips', []):
                known = set(paths.Strip.__dataclass_fields__)
                fields = {k: v for k, v in raw.items() if k in known}
                old = fields.pop('id', '')
                name = fields.pop('name', 'Imported')
                role = fields.pop('role', 'source')
                fields.pop('enabled', None)
                strip = paths.new_strip(name, role, **fields)
                strip.enabled = False
                remap[old] = strip.id
                made.append(strip)
            for strip in made:              # sends refer to the new ids
                strip.sends = [remap.get(s, s) for s in strip.sends]
                paths.save_meta(strip)
            self.window.toast(f'Imported {len(made)} — switched off, so look '
                              'before turning them on')
            self.refresh()
        pick_file(self.window, 'Choose an exported path', got)

    # -------------------------------------------------------- quick setup --
    def _default_sink_name(self):
        n = next((x for x in self._nodes if x.is_sink and x.is_default), None)
        return n.name if n else ''

    def _quick_pair(self, src_name, mix_name, stages, kind='everything'):
        dev = self._default_sink_name()
        mix = paths.new_strip(mix_name, 'mix',
                              outputs=[dev] if dev else [])
        mix.enabled = True
        src = paths.new_strip(src_name, 'source', kind=kind, sends=[mix.id],
                              stages=stages)
        src.enabled = True

        def done(result, e):
            ok, err = result if result else (False, str(e or ''))
            self.window.toast(f'“{src_name}” ready' if ok
                              else f'Failed: {err or "unknown error"}')
            self.refresh()

        def build():
            ok, err = paths.apply(mix, [mix, src])
            if not ok:
                return ok, err
            return paths.apply(src, [mix, src])
        async_call(build, done)

    def _quick_eq_all(self):
        stage = paths.new_stage('eq', 'Equalizer')
        stage['params'] = {'preamp': 0.0, 'bands': [
            {'on': True, 'type': 'PK', 'freq': f, 'gain': 0.0, 'q': 1.0}
            for f in (60, 250, 1000, 4000, 12000)]}
        self._quick_pair('Everything', 'Speakers', [stage],
                         kind='everything')

    def _quick_app_fx(self):
        self._quick_pair('App', 'Speakers', [], kind='app')

    def _quick_stream(self):
        dev = self._default_sink_name()
        speakers = paths.new_strip('Speakers', 'mix',
                                   outputs=[dev] if dev else [])
        speakers.enabled = True
        cap = virtual.new_device('Stream Mix', 'null-sink')
        cap.enabled = True
        stream = paths.new_strip('Stream', 'mix', outputs=[cap.node_name])
        stream.enabled = True
        src = paths.new_strip('Everything', 'source', kind='everything',
                              sends=[speakers.id, stream.id])
        src.enabled = True
        allst = [speakers, stream, src]

        def build():
            ok, err = virtual.apply(cap)
            if not ok:
                return ok, err
            for st in allst:
                ok, err = paths.apply(st, allst)
                if not ok:
                    return ok, err
            return True, ''

        def done(result, e):
            ok, err = result if result else (False, str(e or ''))
            self.window.toast(
                'Ready — pick “Stream Mix” as the source in OBS or Discord'
                if ok else f'Failed: {err or "unknown error"}')
            self.refresh()
        async_call(build, done)
