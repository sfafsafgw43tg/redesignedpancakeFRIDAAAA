/**
 * AntiDetect Emulator - Frontend JavaScript
 */

// Socket.IO connection (the CDN may be unreachable in an offline desktop
// app; the UI also polls, so a no-op stub keeps everything working).
const socket = (typeof io !== 'undefined')
    ? io()
    : { on: () => {}, emit: () => {} };

// State
let devices = [];
let models = {};

// Camera defaults saved via ⚙️ Settings; prefill for the Create Device form.
let cameraDefaults = { front: 'emulated', back: 'virtualscene' };

// Auto-root default saved via ⚙️ Settings; prefill for the Create Device form.
let autoRootDefault = true;

// Initialize
document.addEventListener('DOMContentLoaded', () => {
    loadDevices();
    loadModels();
    refreshStatus();  // Check antidetect status

    // Socket events
    socket.on('connect', () => console.log('Connected to server'));
    socket.on('devices_updated', (data) => {
        devices = data;
        renderDevices();
    });

    // Auto-refresh status every 10 seconds
    setInterval(refreshStatus, 10000);
});

// =========================================
// ANTIDETECT STATUS FUNCTIONS
// =========================================

async function refreshStatus() {
    await checkAdbStatus();
    await checkFridaStatus();
}

async function checkAdbStatus() {
    try {
        const response = await fetch('/api/status/adb');
        const data = await response.json();

        const card = document.getElementById('adbStatus');
        const indicator = card.querySelector('.status-indicator');
        const text = card.querySelector('.status-text');

        if (data.connected) {
            indicator.className = 'status-indicator connected';
            text.textContent = `Connected (${data.devices.length} device${data.devices.length > 1 ? 's' : ''})`;
        } else {
            indicator.className = 'status-indicator disconnected';
            text.textContent = data.error || 'No devices connected';
        }
    } catch (error) {
        console.error('ADB status check failed:', error);
    }
}

function currentDeviceSerial() {
    // Prefer a running device, then any device that already knows its serial.
    const ordered = [...devices].sort((a, b) => (b.running ? 1 : 0) - (a.running ? 1 : 0));
    for (const device of ordered) {
        if (device.serial) return device.serial;
    }
    return null;
}

async function checkFridaStatus() {
    try {
        const serial = currentDeviceSerial();
        const url = serial
            ? `/api/status/frida?serial=${encodeURIComponent(serial)}`
            : '/api/status/frida';
        const response = await fetch(url);
        const data = await response.json();

        const card = document.getElementById('fridaStatus');
        const indicator = card.querySelector('.status-indicator');
        const text = card.querySelector('.status-text');
        const installBtn = document.getElementById('fridaInstallBtn');
        const fileBtn = document.getElementById('fridaFileBtn');

        if (!data.serial) {
            indicator.className = 'status-indicator disconnected';
            text.textContent = 'No device (start an emulator)';
            installBtn.style.display = 'block';
            installBtn.textContent = 'Install';
            fileBtn.style.display = 'block';
            return;
        }

        if (data.running) {
            indicator.className = 'status-indicator connected';
            text.textContent = `Running on ${data.serial} (v${data.version})`;
            installBtn.style.display = 'none';
            fileBtn.style.display = 'none';
        } else if (data.installed) {
            indicator.className = 'status-indicator warning';
            text.textContent = `Installed on ${data.serial} (not running)`;
            installBtn.style.display = 'block';
            installBtn.textContent = 'Start Frida';
            fileBtn.style.display = 'none';
        } else if (data.downloaded) {
            indicator.className = 'status-indicator warning';
            text.textContent = 'Downloaded (not installed)';
            installBtn.style.display = 'block';
            installBtn.textContent = 'Install Frida';
            fileBtn.style.display = 'block';
        } else {
            indicator.className = 'status-indicator disconnected';
            text.textContent = `Not installed (${data.serial})`;
            installBtn.style.display = 'block';
            installBtn.textContent = 'Download & Install';
            fileBtn.style.display = 'block';
        }

        if (data.last_error) {
            installBtn.title = data.last_error;
        }
    } catch (error) {
        console.error('Frida status check failed:', error);
    }
}

async function installFrida(localPath) {
    const btn = document.getElementById('fridaInstallBtn');
    const fileBtn = document.getElementById('fridaFileBtn');
    const previousLabel = btn.textContent;
    btn.disabled = true;
    if (fileBtn) fileBtn.disabled = true;
    btn.textContent = 'Installing...';

    const payload = {};
    const serial = currentDeviceSerial();
    if (serial) payload.serial = serial;
    if (localPath) payload.local_path = localPath;

    try {
        const response = await fetch('/api/frida/install', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        });
        const data = await response.json();
        if (!data.success) {
            showToast(data.error || 'Installation failed to start', 'error');
            btn.disabled = false;
            btn.textContent = previousLabel;
            return;
        }
        showToast('Installing frida-server on the device…', 'info');
        await pollFridaInstall();
    } catch (error) {
        showToast('Installation failed: ' + error, 'error');
    } finally {
        btn.disabled = false;
        btn.textContent = previousLabel;
        if (fileBtn) fileBtn.disabled = false;
        await checkFridaStatus();
    }
}

async function pollFridaInstall() {
    for (let attempt = 0; attempt < 240; attempt++) {  // ~6 minutes
        await new Promise(resolve => setTimeout(resolve, 1500));
        let status;
        try {
            const response = await fetch('/api/frida/install/status');
            status = await response.json();
        } catch (error) {
            continue;
        }
        if (status.running) continue;

        if (status.ok) {
            showToast('Frida server installed and running', 'success');
        } else {
            const message = status.error || 'Installation failed';
            showToast(message, 'error');
            console.error('frida install log:\n' + (status.log || ''));
            if (status.diagnostics) console.error('frida diagnostics:', status.diagnostics);
        }
        return;
    }
    showToast('Frida installation is taking unusually long - check the terminal output', 'error');
}

async function installFridaFromFile() {
    try {
        const response = await fetch('/api/frida/pick-server', { method: 'POST' });
        const data = await response.json();
        if (!data.success || !data.path) {
            showToast(data.error || 'No file selected', 'error');
            return;
        }
        await installFrida(data.path);
    } catch (error) {
        showToast('Could not open the file picker', 'error');
    }
}

async function applyAntidetect(name) {
    showToast('Applying antidetect...', 'info');

    try {
        const response = await fetch(`/api/devices/${name}/apply-antidetect`, {
            method: 'POST'
        });
        const data = await response.json();

        if (data.success) {
            showToast(data.message || 'Antidetect applied!', 'success');

            // Update spoofing status
            const card = document.getElementById('spoofStatus');
            const indicator = card.querySelector('.status-indicator');
            const text = card.querySelector('.status-text');
            indicator.className = 'status-indicator connected';
            text.textContent = 'Applied to ' + name + (data.serial ? ' (' + data.serial + ')' : '');
            loadDevices();
        } else {
            showToast(data.error || 'Failed to apply antidetect', 'error');
        }
    } catch (error) {
        showToast('Failed to apply antidetect', 'error');
    }
}

async function applyFridaHooks(name) {
    showToast('Applying Frida hooks...', 'info');

    try {
        const response = await fetch(`/api/devices/${name}/apply-frida-hooks`, {
            method: 'POST'
        });
        const data = await response.json();

        if (data.success) {
            showToast(data.message || 'Frida hooks applied!', 'success');
            await checkFridaStatus();
        } else {
            showToast(data.error || 'Failed to apply hooks', 'error');
        }
    } catch (error) {
        showToast('Failed to apply Frida hooks', 'error');
    }
}

// API Functions
async function loadDevices() {
    try {
        const response = await fetch('/api/devices');
        devices = await response.json();
        renderDevices();
        updateStats();
    } catch (error) {
        showToast('Failed to load devices', 'error');
    }
}

async function loadModels() {
    try {
        const response = await fetch('/api/models');
        models = await response.json();
        populateModelSelect();
    } catch (error) {
        console.error('Failed to load models:', error);
    }
}

function populateModelSelect() {
    const select = document.getElementById('phoneModel');
    select.innerHTML = '';

    for (const [brand, brandModels] of Object.entries(models)) {
        const group = document.createElement('optgroup');
        group.label = brand;

        brandModels.forEach(model => {
            const option = document.createElement('option');
            option.value = model.name;
            option.textContent = model.name;
            group.appendChild(option);
        });

        select.appendChild(group);
    }
}

function useWebcamForBoth() {
    document.getElementById('cameraFront').value = 'webcam0';
    document.getElementById('cameraBack').value = 'webcam0';
    showToast('Both cameras set to webcam0', 'info');
}

function updateImageVariantHint() {
    const select = document.getElementById('imageVariant');
    const hint = document.getElementById('imageVariantHint');
    if (!select || !hint) return;
    const autoRoot = document.getElementById('autoRoot');
    const autoRootHint = document.getElementById('autoRootHint');
    if (select.value === 'google_apis_playstore') {
        hint.innerHTML = '<strong>⚠ No root:</strong> frida-server and property spoofing ' +
            'will <em>not</em> work on this device. Pick "Google APIs" if you need Frida.';
        if (autoRoot) {
            autoRoot.checked = false;
            autoRoot.disabled = true;
        }
        if (autoRootHint) {
            autoRootHint.innerHTML = '⚠ Play Store images refuse <code>adb root</code> - ' +
                'automatic rooting is disabled for this image.';
        }
    } else {
        hint.innerHTML = 'Frida and property spoofing need <code>adb root</code>, which only ' +
            'the Google APIs (userdebug) image allows.';
        if (autoRoot) {
            autoRoot.disabled = false;
            autoRoot.checked = autoRootDefault;
        }
        if (autoRootHint) {
            autoRootHint.innerHTML = 'Runs <code>adb root</code> as soon as the device boots, ' +
                'so Frida hooks and property spoofing work without any manual step.';
        }
    }
}

async function loadAdbStatus() {
    const hint = document.getElementById('adbStatusHint');
    const log = document.getElementById('adbRepairLog');
    try {
        const response = await fetch('/api/adb/status');
        const data = await response.json();
        const version = (data.version || 'unknown').split('\n')[0];
        if (hint) hint.textContent = `adb: ${data.path} — ${version}`;
        if (log) log.textContent = data.listing || '';
    } catch (error) {
        if (hint) hint.textContent = 'Could not read adb status';
    }
}

async function repairAdb() {
    const button = document.getElementById('repairAdbBtn');
    const log = document.getElementById('adbRepairLog');
    if (button) { button.disabled = true; button.textContent = 'Repairing…'; }
    if (log) log.textContent = 'Reconnecting…';
    try {
        const response = await fetch('/api/adb/repair', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({})
        });
        const data = await response.json();
        if (log) log.textContent = (data.devices || '') + '\n' + (data.detail || '');
        if (data.success) {
            showToast(`ADB connected to ${data.serial}`, 'success');
        } else {
            showToast(data.detail || `ADB could not recover the device (state: ${data.state})`, 'error');
        }
        if (typeof loadDevices === 'function') loadDevices();
    } catch (error) {
        showToast('ADB repair failed: ' + error, 'error');
    } finally {
        if (button) { button.disabled = false; button.textContent = 'Repair ADB'; }
    }
}

async function createDevice() {
    const name = document.getElementById('deviceName').value.trim();
    const model = document.getElementById('phoneModel').value;
    const android = document.getElementById('androidVersion').value;
    const proxy = document.getElementById('proxyAddress').value.trim();
    const cameraFront = document.getElementById('cameraFront').value.trim();
    const cameraBack = document.getElementById('cameraBack').value.trim();
    const imageVariantEl = document.getElementById('imageVariant');
    const imageVariant = imageVariantEl ? imageVariantEl.value : undefined;
    const autoRootEl = document.getElementById('autoRoot');
    const autoRoot = autoRootEl ? autoRootEl.checked : undefined;

    if (!model) {
        showToast('Please select a phone model', 'error');
        return;
    }

    try {
        const response = await fetch('/api/devices', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                name: name || undefined,
                model: model,
                android_version: android,
                proxy: proxy || undefined,
                camera_front: cameraFront || undefined,
                camera_back: cameraBack || undefined,
                image_variant: imageVariant || undefined,
                auto_root: autoRoot
            })
        });

        const data = await response.json();

        if (data.success) {
            showToast('Device created successfully!', 'success');
            if (data.warning) {
                showToast(data.warning, 'error');
            }
            hideCreateDialog();
            loadDevices();

            // Clear form (cameras go back to the saved defaults)
            document.getElementById('deviceName').value = '';
            document.getElementById('proxyAddress').value = '';
            document.getElementById('cameraFront').value = cameraDefaults.front || 'emulated';
            document.getElementById('cameraBack').value = cameraDefaults.back || 'virtualscene';
        } else {
            showToast(data.error || 'Failed to create device', 'error');
        }
    } catch (error) {
        showToast('Failed to create device', 'error');
    }
}

async function launchDevice(name) {
    try {
        const response = await fetch(`/api/devices/${name}/launch`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ frida: true })
        });

        const data = await response.json();

        if (!data.success) {
            showToast(data.error || 'Failed to launch device', 'error');
            return;
        }

        showToast(`Launching ${name}... waiting for boot`, 'success');
        pollLaunchStatus(name, 0);
    } catch (error) {
        showToast('Failed to launch device', 'error');
    }
}

// Poll the async launch state so the UI can report boot progress and the
// real emulator error instead of hanging on the launch request.
async function pollLaunchStatus(name, attempt) {
    const maxAttempts = 90;  // ~3 minutes at 2s intervals
    try {
        const response = await fetch(`/api/devices/${name}/status`);
        const status = await response.json();

        if (status.state === 'error') {
            showToast(`Launch failed: ${status.error}`, 'error');
            console.error('Emulator log tail:\n' + (status.log_tail || ''));
            loadDevices();
            return;
        }

        if (status.state === 'running' || status.running) {
            showToast(`${name} is running (${status.serial || 'emulator'})`, 'success');
            loadDevices();
            return;
        }

        if (attempt < maxAttempts) {
            setTimeout(() => pollLaunchStatus(name, attempt + 1), 2000);
        } else {
            showToast(`${name} is still booting - check the log if it never appears`, 'error');
            loadDevices();
        }
    } catch (error) {
        if (attempt < maxAttempts) {
            setTimeout(() => pollLaunchStatus(name, attempt + 1), 2000);
        }
    }
}

async function stopDevice(name) {
    try {
        const response = await fetch(`/api/devices/${name}/stop`, {
            method: 'POST'
        });

        const data = await response.json();

        if (data.success) {
            showToast('Device stopped', 'success');
            loadDevices();
        } else {
            showToast(data.error || 'Failed to stop device', 'error');
        }
    } catch (error) {
        showToast('Failed to stop device', 'error');
    }
}

async function deleteDevice(name) {
    if (!confirm(`Delete device "${name}"? This cannot be undone.`)) {
        return;
    }

    try {
        const response = await fetch(`/api/devices/${name}`, {
            method: 'DELETE'
        });

        const data = await response.json();

        if (data.success) {
            showToast('Device deleted', 'success');
            loadDevices();
        } else {
            showToast(data.error || 'Failed to delete device', 'error');
        }
    } catch (error) {
        showToast('Failed to delete device', 'error');
    }
}

async function showDeviceDetails(name) {
    try {
        const device = devices.find(d => d.name === name);
        const identityResponse = await fetch(`/api/device/${name}/identity`);
        const identity = await identityResponse.json();

        const modal = document.getElementById('detailsModal');
        const content = document.getElementById('deviceDetails');

        content.innerHTML = `
            <div class="device-details-header">
                <h4>${device.model}</h4>
                <p>Android ${device.android_version} • ${device.brand}</p>
                <p style="font-size: 0.8rem; color: var(--text-secondary);">
                    Image: <code>${device.image || 'n/a'}</code>
                    ${device.rootable === false
                        ? ' — Play Store image: no adb root, so frida-server cannot run.'
                        : ''}
                </p>
                <p style="font-size: 0.8rem;">
                    Root: ${device.rootable === false
                        ? '<span style="color: var(--accent-danger);">unavailable (Play Store image)</span>'
                        : (device.rooted === true
                            ? '<span style="color: var(--accent-success);">✓ rooted via adb (shell/Frida are uid=0)' +
                              (device.rooted_at ? ' since ' + device.rooted_at.slice(0, 19).replace('T', ' ') : '') +
                              '</span>'
                            : (device.rooted === false && device.root_error
                                ? '<span style="color: var(--accent-danger);">⚠ ' + device.root_error + '</span>'
                                : (device.auto_root !== false
                                    ? '<span style="color: var(--accent-success);">will root automatically on launch</span>'
                                    : '<span>disabled for this device</span>')))}
                    ${device.rooted === true
                        ? '<br><small class="hint">Root-checker apps still say "not rooted": there is no su binary on purpose - adb/Frida have root (verify: adb shell id). Apps probing Settings.Global.adb_enabled read 0 via the hooks.</small>'
                        : ''}
                </p>
            </div>
            <h5 style="margin: 1rem 0 0.75rem; color: var(--text-secondary);">Device Identity</h5>
            <div class="details-grid">
                <div class="details-item">
                    <div class="details-label">IMEI</div>
                    <div class="details-value">${identity.imei || 'N/A'}</div>
                </div>
                <div class="details-item">
                    <div class="details-label">Serial Number</div>
                    <div class="details-value">${identity.serial || 'N/A'}</div>
                </div>
                <div class="details-item">
                    <div class="details-label">Android ID</div>
                    <div class="details-value">${identity.android_id || 'N/A'}</div>
                </div>
                <div class="details-item">
                    <div class="details-label">MAC Address</div>
                    <div class="details-value">${identity.mac_address || 'N/A'}</div>
                </div>
                <div class="details-item">
                    <div class="details-label">Build Fingerprint</div>
                    <div class="details-value" style="font-size: 0.75rem;">${identity.fingerprint || 'N/A'}</div>
                </div>
                <div class="details-item">
                    <div class="details-label">Carrier</div>
                    <div class="details-value">${identity.operator || 'N/A'}</div>
                </div>
            </div>
            <h5 style="margin: 1rem 0 0.75rem; color: var(--text-secondary);">Cameras</h5>
            <div class="camera-grid">
                <div>
                    <small class="hint">Back camera</small>
                    <input type="text" id="detailsCameraBack" list="cameraOptions"
                        value="${(device.camera && device.camera.back) || 'virtualscene'}">
                </div>
                <div>
                    <small class="hint">Front camera</small>
                    <input type="text" id="detailsCameraFront" list="cameraOptions"
                        value="${(device.camera && device.camera.front) || 'emulated'}">
                </div>
            </div>
            <div style="margin-top: 0.6rem; display: flex; gap: 0.5rem;">
                <button class="btn btn-small btn-secondary" onclick="useWebcamForBothDetails()">
                    Use webcam0 for both
                </button>
                <button class="btn btn-small btn-primary" onclick="saveCameras('${device.name}')">
                    Save cameras
                </button>
            </div>
            <small class="hint">webcam0 passes your real webcam through to the emulator.
                ${device.running ? 'Restart the device to apply changes.' : ''}</small>
            ${device.proxy ? `
                <h5 style="margin: 1rem 0 0.75rem; color: var(--text-secondary);">Network</h5>
                <div class="details-grid">
                    <div class="details-item">
                        <div class="details-label">Proxy</div>
                        <div class="details-value">${device.proxy}</div>
                    </div>
                </div>
            ` : ''}
        `;

        modal.classList.add('active');
    } catch (error) {
        showToast('Failed to load device details', 'error');
    }
}

function useWebcamForBothDetails() {
    document.getElementById('detailsCameraFront').value = 'webcam0';
    document.getElementById('detailsCameraBack').value = 'webcam0';
}

async function saveCameras(name) {
    const front = document.getElementById('detailsCameraFront').value.trim();
    const back = document.getElementById('detailsCameraBack').value.trim();

    try {
        const response = await fetch(`/api/devices/${name}/camera`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ front, back })
        });
        const data = await response.json();

        if (data.success) {
            const restart = data.needs_restart ? ' Restart the device to apply.' : '';
            showToast(`Cameras saved: front=${data.camera.front}, back=${data.camera.back}.${restart}`, 'success');
            await loadDevices();
        } else {
            showToast(data.error || 'Invalid camera settings', 'error');
        }
    } catch (error) {
        showToast('Failed to save cameras', 'error');
    }
}

// Render Functions
function renderDevices() {
    const grid = document.getElementById('devicesGrid');

    if (devices.length === 0) {
        grid.innerHTML = `
            <div class="empty-state">
                <div class="empty-state-icon">📱</div>
                <h3>No devices yet</h3>
                <p>Create your first anti-detect device to get started</p>
            </div>
        `;
        return;
    }

    grid.innerHTML = devices.map(device => `
        <div class="device-card">
            <div class="device-header">
                <div class="device-info">
                    <h3>${device.name}</h3>
                    <p class="device-model">${device.model}</p>
                </div>
                <div class="device-status ${device.starting ? 'starting' : (device.running ? 'running' : 'stopped')}">
                    <span class="status-dot"></span>
                    ${device.starting ? 'Starting…' : (device.running ? 'Running' : 'Stopped')}
                </div>
            </div>
            <div class="device-details">
                <span class="device-tag">Android ${device.android_version}</span>
                <span class="device-tag">${device.brand}</span>
                ${device.proxy ? '<span class="device-tag">🔒 Proxy</span>' : ''}
                ${device.rootable === false
                    ? '<span class="device-tag tag-warn" title="Play Store image: adb root unavailable, Frida will not work">⚠ No root</span>'
                    : (device.rooted === true
                        ? '<span class="device-tag tag-ok" title="Rooted with adb root (auto-root after boot)">✓ Root</span>'
                        : (device.rooted === false && device.root_error
                            ? `<span class="device-tag tag-warn" title="${device.root_error.replace(/"/g, '&quot;')}">⚠ Root failed</span>`
                            : '<span class="device-tag tag-ok" title="Google APIs image: adb root available (auto-roots on launch)">✓ Root capable</span>'))}
            </div>
            <div class="device-actions">
                ${device.starting ? `
                    <span class="hint">auto-root + spoof + hooks are applying…</span>
                    <button class="btn btn-danger btn-sm" onclick="stopDevice('${device.name}')">Stop</button>
                ` : device.running ? `
                    <button class="btn btn-primary btn-sm" onclick="applyAntidetect('${device.name}')" title="Apply device spoofing">🎭 Spoof</button>
                    <button class="btn btn-warning btn-sm" onclick="applyFridaHooks('${device.name}')" title="Apply Frida hooks">🔧 Hooks</button>
                    <button class="btn btn-danger btn-sm" onclick="stopDevice('${device.name}')">Stop</button>
                ` : `
                    <button class="btn btn-success btn-sm" onclick="launchDevice('${device.name}')">🚀 Launch</button>
                `}
                <button class="btn btn-secondary btn-sm" onclick="showDeviceDetails('${device.name}')">📋 Details</button>
                <button class="btn btn-icon btn-sm" onclick="deleteDevice('${device.name}')" title="Delete">🗑️</button>
            </div>
        </div>
    `).join('');

    updateStats();
}

function updateStats() {
    document.getElementById('totalDevices').textContent = devices.length;
    document.getElementById('runningDevices').textContent = devices.filter(d => d.running).length;
    // Honest count: only devices whose hook chain actually completed.
    document.getElementById('protectedDevices').textContent =
        devices.filter(d => d.running && d.hooks_applied === true).length;
}

// Modal Functions
function showCreateDialog() {
    // Prefill camera fields + auto-root with the defaults chosen in ⚙️ Settings.
    document.getElementById('cameraFront').value = cameraDefaults.front || 'emulated';
    document.getElementById('cameraBack').value = cameraDefaults.back || 'virtualscene';
    const autoRoot = document.getElementById('autoRoot');
    if (autoRoot) autoRoot.checked = autoRootDefault;
    updateImageVariantHint();
    document.getElementById('createModal').classList.add('active');
}

function hideCreateDialog() {
    document.getElementById('createModal').classList.remove('active');
}

function hideDetailsModal() {
    document.getElementById('detailsModal').classList.remove('active');
}

let sdkInstallPoller = null;

async function showSettings() {
    document.getElementById('settingsModal').classList.add('active');
    await loadSettings();
    await loadAdbStatus();
}

function hideSettings() {
    document.getElementById('settingsModal').classList.remove('active');
    if (sdkInstallPoller) {
        clearInterval(sdkInstallPoller);
        sdkInstallPoller = null;
    }
}

async function loadSettings() {
    const hint = document.getElementById('sdkPathHint');
    try {
        const response = await fetch('/api/settings');
        const data = await response.json();

        const input = document.getElementById('sdkPathInput');
        if (data.sdk_root) {
            input.value = data.sdk_root;
        }

        const missing = [];
        if (!data.emulator_found) missing.push('emulator');
        if (!data.adb_found) missing.push('adb');
        if (!data.avdmanager_found) missing.push('avdmanager');

        if (missing.length === 0) {
            hint.className = 'hint ok';
            hint.textContent = `SDK ready (${data.sdk_root_source}). AVDs: ${(data.avds || []).length}`;
        } else if (data.sdk_root) {
            hint.className = 'hint warn';
            hint.textContent = `SDK found (${data.sdk_root_source}) but missing: ${missing.join(', ')}. Use Install below.`;
        } else {
            hint.className = 'hint warn';
            hint.textContent = 'No SDK selected yet. Click Browse… and pick your Android SDK folder.';
        }

        const versionSelect = document.getElementById('sdkInstallVersion');
        if (versionSelect.options.length === 0) {
            const vres = await fetch('/api/android-versions');
            const versions = await vres.json();
            versionSelect.innerHTML = '';
            for (const v of versions) {
                const option = document.createElement('option');
                option.value = v.version;
                option.textContent = `Android ${v.version} (API ${v.api})${v.installed ? ' ✓ installed' : ''}`;
                if (v.installed) option.selected = true;
                versionSelect.appendChild(option);
            }
        }

        const desktopHint = document.getElementById('desktopHint');
        desktopHint.className = 'hint';
        if (data.desktop_mode) {
            desktopHint.textContent = 'Running as a desktop app window.';
        } else if (data.desktop_available) {
            desktopHint.textContent = 'Desktop window available — start with "python main.py gui --desktop" (or START.bat on Windows).';
        } else {
            desktopHint.textContent = 'Browser mode. For a standalone app window run "pip install pywebview" then "python main.py gui --desktop" (START.bat does it for you on Windows).';
        }

        // Default cameras (saved in Settings, used for new devices)
        const savedCameras = (data.settings) || {};
        if (savedCameras.camera_front) cameraDefaults.front = savedCameras.camera_front;
        if (savedCameras.camera_back) cameraDefaults.back = savedCameras.camera_back;
        const settingsFront = document.getElementById('settingsCameraFront');
        const settingsBack = document.getElementById('settingsCameraBack');
        if (settingsFront) settingsFront.value = cameraDefaults.front;
        if (settingsBack) settingsBack.value = cameraDefaults.back;

        // Auto-root default (saved in Settings, used for new devices)
        if (typeof savedCameras.auto_root === 'boolean') {
            autoRootDefault = savedCameras.auto_root;
        }
        const settingsAutoRoot = document.getElementById('settingsAutoRoot');
        if (settingsAutoRoot) settingsAutoRoot.checked = autoRootDefault;
    } catch (error) {
        hint.className = 'hint warn';
        hint.textContent = 'Could not load settings: ' + error;
    }
}

async function saveAutoRoot() {
    const btn = document.getElementById('saveAutoRootBtn');
    const enabled = document.getElementById('settingsAutoRoot').checked;

    btn.disabled = true;
    try {
        const response = await fetch('/api/settings/auto-root', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ auto_root: enabled })
        });
        const data = await response.json();

        if (data.success) {
            autoRootDefault = enabled;
            showToast(data.message || 'Auto-root setting saved', 'success');
        } else {
            showToast(data.error || 'Failed to save auto-root setting', 'error');
        }
    } catch (error) {
        showToast('Failed to save auto-root setting', 'error');
    }
    btn.disabled = false;
}

function useWebcamForBothSettings() {
    document.getElementById('settingsCameraFront').value = 'webcam0';
    document.getElementById('settingsCameraBack').value = 'webcam0';
    showToast('Both cameras set to webcam0 - click "Save defaults"', 'info');
}

async function saveCameraDefaults() {
    const btn = document.getElementById('saveCameraDefaultsBtn');
    const front = document.getElementById('settingsCameraFront').value.trim();
    const back = document.getElementById('settingsCameraBack').value.trim();

    btn.disabled = true;
    try {
        const response = await fetch('/api/settings/cameras', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ camera_front: front, camera_back: back })
        });
        const data = await response.json();

        if (data.success) {
            cameraDefaults = { front: data.camera_front, back: data.camera_back };
            showToast(data.message || 'Default cameras saved', 'success');
        } else {
            showToast(data.error || 'Failed to save camera defaults', 'error');
        }
    } catch (error) {
        showToast('Failed to save camera defaults', 'error');
    }
    btn.disabled = false;
}

async function browseSdkFolder() {
    showToast('Opening folder picker…', 'info');
    try {
        const response = await fetch('/api/settings/pick-sdk', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({})
        });
        const data = await response.json();
        if (data.success && data.path) {
            document.getElementById('sdkPathInput').value = data.path;
            showToast('Folder selected - click Save', 'success');
        } else {
            showToast(data.error || 'No folder selected', 'error');
        }
    } catch (error) {
        showToast('Could not open the folder picker', 'error');
    }
}

async function saveSdkPath() {
    const path = document.getElementById('sdkPathInput').value.trim();
    if (!path) {
        showToast('Enter or browse to your Android SDK folder', 'error');
        return;
    }
    try {
        const response = await fetch('/api/settings/sdk', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ path })
        });
        const data = await response.json();
        if (data.success) {
            showToast('SDK folder saved', 'success');
            await loadSettings();
            await refreshStatus();
            loadDevices();
        } else {
            showToast(data.error || 'Invalid SDK folder', 'error');
        }
    } catch (error) {
        showToast('Failed to save the SDK folder', 'error');
    }
}

async function installSdkComponents() {
    const versionSelect = document.getElementById('sdkInstallVersion');
    const log = document.getElementById('sdkInstallLog');
    const button = document.getElementById('sdkInstallBtn');
    const path = document.getElementById('sdkPathInput').value.trim();

    if (!path) {
        showToast('Pick your Android SDK folder first', 'error');
        return;
    }

    log.classList.add('visible');
    log.textContent = 'Starting installation…';
    button.disabled = true;

    try {
        const response = await fetch('/api/sdk/install', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ android_version: versionSelect.value, path })
        });
        const data = await response.json();
        if (!data.success) {
            showToast(data.error || 'Installation failed to start', 'error');
            button.disabled = false;
            return;
        }
    } catch (error) {
        showToast('Installation failed to start', 'error');
        button.disabled = false;
        return;
    }

    if (sdkInstallPoller) clearInterval(sdkInstallPoller);
    sdkInstallPoller = setInterval(async () => {
        try {
            const response = await fetch('/api/sdk/install/status');
            const status = await response.json();
            log.textContent = status.log || 'Working…';
            log.scrollTop = log.scrollHeight;
            if (!status.running) {
                clearInterval(sdkInstallPoller);
                sdkInstallPoller = null;
                button.disabled = false;
                if (status.error) {
                    showToast('Install finished with errors', 'error');
                } else {
                    showToast('SDK components installed', 'success');
                }
                await loadSettings();
                await refreshStatus();
            }
        } catch (error) {
            clearInterval(sdkInstallPoller);
            sdkInstallPoller = null;
            button.disabled = false;
        }
    }, 1500);
}

function showHelp() {
    showToast('Help documentation coming soon!', 'info');
}

// Toast Notifications
function showToast(message, type = 'info') {
    const container = document.getElementById('toastContainer');

    const toast = document.createElement('div');
    toast.className = `toast ${type}`;

    const icons = {
        success: '✅',
        error: '❌',
        info: 'ℹ️'
    };

    toast.innerHTML = `
        <span class="toast-icon">${icons[type] || icons.info}</span>
        <span>${message}</span>
    `;

    // Click a toast to dismiss it immediately (handy for long diagnostics).
    toast.addEventListener('click', () => toast.remove());

    container.appendChild(toast);

    // Errors carry actionable diagnostics — keep them readable much longer.
    const duration = type === 'error' ? 12000 : 3000;
    setTimeout(() => {
        toast.style.animation = 'slideIn 0.3s ease reverse';
        setTimeout(() => toast.remove(), 300);
    }, duration);
}

// Close modals on outside click
document.querySelectorAll('.modal').forEach(modal => {
    modal.addEventListener('click', (e) => {
        if (e.target === modal) {
            modal.classList.remove('active');
        }
    });
});

// Keyboard shortcuts
document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
        document.querySelectorAll('.modal.active').forEach(modal => {
            modal.classList.remove('active');
        });
    }
});
