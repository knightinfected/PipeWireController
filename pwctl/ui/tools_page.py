"""Tools: service control, latency calculator, maintenance."""

from __future__ import annotations

import subprocess
from pathlib import Path

import gi

gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Adw, Gtk  # noqa: E402

from .. import __version__
from ..backend import config, system
from ..backend.schema import QUANTA, RATES
from .widgets import (async_call, confirm, group, icon_button, page_scroller,
                      text_viewer_dialog)


class ToolsPage:
    def __init__(self, window):
        self.window = window

        svc = group('Service control')
        for title, subtitle, fn in (
            ('Restart audio stack',
             'pipewire + pipewire-pulse + wireplumber — apply persistent '
             'config changes', system.restart_pipewire),
            ('Restart WirePlumber only',
             'Re-reads session-manager policy without dropping the graph',
             system.restart_wireplumber),
        ):
            row = Adw.ActionRow(title=title, subtitle=subtitle)
            btn = Gtk.Button(icon_name='view-refresh-symbolic',
                             valign=Gtk.Align.CENTER)
            btn.connect('clicked', self._run_service, fn, title)
            row.add_suffix(btn)
            row.set_activatable_widget(btn)
            svc.add(row)
        log_row = Adw.ActionRow(title='View PipeWire journal',
                                subtitle='Last 80 log lines')
        log_btn = Gtk.Button(icon_name='utilities-terminal-symbolic',
                             valign=Gtk.Align.CENTER)
        log_btn.connect('clicked', self._view_journal)
        log_row.add_suffix(log_btn)
        log_row.set_activatable_widget(log_btn)
        svc.add(log_row)

        # latency calculator
        calc = group('Latency calculator',
                     'One graph cycle = quantum ÷ rate. Real round-trip adds '
                     'device buffers on top.')
        self.calc_rate = Adw.ComboRow(
            title='Sample rate', model=Gtk.StringList.new(
                [str(r) for r in RATES]))
        self.calc_rate.set_selected(1)
        self.calc_quantum = Adw.ComboRow(
            title='Quantum', model=Gtk.StringList.new(
                [str(q) for q in QUANTA]))
        self.calc_quantum.set_selected(6)
        self.calc_result = Adw.ActionRow(title='Cycle latency')
        self.calc_label = Gtk.Label(label='—')
        self.calc_label.add_css_class('numeric-value')
        self.calc_label.set_valign(Gtk.Align.CENTER)
        self.calc_result.add_suffix(self.calc_label)
        self.calc_rate.connect('notify::selected', self._recalc)
        self.calc_quantum.connect('notify::selected', self._recalc)
        for r in (self.calc_rate, self.calc_quantum, self.calc_result):
            calc.add(r)
        self._recalc()

        # maintenance
        maint = group('Maintenance')
        for title, subtitle, cb, icon in (
            ('View app overrides',
             'Show every drop-in file this app has written',
             self._view_overrides, 'document-properties-symbolic'),
            ('Open config folder',
             str(config.XDG_CONFIG / 'pipewire'),
             lambda *_: self._open_folder(config.XDG_CONFIG / 'pipewire'),
             'folder-open-symbolic'),
            ('Open app data folder',
             str(config.XDG_CONFIG / 'pipewire-controller'),
             lambda *_: self._open_folder(
                 config.XDG_CONFIG / 'pipewire-controller'),
             'folder-open-symbolic'),
            ('Reset all overrides',
             'Delete every drop-in written by this app (chains are kept)',
             self._reset_overrides, 'edit-clear-all-symbolic'),
        ):
            row = Adw.ActionRow(title=title, subtitle=subtitle)
            btn = Gtk.Button(icon_name=icon, valign=Gtk.Align.CENTER)
            if title.startswith('Reset'):
                btn.add_css_class('destructive-action')
            btn.connect('clicked', cb)
            row.add_suffix(btn)
            row.set_activatable_widget(btn)
            maint.add(row)

        about = group('About')
        row = Adw.ActionRow(
            title='PipeWire Controller',
            subtitle='Config drop-ins, filter chains and HRIR management for '
                     'PipeWire — without hand-editing files.')
        ver = Gtk.Label(label=f'v{__version__}', css_classes=['dim-label'])
        row.add_suffix(ver)
        about.add(row)
        credit = Adw.ActionRow(
            title='Created by knightinfected',
            subtitle='github.com/knightinfected/PipeWireController',
            activatable=True)
        credit.add_suffix(Gtk.Image(icon_name='adw-external-link-symbolic'))
        credit.connect('activated', self._open_repo)
        about.add(credit)

        self.widget = page_scroller(svc, calc, maint, about)

    def _run_service(self, _b, fn, title):
        self.window.toast(f'{title}…')
        async_call(fn, lambda r, e: self.window.toast(
            'Done' if not e else f'Failed: {e}'))

    def _view_journal(self, _b):
        def work():
            return system.unit_journal('pipewire.service', 80)
        async_call(work, lambda r, e: text_viewer_dialog(
            self.window, 'PipeWire journal', r or '(empty)'))

    def _recalc(self, *_a):
        rate = RATES[self.calc_rate.get_selected()]
        quantum = QUANTA[self.calc_quantum.get_selected()]
        self.calc_label.set_label(f'{quantum / rate * 1000:.2f} ms')

    def _view_overrides(self, _b):
        parts = []
        for conf, dirs in (('pipewire.conf', config.PW_DIRS),
                           ('client.conf', config.PW_DIRS),
                           ('pipewire-pulse.conf', config.PW_DIRS),
                           ('wireplumber.conf', config.WP_DIRS)):
            p = config._dropin_path(conf, dirs)
            if p.is_file():
                parts.append(f'──── {p} ────\n{p.read_text()}')
        text_viewer_dialog(self.window, 'App-written drop-ins',
                           '\n'.join(parts) or
                           'No overrides written yet — everything is at '
                           'system defaults.')

    def _open_folder(self, path: Path):
        path.mkdir(parents=True, exist_ok=True)
        subprocess.Popen(['xdg-open', str(path)])

    def _open_repo(self, _row):
        subprocess.Popen(
            ['xdg-open', 'https://github.com/knightinfected/PipeWireController'])

    def _reset_overrides(self, _b):
        def do():
            removed = config.clear_all_overrides()
            self.window.toast(f'Removed {len(removed)} drop-in(s) — restart '
                              'the audio stack to go back to defaults')
            self.window.flag_restart('pipewire')
        confirm(self.window, 'Reset all overrides?',
                'Every setting drop-in written by this app is deleted. '
                'Filter chains and the HRIR library are not touched.',
                'Reset', do)
