// SPDX-License-Identifier: MIT
import Clutter from 'gi://Clutter';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import GObject from 'gi://GObject';
import St from 'gi://St';

import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as PanelMenu from 'resource:///org/gnome/shell/ui/panelMenu.js';
import * as PopupMenu from 'resource:///org/gnome/shell/ui/popupMenu.js';

import { Extension } from 'resource:///org/gnome/shell/extensions/extension.js';

// The newest `gpuq snapshot` schema this extension understands.
const SCHEMA = 1;
const COMMAND_TIMEOUT_SECONDS = 20;

function minutes(seconds) {
    let total = Math.max(0, Math.round(seconds / 60));
    return total >= 60 ? `${Math.floor(total / 60)}h ${total % 60}m` : `${total}m`;
}

function number(value, digits = 0) {
    return typeof value === 'number' && Number.isFinite(value) ? value.toFixed(digits) : null;
}

// Run a command through sh and resolve with its parsed snapshot JSON.
function runSnapshot(command, cancellable) {
    return new Promise((resolve, reject) => {
        let proc = Gio.Subprocess.new(['sh', '-c', command],
            Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_PIPE);
        let timer = GLib.timeout_add_seconds(GLib.PRIORITY_DEFAULT, COMMAND_TIMEOUT_SECONDS, () => {
            timer = 0;
            proc.force_exit();
            return GLib.SOURCE_REMOVE;
        });
        proc.communicate_utf8_async(null, cancellable, (p, res) => {
            if (timer)
                GLib.source_remove(timer);
            try {
                let [, stdout, stderr] = p.communicate_utf8_finish(res);
                if (!p.get_successful())
                    throw new Error((stderr || '').trim().split('\n').pop() || 'command failed');
                let view = JSON.parse(stdout);
                if (view.schema > SCHEMA)
                    throw new Error(`unsupported snapshot schema ${view.schema}`);
                resolve(view);
            } catch (e) {
                reject(e);
            }
        });
    });
}

const GpuqButton = GObject.registerClass(
class GpuqButton extends PanelMenu.Button {
    _init(settings) {
        super._init(0.0, 'gpuq');
        this._settings = settings;
        this._cancellable = new Gio.Cancellable();
        this._timer = 0;

        let box = new St.BoxLayout();
        box.add_child(new St.Icon({ icon_name: 'video-display-symbolic', style_class: 'system-status-icon' }));
        this._label = new St.Label({
            text: '…', y_align: Clutter.ActorAlign.CENTER, style_class: 'gpuq-label',
        });
        box.add_child(this._label);
        this.add_child(box);

        this._settingsIds = ['changed::sources', 'changed::refresh-interval'].map(
            signal => settings.connect(signal, () => this._schedule()));
        this._schedule();
    }

    _schedule() {
        if (this._timer)
            GLib.source_remove(this._timer);
        this._refresh();
        this._timer = GLib.timeout_add_seconds(GLib.PRIORITY_DEFAULT,
            this._settings.get_int('refresh-interval'), () => {
                this._refresh();
                return GLib.SOURCE_CONTINUE;
            });
    }

    async _refresh() {
        if (this._busy)
            return;
        this._busy = true;
        let sources = this._settings.get_strv('sources');
        let results = await Promise.all(sources.map(command =>
            runSnapshot(command, this._cancellable).then(
                view => ({ command, view }), error => ({ command, error }))));
        this._busy = false;
        if (!this._cancellable.is_cancelled())
            this._render(results);
    }

    _item(text, style = null) {
        let item = new PopupMenu.PopupMenuItem(text, { reactive: false });
        if (style)
            item.label.add_style_class_name(style);
        this.menu.addMenuItem(item);
    }

    _render(results) {
        this.menu.removeAll();
        let parts = [];
        let waiting = 0;
        let failed = 0;
        for (let { command, view, error } of results) {
            this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem(view?.host ?? command));
            if (error) {
                failed++;
                this._item(`⚠ ${error.message}`, 'gpuq-error');
                continue;
            }
            for (let slot of view.resources) {
                let telemetry = slot.telemetry && !slot.telemetry.stale ? slot.telemetry.stats ?? {} : {};
                let util = number(telemetry.utilization);
                let name = view.resources.length > 1 || results.length > 1 ? `${view.host}/${slot.name}` : slot.name;
                waiting += slot.queue.length;
                let active = slot.active;
                if (active) {
                    parts.push(util === null ? name : `${name} ${util}%`);
                    let state = active.state === 'running' ? '' : ` [${active.state}]`;
                    this._item(`${slot.name}: ${active.username} · ${active.experiment_id}${state}`, 'gpuq-host');
                    this._item(`    ${active.description} — ~${minutes(active.remaining_seconds)} left`, 'gpuq-detail');
                } else {
                    this._item(`${slot.name}: idle`, 'gpuq-idle');
                }
                let stats = [
                    util !== null ? `util ${util}%` : null,
                    number(telemetry.memory_used) !== null ? `mem ${number(telemetry.memory_used)}` : null,
                    number(telemetry.power) !== null ? `${number(telemetry.power)} W` : null,
                    number(telemetry.temperature) !== null ? `${number(telemetry.temperature)}°C` : null,
                ].filter(Boolean);
                if (stats.length)
                    this._item(`    ${stats.join(' · ')}`, 'gpuq-detail');
                for (let job of slot.queue)
                    this._item(`    ${job.position}. ${job.username} · ${job.experiment_id} — starts in ~${
                        minutes(job.remaining_seconds - job.expected_seconds)}`, 'gpuq-detail');
            }
        }
        if (!results.length)
            this._item('No sources configured', 'gpuq-idle');
        let text = parts.join('  ') || 'idle';
        if (waiting)
            text += ` +${waiting}`;
        if (failed)
            text += ' ⚠';
        this._label.set_text(text);
        this._label.style_class = parts.length ? 'gpuq-label' : 'gpuq-label gpuq-idle';

        this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
        let refresh = new PopupMenu.PopupMenuItem('Refresh now');
        refresh.connect('activate', () => this._refresh());
        this.menu.addMenuItem(refresh);
    }

    destroy() {
        this._cancellable.cancel();
        if (this._timer)
            GLib.source_remove(this._timer);
        this._timer = 0;
        this._settingsIds.forEach(id => this._settings.disconnect(id));
        super.destroy();
    }
});

export default class GpuqExtension extends Extension {
    enable() {
        this._settings = this.getSettings();
        this._place();
        this._positionId = this._settings.connect('changed::position-in-panel', () => this._place());
    }

    _place() {
        this._button?.destroy();
        this._button = new GpuqButton(this._settings);
        let position = this._settings.get_string('position-in-panel');
        Main.panel.addToStatusArea(this.uuid, this._button, position === 'right' ? 0 : -1, position);
    }

    disable() {
        this._settings.disconnect(this._positionId);
        this._button?.destroy();
        this._button = null;
        this._settings = null;
    }
}
