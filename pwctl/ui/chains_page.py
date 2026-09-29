"""Filter-chain manager page: list, toggle, create, edit, clone, import."""

from __future__ import annotations

from pathlib import Path

import gi

gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Adw, Gtk  # noqa: E402

from ..backend import chains, enhance, headroom, hrir, pw, system
from ..backend.templates import TEMPLATES
from .widgets import (async_call, confirm, esc, group, icon_button, page_scroller,
                      pick_file, pill, state_style, text_viewer_dialog)

AUDIO_FILTER = [('Impulse responses', ['*.wav', '*.flac', '*.ogg', '*.w64',
                                       '*.aiff', '*.sofa'])]
EQ_FILTER = [('AutoEq / text', ['*.txt', '*.csv'])]

# templates whose output level the Level section speaks about: the convolving
# ones it can measure, and SOFA, where it says why it cannot
LEVEL_NEEDS = headroom.CONVOLVING + ('sofa',)


def _too_loud_tip(found, raw):
    fix = ('It is an imported config, so the gain lives in its text: set '
           f'gain = {found.suggested:.2f} in each convolver\'s config.'
           if raw else
           f'Edit the chain and use the suggested gain, {found.suggested:.2f}.')
    return (f'Peaks about {found.over_db:.1f} dB over full scale on typical '
            f'surround content, so it distorts. {fix}')


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
            return [(m, chains.status(m) if m.enabled else 'disabled',
                     headroom.check(headroom.for_chain, m)[0])
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
        for meta, state, found in items:
            row = self._chain_row(meta, state, found)
            self.list_group.add(row)
            self._rows.append(row)

    def _chain_row(self, meta, state, found=None):
        tpl = TEMPLATES.get(meta.template)
        sub = tpl['title'] if tpl else 'Imported config'
        if meta.hrir:
            sub += f'  ·  {Path(meta.hrir).name}'
        row = Adw.ActionRow(title=esc(meta.name), subtitle=sub)
        row.add_prefix(Gtk.Image.new_from_icon_name(
            'audio-input-microphone-symbolic'
            if meta.template == 'rnnoise-source'
            else 'pwctl-chains-symbolic'))
        # Issue #17: a chain made before the gain was measured keeps the gain
        # it was saved with, so say so where it will be seen, enabled or not.
        if found is not None and found.clips:
            loud = pill('Too loud', 'warning')
            loud.set_tooltip_text(_too_loud_tip(found, meta.is_raw))
            row.add_suffix(loud)
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

        # level (issue #17): what the chain comes out at, measured from the
        # IR, and the gain that fixes it.  A new chain follows the measured
        # gain until the user sets one; an existing chain keeps what it was
        # saved with, and is only told.
        self._updating = False
        self._measure_gen = 0
        self._found = None                 # headroom.Headroom, or None
        self._why = ''                     # why not, when None
        self._gain_auto = self.is_new
        self._auto_checked = self.is_new
        self.level_row = LevelRow(self._use_suggested)
        self.gain_row = Adw.SpinRow(
            title='Convolver gain',
            subtitle='1.0 = the IR as recorded',
            adjustment=Gtk.Adjustment(lower=0.01, upper=4.0,
                                      step_increment=0.01, page_increment=0.1),
            digits=2)
        self.gain_row.set_value(
            (meta.params.get('gain', 1.0) if meta else 1.0))
        self.gain_row.connect('notify::value', self._gain_changed)
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
                  self.hrir_info):
            g.add(r)
        self.level_group = group('Level')
        self.level_group.add(self.level_row)
        self.level_group.add(self.gain_row)
        rest = group('')
        for r in (self.target_row, self.eq_row, self.bass_gain,
                  self.bass_freq, self.cross_gain, self.vad_row,
                  self.edit_raw_row):
            rest.add(r)

        save = Gtk.Button(label='Save & apply')
        save.add_css_class('suggested-action')
        save.connect('clicked', self._save)
        header = Adw.HeaderBar()
        header.pack_end(save)
        view = Adw.ToolbarView()
        view.add_top_bar(header)
        view.set_content(page_scroller(g, self.level_group, rest, columns=1))
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
        # Only a convolver has a convolver gain.  The passthrough sink and the
        # effect rack showed this row too and ignored it.  An imported config
        # keeps its gain in its own text, so it gets the reading, not the knob.
        self.gain_row.set_visible(bool(tpl) and tpl['needs'] in LEVEL_NEEDS)
        self.level_group.set_visible(self.gain_row.get_visible())
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
        self._remeasure()
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

    # -------------------------------------------------------------- level --
    def _remeasure(self):
        """Measure what is selected now; a slower, older answer is dropped."""
        self._measure_gen += 1
        gen = self._measure_gen
        tpl_id = self._current_template()
        if tpl_id == 'raw':
            raw = self.meta.params.get('raw_text', '')
            if self.selected_hrir():    # the swap generate() will make
                raw = chains._rewrite_filenames(raw, self.selected_hrir())

            def work():
                return headroom.check(headroom.for_raw, raw)
        else:
            path = self.selected_hrir()

            def work():                 # at 1.0: the gain is applied live
                return headroom.check(headroom.for_template, tpl_id, path)
        async_call(work, lambda res, err: self._measured(gen, res, err))

    def _measured(self, gen, res, error):
        if gen != self._measure_gen:
            return
        self._found, self._why = res if res else (None, str(error))
        found = self._found
        raw = self._current_template() == 'raw'
        if raw:
            self.level_group.set_visible(found is not None)
        elif found is not None:
            if not self._auto_checked:
                # an existing chain follows the measurement only if it was
                # already sitting on it; otherwise its own value stands
                self._auto_checked = True
                self._gain_auto = abs(self.gain_row.get_value()
                                      - found.suggested) < 0.005
            if self._gain_auto:
                self._set_gain(found.suggested)
        self._update_level()

    def _set_gain(self, value):
        self._updating = True
        try:
            self.gain_row.set_value(value)
        finally:
            self._updating = False

    def _gain_changed(self, *_a):
        if not self._updating:
            found = self._found
            self._gain_auto = bool(found) and abs(
                self.gain_row.get_value() - found.suggested) < 0.005
        self._update_level()

    def _use_suggested(self):
        if self._found is not None:
            self._set_gain(self._found.suggested)
            self._gain_auto = True
            self._update_level()

    def _update_level(self):
        found = self._found
        raw = self._current_template() == 'raw'
        gain = self.gain_row.get_value()
        if found is None:
            self.level_row.show_unknown(self._why)
            self.gain_row.set_subtitle('1.0 = the IR as recorded')
            return
        peak = found.peak_db if raw else found.peak_at(gain)
        self.level_row.show(found, peak, raw,
                            at_suggested=abs(gain - found.suggested) < 0.005)
        self.gain_row.set_subtitle(
            f'{_db(headroom.db(gain))}  ·  suggested for this file: '
            f'{found.suggested:.2f}')

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
        def collect():
            return (pw.list_audio_nodes(), chains.list_chains(),
                    enhance.list_enhancements())

        def apply(result, error):
            if error or not result:
                return
            nodes, all_chains, all_enh = result
            # One chain may feed another (and an equalizer counts as one):
            # only this chain and targets that lead back to it are excluded.
            edges = chains.target_edges(all_chains)
            edges.update(enhance.target_edges(all_enh))
            sinks = chains.pick_targets(
                self.meta.node_name if self.meta else '',
                [n for n in nodes if n.is_sink], edges)
            names = ['Auto (follow default)'] + [n.description for n in sinks]
            self.target_names = [''] + [n.name for n in sinks]
            self.target_row.set_model(Gtk.StringList.new(names))
            if self.meta and self.meta.target in self.target_names:
                self.target_row.set_selected(
                    self.target_names.index(self.meta.target))
        async_call(collect, apply)

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
            self._remeasure()
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


# ----------------------------------------------------------------- level ----

class HeadroomBar(Gtk.DrawingArea):
    """Where the chain's output peaks against full scale, drawn to scale.

    -24 to +24 dB with full scale (0 dB) marked and the zone past it always
    tinted, so the one question the bar answers — does it cross that line —
    can be read without the numbers.  Colours come from the two pen labels
    (`set_pens`), the stylesheet-to-Cairo bridge Signal Paths' wires use.
    """
    RANGE = 24.0

    def __init__(self):
        super().__init__(content_height=12, hexpand=True)
        self._db = None
        self._ok = self._hot = None
        self.set_draw_func(self._draw)

    def set_pens(self, ok, hot):
        self._ok, self._hot = ok, hot

    def set_level(self, peak_db):
        self._db = peak_db
        self.queue_draw()

    def _x(self, value, w):
        r = self.RANGE
        return (max(-r, min(r, value)) + r) / (2 * r) * w

    def _draw(self, _area, cr, w, h):
        fg = self.get_color()
        ok = self._ok.get_color() if self._ok else fg
        hot = self._hot.get_color() if self._hot else fg
        x0 = self._x(0.0, w)

        def pill_path():
            r = h / 2
            cr.new_sub_path()
            cr.arc(r, r, r, 1.5708, 4.7124)
            cr.arc(w - r, r, r, 4.7124, 1.5708)
            cr.close_path()

        pill_path()
        cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.10)
        cr.fill()
        cr.save()
        pill_path()
        cr.clip()
        cr.rectangle(x0, 0, w - x0, h)
        cr.set_source_rgba(hot.red, hot.green, hot.blue, 0.22)
        cr.fill()
        if self._db is not None:
            c = hot if self._db > headroom.CLIP_DB else ok
            cr.rectangle(0, 0, self._x(self._db, w), h)
            cr.set_source_rgba(c.red, c.green, c.blue, 0.9)
            cr.fill()
        cr.restore()
        cr.rectangle(round(x0) - 1, 0, 2, h)
        cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.85)
        cr.fill()


class LevelRow(Adw.PreferencesRow):
    """The Level section's reading: a verdict, the bar, and the fix.

    Deliberately self-contained — one row that takes a `headroom.Headroom`
    and a peak — so the full UI round can move it without unpicking the
    dialog around it.
    """

    def __init__(self, on_use_suggested):
        super().__init__(activatable=False, focusable=False)
        self.add_css_class('chain-level')
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6,
                      margin_top=12, margin_bottom=12,
                      margin_start=12, margin_end=12)
        self.verdict = Gtk.Label(xalign=0, wrap=True)
        self.verdict.add_css_class('chain-level-verdict')
        box.append(self.verdict)

        self.bar = HeadroomBar()
        pens = [Gtk.Label(css_classes=[c], visible=False)
                for c in ('chain-level-pen-ok', 'chain-level-pen-hot')]
        self.bar.set_pens(*pens)
        scale = Gtk.CenterBox()
        scale.add_css_class('chain-level-scale')
        for pos, text in (('start', f'−{HeadroomBar.RANGE:.0f} dB'),
                          ('center', '0 dB · full scale'),
                          ('end', f'+{HeadroomBar.RANGE:.0f} dB')):
            getattr(scale, f'set_{pos}_widget')(Gtk.Label(label=text))
        self.bar_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL,
                               spacing=3)
        self.bar_box.append(self.bar)
        self.bar_box.append(scale)
        for pen in pens:
            self.bar_box.append(pen)
        box.append(self.bar_box)

        self.detail = Gtk.Label(xalign=0, wrap=True)
        self.detail.add_css_class('chain-level-detail')
        box.append(self.detail)

        self.use_btn = Gtk.Button(halign=Gtk.Align.START)
        self.use_btn.add_css_class('pill')
        self.use_btn.add_css_class('chain-level-use')
        self.use_btn.connect('clicked', lambda *_: on_use_suggested())
        box.append(self.use_btn)
        self.set_child(box)

    def _state(self, name):
        for c in ('clips', 'fits', 'unknown'):
            (self.verdict.add_css_class if c == name
             else self.verdict.remove_css_class)(c)

    def show_unknown(self, why):
        self._state('unknown')
        self.verdict.set_text('The level can’t be measured')
        self.detail.set_text(why[:1].upper() + why[1:] + '.' if why else '')
        self.bar_box.set_visible(False)
        self.use_btn.set_visible(False)

    def show(self, found, peak_db, raw, at_suggested):
        clips = peak_db > headroom.CLIP_DB
        self._state('clips' if clips else 'fits')
        self.verdict.set_text(
            f'Too loud: peaks {_db(peak_db, sign=False)} over full scale, '
            'so it distorts' if clips else
            f'Level is fine: peaks {_db(-peak_db, sign=False)} below full '
            'scale')
        self.bar.set_level(peak_db)
        self.bar.set_tooltip_text(self.verdict.get_text())
        self.bar_box.set_visible(True)

        at_one = found.peak_at(1.0)
        louder = (f'{_db(abs(at_one), sign=False)} '
                  + ('louder' if at_one >= 0 else 'quieter'))
        why = [(f'{found.paths} convolvers are mixed into each ear and '
                'impulse responses aren’t recorded at unity, so at gain '
                f'1.0 this file comes out {louder} than what goes in.')
               if found.paths > 1 else
               f'At gain 1.0 this file comes out {louder} than what goes in.']
        if found.paths > 1 and found.peak > 0:
            why.append('Measured from the file for typical surround content; '
                       'one sound in every speaker at once can peak '
                       f'{_db(headroom.db(found.coherent / found.peak), sign=False)}'
                       ' higher.')
        else:
            why.append('Measured from the file.')
        if raw and clips:
            why.append('An imported config keeps its gain in its text: set '
                       f'gain = {found.suggested:.2f} on each convolver, under '
                       'Edit config text.')
        self.detail.set_text(' '.join(why))

        self.use_btn.set_label(f'Use the suggested gain, {found.suggested:.2f}')
        self.use_btn.set_visible(not raw and not at_suggested)


def _db(value, sign=True):
    """One decimal, with a real minus sign, as the scale under the bar has."""
    text = f'{value:+.1f}' if sign else f'{value:.1f}'
    return text.replace('-', '−') + ' dB'
