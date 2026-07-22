"""Effects page: insert LADSPA/LV2 plugins into the signal path.

An "insert" is a filter-chain sink built from a rack of plugins in series:
route any app (or make it the default output) through it and its processed
signal continues to the chosen device.  Racks are regular chains underneath,
so they also appear on the Filter Chains page and survive reboots.
"""

from __future__ import annotations

from dataclasses import asdict

import gi

gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from ..backend import chains, plugins, pw
from .widgets import async_call, confirm, group, icon_button, page_scroller, \
    pill, state_style

_CACHE = {'ladspa': None, 'lv2': None}


def _scan_all():
    if _CACHE['ladspa'] is None:
        _CACHE['ladspa'] = plugins.scan_ladspa()
    if _CACHE['lv2'] is None:
        _CACHE['lv2'] = plugins.scan_lv2()
    return _CACHE['ladspa'], _CACHE['lv2']


class EffectsPage:
    def __init__(self, window):
        self.window = window

        head = group('Real-time effects',
                     'Insert plugin racks into the signal path. The rack '
                     'shows up as an output device — route apps to it (or '
                     'make it the default) and it feeds the processed audio '
                     'to your real output.')
        self.avail_row = Adw.ActionRow(title='Available plugins',
                                       subtitle='Scanning…')
        head.add(self.avail_row)
        new_row = Adw.ActionRow(title='Create effect insert',
                                subtitle='Pick plugins, order them, choose '
                                         'the output they feed')
        new_btn = Gtk.Button(icon_name='list-add-symbolic',
                             valign=Gtk.Align.CENTER)
        new_btn.add_css_class('suggested-action')
        new_btn.connect('clicked', lambda *_: self._open_dialog(None))
        new_row.add_suffix(new_btn)
        new_row.set_activatable_widget(new_btn)
        head.add(new_row)

        self.unsupported_row = Adw.ActionRow(
            title='VST3 / CLAP plugins',
            subtitle='Checking…')
        self.unsupported_row.add_prefix(
            Gtk.Image.new_from_icon_name('dialog-information-symbolic'))
        head.add(self.unsupported_row)

        self.racks = group('Effect inserts')
        self._rack_rows = []
        self.widget = page_scroller(head, self.racks)
        self.widget.connect('map', lambda *_: self.refresh())
        self._scanned = False

    def refresh(self):
        def collect():
            lad, lv2 = _scan_all()
            unsup = plugins.detect_unsupported()
            bridge = plugins.have_bridge_host()
            metas = [m for m in chains.list_chains()
                     if m.template == 'effect-rack']
            states = {m.id: chains.status(m) for m in metas}
            return lad, lv2, unsup, bridge, metas, states
        async_call(collect, self._apply)

    def _apply(self, result, error):
        if error or result is None:
            return
        lad, lv2, unsup, bridge, metas, states = result
        self.avail_row.set_subtitle(
            f'{len(lad)} LADSPA · {len(lv2)} LV2 plugins found')

        if unsup:
            found = ' and '.join(f'{v} {k}' for k, v in unsup.items())
            hint = (f'Load them in {bridge} and route it through the patchbay.'
                    if bridge else
                    'PipeWire cannot host these natively — install a bridge '
                    'host such as Carla and patch it in via the patchbay.')
            self.unsupported_row.set_subtitle(
                f'{found} plugin(s) detected. {hint}')
        else:
            self.unsupported_row.set_subtitle(
                'None installed. PipeWire hosts LADSPA and LV2 natively; '
                'VST3/CLAP need a bridge host such as Carla.')

        for row in self._rack_rows:
            self.racks.remove(row)
        self._rack_rows = []
        if not metas:
            row = Adw.ActionRow(title='No effect inserts yet',
                                subtitle='Create one above — e.g. a '
                                         'compressor + EQ on your speakers.')
            self.racks.add(row)
            self._rack_rows.append(row)
            return
        for meta in metas:
            row = self._rack_row(meta, states.get(meta.id, 'unknown'))
            self.racks.add(row)
            self._rack_rows.append(row)

    def _rack_row(self, meta, state):
        names = [p.get('name', '?') for p in meta.params.get('plugins', [])]
        row = Adw.ActionRow(title=meta.name,
                            subtitle=' → '.join(names) or 'empty rack',
                            title_lines=1, subtitle_lines=1)
        row.add_prefix(Gtk.Image.new_from_icon_name(
            'applications-multimedia-symbolic'))
        if meta.enabled:
            row.add_suffix(pill(state, state_style(state)))
        sw = Gtk.Switch(valign=Gtk.Align.CENTER, active=meta.enabled)
        sw.connect('notify::active', self._toggled, meta)
        row.add_suffix(icon_button('document-edit-symbolic', 'Edit rack',
                                   lambda *_: self._open_dialog(meta)))
        row.add_suffix(icon_button(
            'user-trash-symbolic', 'Delete rack',
            lambda *_: confirm(
                self.window, f'Delete “{meta.name}”?',
                'The insert is stopped and removed.',
                'Delete', lambda: self._delete(meta))))
        row.add_suffix(sw)
        return row

    def _toggled(self, sw, _p, meta):
        enabled = sw.get_active()
        if enabled == meta.enabled:
            return

        def done(result, e):
            ok, err = result if result else (False, str(e or ''))
            self.window.toast(
                f'{meta.name} {"started" if enabled else "stopped"}'
                if ok else f'Failed: {err}')
            GLib.timeout_add(400, lambda: (self.refresh(), False)[1])
        async_call(lambda: chains.set_enabled(meta, enabled), done)

    def _delete(self, meta):
        async_call(lambda: chains.delete(meta),
                   lambda r, e: (self.window.toast('Insert deleted'),
                                 self.refresh()))

    def _open_dialog(self, meta):
        EffectDialog(self.window, self, meta).present(self.window)


class EffectDialog(Adw.Dialog):
    def __init__(self, window, page, meta):
        super().__init__(title='Edit effect insert' if meta
                         else 'New effect insert',
                         content_width=680, content_height=720)
        self.window = window
        self.page = page
        self.meta = meta
        self.rack: list[dict] = [dict(p) for p in
                                 (meta.params.get('plugins', [])
                                  if meta else [])]
        self._all = []
        self._sinks = []

        g = Adw.PreferencesGroup()
        self.name_row = Adw.EntryRow(title='Name')
        self.name_row.set_text(meta.name if meta else '')
        g.add(self.name_row)
        self.target_row = Adw.ComboRow(
            title='Output to',
            subtitle='Where the processed audio goes (Auto = default output)')
        g.add(self.target_row)

        self.rack_group = Adw.PreferencesGroup(
            title='Rack (signal order)',
            description='Audio flows top to bottom. Mono plugins are run '
                        'as an L/R pair automatically.')
        self._rack_rows = []

        browser = Adw.PreferencesGroup(
            title='Plugin browser',
            description='LADSPA and LV2 plugins found on this system.')
        self.search = Gtk.SearchEntry(placeholder_text='Search plugins…',
                                      margin_bottom=6)
        self.search.connect('search-changed', lambda *_: self._fill_browser())
        browser.add(self.search)
        self.browser_list = Gtk.ListBox(css_classes=['boxed-list'],
                                        selection_mode=Gtk.SelectionMode.NONE)
        browser.add(self.browser_list)

        save = Gtk.Button(label='Save' if meta else 'Create',
                          halign=Gtk.Align.END, margin_top=12)
        save.add_css_class('suggested-action')
        save.connect('clicked', self._save)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18,
                      margin_top=12, margin_bottom=24,
                      margin_start=18, margin_end=18)
        box.append(g)
        box.append(self.rack_group)
        box.append(browser)
        box.append(save)
        sw = Gtk.ScrolledWindow(vexpand=True,
                                hscrollbar_policy=Gtk.PolicyType.NEVER)
        clamp = Adw.Clamp(maximum_size=680)
        clamp.set_child(box)
        sw.set_child(clamp)
        view = Adw.ToolbarView()
        view.add_top_bar(Adw.HeaderBar())
        view.set_content(sw)
        self.set_child(view)

        self._fill_rack()

        def collect():
            lad, lv2 = _scan_all()
            return lad + lv2, pw.list_audio_nodes()
        async_call(collect, self._loaded)

    def _loaded(self, result, error):
        if error or result is None:
            return
        self._all, nodes = result
        self._sinks = [n for n in nodes if n.is_sink
                       and not n.name.startswith('effect_input.')]
        names = ['Auto (default output)'] + \
                [n.description for n in self._sinks]
        self.target_row.set_model(Gtk.StringList.new(names))
        if self.meta and self.meta.target:
            idx = next((i + 1 for i, n in enumerate(self._sinks)
                        if n.name == self.meta.target), 0)
            self.target_row.set_selected(idx)
        self._fill_browser()

    # ------------------------------------------------------------- browser --
    def _fill_browser(self):
        child = self.browser_list.get_first_child()
        while child:
            nxt = child.get_next_sibling()
            self.browser_list.remove(child)
            child = nxt
        query = self.search.get_text().lower().strip()
        shown = 0
        for p in self._all:
            if query and query not in p.name.lower() \
                    and query not in p.maker.lower():
                continue
            if shown >= 60:            # keep the dialog snappy
                more = Gtk.ListBoxRow(activatable=False)
                lbl = Gtk.Label(label='… refine the search to see more',
                                margin_top=8, margin_bottom=8)
                lbl.add_css_class('dim-label')
                more.set_child(lbl)
                self.browser_list.append(more)
                break
            row = Adw.ActionRow(
                title=p.name,
                subtitle=f'{p.type.upper()}'
                         + (f' · {p.maker}' if p.maker else '')
                         + ('' if p.ports_known
                            else ' · ports unknown (use alone)'),
                title_lines=1, subtitle_lines=1)
            add = icon_button('list-add-symbolic', 'Add to rack',
                              lambda *_, pl=p: self._add_plugin(pl))
            row.add_suffix(add)
            row.set_activatable_widget(add)
            self.browser_list.append(row)
            shown += 1

    def _add_plugin(self, plugin):
        spec = asdict(plugin)
        if not plugin.ports_known and self.rack:
            self.window.toast(f'{plugin.name}: audio ports unknown — it can '
                              'only be used alone in a rack')
            return
        if self.rack and not all(p.get('audio_in') and p.get('audio_out')
                                 for p in self.rack):
            self.window.toast('The current plugin must stay alone in the '
                              'rack (its ports are unknown)')
            return
        self.rack.append(spec)
        self._fill_rack()

    # ---------------------------------------------------------------- rack --
    def _fill_rack(self):
        for row in self._rack_rows:
            self.rack_group.remove(row)
        self._rack_rows = []
        if not self.rack:
            row = Adw.ActionRow(title='Rack is empty',
                                subtitle='Add plugins from the browser below')
            self.rack_group.add(row)
            self._rack_rows.append(row)
            return
        for i, spec in enumerate(self.rack):
            row = Adw.ActionRow(title=f'{i + 1}. {spec.get("name", "?")}',
                                subtitle=spec.get('plugin', ''),
                                title_lines=1, subtitle_lines=1)
            if i > 0:
                row.add_suffix(icon_button(
                    'go-up-symbolic', 'Move up',
                    lambda *_, idx=i: self._move(idx, -1)))
            if i < len(self.rack) - 1:
                row.add_suffix(icon_button(
                    'go-down-symbolic', 'Move down',
                    lambda *_, idx=i: self._move(idx, 1)))
            row.add_suffix(icon_button(
                'list-remove-symbolic', 'Remove',
                lambda *_, idx=i: self._remove(idx)))
            self.rack_group.add(row)
            self._rack_rows.append(row)

    def _move(self, idx, delta):
        self.rack[idx], self.rack[idx + delta] = \
            self.rack[idx + delta], self.rack[idx]
        self._fill_rack()

    def _remove(self, idx):
        del self.rack[idx]
        self._fill_rack()

    # ---------------------------------------------------------------- save --
    def _save(self, _b):
        name = self.name_row.get_text().strip()
        if not name:
            self.window.toast('Give the insert a name')
            return
        if not self.rack:
            self.window.toast('Add at least one plugin')
            return
        target = ''
        idx = self.target_row.get_selected()
        if idx > 0 and idx <= len(self._sinks):
            target = self._sinks[idx - 1].name

        if self.meta:
            meta = self.meta
            meta.name = name
            meta.target = target
            meta.params['plugins'] = self.rack
        else:
            meta = chains.new_chain(name, 'effect-rack', target=target,
                                    params={'plugins': self.rack})
            meta.enabled = True

        def done(result, e):
            ok, err = result if result else (False, str(e or ''))
            self.window.toast(f'“{name}” ready — route apps to it from the '
                              'Dashboard' if ok else f'Failed: {err}')
            self.page.refresh()
        self.close()
        async_call(lambda: chains.apply(meta), done)
