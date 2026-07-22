"""Filter-chain manager page: list, toggle, create, edit, clone, import."""

from __future__ import annotations

from pathlib import Path

import gi

gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Adw, Gtk  # noqa: E402

from ..backend import chains, hrir, pw, system
from ..backend.templates import TEMPLATES
from .widgets import (async_call, confirm, group, icon_button, page_scroller,
                      pick_file, pill, state_style, text_viewer_dialog)

AUDIO_FILTER = [('Impulse responses', ['*.wav', '*.flac', '*.ogg', '*.w64',
                                       '*.aiff', '*.sofa'])]
EQ_FILTER = [('AutoEq / text', ['*.txt', '*.csv'])]


class ChainsPage:
    def __init__(self, window):
        self.window = window
        self.list_group = group('Configured chains',
                                'Each chain runs as its own process — '
                                'toggling or editing one never interrupts '
                                'the rest of your audio.')
        actions = group('')
        new_row = Adw.ActionRow(
            title='New filter chain',
            subtitle='Build from a template: virtual surround, convolver, '
                     'EQ, crossfeed, noise cancelling…')
        new_btn = Gtk.Button(icon_name='list-add-symbolic')
        new_btn.add_css_class('suggested-action')
        new_btn.set_valign(Gtk.Align.CENTER)
        new_btn.connect('clicked', lambda *_: ChainDialog(self.window, self, None))
        new_row.add_suffix(new_btn)
        new_row.set_activatable_widget(new_btn)
        actions.add(new_row)

        imp_row = Adw.ActionRow(
            title='Import existing .conf',
            subtitle='Bring hand-written filter-chain drop-ins under app '
                     'management (HRIR swap included)')
        imp_btn = Gtk.Button(icon_name='document-open-symbolic')
        imp_btn.set_valign(Gtk.Align.CENTER)
        imp_btn.connect('clicked', self._import_clicked)
        imp_row.add_suffix(imp_btn)
        imp_row.set_activatable_widget(imp_btn)
        actions.add(imp_row)

        self._rows = []
        self.widget = page_scroller(actions, self.list_group)
        self.widget.connect('map', lambda *_: self.refresh())

    # ------------------------------------------------------------- listing --
    def refresh(self):
        def collect():
            metas = chains.list_chains()
            return [(m, chains.status(m) if m.enabled else 'disabled')
                    for m in metas]
        async_call(collect, self._apply)

    def _apply(self, items, error):
        if error or items is None:
            return
        for row in self._rows:
            self.list_group.remove(row)
        self._rows = []
        if not items:
            empty = Adw.ActionRow(
                title='No chains yet',
                subtitle='Create one from a template above, or import your '
                         'existing configs.')
            self.list_group.add(empty)
            self._rows.append(empty)
            return
        for meta, state in items:
            row = self._chain_row(meta, state)
            self.list_group.add(row)
            self._rows.append(row)

    def _chain_row(self, meta, state):
        tpl = TEMPLATES.get(meta.template)
        sub = tpl['title'] if tpl else 'Imported config'
        if meta.hrir:
            sub += f'  ·  {Path(meta.hrir).name}'
        row = Adw.ActionRow(title=meta.name, subtitle=sub)
        row.add_prefix(Gtk.Image.new_from_icon_name(
            'audio-input-microphone-symbolic'
            if meta.template == 'rnnoise-source'
            else 'audio-headphones-symbolic'))
        if meta.enabled:
            row.add_suffix(pill(state, state_style(state)))

        switch = Gtk.Switch(valign=Gtk.Align.CENTER, active=meta.enabled,
                            tooltip_text='Enable chain')
        switch.connect('state-set', self._toggled, meta)
        edit = icon_button('document-edit-symbolic', 'Edit chain',
                           lambda *_: ChainDialog(self.window, self, meta))

        menu_btn = Gtk.MenuButton(icon_name='view-more-symbolic',
                                  valign=Gtk.Align.CENTER)
        menu_btn.add_css_class('flat')
        pop = Gtk.Popover()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        for label, cb in (
                ('Restart chain', lambda: self._restart(meta)),
                ('Clone', lambda: self._clone(meta)),
                ('View generated config', lambda: self._view_conf(meta)),
                ('View log', lambda: self._view_log(meta)),
                ('Delete', lambda: self._delete(meta))):
            b = Gtk.Button(label=label)
            b.add_css_class('flat')
            b.get_child().set_halign(Gtk.Align.START)
            if label == 'Delete':
                b.add_css_class('destructive-flat')

            def clicked(_b, fn=cb):
                pop.popdown()
                fn()
            b.connect('clicked', clicked)
            box.append(b)
        pop.set_child(box)
        menu_btn.set_popover(pop)

        row.add_suffix(edit)
        row.add_suffix(menu_btn)
        row.add_suffix(switch)
        return row

    # ------------------------------------------------------------- actions --
    def _toggled(self, switch, active, meta):
        def work():
            return chains.set_enabled(meta, active)

        def done(res, error):
            ok, msg = res if res else (False, str(error))
            if ok:
                self.window.toast(
                    f'{meta.name} {"enabled" if active else "disabled"}')
            else:
                self.window.toast(f'Failed: {msg}')
            self.refresh()
        async_call(work, done)
        return False

    def _restart(self, meta):
        async_call(lambda: chains.restart(meta),
                   lambda r, e: (self.window.toast(
                       f'{meta.name} restarted' if r and r[0]
                       else 'Restart failed'), self.refresh()))

    def _clone(self, meta):
        dup = chains.clone(meta)
        self.window.toast(f'Cloned as {dup.name}')
        self.refresh()

    def _view_conf(self, meta):
        try:
            chains.generate(meta)
            text = meta.conf_path.read_text()
        except Exception as e:
            text = f'# failed to generate: {e}'
        text_viewer_dialog(self.window, f'{meta.name} — generated config', text)

    def _view_log(self, meta):
        log = system.unit_journal(meta.unit) or '(no log output)'
        text_viewer_dialog(self.window, f'{meta.name} — journal', log)

    def _delete(self, meta):
        def do():
            chains.delete(meta)
            self.window.toast(f'{meta.name} deleted')
            self.refresh()
        confirm(self.window, f'Delete “{meta.name}”?',
                'The chain is stopped and its config removed. Your HRIR '
                'files are untouched.', 'Delete', do)

    # -------------------------------------------------------------- import --
    def _import_clicked(self, _b):
        found = chains.scan_importable()
        if not found:
            self._import_from_file()
            return
        dlg = Adw.Dialog(title='Import filter-chain configs',
                         content_width=560, content_height=480)
        g = group('Found on this system',
                  'Drop-ins detected in your pipewire config folders.')
        for path in found:
            info = chains.sniff_conf(path)
            row = Adw.ActionRow(
                title=info['name'], subtitle=str(path),
                sensitive=info['valid'])
            btn = Gtk.Button(label='Import')
            btn.set_valign(Gtk.Align.CENTER)

            def do_import(_b, p=path, r=row):
                meta = chains.import_conf(p)
                if meta:
                    self.window.toast(f'Imported {meta.name} (disabled — '
                                      'enable it from the list)')
                    r.set_sensitive(False)
                    self.refresh()
                else:
                    self.window.toast('Not a valid filter-chain config')
            btn.connect('clicked', do_import)
            row.add_suffix(btn)
            g.add(row)
        other = group('')
        row = Adw.ActionRow(title='Choose another file…')
        b = Gtk.Button(icon_name='document-open-symbolic',
                       valign=Gtk.Align.CENTER)
        b.connect('clicked', lambda *_: (dlg.close(), self._import_from_file()))
        row.add_suffix(b)
        row.set_activatable_widget(b)
        other.add(row)
        view = Adw.ToolbarView()
        view.add_top_bar(Adw.HeaderBar())
        view.set_content(page_scroller(g, other))
        dlg.set_child(view)
        dlg.present(self.window)

    def _import_from_file(self):
        def picked(path):
            meta = chains.import_conf(path)
            if meta:
                self.window.toast(f'Imported {meta.name}')
                self.refresh()
            else:
                self.window.toast('Not a valid filter-chain config')
        pick_file(self.window, 'Import filter-chain .conf', picked,
                  filters=[('PipeWire configs', ['*.conf'])])


# ---------------------------------------------------------------- dialog ----

class ChainDialog(Adw.Dialog):
    """Create/edit dialog. Regenerates + restarts only this chain on save."""

    def __init__(self, window, page, meta):
        super().__init__(title='Edit chain' if meta else 'New filter chain',
                         content_width=640, content_height=640)
        self.window = window
        self.page = page
        self.meta = meta
        self.is_new = meta is None
        # effect racks carry a plugin list and are built on the Effects page
        self.template_ids = [t for t in TEMPLATES
                             if TEMPLATES[t]['needs'] != 'plugins'
                             or (meta and meta.template == t)]

        self.name_row = Adw.EntryRow(title='Name')
        self.name_row.set_text(meta.name if meta else '')

        tpl_titles = [TEMPLATES[t]['title'] for t in self.template_ids]
        self.tpl_row = Adw.ComboRow(title='Template',
                                    model=Gtk.StringList.new(tpl_titles))
        self.tpl_desc = Adw.ActionRow(title='')
        self.tpl_desc.add_css_class('dim-row')
        if meta and meta.is_raw:
            self.tpl_row.set_sensitive(False)
            self.tpl_row.set_subtitle('Imported config (raw)')
        elif meta:
            self.tpl_row.set_selected(self.template_ids.index(meta.template))
        self.tpl_row.connect('notify::selected', self._tpl_changed)

        # HRIR / IR selection
        self.hrir_row = Adw.ComboRow(title='Impulse response / HRIR')
        self.hrir_paths = []
        browse = icon_button('document-open-symbolic', 'Browse…',
                             self._browse_ir)
        self.hrir_row.add_suffix(browse)
        self.hrir_info = Adw.ActionRow(title='')
        self.hrir_info.add_css_class('dim-row')

        # target
        self.target_row = Adw.ComboRow(title='Output to',
                                       subtitle='Where the processed audio '
                                                'goes (Auto = default output)')
        self.target_names = ['']
        self.target_row.set_model(Gtk.StringList.new(['Auto (follow default)']))

        # params
        self.gain_row = Adw.SpinRow(
            title='Convolver gain',
            subtitle='Scale the IR to avoid clipping (1.0 = unchanged)',
            adjustment=Gtk.Adjustment(lower=0.05, upper=4.0,
                                      step_increment=0.05),
            digits=2)
        self.gain_row.set_value(
            (meta.params.get('gain', 1.0) if meta else 1.0))
        self.eq_row = Adw.ActionRow(
            title='AutoEq file',
            subtitle='ParametricEQ.txt from autoeq.app for your headphones')
        self.eq_path = meta.params.get('eq_file', '') if meta else ''
        self.eq_label = Gtk.Label(label='none')
        self.eq_label.set_valign(Gtk.Align.CENTER)
        self.eq_row.add_suffix(self.eq_label)
        self.eq_row.add_suffix(icon_button('document-open-symbolic',
                                           'Choose EQ file', self._browse_eq))
        self.bass_gain = Adw.SpinRow(
            title='Bass gain (dB)',
            adjustment=Gtk.Adjustment(lower=-12, upper=18, step_increment=0.5),
            digits=1)
        self.bass_gain.set_value(meta.params.get('bass_gain', 6.0) if meta else 6.0)
        self.bass_freq = Adw.SpinRow(
            title='Shelf frequency (Hz)',
            adjustment=Gtk.Adjustment(lower=40, upper=400, step_increment=5))
        self.bass_freq.set_value(meta.params.get('bass_freq', 100) if meta else 100)
        self.cross_gain = Adw.SpinRow(
            title='Crossfeed amount',
            subtitle='0.2 subtle … 0.5 strong',
            adjustment=Gtk.Adjustment(lower=0.1, upper=0.6, step_increment=0.05),
            digits=2)
        self.cross_gain.set_value(meta.params.get('cross_gain', 0.35) if meta else 0.35)
        self.vad_row = Adw.SpinRow(
            title='Voice detection threshold (%)',
            subtitle='Higher removes more noise but may clip quiet speech',
            adjustment=Gtk.Adjustment(lower=0, upper=95, step_increment=5))
        self.vad_row.set_value(meta.params.get('vad_threshold', 50) if meta else 50)

        self.edit_raw_row = Adw.ActionRow(
            title='Edit config text',
            subtitle='Direct SPA-JSON editing with validation')
        self.edit_raw_row.add_suffix(icon_button(
            'document-edit-symbolic', 'Edit', self._edit_raw))

        g = group('')
        for r in (self.name_row, self.tpl_row, self.tpl_desc, self.hrir_row,
                  self.hrir_info, self.target_row, self.gain_row, self.eq_row,
                  self.bass_gain, self.bass_freq, self.cross_gain,
                  self.vad_row, self.edit_raw_row):
            g.add(r)

        save = Gtk.Button(label='Save & apply')
        save.add_css_class('suggested-action')
        save.connect('clicked', self._save)
        header = Adw.HeaderBar()
        header.pack_end(save)
        view = Adw.ToolbarView()
        view.add_top_bar(header)
        view.set_content(page_scroller(g))
        self.set_child(view)

        self._load_hrir_choices()
        self._load_targets()
        self._tpl_changed()
        self.present(window)

    # ------------------------------------------------------------ helpers --
    def _current_template(self):
        if self.meta and self.meta.is_raw:
            return 'raw'
        return self.template_ids[self.tpl_row.get_selected()]

    def _tpl_changed(self, *_a):
        tpl_id = self._current_template()
        tpl = TEMPLATES.get(tpl_id)
        self.tpl_desc.set_title(tpl['desc'] if tpl else
                                'Imported config — swap the IR below or edit '
                                'the text directly.')
        needs = tpl['needs'] if tpl else ('hesuvi' if self.meta and
                                          self.meta.hrir else None)
        is_conv = needs is not None or tpl_id == 'raw' and bool(
            self.meta and self.meta.hrir)
        self.hrir_row.set_visible(is_conv)
        self.hrir_info.set_visible(is_conv)
        self.gain_row.set_visible(tpl_id not in
                                  ('parametric-eq', 'bass-boost', 'crossfeed',
                                   'rnnoise-source', 'raw'))
        self.eq_row.set_visible(tpl_id == 'parametric-eq')
        self.bass_gain.set_visible(tpl_id == 'bass-boost')
        self.bass_freq.set_visible(tpl_id == 'bass-boost')
        self.cross_gain.set_visible(tpl_id == 'crossfeed')
        self.vad_row.set_visible(tpl_id == 'rnnoise-source')
        self.edit_raw_row.set_visible(tpl_id == 'raw')
        self.target_row.set_visible(tpl_id != 'rnnoise-source')
        self.eq_label.set_label(Path(self.eq_path).name if self.eq_path
                                else 'none')
        self._filter_hrir_choices(needs)
        if self.is_new and not self.name_row.get_text() and tpl:
            self.name_row.set_text(tpl['title'])

    def _load_hrir_choices(self):
        self.library = hrir.scan_dir()

    def _filter_hrir_choices(self, needs):
        """Populate the IR combo, preferring files that match the template."""
        sel_path = self.meta.hrir if self.meta else ''
        labels, self.hrir_paths = [], []
        for info in self.library:
            match = (needs is None or info.kind == needs
                     or (needs == 'stereo' and info.kind in ('stereo', 'mono')))
            tag = info.kind_label if not match else ''
            labels.append(info.path.name + (f'  — {tag}' if tag else ''))
            self.hrir_paths.append(str(info.path))
        if sel_path and sel_path not in self.hrir_paths:
            labels.insert(0, Path(sel_path).name + '  (external)')
            self.hrir_paths.insert(0, sel_path)
        if not labels:
            labels = ['— library is empty, browse for a file —']
            self.hrir_paths = ['']
        self.hrir_row.set_model(Gtk.StringList.new(labels))
        if sel_path in self.hrir_paths:
            self.hrir_row.set_selected(self.hrir_paths.index(sel_path))
        if not getattr(self, '_hrir_connected', False):
            self.hrir_row.connect('notify::selected', self._hrir_selected)
            self._hrir_connected = True
        self._hrir_selected()

    def _hrir_selected(self, *_a):
        path = self.selected_hrir()
        if not path:
            self.hrir_info.set_title('No IR selected')
            return
        info = hrir.analyze(path)
        if info.ok and not info.is_sofa:
            self.hrir_info.set_title(
                f'{info.kind_label}  ·  {info.samplerate} Hz  ·  '
                f'{info.duration:.2f}s  ·  {info.subtype}')
        elif info.is_sofa:
            self.hrir_info.set_title('SOFA HRTF file')
        else:
            self.hrir_info.set_title(f'⚠ {info.error}')

    def selected_hrir(self):
        idx = self.hrir_row.get_selected()
        if 0 <= idx < len(self.hrir_paths):
            return self.hrir_paths[idx]
        return ''

    def _browse_ir(self, _b):
        def picked(path):
            info = hrir.analyze(path)
            # auto-select the matching template for the channel count
            if self.is_new and info.templates and not (
                    self.meta and self.meta.is_raw):
                self.tpl_row.set_selected(
                    self.template_ids.index(info.templates[0]))
            if str(path) not in self.hrir_paths:
                self.hrir_paths.insert(0, str(path))
                model = self.hrir_row.get_model()
                # rebuild model with new entry first
                labels = [Path(path).name + '  (external)']
                for i in range(model.get_n_items()):
                    labels.append(model.get_string(i))
                self.hrir_row.set_model(Gtk.StringList.new(labels))
            self.hrir_row.set_selected(self.hrir_paths.index(str(path)))
        pick_file(self.window, 'Choose impulse response', picked,
                  filters=AUDIO_FILTER,
                  initial_folder=str(hrir.LIBRARY_DIR))

    def _browse_eq(self, _b):
        def picked(path):
            self.eq_path = path
            self.eq_label.set_label(Path(path).name)
        pick_file(self.window, 'Choose AutoEq file', picked, filters=EQ_FILTER)

    def _load_targets(self):
        def apply(nodes, error):
            if error or not nodes:
                return
            names = ['Auto (follow default)']
            self.target_names = ['']
            for n in nodes:
                if n.is_sink and not n.name.startswith('effect_input.'):
                    names.append(n.description)
                    self.target_names.append(n.name)
            self.target_row.set_model(Gtk.StringList.new(names))
            if self.meta and self.meta.target in self.target_names:
                self.target_row.set_selected(
                    self.target_names.index(self.meta.target))
        async_call(pw.list_audio_nodes, apply)

    def _edit_raw(self, _b):
        raw = self.meta.params.get('raw_text', '') if self.meta else ''

        def on_save(text):
            from .. import spa_json
            try:
                spa_json.loads(text)
            except spa_json.SpaJsonError as e:
                self.window.toast(f'Invalid SPA JSON: {e}')
                return False
            self.meta.params['raw_text'] = text
            self.window.toast('Config text updated — save to apply')
            return True
        text_viewer_dialog(self.window, 'Edit config', raw,
                           editable=True, on_save=on_save)

    # --------------------------------------------------------------- save --
    def _save(self, _b):
        name = self.name_row.get_text().strip()
        if not name:
            self.window.toast('Give the chain a name')
            return
        tpl_id = self._current_template()
        tpl = TEMPLATES.get(tpl_id)
        hrir_path = self.selected_hrir() if self.hrir_row.get_visible() else \
            (self.meta.hrir if self.meta else '')
        if tpl and tpl['needs'] and not hrir_path:
            self.window.toast('This template needs an impulse response file')
            return

        if self.meta is None:
            self.meta = chains.new_chain(name, tpl_id)
        meta = self.meta
        meta.name = name
        if not meta.is_raw:
            meta.template = tpl_id
        meta.hrir = hrir_path
        if hrir_path:
            meta.hrir_channels = hrir.analyze(hrir_path).channels
        idx = self.target_row.get_selected()
        meta.target = (self.target_names[idx]
                       if 0 <= idx < len(self.target_names) else '')
        meta.params.update({
            'gain': round(self.gain_row.get_value(), 2),
            'eq_file': self.eq_path,
            'bass_gain': round(self.bass_gain.get_value(), 1),
            'bass_freq': int(self.bass_freq.get_value()),
            'cross_gain': round(self.cross_gain.get_value(), 2),
            'vad_threshold': int(self.vad_row.get_value()),
        })

        def work():
            if meta.enabled:
                return chains.apply(meta)      # regenerate + restart this unit
            chains.generate(meta)
            chains.save_meta(meta)
            return True, ''

        def done(res, error):
            ok, msg = res if res else (False, str(error))
            if ok:
                self.window.toast(f'{meta.name} saved'
                                  + (' and restarted' if meta.enabled else ''))
                self.close()
                self.page.refresh()
            else:
                self.window.toast(f'Failed: {msg}')
        async_call(work, done)
