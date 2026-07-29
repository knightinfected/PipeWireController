"""Enhancements page: parametric equalizer sinks and microphone cleanup.

Two managed sections sharing one page.  Each equalizer / cleanup device runs
as its own PipeWire process (see backend/enhance.py), so nothing here touches
the main graph or needs a service restart — devices just start and stop.
"""

from __future__ import annotations

import gi

gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Adw, Gdk, GLib, Gtk  # noqa: E402

from ..backend import enhance, prefs, pw
from .volume import make_volume
from .widgets import async_call, confirm, esc, group, icon_button, page_scroller, \
    pick_file, pill, state_style


def _adj(lower, upper, value, step, page):
    return Gtk.Adjustment(lower=lower, upper=upper, value=value,
                          step_increment=step, page_increment=page)


class EnhancePage:
    def __init__(self, window):
        self.window = window
        self.volume_style = prefs.get('volume_style') or 'meter'
        self._nodes = []
        self._streams = []
        self._sinks = []

        eq_head = group(
            'Equalizer',
            'A parametric-equalizer output. Audio is only equalized when it '
            'plays through it, so pick an equalizer below and use “Set as '
            'default output” to route everything through it (it then feeds the '
            'device you chose). Import an AutoEQ / APO file, or dial the bands '
            'in by hand.')
        eq_head.add(self._create_row(
            'Create an equalizer',
            'A new equalizer sink with a flat starting curve.',
            lambda: self._open_eq(None)))
        imp_row = Adw.ActionRow(
            title='Import an AutoEQ / APO file',
            subtitle='Load a ParametricEQ.txt curve (from AutoEq or '
                     'Squiglink) — click to browse, or drag the file here.')
        imp_btn = Gtk.Button(icon_name='document-open-symbolic',
                             valign=Gtk.Align.CENTER)
        imp_btn.connect('clicked', lambda *_: self._import_eq())
        imp_row.add_suffix(imp_btn)
        imp_row.set_activatable_widget(imp_btn)
        # Drag-and-drop a file straight onto the row. This uses GTK's own DnD,
        # so it works even where the desktop's file-chooser portal is broken.
        drop = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY)
        drop.connect('drop', self._on_drop)
        imp_row.add_controller(drop)
        eq_head.add(imp_row)

        # Experimental: live A/B.  Off by default — with it off the page
        # behaves exactly as before.
        self.ab_enabled = bool(prefs.get('eq_ab_compare'))
        self._bypass = {}               # enh.id -> state dict from enhance.bypass
        beta = group('Experimental',
                     'Features still being tried out. They can be turned off '
                     'again at any time and change nothing when off.')
        ab_row = Adw.SwitchRow(
            title='Live A/B compare',
            subtitle='Switch an equalizer in and out while it keeps running, '
                     'so playback never pauses. Bypassed audio goes straight '
                     'to the output device.')
        ab_row.add_prefix(Gtk.Image.new_from_icon_name(
            'applications-science-symbolic'))
        ab_row.set_active(self.ab_enabled)
        ab_row.connect('notify::active', self._toggle_ab)
        beta.set_header_suffix(pill('beta', 'warning'))
        beta.add(ab_row)
        self.beta_group = beta

        self.eq_listing = group('Configured equalizers')

        mic_head = group(
            'Microphone cleanup',
            'A clean copy of your microphone with echo and background noise '
            'removed (WebRTC), for calls, meetings and streaming. Pick the '
            'clean microphone as your input in the app that needs it.')
        mic_head.add(self._create_row(
            'Create a clean microphone',
            'Echo + noise cancellation on top of a real microphone.',
            lambda: self._open_mic(None)))
        self.mic_listing = group('Configured microphones')

        self.widget = page_scroller(eq_head, self.beta_group, self.eq_listing,
                                    mic_head, self.mic_listing)
        self._eq_rows = []
        self._mic_rows = []
        self.widget.connect('map', lambda *_: self.refresh())

    def _create_row(self, title, subtitle, on_click):
        row = Adw.ActionRow(title=title, subtitle=subtitle)
        btn = Gtk.Button(icon_name='list-add-symbolic', valign=Gtk.Align.CENTER)
        btn.add_css_class('suggested-action')
        btn.connect('clicked', lambda *_: on_click())
        row.add_suffix(btn)
        row.set_activatable_widget(btn)
        return row

    # ---------------------------------------------------------------- list --
    def refresh(self):
        def collect():
            items = [(e, enhance.status(e))
                     for e in enhance.list_enhancements()]
            # extra context so an active EQ can show an inline mini-mixer
            dump = pw.pw_dump()
            nodes = pw.list_audio_nodes(dump)
            # real app streams only — never our own EQ/mic helper streams
            streams = [s for s in pw.list_streams(dump) if s.is_playback
                       and not s.props.get('node.name', '').startswith('pwctl.')]
            return items, nodes, streams
        async_call(collect, self._apply)

    def _apply(self, payload, error):
        if error or payload is None:
            return
        items, nodes, streams = payload
        self._nodes = nodes
        self._streams = streams
        self._sinks = [n for n in nodes if n.is_sink
                       and not n.name.startswith('pwctl.eq.')]
        for row in self._eq_rows:
            self.eq_listing.remove(row)
        for row in self._mic_rows:
            self.mic_listing.remove(row)
        self._eq_rows, self._mic_rows = [], []
        eqs = [(e, s) for e, s in items if e.kind == 'eq']
        mics = [(e, s) for e, s in items if e.kind == 'mic']
        self._fill(self.eq_listing, self._eq_rows, eqs,
                   'No equalizers yet.', self._open_eq)
        self._fill(self.mic_listing, self._mic_rows, mics,
                   'No clean microphones yet.', self._open_mic)

    def _fill(self, listing, store, items, empty_text, editor):
        if not items:
            row = Adw.ActionRow(title=empty_text,
                                subtitle='Create one above.')
            listing.add(row)
            store.append(row)
            return
        for enh, state in items:
            row = self._device_row(enh, state, editor)
            listing.add(row)
            store.append(row)

    def _device_row(self, enh, state, editor):
        if enh.kind == 'eq':
            n = len([b for b in enh.params.get('bands') or []
                     if b.get('on', True)])
            subtitle = f'{n} band{"s" if n != 1 else ""} active'
            icon = 'audio-x-generic-symbolic'
        else:
            bits = []
            if enh.params.get('noise_suppression', True):
                bits.append('noise')
            bits.append('echo')
            if enh.params.get('gain_control'):
                bits.append('auto-gain')
            subtitle = 'Removing: ' + ', '.join(bits)
            icon = 'audio-input-microphone-symbolic'

        node = next((n for n in self._nodes if n.name == enh.node_name), None)
        # A running equalizer gets an inline mini-mixer (volume, an app router
        # and a live output picker) revealed under its row.
        rich = enh.kind == 'eq' and enh.enabled and node is not None
        row = (Adw.ExpanderRow if rich else Adw.ActionRow)(
            title=esc(enh.name), subtitle=subtitle,
            title_lines=1, subtitle_lines=1)
        row.add_prefix(Gtk.Image.new_from_icon_name(icon))
        sw = Gtk.Switch(valign=Gtk.Align.CENTER, active=enh.enabled,
                        tooltip_text='Running (visible to apps)')
        sw.connect('notify::active', self._toggled, enh)
        # desired left→right order; ExpanderRow packs suffixes right-to-left,
        # so add them reversed there to match the plain rows and the rest of
        # the app (enable switch always on the far right).
        suffixes = []
        if enh.id in self._bypass:
            suffixes.append(pill('bypassed', 'warning'))
        elif enh.enabled:
            suffixes.append(pill(state, state_style(state)))
        if enh.kind == 'eq' and enh.enabled:
            suffixes.append(icon_button(
                'audio-speakers-symbolic',
                'Set as default output — route all audio through this '
                'equalizer', lambda *_: self._set_default(enh)))
        suffixes.append(icon_button('document-edit-symbolic', 'Edit',
                                    lambda *_: editor(enh)))
        suffixes.append(icon_button(
            'user-trash-symbolic', 'Delete',
            lambda *_: confirm(
                self.window, f'Delete “{enh.name}”?',
                'The device is stopped and its configuration removed.',
                'Delete', lambda: self._delete(enh))))
        suffixes.append(sw)
        for w in (reversed(suffixes) if rich else suffixes):
            row.add_suffix(w)
        if rich:
            row.set_expanded(True)
            row.add_row(self._volume_row(enh, node))
            if self.ab_enabled:
                row.add_row(self._ab_row(enh))
            for r in self._routed_rows(enh, node):
                row.add_row(r)
            row.add_row(self._send_row(enh, node))
            row.add_row(self._output_row(enh))
        return row

    # ----------------------------------------------- inline EQ mini-mixer --
    def _volume_row(self, enh, node):
        row = Adw.ActionRow(title='Volume', title_lines=1)
        pct = Gtk.Label(label=f'{round((node.volume or 0) * 100)}%',
                        width_chars=4, xalign=1)
        pct.add_css_class('caption')
        ctl = make_volume(self.volume_style,
                          lambda v: self._on_eq_volume(node.id, v, pct),
                          compact=True)
        ctl.set_value(node.volume if node.volume is not None else 1.0)
        mute = Gtk.ToggleButton(
            icon_name='audio-volume-muted-symbolic', valign=Gtk.Align.CENTER,
            active=node.muted, tooltip_text='Mute this equalizer')
        mute.add_css_class('flat')
        mute.connect('toggled',
                     lambda b: async_call(lambda: pw.set_mute(node.id,
                                                              b.get_active())))
        row.add_suffix(mute)
        row.add_suffix(ctl.widget)
        row.add_suffix(pct)
        return row

    def _on_eq_volume(self, node_id, value, pct):
        pct.set_text(f'{round(value * 100)}%')
        async_call(lambda: pw.set_volume(node_id, value))

    # -- app routing ------------------------------------------------------
    # Two Firefox windows are two streams with the same application.name, so
    # the app name alone can't identify one.  Everything here is labelled
    # "app — what it is playing" (media.name), which is what the user sees in
    # the app itself.

    def _stream_label(self, s):
        # Both halves land in Adw row titles, which are Pango markup — and a
        # media.name is whatever an app decided to call what it is playing.
        detail = s.media or s.binary or ''
        if detail and detail.strip().lower() == s.name.strip().lower():
            detail = ''
        return esc(s.name), esc(detail) or 'Playing'

    def _stream_icon(self, s):
        return Gtk.Image.new_from_icon_name(s.icon or 'multimedia-player-symbolic')

    def _routed_here(self, enh, node):
        """Streams currently playing into this equalizer.

        While bypassed the streams have been moved to the output device on
        purpose, so keep listing the ones we moved — they still belong to this
        equalizer as far as the user is concerned.
        """
        state = self._bypass.get(enh.id)
        if state:
            ids = set(state.get('streams') or [])
            return [s for s in self._streams if s.id in ids]
        return [s for s in self._streams if s.target_id == node.id]

    def _routed_rows(self, enh, node):
        rows = []
        routed = self._routed_here(enh, node)
        bypassed = enh.id in self._bypass
        for s in routed:
            title, detail = self._stream_label(s)
            row = Adw.ActionRow(title=title, subtitle=detail,
                                title_lines=1, subtitle_lines=1)
            row.add_prefix(self._stream_icon(s))
            if bypassed:
                row.add_suffix(pill('bypassed', 'dim'))
            row.add_suffix(icon_button(
                'window-close-symbolic', 'Stop sending this app here',
                lambda *_, st=s: self._unroute(enh, st)))
            rows.append(row)
        if not routed:
            row = Adw.ActionRow(
                title='Nothing is playing through this equalizer',
                subtitle='Send an app here, or make it the default output.',
                title_lines=1, subtitle_lines=1)
            row.add_prefix(Gtk.Image.new_from_icon_name(
                'audio-volume-muted-symbolic'))
            row.set_sensitive(False)
            rows.append(row)
        return rows

    def _send_row(self, enh, node):
        """Picker for apps that aren't already going through this equalizer."""
        routed = {s.id for s in self._routed_here(enh, node)}
        candidates = [s for s in self._streams if s.id not in routed]
        row = Adw.ActionRow(
            title='Send an app here',
            subtitle='Route a running app’s audio into this equalizer.')
        row.add_prefix(Gtk.Image.new_from_icon_name('go-next-symbolic'))
        if not candidates:
            row.set_subtitle('No other app is playing right now.')
            row.set_sensitive(False)
            return row

        btn = Gtk.MenuButton(label='Choose an app…', valign=Gtk.Align.CENTER)
        box = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        box.add_css_class('boxed-list')
        pop = Gtk.Popover(child=box, width_request=320)
        for s in candidates:
            title, detail = self._stream_label(s)
            item = Adw.ActionRow(title=title, subtitle=detail, activatable=True,
                                 title_lines=1, subtitle_lines=1)
            item.add_prefix(self._stream_icon(s))
            item.connect('activated',
                         lambda *_, st=s: (pop.popdown(),
                                           self._route(enh, node, st)))
            box.append(item)
        btn.set_popover(pop)
        row.add_suffix(btn)
        row.set_activatable_widget(btn)
        return row

    def _route(self, enh, node, s):
        def done(ok, e):
            self.window.toast(f'{s.name} → {enh.name}' if ok
                              else 'Could not route that app')
            GLib.timeout_add(500, lambda: (self.refresh(), False)[1])
        async_call(lambda: pw.move_stream(s.id, node.serial), done)

    def _unroute(self, enh, s):
        """Send one app back to the equalizer's output device."""
        def work():
            dest = enhance.eq_output_node(enh)
            if dest is None:
                return None
            return dest.description if pw.move_stream(s.id, dest.serial) else None

        def done(where, e):
            self.window.toast(f'{s.name} → {where}' if where
                              else 'Could not move that app')
            if self._bypass.get(enh.id):
                ids = self._bypass[enh.id].get('streams') or []
                self._bypass[enh.id]['streams'] = [i for i in ids if i != s.id]
            GLib.timeout_add(500, lambda: (self.refresh(), False)[1])
        async_call(work, done)

    # -- experimental live A/B --------------------------------------------
    def _toggle_ab(self, row, _p):
        self.ab_enabled = row.get_active()
        prefs.save(eq_ab_compare=self.ab_enabled)
        if not self.ab_enabled:
            # leaving the experiment must not strand anything in bypass
            for enh in enhance.list_enhancements():
                state = self._bypass.pop(enh.id, None)
                if state:
                    async_call(lambda e=enh, s=state: enhance.unbypass(e, s))
        self.refresh()

    def _ab_row(self, enh):
        bypassed = enh.id in self._bypass
        row = Adw.ActionRow(
            title='Compare', title_lines=1, subtitle_lines=1,
            subtitle=('Audio is going straight to the output — the equalizer '
                      'is still running.' if bypassed
                      else 'Audio is going through the equalizer.'))
        row.add_prefix(Gtk.Image.new_from_icon_name(
            'media-playlist-repeat-symbolic'))
        box = Gtk.Box(valign=Gtk.Align.CENTER)
        box.add_css_class('linked')
        on_btn = Gtk.ToggleButton(label='Equalized', active=not bypassed)
        off_btn = Gtk.ToggleButton(label='Bypassed', active=bypassed,
                                   group=on_btn)
        on_btn.set_tooltip_text('Play through the equalizer')
        off_btn.set_tooltip_text('Play around the equalizer, without stopping '
                                 'it — the streams move, so nothing pauses')
        def clicked(btn, want_bypass):
            # grouped toggles: the button being switched *off* also emits,
            # so only act on the one that just became active
            if not btn.get_active():
                return
            self._set_bypass(enh, want_bypass)
        on_btn.connect('toggled', clicked, False)
        off_btn.connect('toggled', clicked, True)
        box.append(on_btn)
        box.append(off_btn)
        row.add_suffix(box)
        return row

    def _set_bypass(self, enh, want_bypass):
        if want_bypass == (enh.id in self._bypass):
            return

        def done(result, e):
            if want_bypass:
                ok, msg, state = result if result else (False, str(e or ''), {})
                if ok:
                    self._bypass[enh.id] = state
                    self.window.toast(f'Bypassed — playing straight to {msg}'
                                      if msg else 'Bypassed')
                else:
                    self.window.toast(msg or 'Could not bypass')
            else:
                ok, msg = result if result else (False, str(e or ''))
                self.window.toast('Back through the equalizer' if ok
                                  else (msg or 'Could not restore'))
            GLib.timeout_add(400, lambda: (self.refresh(), False)[1])

        if want_bypass:
            async_call(lambda: enhance.bypass(enh), done)
        else:
            state = self._bypass.pop(enh.id, {})
            async_call(lambda: enhance.unbypass(enh, state), done)

    def _output_row(self, enh):
        row = Adw.ComboRow(
            title='Output device',
            subtitle='Where the equalized audio is sent.')
        labels = ['Follow default'] + [n.description for n in self._sinks]
        row.set_model(Gtk.StringList.new(labels))
        target = enh.params.get('target', '')
        sel = 0
        if target:
            sel = next((i + 1 for i, n in enumerate(self._sinks)
                        if n.name == target), 0)
        row.set_selected(sel)               # set BEFORE connecting

        def on_sel(*_):
            i = row.get_selected()
            new = '' if i == 0 else self._sinks[i - 1].name
            if new == enh.params.get('target', ''):
                return
            enh.params['target'] = new

            def done(result, e):
                ok, err = result if result else (False, str(e or ''))
                self.window.toast('Output updated' if ok
                                  else f'Failed: {err}')
                GLib.timeout_add(500, lambda: (self.refresh(), False)[1])
            async_call(lambda: enhance.apply(enh), done)
        row.connect('notify::selected', on_sel)
        return row

    def _toggled(self, sw, _p, enh):
        enabled = sw.get_active()
        if enabled == enh.enabled:
            return
        # stopping the unit removes the sink, so any bypass bookkeeping for it
        # is meaningless from here on
        self._bypass.pop(enh.id, None)

        def done(result, e):
            ok, err = result if result else (False, str(e or ''))
            self.window.toast(
                (f'{enh.name} {"started" if enabled else "stopped"}')
                if ok else (f'Failed: {err}' if err else 'Failed'))
            GLib.timeout_add(400, lambda: (self.refresh(), False)[1])
        async_call(lambda: enhance.set_enabled(enh, enabled), done)

    def _delete(self, enh):
        self._bypass.pop(enh.id, None)
        async_call(lambda: enhance.delete(enh),
                   lambda r, e: (self.window.toast('Deleted'), self.refresh()))

    def _set_default(self, enh):
        def done(result, e):
            ok, msg = result if result else (False, str(e or ''))
            if ok:
                where = f' → {msg}' if msg else ''
                self.window.toast(f'“{enh.name}” is now your output{where}. '
                                  'Everything is equalized through it.')
            else:
                self.window.toast(msg or 'Could not set as default output')
            self.refresh()
        async_call(lambda: enhance.make_default_output(enh), done)

    # ------------------------------------------------------------- editors --
    def _open_eq(self, enh):
        EqDialog(self.window, self, enh).present()

    def _open_mic(self, enh):
        MicDialog(self.window, self, enh).present()

    def _import_eq(self):
        pick_file(self.window, 'Import an AutoEQ / APO file', self._load_eq,
                  filters=[('Parametric EQ', ['*.txt']), ('All files', ['*'])])

    def _on_drop(self, _target, value, _x, _y):
        files = value.get_files() if value else []
        if not files:
            return False
        self._load_eq(files[0].get_path())
        return True

    def _load_eq(self, path):
        if not path:
            return
        try:
            parsed = enhance.parse_eq_file(path)
        except OSError:
            self.window.toast('Could not read that file')
            return
        if not parsed['bands']:
            self.window.toast('No parametric EQ filters found in that file')
            return
        name = GLib.path_get_basename(path).rsplit('.', 1)[0]
        EqDialog(self.window, self, None,
                 preset={'name': name, **parsed}).present()


# ------------------------------------------------------------- EQ dialog --

class EqDialog(Adw.Window):
    def __init__(self, window, page, enh, preset=None):
        super().__init__(title='Edit equalizer' if enh else 'New equalizer',
                         transient_for=window, modal=True, resizable=True,
                         default_width=640, default_height=780)
        self.window = window
        self.page = page
        self.enh = enh
        self._band_rows = []

        preset = preset or {}
        params = enh.params if enh else {}
        init_name = (enh.name if enh else preset.get('name', ''))
        init_preamp = params.get('preamp', preset.get('preamp', 0.0))
        init_bands = params.get('bands') or preset.get('bands') \
            or enhance._default_bands()
        self._target = params.get('target', '')

        g = group('Equalizer')
        self.name_row = Adw.EntryRow(title='Name')
        self.name_row.set_text(init_name)
        g.add(self.name_row)

        self.preamp_row = Adw.SpinRow(
            title='Preamp', subtitle='Overall level trim, in dB. Use a '
            'negative preamp to leave headroom for boosted bands.',
            adjustment=_adj(-24, 24, float(init_preamp), 0.5, 3), digits=1)
        g.add(self.preamp_row)

        self.target_row = Adw.ComboRow(
            title='Output device',
            subtitle='Where the equalized audio goes. “Follow default” lets '
                     'you route it like any output.')
        g.add(self.target_row)

        self.bands_group = group(
            'Bands', 'Each band is one filter. Turn a band off with its '
            'switch without deleting it.')
        add_btn = Gtk.Button(icon_name='list-add-symbolic',
                             valign=Gtk.Align.CENTER, tooltip_text='Add band')
        add_btn.connect('clicked', lambda *_: self._add_band())
        self.bands_group.set_header_suffix(add_btn)
        for b in init_bands:
            self._add_band(b)

        save = Gtk.Button(label='Save' if enh else 'Create',
                          halign=Gtk.Align.END, margin_top=12)
        save.add_css_class('suggested-action')
        save.connect('clicked', self._save)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18,
                      margin_top=12, margin_bottom=24,
                      margin_start=18, margin_end=18)
        box.append(g)
        box.append(self.bands_group)
        box.append(save)
        sw = Gtk.ScrolledWindow(vexpand=True,
                                hscrollbar_policy=Gtk.PolicyType.NEVER)
        clamp = Adw.Clamp(maximum_size=560)
        clamp.set_child(box)
        sw.set_child(clamp)
        view = Adw.ToolbarView()
        view.add_top_bar(Adw.HeaderBar())
        view.set_content(sw)
        self.set_content(view)

        async_call(pw.list_audio_nodes, self._nodes_loaded)

    def _nodes_loaded(self, nodes, error):
        if error or nodes is None:
            nodes = []
        self._sinks = [n for n in nodes
                       if n.is_sink and not n.name.startswith('pwctl.eq.')]
        names = ['Follow default'] + [n.description for n in self._sinks]
        self.target_row.set_model(Gtk.StringList.new(names))
        if self._target:
            idx = next((i + 1 for i, n in enumerate(self._sinks)
                        if n.name == self._target), 0)
            self.target_row.set_selected(idx)

    def _add_band(self, band=None):
        band = band or {'on': True, 'type': 'PK', 'freq': 1000,
                        'gain': 0.0, 'q': 1.0}
        row = Adw.ExpanderRow()
        row.set_show_enable_switch(True)
        row.set_enable_expansion(bool(band.get('on', True)))

        type_row = Adw.ComboRow(
            title='Type',
            model=Gtk.StringList.new(
                [enhance.FILTER_TYPE_LABELS[t] for t in enhance.FILTER_TYPES]))
        ftype = str(band.get('type', 'PK')).upper()
        if ftype in enhance.FILTER_TYPES:
            type_row.set_selected(enhance.FILTER_TYPES.index(ftype))
        freq_row = Adw.SpinRow(title='Frequency (Hz)',
                               adjustment=_adj(20, 20000,
                                               float(band.get('freq', 1000)),
                                               10, 100), digits=0)
        gain_row = Adw.SpinRow(title='Gain (dB)',
                               adjustment=_adj(-24, 24,
                                               float(band.get('gain', 0.0)),
                                               0.5, 3), digits=1)
        q_row = Adw.SpinRow(title='Q (bandwidth)',
                            adjustment=_adj(0.1, 10,
                                            float(band.get('q', 1.0)),
                                            0.1, 1), digits=2)
        remove_row = Adw.ActionRow(title='Remove this band')
        rm = icon_button('user-trash-symbolic', 'Remove band',
                         lambda *_: self._remove_band(entry))
        remove_row.add_suffix(rm)
        for r in (type_row, freq_row, gain_row, q_row, remove_row):
            row.add_row(r)

        entry = {'row': row, 'type': type_row, 'freq': freq_row,
                 'gain': gain_row, 'q': q_row}

        def update_title(*_a):
            t = enhance.FILTER_TYPES[type_row.get_selected()]
            hz = freq_row.get_value()
            label = f'{hz/1000:g} kHz' if hz >= 1000 else f'{hz:g} Hz'
            row.set_title(f'{t} · {label}')
            row.set_subtitle(f'{gain_row.get_value():+.1f} dB · '
                             f'Q {q_row.get_value():.2f}')
        for w in (type_row, freq_row, gain_row, q_row):
            w.connect('notify::selected' if isinstance(w, Adw.ComboRow)
                      else 'notify::value', update_title)
        update_title()

        self.bands_group.add(row)
        self._band_rows.append(entry)

    def _remove_band(self, entry):
        if len(self._band_rows) <= 1:
            self.window.toast('An equalizer needs at least one band')
            return
        self.bands_group.remove(entry['row'])
        if entry in self._band_rows:
            self._band_rows.remove(entry)

    def _collect_bands(self):
        out = []
        for e in self._band_rows:
            out.append({
                'on': e['row'].get_enable_expansion(),
                'type': enhance.FILTER_TYPES[e['type'].get_selected()],
                'freq': round(e['freq'].get_value(), 3),
                'gain': round(e['gain'].get_value(), 2),
                'q': round(e['q'].get_value(), 3),
            })
        return out

    def _save(self, _b):
        name = self.name_row.get_text().strip()
        if not name:
            self.window.toast('Give the equalizer a name')
            return
        target = ''
        sel = self.target_row.get_selected()
        if sel > 0 and 0 <= sel - 1 < len(getattr(self, '_sinks', [])):
            target = self._sinks[sel - 1].name
        params = {
            'preamp': round(self.preamp_row.get_value(), 2),
            'bands': self._collect_bands(),
            'positions': ['FL', 'FR'],
            'target': target,
        }
        if self.enh:
            enh = self.enh
            enh.name = name
            enh.params.update(params)
        else:
            enh = enhance.new_enhancement(name, 'eq', params=params)
            enh.enabled = True

        def done(result, e):
            ok, err = result if result else (False, str(e or ''))
            self.window.toast(f'“{name}” ready' if ok
                              else f'Failed: {err or "unknown error"}')
            self.page.refresh()
        self.close()
        async_call(lambda: enhance.apply(enh), done)


# ------------------------------------------------------------ mic dialog --

class MicDialog(Adw.Window):
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

    def __init__(self, window, page, enh):
        super().__init__(title='Edit clean microphone' if enh
                         else 'New clean microphone',
                         transient_for=window, modal=True, resizable=True,
                         default_width=620, default_height=760)
        self.window = window
        self.page = page
        self.enh = enh
        params = enh.params if enh else {}
        self._source = params.get('source_target', '')

        g = group('Microphone cleanup')
        self.name_row = Adw.EntryRow(title='Name')
        self.name_row.set_text(enh.name if enh else 'Clean Microphone')
        g.add(self.name_row)

        self.source_row = Adw.ComboRow(
            title='Source microphone',
            subtitle='The real microphone to clean up.')
        g.add(self.source_row)

        self.monitor_row = Adw.SwitchRow(
            title='Reference from system output',
            subtitle='Use whatever is playing on your speakers/headphones as '
                     'the echo reference. Turn off only if you route the echo '
                     'reference manually.')
        self.monitor_row.set_active(bool(params.get('monitor_mode', True)))
        g.add(self.monitor_row)

        proc = group('Processing')
        adv = group('Advanced processing')
        self.toggle_rows = {}
        for label, key, subtitle, is_adv in self.TOGGLES:
            sw = Adw.SwitchRow(title=label, subtitle=subtitle)
            sw.set_active(bool(params.get(key, enhance.WEBRTC_DEFAULTS[key])))
            (adv if is_adv else proc).add(sw)
            self.toggle_rows[key] = sw
        # Modal dialog: the sidebar Advanced switch can't be reached while it's
        # open, so gate the advanced group statically instead of registering it
        # window-wide (which would leak this widget past the dialog's lifetime).
        adv.set_visible(getattr(self.window, 'advanced', False))

        save = Gtk.Button(label='Save' if enh else 'Create',
                          halign=Gtk.Align.END, margin_top=12)
        save.add_css_class('suggested-action')
        save.connect('clicked', self._save)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18,
                      margin_top=12, margin_bottom=24,
                      margin_start=18, margin_end=18)
        box.append(g)
        box.append(proc)
        box.append(adv)
        box.append(save)
        sw = Gtk.ScrolledWindow(vexpand=True,
                                hscrollbar_policy=Gtk.PolicyType.NEVER)
        clamp = Adw.Clamp(maximum_size=560)
        clamp.set_child(box)
        sw.set_child(clamp)
        view = Adw.ToolbarView()
        view.add_top_bar(Adw.HeaderBar())
        view.set_content(sw)
        self.set_content(view)

        async_call(pw.list_audio_nodes, self._nodes_loaded)

    def _nodes_loaded(self, nodes, error):
        if error or nodes is None:
            nodes = []
        self._sources = [n for n in nodes if not n.is_sink
                         and not n.name.startswith('pwctl.mic.')]
        names = ['Follow default'] + [n.description for n in self._sources]
        self.source_row.set_model(Gtk.StringList.new(names))
        if self._source:
            idx = next((i + 1 for i, n in enumerate(self._sources)
                        if n.name == self._source), 0)
            self.source_row.set_selected(idx)

    def _save(self, _b):
        name = self.name_row.get_text().strip()
        if not name:
            self.window.toast('Give the microphone a name')
            return
        source = ''
        sel = self.source_row.get_selected()
        if sel > 0 and 0 <= sel - 1 < len(getattr(self, '_sources', [])):
            source = self._sources[sel - 1].name
        params = {'source_target': source,
                  'monitor_mode': self.monitor_row.get_active()}
        for key, sw in self.toggle_rows.items():
            params[key] = sw.get_active()
        if self.enh:
            enh = self.enh
            enh.name = name
            enh.params.update(params)
        else:
            enh = enhance.new_enhancement(name, 'mic', params=params)
            enh.enabled = True

        def done(result, e):
            ok, err = result if result else (False, str(e or ''))
            self.window.toast(f'“{name}” ready' if ok
                              else f'Failed: {err or "unknown error"}')
            self.page.refresh()
        self.close()
        async_call(lambda: enhance.apply(enh), done)
