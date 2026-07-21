"""HRIR / impulse-response library browser and analyzer."""

from __future__ import annotations

from pathlib import Path

import gi

gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Adw, Gtk  # noqa: E402

from ..backend import hrir
from .widgets import (async_call, confirm, group, icon_button, page_scroller,
                      pick_files, pick_folder, pill)

KIND_STYLE = {'hesuvi': 'success', 'true-stereo': 'warning',
              'stereo': 'dim', 'mono': 'dim', 'sofa': 'warning'}


class HrirPage:
    def __init__(self, window):
        self.window = window

        actions = group('Library',
                        f'Files live in {hrir.LIBRARY_DIR} — chains reference '
                        'them by absolute path.')
        add_row = Adw.ActionRow(
            title='Import impulse responses',
            subtitle='WAV / FLAC / SOFA — files are copied into the library')
        add_btn = Gtk.Button(icon_name='list-add-symbolic',
                             valign=Gtk.Align.CENTER)
        add_btn.add_css_class('suggested-action')
        add_btn.connect('clicked', self._import_files)
        add_row.add_suffix(add_btn)
        add_row.set_activatable_widget(add_btn)
        actions.add(add_row)

        scan_row = Adw.ActionRow(
            title='Import a whole folder',
            subtitle='e.g. a downloaded HeSuVi hrir/ directory')
        scan_btn = Gtk.Button(icon_name='folder-open-symbolic',
                              valign=Gtk.Align.CENTER)
        scan_btn.connect('clicked', self._import_folder)
        scan_row.add_suffix(scan_btn)
        scan_row.set_activatable_widget(scan_btn)
        actions.add(scan_row)

        demo_row = Adw.ActionRow(
            title='Generate demo HRIR',
            subtitle='Synthesizes a basic 14-channel HeSuVi-layout file so '
                     'you can test virtual surround before downloading a '
                     'real set (HeSuVi HRIRs: github.com/jaakkopasanen/HeSuVi '
                     '→ hrir folder, use the 14-channel wavs)')
        demo_btn = Gtk.Button(icon_name='system-run-symbolic',
                              valign=Gtk.Align.CENTER)
        demo_btn.connect('clicked', self._gen_demo)
        demo_row.add_suffix(demo_btn)
        demo_row.set_activatable_widget(demo_btn)
        actions.add(demo_row)

        self.files_group = group('Files', '')
        self._rows = []
        self.widget = page_scroller(actions, self.files_group)
        self.widget.connect('map', lambda *_: self.refresh())

    def refresh(self):
        async_call(hrir.scan_dir, self._apply)

    def _apply(self, infos, error):
        if error or infos is None:
            return
        for row in self._rows:
            self.files_group.remove(row)
        self._rows = []
        self.files_group.set_description(
            f'{len(infos)} file(s) in library' if infos else
            'Library is empty — import HRIRs or generate the demo file.')
        for info in infos:
            row = self._file_row(info)
            self.files_group.add(row)
            self._rows.append(row)

    def _file_row(self, info: hrir.IRInfo):
        if info.is_sofa:
            sub = 'SOFA HRTF — for the spatializer templates'
        elif info.ok:
            sub = (f'{info.channels} ch  ·  {info.samplerate} Hz  ·  '
                   f'{info.duration:.2f} s  ·  {info.fmt} {info.subtype}')
        else:
            sub = f'unreadable: {info.error}'
        row = Adw.ActionRow(title=info.path.name, subtitle=sub)
        row.add_prefix(Gtk.Image.new_from_icon_name('folder-music-symbolic'))
        row.add_suffix(pill(info.kind_label.split(' (')[0],
                            KIND_STYLE.get(info.kind, 'dim')))

        if info.templates:
            use = Gtk.Button(label='New chain', valign=Gtk.Align.CENTER,
                             tooltip_text='Create a chain using this file — '
                                          'template auto-selected from the '
                                          'channel count')
            use.connect('clicked', self._use_file, info)
            row.add_suffix(use)

        row.add_suffix(icon_button('user-trash-symbolic', 'Remove from library',
                                   lambda *_: self._delete(info)))
        return row

    def _use_file(self, _b, info):
        from ..backend import chains
        from .chains_page import ChainDialog
        tpl = info.templates[0]
        meta = chains.new_chain(info.path.stem, tpl, hrir=str(info.path))
        chains.save_meta(meta)
        self.window.toast(f'Chain created with template '
                          f'“{tpl}” — configure and enable it')
        self.window.goto('chains')
        ChainDialog(self.window, self.window.pages['chains'], meta)

    def _delete(self, info):
        def do():
            if hrir.remove_file(info.path):
                self.window.toast(f'{info.path.name} removed')
            self.refresh()
        confirm(self.window, f'Remove “{info.path.name}”?',
                'Chains still pointing at it will fail to start until you '
                'assign a new file.', 'Remove', do)

    def _import_files(self, _b):
        def picked(paths):
            def work():
                return [hrir.import_file(p) for p in paths]

            def done(res, error):
                if error:
                    self.window.toast(f'Import failed: {error}')
                else:
                    self.window.toast(f'Imported {len(res)} file(s)')
                self.refresh()
            async_call(work, done)
        pick_files(self.window, 'Import impulse responses', picked,
                   filters=[('Audio / SOFA', ['*.wav', '*.flac', '*.ogg',
                                              '*.w64', '*.aiff', '*.sofa'])])

    def _import_folder(self, _b):
        def picked(folder):
            def work():
                imported = 0
                for info in hrir.scan_dir(folder):
                    hrir.import_file(info.path)
                    imported += 1
                return imported

            def done(res, error):
                self.window.toast(f'Imported {res or 0} file(s)'
                                  if not error else f'Failed: {error}')
                self.refresh()
            async_call(work, done)
        pick_folder(self.window, 'Import folder of IRs', picked)

    def _gen_demo(self, _b):
        def done(res, error):
            if res:
                self.window.toast(f'Demo HRIR written: {res.name}')
            else:
                self.window.toast('numpy/soundfile needed to generate')
            self.refresh()
        async_call(hrir.generate_demo_hrir, done)
