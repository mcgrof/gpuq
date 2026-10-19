// SPDX-License-Identifier: MIT
import Adw from 'gi://Adw';
import Gio from 'gi://Gio';
import Gtk from 'gi://Gtk';

import { ExtensionPreferences } from 'resource:///org/gnome/shell/extensions/prefs.js';

export default class GpuqPreferences extends ExtensionPreferences {
    fillPreferencesWindow(window) {
        let settings = this.getSettings();
        let page = new Adw.PreferencesPage({ title: 'gpuq', icon_name: 'video-display-symbolic' });
        window.add(page);

        let sources = new Adw.PreferencesGroup({
            title: 'Snapshot commands',
            description: 'One shell command per host; each must print `gpuq snapshot` JSON. ' +
                'GNOME Shell does not read your shell profile, so use absolute paths and set ' +
                'GPUQ_CONFIG, e.g. "GPUQ_CONFIG=/etc/gpuq.json /opt/gpuq/bin/gpuq snapshot" or ' +
                '"ssh -o BatchMode=yes gpu1 gpuq snapshot". Leave a row empty to remove it.',
        });
        page.add(sources);
        let rows = [];
        let save = () => settings.set_strv('sources',
            rows.map(row => row.get_text().trim()).filter(Boolean));
        let addRow = text => {
            let row = new Adw.EntryRow({ title: 'Command', show_apply_button: true });
            row.set_text(text);
            row.connect('apply', save);
            rows.push(row);
            sources.add(row);
        };
        settings.get_strv('sources').forEach(addRow);
        let add = new Gtk.Button({ label: 'Add host', halign: Gtk.Align.START, margin_top: 8 });
        add.connect('clicked', () => addRow(''));
        sources.set_header_suffix(add);

        let display = new Adw.PreferencesGroup({ title: 'Display' });
        page.add(display);
        let interval = new Adw.SpinRow({
            title: 'Refresh interval (seconds)',
            adjustment: new Gtk.Adjustment({ lower: 2, upper: 3600, step_increment: 1 }),
        });
        settings.bind('refresh-interval', interval, 'value', Gio.SettingsBindFlags.DEFAULT);
        display.add(interval);
        let positions = ['left', 'center', 'right'];
        let position = new Adw.ComboRow({
            title: 'Position in panel', model: Gtk.StringList.new(positions),
            selected: Math.max(0, positions.indexOf(settings.get_string('position-in-panel'))),
        });
        position.connect('notify::selected', () =>
            settings.set_string('position-in-panel', positions[position.selected]));
        display.add(position);
    }
}
