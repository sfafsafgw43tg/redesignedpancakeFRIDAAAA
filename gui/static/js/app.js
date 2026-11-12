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

async function checkFridaStatus() {
    try {
        const response = await fetch('/api/status/frida');
        const data = await response.json();

        const card = document.getElementById('fridaStatus');
        const indicator = card.querySelector('.status-indicator');
        const text = card.querySelector('.status-text');
        const installBtn = document.getElementById('fridaInstallBtn');

        if (data.running) {
            indicator.className = 'status-indicator connected';
            text.textContent = `Running (v${data.version})`;
            installBtn.style.display = 'none';
        } else if (data.installed) {
            indicator.className = 'status-indicator warning';
            text.textContent = 'Installed (not running)';
            installBtn.style.display = 'block';
            installBtn.textContent = 'Start Frida';
        } else if (data.downloaded) {
            indicator.className = 'status-indicator warning';
            text.textContent = 'Downloaded (not installed)';
            installBtn.style.display = 'block';
            installBtn.textContent = 'Install Frida';
        } else {
            indicator.className = 'status-indicator disconnected';
            text.textContent = 'Not installed';
            installBtn.style.display = 'block';
            installBtn.textContent = 'Download & Install';
        }
    } catch (error) {
        console.error('Frida status check failed:', error);
    }
}

async function installFrida() {
    const btn = document.getElementById('fridaInstallBtn');
    btn.disabled = true;
    btn.textContent = 'Installing...';

    try {
        const response = await fetch('/api/frida/install', { method: 'POST' });
        const data = await response.json();

        if (data.success) {
            showToast('Frida server installed successfully!', 'success');
        } else {
            showToast(data.error || 'Installation failed', 'error');
        }
    } catch (error) {
        showToast('Installation failed', 'error');
    }

    btn.disabled = false;
    await checkFridaStatus();
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
            text.textContent = 'Applied to ' + name;
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

async function createDevice() {
    const name = document.getElementById('deviceName').value.trim();
    const model = document.getElementById('phoneModel').value;
    const android = document.getElementById('androidVersion').value;
    const proxy = document.getElementById('proxyAddress').value.trim();

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
                proxy: proxy || undefined
            })
        });

        const data = await response.json();

        if (data.success) {
            showToast('Device created successfully!', 'success');
            hideCreateDialog();
            loadDevices();

            // Clear form
            document.getElementById('deviceName').value = '';
            document.getElementById('proxyAddress').value = '';
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
                <div class="device-status ${device.running ? 'running' : 'stopped'}">
                    <span class="status-dot"></span>
                    ${device.running ? 'Running' : 'Stopped'}
                </div>
            </div>
            <div class="device-details">
                <span class="device-tag">Android ${device.android_version}</span>
                <span class="device-tag">${device.brand}</span>
                ${device.proxy ? '<span class="device-tag">🔒 Proxy</span>' : ''}
            </div>
            <div class="device-actions">
                ${device.running ? `
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
    document.getElementById('protectedDevices').textContent = devices.length; // All devices have anti-detect
}

// Modal Functions
function showCreateDialog() {
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
    } catch (error) {
        hint.className = 'hint warn';
        hint.textContent = 'Could not load settings: ' + error;
    }
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

    container.appendChild(toast);

    setTimeout(() => {
        toast.style.animation = 'slideIn 0.3s ease reverse';
        setTimeout(() => toast.remove(), 300);
    }, 3000);
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
