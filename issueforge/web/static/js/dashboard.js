// Modal controls
function openModal() {
    document.getElementById('taskModal').classList.remove('hidden');
}

function closeModal() {
    document.getElementById('taskModal').classList.add('hidden');
}

function openRevisionModal() {
    document.getElementById('revisionModal').classList.remove('hidden');
}

function closeRevisionModal() {
    document.getElementById('revisionModal').classList.add('hidden');
}

// API Actions
async function submitTask(event) {
    event.preventDefault();
    const repoUrl = document.getElementById('repoUrl').value.trim();
    const title = document.getElementById('taskTitle').value.trim();
    const baseBranch = document.getElementById('baseBranch').value.trim() || 'main';
    const description = document.getElementById('taskDesc').value.trim();

    try {
        const res = await fetch('/api/tasks', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                repo_url: repoUrl,
                title: title,
                base_branch: baseBranch,
                description: description,
                task_type: 'MANUAL',
                platform: 'MANUAL'
            })
        });

        if (res.ok) {
            const task = await res.json();
            window.location.href = `/tasks/${task.id}`;
        } else {
            alert('Failed to create task: ' + (await res.text()));
        }
    } catch (e) {
        alert('Error: ' + e.message);
    }
}

async function approveTask(taskId) {
    if (!confirm(`Launch native codespace and run multi-LLM team for ${taskId}?`)) return;
    try {
        const res = await fetch(`/api/tasks/${taskId}/approve`, { method: 'POST' });
        if (res.ok) {
            window.location.reload();
        } else {
            alert('Failed to approve task: ' + (await res.text()));
        }
    } catch (e) {
        alert('Error: ' + e.message);
    }
}

async function retryTask(taskId) {
    if (!confirm(`Retry pipeline for ${taskId}? This will spawn a fresh sandbox attempt folder and re-engage the agent team.`)) return;
    try {
        const res = await fetch(`/api/tasks/${taskId}/retry`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({})
        });
        if (res.ok) {
            window.location.reload();
        } else {
            alert('Failed to retry task: ' + (await res.text()));
        }
    } catch (e) {
        alert('Error retrying task: ' + e.message);
    }
}

async function rejectTask(taskId) {
    if (!confirm(`Are you sure you want to reject task ${taskId}?`)) return;
    try {
        const res = await fetch(`/api/tasks/${taskId}/reject`, { method: 'POST' });
        if (res.ok) {
            window.location.reload();
        } else {
            alert('Failed to reject task: ' + (await res.text()));
        }
    } catch (e) {
        alert('Error: ' + e.message);
    }
}

async function confirmTask(taskId) {
    if (!confirm(`Commit changes, push to git branch, and create Pull Request for ${taskId}?`)) return;
    try {
        const res = await fetch(`/api/tasks/${taskId}/confirm`, { method: 'POST' });
        if (res.ok) {
            window.location.reload();
        } else {
            alert('Failed to confirm and push: ' + (await res.text()));
        }
    } catch (e) {
        alert('Error: ' + e.message);
    }
}

async function confirmTaskWithBranch(taskId) {
    const select = document.getElementById('targetBranchSelect') || document.getElementById('targetBranchSelectPending');
    let targetBranch = select ? select.value : null;
    if (targetBranch === '__custom__') {
        const custom = prompt('Enter the custom target branch name (e.g. staging, develop):');
        if (!custom || !custom.trim()) return;
        targetBranch = custom.trim();
    }
    if (!targetBranch) {
        const headerEl = document.getElementById('headerTargetBranch');
        targetBranch = headerEl ? headerEl.textContent.trim() : 'main';
    }

    const branchDisplay = targetBranch || 'default';
    if (!confirm(`Commit changes, push branch, and create PR targeting '${branchDisplay}' for ${taskId}?`)) return;

    try {
        const res = await fetch(`/api/tasks/${taskId}/confirm`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ target_branch: targetBranch })
        });
        if (res.ok) {
            window.location.reload();
        } else {
            alert('Failed to confirm and push: ' + (await res.text()));
        }
    } catch (e) {
        alert('Error: ' + e.message);
    }
}

async function updateTargetBranch(taskId, branchValue) {
    const selects = [
        document.getElementById('targetBranchSelect'),
        document.getElementById('targetBranchSelectPending')
    ].filter(Boolean);

    if (branchValue === '__custom__') {
        const customBranch = prompt('Enter the custom target branch name (e.g. develop, staging, release/v1):');
        if (!customBranch || !customBranch.trim()) {
            // Restore selects to their current target branch
            const current = document.getElementById('headerTargetBranch')?.textContent?.trim() || 'main';
            selects.forEach(s => s.value = current);
            return;
        }
        branchValue = customBranch.trim();
    }

    // Immediately update UI selects so the user sees their chosen branch
    selects.forEach(select => {
        let existing = Array.from(select.options).find(o => o.value === branchValue);
        if (!existing) {
            const opt = document.createElement('option');
            opt.value = branchValue;
            opt.textContent = branchValue;
            opt.className = 'bg-slate-900 text-white';
            // Insert before the last option (+ Custom Branch...)
            select.insertBefore(opt, select.lastElementChild);
        }
        select.value = branchValue;
    });

    const headerEl = document.getElementById('headerTargetBranch');
    if (headerEl) headerEl.textContent = branchValue;

    try {
        const res = await fetch(`/api/tasks/${taskId}/target-branch`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ branch: branchValue })
        });
        if (res.ok) {
            // Briefly reload so server-rendered components stay synchronized
            setTimeout(() => {
                window.location.reload();
            }, 300);
        } else {
            alert('Failed to update target branch: ' + (await res.text()));
            window.location.reload();
        }
    } catch (e) {
        alert('Error updating target branch: ' + e.message);
    }
}

function promptChangeTargetBranch(taskId) {
    const current = document.getElementById('headerTargetBranch')?.textContent?.trim() || 'main';
    const newBranch = prompt('Enter target branch for PR (e.g. main, v1, staging):', current);
    if (newBranch && newBranch.trim() && newBranch.trim() !== current) {
        updateTargetBranch(taskId, newBranch.trim());
    }
}

async function submitQuestionAnswer(taskId, questionId, answerText, selectedOption) {
    try {
        const res = await fetch(`/api/tasks/${taskId}/questions/${questionId}/answer`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ answer: answerText, selected_option: selectedOption })
        });
        if (res.ok) {
            window.location.reload();
        } else {
            alert('Failed to record answer: ' + (await res.text()));
        }
    } catch (e) {
        alert('Error: ' + e.message);
    }
}

async function submitCustomQuestionAnswer(event, taskId, questionId) {
    event.preventDefault();
    const input = document.getElementById(`customAnswer_${questionId}`);
    const answer = input ? input.value.trim() : '';
    if (!answer) return;
    await submitQuestionAnswer(taskId, questionId, answer, answer);
}

async function submitRevision(event, taskId) {
    event.preventDefault();
    const notes = document.getElementById('revisionNotes').value.trim();
    if (!notes) return;

    try {
        const res = await fetch(`/api/tasks/${taskId}/revise`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ notes: notes })
        });
        if (res.ok) {
            closeRevisionModal();
            window.location.reload();
        } else {
            alert('Failed to request revision: ' + (await res.text()));
        }
    } catch (e) {
        alert('Error: ' + e.message);
    }
}

async function stopTask(taskId) {
    if (!confirm(`Are you sure you want to stop/cancel task ${taskId}? This will immediately terminate all active sandbox processes and halt the agent pipeline.`)) return;
    try {
        const res = await fetch(`/api/tasks/${taskId}/stop`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ reason: 'Stopped by user via dashboard' })
        });
        if (res.ok) {
            window.location.reload();
        } else {
            alert('Failed to stop task: ' + (await res.text()));
        }
    } catch (e) {
        alert('Error stopping task: ' + e.message);
    }
}

// Live SSE Terminal Log Streamer
function setupTaskSSE(taskId) {
    const logsContainer = document.getElementById('terminalLogs');
    const statusBadge = document.getElementById('taskStatusBadge');
    if (!logsContainer) return;

    const evtSource = new EventSource(`/api/events/stream?task_id=${taskId}`);

    evtSource.onmessage = function(event) {
        try {
            const data = JSON.parse(event.data);
            if (data.task_id !== taskId) return;

            const emptyNotice = document.getElementById('emptyEventsNotice');
            if (emptyNotice) emptyNotice.remove();

            const timeStr = new Date(data.created_at).toTimeString().split(' ')[0];
            const div = document.createElement('div');
            div.className = 'leading-relaxed';

            let roleTag = data.role ? `<span class="text-cyan-400 font-bold">[${data.role}]</span> ` : '';
            let msgClass = 'text-slate-300';
            if (data.event_type === 'ERROR') msgClass = 'text-rose-400 font-semibold';
            else if (data.event_type === 'STEP') msgClass = 'text-amber-300 font-semibold';
            else if (data.event_type === 'TEST_RUN') msgClass = 'text-purple-300 font-semibold';
            else if (data.event_type === 'QUESTION') msgClass = 'text-amber-300 font-bold';
            else if (data.event_type === 'LEARNING') msgClass = 'text-emerald-300 font-bold';
            else if (data.event_type === 'STATUS_CHANGE') msgClass = 'text-emerald-300 font-bold';

            // Filter live events by active run view
            if (data.run_id && window.currentActiveRunId && data.run_id !== window.currentActiveRunId) {
                const numCurrent = parseInt((window.currentActiveRunId || 'run-0').replace('run-', '')) || 0;
                const numNew = parseInt((data.run_id || 'run-0').replace('run-', '')) || 0;
                if (numNew > numCurrent) {
                    window.currentActiveRunId = data.run_id;
                    const pill = document.getElementById('activeRunPill');
                    if (pill) pill.textContent = data.run_id;
                    const lbl = document.getElementById('activeRunLabel');
                    if (lbl) lbl.textContent = data.run_id;
                } else {
                    return; // Ignore events belonging to older/different run
                }
            }

            div.innerHTML = `<span class="text-slate-600">[${timeStr}]</span> ${roleTag}<span class="${msgClass}">${escapeHtml(data.message)}</span>`;
            logsContainer.appendChild(div);
            logsContainer.scrollTop = logsContainer.scrollHeight;

            // Live refresh sandbox metrics as commands execute
            if (typeof refreshSandboxStats === 'function') {
                refreshSandboxStats(taskId, window.currentActiveRunId);
            }

            if (data.event_type === 'STATUS_CHANGE' && statusBadge) {
                for (const st of ['PLANNING', 'CODING', 'TESTING', 'AWAITING_INPUT', 'AWAITING_BRANCH_SELECTION', 'AWAITING_CONFIRMATION', 'COMPLETED', 'FAILED', 'CANCELLED', 'REVISION_REQUESTED']) {
                    if (data.message.includes(st)) {
                        statusBadge.textContent = st;
                        if (st === 'FAILED' || st === 'CANCELLED') {
                            statusBadge.className = 'text-rose-400 font-bold';
                        } else if (['PLANNING', 'CODING', 'TESTING'].includes(st)) {
                            statusBadge.className = 'text-cyan-400 animate-pulse font-bold';
                        } else if (['AWAITING_CONFIRMATION', 'AWAITING_INPUT', 'AWAITING_BRANCH_SELECTION'].includes(st)) {
                            statusBadge.className = 'text-purple-400 font-bold';
                        } else {
                            statusBadge.className = 'text-emerald-400 font-bold';
                        }
                        break;
                    }
                }
                // If terminal, question, or confirmation state reached, reload to update UI buttons
                if (data.message.includes('AWAITING_CONFIRMATION') || data.message.includes('AWAITING_INPUT') || data.message.includes('COMPLETED') || data.message.includes('CANCELLED') || data.message.includes('FAILED')) {
                    setTimeout(() => window.location.reload(), 1200);
                }
            }
        } catch (e) {
            console.error('SSE parse error:', e);
        }
    };

    evtSource.onerror = function() {
        const ind = document.getElementById('sseIndicator');
        if (ind) ind.classList.remove('bg-emerald-400', 'animate-ping');
        if (ind) ind.classList.add('bg-rose-400');
    };
}


// Markdown on these pages carries issue text, LLM output and test logs, and marked does not
// sanitize. The page holds the dashboard cookie, so all rendered markdown goes through
// DOMPurify; if it is missing this throws and callers fall back to escaped text.
function renderMarkdown(text) {
    return DOMPurify.sanitize(marked.parse(text || ''));
}

// For a value placed inside '...' in an inline handler: encodeURIComponent leaves the
// apostrophe alone, which let a crafted file name close the string and run script.
function jsArg(value) {
    return encodeURIComponent(value).replace(/'/g, '%27');
}

function escapeHtml(str) {
    return str
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#039;");
}

async function triggerSync() {
    const btn = document.getElementById('syncBtn');
    const icon = document.getElementById('syncIcon');
    const text = document.getElementById('syncText');

    if (btn) btn.disabled = true;
    if (icon) icon.classList.add('animate-spin');
    if (text) text.textContent = 'Syncing...';

    try {
        const res = await fetch('/api/sync', { method: 'POST' });
        const data = await res.json();
        if (data.success) {
            alert(`Sync complete!\nIngested ${data.total_new} new task(s).\nGitLab: ${data.gitlab_count}, GitHub: ${data.github_count}`);
            window.location.reload();
        } else {
            alert('Sync failed: ' + (data.error || 'Unknown error'));
        }
    } catch (e) {
        alert('Sync error: ' + e.message);
    } finally {
        if (btn) btn.disabled = false;
        if (icon) icon.classList.remove('animate-spin');
        if (text) text.textContent = 'Sync Now';
    }
}

async function deleteSandbox(taskId) {
    if (!confirm(`Are you sure you want to delete the sandbox workspace for task ${taskId}? This will permanently remove all files and execution logs in .issueforge/workspaces/${taskId}.`)) {
        return;
    }
    try {
        const res = await fetch(`/api/sandboxes/${taskId}`, { method: 'DELETE' });
        const data = await res.json();
        if (data.success) {
            alert(`Sandbox workspace for ${taskId} deleted successfully.`);
            window.location.reload();
        } else {
            alert('Failed to delete sandbox: ' + (data.error || 'Unknown error'));
        }
    } catch (e) {
        alert('Error deleting sandbox: ' + e.message);
    }
}

async function viewSandboxLog(taskId) {
    const modal = document.getElementById('sandboxLogModal');
    const title = document.getElementById('sandboxLogTitle');
    const content = document.getElementById('sandboxLogContent');
    const downloadLink = document.getElementById('downloadLogLink');
    if (!modal || !content) return;

    modal.classList.remove('hidden');
    if (title) title.textContent = `Sandbox Log: ${taskId}`;
    content.textContent = 'Fetching sandbox execution log...';
    if (downloadLink) downloadLink.href = `/api/sandboxes/${taskId}/log`;

    try {
        const res = await fetch(`/api/sandboxes/${taskId}/log`);
        const text = await res.text();
        content.textContent = text || 'No execution stream recorded in this sandbox.';
    } catch (e) {
        content.textContent = 'Error loading log: ' + e.message;
    }
}

function closeSandboxLogModal() {
    const modal = document.getElementById('sandboxLogModal');
    if (modal) modal.classList.add('hidden');
}

// Interactive xterm.js ANSI Web Terminal for Live AGY CLI Streaming
function initXtermTerminal(taskId, runId) {
    const mount = document.getElementById('xtermMount');
    if (!mount) return;

    if (typeof Terminal === 'undefined') {
        console.warn('xterm.js library not yet loaded, retrying in 200ms...');
        setTimeout(() => initXtermTerminal(taskId, runId), 200);
        return;
    }

    const termStatusDot = document.getElementById('termStatusDot');
    const termStatusText = document.getElementById('termStatusText');

    // Close any previous websocket connection
    if (window.issueforgeTermSocket) {
        try {
            window.issueforgeTermSocket.onclose = null;
            window.issueforgeTermSocket.close();
        } catch (e) {}
        window.issueforgeTermSocket = null;
    }

    // Initialize or reset Terminal instance
    if (!window.issueforgeTerminal) {
        mount.innerHTML = '';
        const term = new Terminal({
            cursorBlink: true,
            cursorStyle: 'block',
            fontFamily: 'JetBrains Mono, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace',
            fontSize: 12,
            lineHeight: 1.25,
            convertEol: true,
            scrollback: 50000,
            theme: {
                background: '#0d1117',
                foreground: '#c9d1d9',
                cursor: '#58a6ff',
                cursorAccent: '#0d1117',
                selectionBackground: '#3b82f640',
                black: '#0d1117',
                red: '#ff7b72',
                green: '#3fb950',
                yellow: '#d29922',
                blue: '#58a6ff',
                magenta: '#bc8cff',
                cyan: '#39c5cf',
                white: '#b1bac4',
                brightBlack: '#6e7681',
                brightRed: '#ffa198',
                brightGreen: '#56d364',
                brightYellow: '#e3b341',
                brightBlue: '#79c0ff',
                brightMagenta: '#d2a8ff',
                brightCyan: '#56d4dd',
                brightWhite: '#f0f6fc'
            }
        });

        const FitAddonClass = window.FitAddon?.FitAddon || window.FitAddon;
        if (FitAddonClass) {
            const fitAddon = new FitAddonClass();
            term.loadAddon(fitAddon);
            window.termFitAddon = fitAddon;
        }

        const WebLinksClass = window.WebLinksAddon?.WebLinksAddon || window.WebLinksAddon;
        if (WebLinksClass) {
            term.loadAddon(new WebLinksClass());
        }

        term.open(mount);
        if (window.termFitAddon) {
            setTimeout(() => window.termFitAddon.fit(), 50);
        }

        // Forward interactive user keystrokes to the server PTY
        term.onData(data => {
            if (window.issueforgeTermSocket && window.issueforgeTermSocket.readyState === WebSocket.OPEN) {
                window.issueforgeTermSocket.send(JSON.stringify({
                    type: 'input',
                    data: data
                }));
            }
        });

        window.issueforgeTerminal = term;

        // Auto-fit on window resize
        window.addEventListener('resize', () => {
            if (window.termFitAddon) {
                window.termFitAddon.fit();
                if (window.issueforgeTermSocket && window.issueforgeTermSocket.readyState === WebSocket.OPEN && window.issueforgeTerminal) {
                    window.issueforgeTermSocket.send(JSON.stringify({
                        type: 'resize',
                        cols: window.issueforgeTerminal.cols,
                        rows: window.issueforgeTerminal.rows
                    }));
                }
            }
        });
    } else {
        window.issueforgeTerminal.reset();
        if (window.termFitAddon) {
            setTimeout(() => window.termFitAddon.fit(), 50);
        }
    }

    // Connect WebSocket
    const effectiveRunId = runId || window.currentActiveRunId || 'run-1';
    const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const wsUrl = `${proto}//${window.location.host}/ws/tasks/${taskId}/terminal?run_id=${encodeURIComponent(effectiveRunId)}`;

    if (termStatusDot) termStatusDot.className = 'inline-block w-2 h-2 rounded-full bg-amber-400 animate-pulse';
    if (termStatusText) termStatusText.textContent = `Connecting (${effectiveRunId})...`;

    try {
        const ws = new WebSocket(wsUrl);
        ws.binaryType = 'arraybuffer';

        ws.onopen = function() {
            if (termStatusDot) termStatusDot.className = 'inline-block w-2 h-2 rounded-full bg-emerald-400 animate-ping';
            if (termStatusText) termStatusText.textContent = `Live AGY PTY (${effectiveRunId})`;

            if (window.termFitAddon && window.issueforgeTerminal) {
                window.termFitAddon.fit();
                ws.send(JSON.stringify({
                    type: 'resize',
                    cols: window.issueforgeTerminal.cols,
                    rows: window.issueforgeTerminal.rows
                }));
            }
        };

        ws.onmessage = function(event) {
            if (!window.issueforgeTerminal) return;
            if (event.data instanceof ArrayBuffer) {
                const text = new TextDecoder().decode(event.data);
                window.issueforgeTerminal.write(text);
            } else if (typeof event.data === 'string') {
                window.issueforgeTerminal.write(event.data);
            } else if (event.data instanceof Blob) {
                event.data.arrayBuffer().then(buf => {
                    const text = new TextDecoder().decode(buf);
                    window.issueforgeTerminal.write(text);
                });
            }
        };

        ws.onerror = function(err) {
            console.debug('Terminal websocket error:', err);
            if (termStatusDot) termStatusDot.className = 'inline-block w-2 h-2 rounded-full bg-rose-400';
            if (termStatusText) termStatusText.textContent = 'PTY Offline';
        };

        ws.onclose = function() {
            if (termStatusDot) termStatusDot.className = 'inline-block w-2 h-2 rounded-full bg-slate-500';
            if (termStatusText) termStatusText.textContent = `Closed (${effectiveRunId})`;
        };

        window.issueforgeTermSocket = ws;

        // Immediate scrollback fetch so terminal is instantly populated even before WS handshake completes
        fetch(`/api/tasks/${taskId}/runs/${effectiveRunId}/terminal`)
            .then(r => r.text())
            .then(txt => {
                if (txt && window.issueforgeTerminal && !window[`_termLoaded_${effectiveRunId}`]) {
                    window.issueforgeTerminal.write(txt);
                    window[`_termLoaded_${effectiveRunId}`] = true;
                }
            }).catch(() => {});

    } catch (err) {
        console.error('Failed to create terminal websocket:', err);
        if (termStatusDot) termStatusDot.className = 'inline-block w-2 h-2 rounded-full bg-rose-400';
        if (termStatusText) termStatusText.textContent = 'Connection Error';
    }
}

function switchStreamTab(tab) {
    const termContainer = document.getElementById('terminalXtermContainer');
    const eventsDiv = document.getElementById('terminalLogs');
    const rawDiv = document.getElementById('sandboxRawLogs');
    const tabTerminal = document.getElementById('tabStreamTerminal');
    const tabEvents = document.getElementById('tabStreamEvents');
    const tabRaw = document.getElementById('tabStreamRaw');

    if (!termContainer || !eventsDiv || !rawDiv) return;

    [tabTerminal, tabEvents, tabRaw].forEach(btn => {
        if (btn) btn.className = 'px-2 py-0.5 rounded text-[11px] font-medium text-slate-400 hover:text-slate-200 transition flex items-center space-x-1';
    });

    termContainer.classList.add('hidden');
    eventsDiv.classList.add('hidden');
    rawDiv.classList.add('hidden');

    if (tab === 'terminal') {
        termContainer.classList.remove('hidden');
        if (tabTerminal) {
            tabTerminal.className = 'px-2.5 py-0.5 rounded text-[11px] font-medium bg-purple-600 text-white transition flex items-center space-x-1.5 shadow-sm';
        }
        if (window.termFitAddon) {
            setTimeout(() => window.termFitAddon.fit(), 30);
        }
    } else if (tab === 'events') {
        eventsDiv.classList.remove('hidden');
        if (tabEvents) {
            tabEvents.className = 'px-2.5 py-0.5 rounded text-[11px] font-medium bg-slate-700 text-white transition flex items-center space-x-1';
        }
    } else {
        rawDiv.classList.remove('hidden');
        if (tabRaw) {
            tabRaw.className = 'px-2.5 py-0.5 rounded text-[11px] font-medium bg-slate-700 text-white transition flex items-center space-x-1';
        }
    }
}

// ==========================================
// Co-Pilot Mid-Flight Steering
// ==========================================

// ==========================================
// Co-Pilot Mid-Flight Steering & AGY Studio
// ==========================================

async function submitSteeringDirective(taskId) {
    const input = document.getElementById('steeringDirectiveInput');
    const btn = document.getElementById('steerBtn');
    if (!input) return;
    const text = input.value.trim();
    if (!text) return;

    if (btn) btn.disabled = true;

    // Optimistically render directive card immediately in AGY Rich Stream
    appendAgyCard({
        created_at: new Date().toISOString(),
        event_type: 'STEP',
        message: `💬 Operator Prompt: ${text}`,
        data: { type: 'user_prompt', directive: text }
    });

    try {
        const res = await fetch(`/api/tasks/${taskId}/steer`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ directive: text })
        });
        if (!res.ok) throw new Error('Failed to send steering directive');
        showToast('🎯 Mid-flight steering directive sent to agents!', 'success');
        input.value = '';
    } catch (err) {
        showToast(`Error: ${err.message}`, 'error');
    } finally {
        if (btn) btn.disabled = false;
    }
}

// ==========================================
// Issueforge Agent Studio: Tabbed Editor & Diff
// ==========================================

let activeEditorFilePath = null;
let studioFilesCache = [];
let studioCollapsedDirs = new Set();
let studioSearchFilter = '';

window.studioOpenTabs = []; // [{ path, content, originalContent, isDirty, status, diff }]
window.studioActiveTab = null;
window.studioViewMode = 'code'; // 'code' or 'diff'
window.studioRightView = 'rich'; // 'rich' or 'term'

function getFileIcon(filename) {
    const ext = filename.split('.').pop().toLowerCase();
    if (['py'].includes(ext)) return 'file-code';
    if (['js', 'ts', 'jsx', 'tsx', 'html', 'css'].includes(ext)) return 'file-code';
    if (['json', 'yaml', 'yml', 'toml'].includes(ext)) return 'file-json';
    if (['md', 'txt', 'rst'].includes(ext)) return 'file-text';
    if (['sh', 'bash', 'zsh'].includes(ext)) return 'terminal';
    if (filename.startsWith('test_') || filename.endsWith('_test.py')) return 'flask-conical';
    return 'file';
}

function filterStudioFileTree(query) {
    studioSearchFilter = (query || '').trim().toLowerCase();
    renderStudioTree();
}

function renderStudioTree() {
    const listEl = document.getElementById('studioFileTreeList');
    if (!listEl) return;
    if (!studioFilesCache || studioFilesCache.length === 0) {
        listEl.innerHTML = '<div class="text-slate-500 italic p-2 text-xs">No files found in workspace.</div>';
        return;
    }

    let files = studioFilesCache;
    if (studioSearchFilter) {
        files = studioFilesCache.filter(f => f.path.toLowerCase().includes(studioSearchFilter));
        if (files.length === 0) {
            listEl.innerHTML = `<div class="text-slate-500 italic p-3 text-center text-xs">No files matching "${escapeHtml(studioSearchFilter)}"</div>`;
            return;
        }
    }

    // Build hierarchical tree structure
    const root = { name: '', fullPath: '', isDir: true, children: new Map(), files: [] };
    for (const file of files) {
        const parts = file.path.split('/');
        let curr = root;
        for (let i = 0; i < parts.length - 1; i++) {
            const dirName = parts[i];
            const dirPath = parts.slice(0, i + 1).join('/');
            if (!curr.children.has(dirName)) {
                curr.children.set(dirName, {
                    name: dirName,
                    fullPath: dirPath,
                    isDir: true,
                    children: new Map(),
                    files: []
                });
            }
            curr = curr.children.get(dirName);
        }
        curr.files.push(file);
    }

    const taskId = window.currentTaskId;
    const runId = window.currentActiveRunId || 'run-1';

    function renderNode(node, depth = 0) {
        let html = '';
        const paddingLeft = depth * 12 + 6;

        const sortedDirs = Array.from(node.children.values()).sort((a, b) => a.name.localeCompare(b.name));
        for (const dir of sortedDirs) {
            const isCollapsed = studioSearchFilter ? false : studioCollapsedDirs.has(dir.fullPath);
            const chevronIcon = isCollapsed ? 'chevron-right' : 'chevron-down';
            const folderIcon = isCollapsed ? 'folder' : 'folder-open';

            html += `
                <div onclick="toggleStudioFolder('${jsArg(dir.fullPath)}')"
                    class="folder-tree-node cursor-pointer group flex items-center justify-between py-1 px-1 rounded hover:bg-slate-800/80 transition text-slate-400 hover:text-slate-200 select-none"
                    style="padding-left: ${paddingLeft}px;">
                    <div class="flex items-center space-x-1.5 truncate">
                        <i data-lucide="${chevronIcon}" class="w-3 h-3 text-slate-500 shrink-0"></i>
                        <i data-lucide="${folderIcon}" class="w-3.5 h-3.5 text-amber-400/80 shrink-0"></i>
                        <span class="font-medium text-slate-300 truncate text-xs">${escapeHtml(dir.name)}</span>
                    </div>
                </div>
            `;

            if (!isCollapsed) {
                html += renderNode(dir, depth + 1);
            }
        }

        const sortedFiles = node.files.sort((a, b) => a.path.localeCompare(b.path));
        for (const file of sortedFiles) {
            const fileName = file.path.split('/').pop();
            const iconName = getFileIcon(fileName);
            let statusBadge = '';
            if (file.status === 'added') {
                statusBadge = '<span class="text-[9px] bg-emerald-500/20 text-emerald-300 px-1 rounded font-bold font-mono shrink-0">+A</span>';
            } else if (file.status === 'modified') {
                statusBadge = '<span class="text-[9px] bg-amber-500/20 text-amber-300 px-1 rounded font-bold font-mono shrink-0">M</span>';
            }

            const sizeStr = file.size < 1024 ? `${file.size} B` : `${(file.size / 1024).toFixed(1)} KB`;
            const isActive = window.studioActiveTab === file.path;
            const activeClass = isActive ? 'bg-cyan-950/40 text-cyan-300 border-l-2 border-cyan-400' : 'text-slate-300 hover:bg-slate-800/60 hover:text-white';

            html += `
                <div onclick="openStudioFileTab('${taskId}', '${runId}', '${jsArg(file.path)}')"
                    id="fileTreeItem_${encodeURIComponent(file.path).replace(/[^a-zA-Z0-9]/g, '_')}"
                    class="file-tree-node cursor-pointer group flex items-center justify-between py-1 px-1 rounded transition ${activeClass}"
                    style="padding-left: ${paddingLeft + 12}px;"
                    title="${escapeHtml(file.path)}">
                    <div class="flex items-center space-x-1.5 truncate max-w-[170px]">
                        <i data-lucide="${iconName}" class="w-3.5 h-3.5 text-slate-500 group-hover:text-cyan-400 shrink-0"></i>
                        <span class="truncate text-xs">${escapeHtml(fileName)}</span>
                        ${statusBadge}
                    </div>
                    <span class="text-[10px] text-slate-600 font-mono shrink-0 ml-1">${sizeStr}</span>
                </div>
            `;
        }
        return html;
    }

    listEl.innerHTML = renderNode(root);
    if (typeof lucide !== 'undefined') lucide.createIcons();
}

function toggleStudioFolder(encodedPath) {
    const p = decodeURIComponent(encodedPath);
    if (studioCollapsedDirs.has(p)) {
        studioCollapsedDirs.delete(p);
    } else {
        studioCollapsedDirs.add(p);
    }
    renderStudioTree();
}

async function refreshFileTree(taskId, runId) {
    const effectiveRunId = runId || window.currentActiveRunId || 'run-1';
    const listEl = document.getElementById('studioFileTreeList');
    const countEl = document.getElementById('studioFileCount');
    if (!listEl) return;

    try {
        listEl.innerHTML = '<div class="text-slate-500 italic p-2 text-xs">Scanning workspace files...</div>';
        const res = await fetch(`/api/tasks/${taskId}/runs/${effectiveRunId}/files/tree`);
        if (!res.ok) throw new Error('Failed to fetch files');
        const data = await res.json();
        const tree = data.tree || [];

        studioFilesCache = tree;
        if (countEl) countEl.textContent = `${tree.length} files`;

        renderStudioTree();
    } catch (err) {
        listEl.innerHTML = `<div class="text-rose-400 p-2 text-xs">Error loading file tree: ${err.message}</div>`;
    }
}

async function openStudioFileTab(taskId, runId, encodedFilePath, autoSwitch = true) {
    const filePath = decodeURIComponent(encodedFilePath);
    const effectiveRunId = runId || window.currentActiveRunId || 'run-1';

    let existing = window.studioOpenTabs.find(t => t.path === filePath);
    if (!existing) {
        try {
            const [contentRes, diffRes] = await Promise.all([
                fetch(`/api/tasks/${taskId}/runs/${effectiveRunId}/files/content?path=${encodeURIComponent(filePath)}`),
                fetch(`/api/tasks/${taskId}/files/diff?file_path=${encodeURIComponent(filePath)}&run_id=${encodeURIComponent(effectiveRunId)}`)
            ]);

            if (!contentRes.ok) throw new Error('Could not load file content');
            const contentData = await contentRes.json();
            const diffData = diffRes.ok ? await diffRes.json() : { diff: '' };

            existing = {
                path: filePath,
                content: contentData.content,
                originalContent: contentData.content,
                isDirty: false,
                status: diffData.is_new ? 'added' : (diffData.diff ? 'modified' : 'clean'),
                diff: diffData.diff || ''
            };
            window.studioOpenTabs.push(existing);
        } catch (err) {
            showToast(`Failed to open ${filePath}: ${err.message}`, 'error');
            return;
        }
    }

    if (autoSwitch) {
        window.studioActiveTab = filePath;
    }
    renderStudioTabs();
    renderActiveTabContent();
    renderStudioTree();
}

function closeStudioTab(event, encodedFilePath) {
    if (event) event.stopPropagation();
    const filePath = decodeURIComponent(encodedFilePath);
    const idx = window.studioOpenTabs.findIndex(t => t.path === filePath);
    if (idx === -1) return;

    window.studioOpenTabs.splice(idx, 1);
    if (window.studioActiveTab === filePath) {
        if (window.studioOpenTabs.length > 0) {
            window.studioActiveTab = window.studioOpenTabs[Math.max(0, idx - 1)].path;
        } else {
            window.studioActiveTab = null;
        }
    }
    renderStudioTabs();
    renderActiveTabContent();
    renderStudioTree();
}

function switchStudioTab(filePath) {
    window.studioActiveTab = filePath;
    renderStudioTabs();
    renderActiveTabContent();
    renderStudioTree();
}

function renderStudioTabs() {
    const tabsList = document.getElementById('studioTabsList');
    if (!tabsList) return;

    if (window.studioOpenTabs.length === 0) {
        tabsList.innerHTML = '<span class="text-slate-600 text-xs italic px-2">No open files</span>';
        return;
    }

    let html = '';
    window.studioOpenTabs.forEach(tab => {
        const isActive = tab.path === window.studioActiveTab;
        const fileName = tab.path.split('/').pop();
        const iconName = getFileIcon(fileName);

        let badge = '';
        if (tab.status === 'added') {
            badge = '<span class="text-[9px] bg-emerald-500/20 text-emerald-300 px-1 rounded font-bold font-mono">+new</span>';
        } else if (tab.status === 'modified' || tab.isDirty) {
            badge = '<span class="text-[9px] bg-amber-500/20 text-amber-300 px-1 rounded font-bold font-mono">●mod</span>';
        }

        const tabBg = isActive 
            ? 'bg-[#0d1117] text-white border-t-2 border-cyan-400 border-x border-[#30363d]' 
            : 'bg-[#161b22] text-slate-400 hover:text-slate-200 hover:bg-slate-800/60 border border-transparent';

        html += `
            <div onclick="switchStudioTab('${jsArg(tab.path)}')"
                class="flex items-center space-x-1.5 px-3 py-1.5 rounded-t text-xs font-mono cursor-pointer shrink-0 transition select-none ${tabBg}"
                title="${escapeHtml(tab.path)}">
                <i data-lucide="${iconName}" class="w-3.5 h-3.5 ${isActive ? 'text-cyan-400' : 'text-slate-500'} shrink-0"></i>
                <span class="max-w-[130px] truncate font-medium">${escapeHtml(fileName)}</span>
                ${badge}
                <button onclick="closeStudioTab(event, '${jsArg(tab.path)}')" class="hover:text-rose-400 hover:bg-slate-800 p-0.5 rounded text-slate-500 transition ml-1" title="Close tab">
                    <i data-lucide="x" class="w-3 h-3"></i>
                </button>
            </div>
        `;
    });
    tabsList.innerHTML = html;
    if (typeof lucide !== 'undefined') lucide.createIcons();
}

function renderActiveTabContent() {
    const emptyNotice = document.getElementById('studioEmptyNotice');
    const editorWrapper = document.getElementById('studioEditorWrapper');
    const diffWrapper = document.getElementById('studioDiffWrapper');
    const textarea = document.getElementById('studioEditorTextarea');
    const pathLabel = document.getElementById('studioFilePathLabel');
    const saveBtn = document.getElementById('studioSaveFileBtn');
    const metaInfo = document.getElementById('studioFileMetaInfo');

    const tab = window.studioOpenTabs.find(t => t.path === window.studioActiveTab);
    if (!tab) {
        if (emptyNotice) emptyNotice.classList.remove('hidden');
        if (editorWrapper) editorWrapper.classList.add('hidden');
        if (diffWrapper) diffWrapper.classList.add('hidden');
        if (pathLabel) pathLabel.textContent = 'Select a file from the explorer';
        if (saveBtn) saveBtn.classList.add('hidden');
        if (metaInfo) metaInfo.textContent = '';
        activeEditorFilePath = null;
        return;
    }

    activeEditorFilePath = tab.path;
    if (emptyNotice) emptyNotice.classList.add('hidden');
    if (pathLabel) pathLabel.textContent = tab.path;
    if (saveBtn) saveBtn.classList.remove('hidden');

    const lineCount = (tab.content || '').split('\n').length;
    const charCount = (tab.content || '').length;
    if (metaInfo) metaInfo.textContent = `${lineCount} lines • ${charCount} chars`;

    if (window.studioViewMode === 'code') {
        if (editorWrapper) editorWrapper.classList.remove('hidden');
        if (diffWrapper) diffWrapper.classList.add('hidden');
        if (textarea) {
            textarea.value = tab.content;
            textarea.oninput = () => {
                tab.content = textarea.value;
                tab.isDirty = (tab.content !== tab.originalContent);
                renderStudioTabs();
            };
        }
    } else {
        if (editorWrapper) editorWrapper.classList.add('hidden');
        if (diffWrapper) diffWrapper.classList.remove('hidden');
        renderStudioDiff(tab);
    }
}

function switchStudioViewMode(mode) {
    window.studioViewMode = mode;
    const codeBtn = document.getElementById('studioModeCodeBtn');
    const diffBtn = document.getElementById('studioModeDiffBtn');

    if (mode === 'code') {
        if (codeBtn) codeBtn.className = 'px-2 py-0.5 rounded text-xs font-semibold bg-cyan-600 text-white transition flex items-center space-x-1';
        if (diffBtn) diffBtn.className = 'px-2 py-0.5 rounded text-xs font-semibold text-slate-400 hover:text-white transition flex items-center space-x-1';
    } else {
        if (codeBtn) codeBtn.className = 'px-2 py-0.5 rounded text-xs font-semibold text-slate-400 hover:text-white transition flex items-center space-x-1';
        if (diffBtn) diffBtn.className = 'px-2 py-0.5 rounded text-xs font-semibold bg-cyan-600 text-white transition flex items-center space-x-1';
    }
    renderActiveTabContent();
}

function renderStudioDiff(tab) {
    const diffContainer = document.getElementById('studioDiffContainer');
    if (!diffContainer) return;

    if (!tab.diff || !tab.diff.trim()) {
        diffContainer.innerHTML = '<div class="text-slate-500 italic p-4 text-center">No modifications relative to base branch in this file.</div>';
        return;
    }

    const lines = tab.diff.split('\n');
    let html = '';
    lines.forEach((l) => {
        const safe = escapeHtml(l);
        if (l.startsWith('+++') || l.startsWith('---')) {
            html += `<div class="text-slate-400 font-bold bg-slate-900/80 px-2 py-0.5">${safe}</div>`;
        } else if (l.startsWith('@@')) {
            html += `<div class="text-cyan-400 font-semibold bg-cyan-950/30 px-2 py-0.5 border-y border-cyan-800/40">${safe}</div>`;
        } else if (l.startsWith('+')) {
            html += `<div class="bg-emerald-950/40 text-emerald-300 border-l-2 border-emerald-500 px-2 py-0.5"><span class="select-none opacity-50 mr-2">+</span>${safe.substring(1)}</div>`;
        } else if (l.startsWith('-')) {
            html += `<div class="bg-rose-950/40 text-rose-300 border-l-2 border-rose-500 px-2 py-0.5"><span class="select-none opacity-50 mr-2">-</span>${safe.substring(1)}</div>`;
        } else {
            html += `<div class="text-slate-400 px-2 py-0.5"><span class="select-none opacity-30 mr-2"> </span>${safe}</div>`;
        }
    });
    diffContainer.innerHTML = html;
}

// Backward-compatible alias for single-file load
async function loadFileContent(taskId, runId, encodedFilePath) {
    await openStudioFileTab(taskId, runId, encodedFilePath, true);
}

async function saveActiveFile(taskId, runId) {
    if (!window.studioActiveTab) return;
    const effectiveRunId = runId || window.currentActiveRunId || 'run-1';
    const textarea = document.getElementById('studioEditorTextarea');
    const saveStatus = document.getElementById('studioSaveStatus');
    if (!textarea) return;

    const tab = window.studioOpenTabs.find(t => t.path === window.studioActiveTab);

    try {
        const res = await fetch(`/api/tasks/${taskId}/runs/${effectiveRunId}/files/content`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                path: window.studioActiveTab,
                content: textarea.value
            })
        });
        if (!res.ok) throw new Error('Save failed');

        if (tab) {
            tab.content = textarea.value;
            tab.originalContent = textarea.value;
            tab.isDirty = false;
            try {
                const diffRes = await fetch(`/api/tasks/${taskId}/files/diff?file_path=${encodeURIComponent(tab.path)}&run_id=${encodeURIComponent(effectiveRunId)}`);
                if (diffRes.ok) {
                    const diffData = await diffRes.json();
                    tab.diff = diffData.diff || '';
                    tab.status = diffData.is_new ? 'added' : (diffData.diff ? 'modified' : 'clean');
                }
            } catch (_) {}
            renderStudioTabs();
        }

        if (saveStatus) {
            saveStatus.classList.remove('hidden');
            setTimeout(() => saveStatus.classList.add('hidden'), 2500);
        }
        showToast(`Saved ${window.studioActiveTab} successfully!`, 'success');
        refreshFileTree(taskId, effectiveRunId);
    } catch (err) {
        showToast(`Failed to save file: ${err.message}`, 'error');
    }
}

// ==========================================
// Right Pane: Dual-View & AGY Rich Stream
// ==========================================

function switchRightPaneView(mode) {
    window.studioRightView = mode;
    const richBtn = document.getElementById('streamTabRichBtn');
    const termBtn = document.getElementById('streamTabTermBtn');
    const richContainer = document.getElementById('studioRichStreamContainer');
    const termContainer = document.getElementById('terminalXtermContainer');

    if (mode === 'rich') {
        if (richBtn) richBtn.className = 'px-2.5 py-0.5 rounded text-xs font-semibold bg-purple-600 text-white transition flex items-center space-x-1.5 shadow-sm';
        if (termBtn) termBtn.className = 'px-2.5 py-0.5 rounded text-xs font-semibold text-slate-400 hover:text-white transition flex items-center space-x-1.5';
        if (richContainer) richContainer.classList.remove('hidden');
        if (termContainer) termContainer.classList.add('hidden');
    } else {
        if (richBtn) richBtn.className = 'px-2.5 py-0.5 rounded text-xs font-semibold text-slate-400 hover:text-white transition flex items-center space-x-1.5';
        if (termBtn) termBtn.className = 'px-2.5 py-0.5 rounded text-xs font-semibold bg-purple-600 text-white transition flex items-center space-x-1.5 shadow-sm';
        if (richContainer) richContainer.classList.add('hidden');
        if (termContainer) termContainer.classList.remove('hidden');
        if (window.termFitAddon) {
            setTimeout(() => window.termFitAddon.fit(), 50);
        }
    }
}

function getRoleBadgeClass(role) {
    if (role === 'ORCHESTRATOR') return 'bg-purple-500/20 text-purple-300 border border-purple-500/30';
    if (role === 'PLANNER') return 'bg-cyan-500/20 text-cyan-300 border border-cyan-500/30';
    if (role === 'CODER') return 'bg-emerald-500/20 text-emerald-300 border border-emerald-500/30';
    if (role === 'TESTER') return 'bg-amber-500/20 text-amber-300 border border-amber-500/30';
    if (role === 'REVIEWER') return 'bg-pink-500/20 text-pink-300 border border-pink-500/30';
    return 'bg-slate-700/50 text-slate-300';
}

function appendAgyCard(eventData) {
    const list = document.getElementById('richStreamCardsList');
    if (!list) return;

    const timeStr = eventData.created_at ? new Date(eventData.created_at).toTimeString().split(' ')[0] : new Date().toTimeString().split(' ')[0];
    const card = document.createElement('div');
    card.className = 'rounded-lg border p-3 text-xs leading-relaxed transition shadow-sm';

    const msg = eventData.message || '';
    const role = (eventData.role || '').toUpperCase();
    const evtType = eventData.event_type;
    const data = eventData.data || {};

    // 1. Operator directive / prompt
    if (data.type === 'user_prompt' || msg.startsWith('💬') || evtType === 'QUESTION') {
        card.className += ' bg-blue-950/20 border-blue-500/40 text-blue-200';
        card.innerHTML = `
            <div class="flex items-center justify-between font-semibold mb-1 text-blue-300">
                <div class="flex items-center space-x-1.5">
                    <i data-lucide="user" class="w-3.5 h-3.5 text-blue-400"></i>
                    <span>Operator Directive</span>
                </div>
                <span class="text-[10px] text-blue-400/70 font-mono">${timeStr}</span>
            </div>
            <div class="text-slate-200 font-medium whitespace-pre-wrap">${escapeHtml(data.directive || msg)}</div>
        `;
    }
    // 2. Agent Acknowledgment Reply
    else if (data.type === 'agent_reply' || msg.startsWith('🤖')) {
        card.className += ' bg-emerald-950/20 border-emerald-500/40 text-emerald-200';
        card.innerHTML = `
            <div class="flex items-center justify-between font-semibold mb-1 text-emerald-300">
                <div class="flex items-center space-x-1.5">
                    <i data-lucide="check-circle" class="w-3.5 h-3.5 text-emerald-400"></i>
                    <span>Agent Swarm</span>
                </div>
                <span class="text-[10px] text-emerald-400/70 font-mono">${timeStr}</span>
            </div>
            <div class="text-emerald-100 font-medium">${escapeHtml(data.ack || msg)}</div>
        `;
    }
    // 3. Tool Execution STEP
    else if (evtType === 'STEP' && data.tool) {
        card.className += ' bg-slate-900/90 border-slate-700/80 text-slate-200';
        const fileTarget = data.file;
        let fileLink = '';
        if (fileTarget) {
            fileLink = `<button onclick="openStudioFileTab('${window.currentTaskId}', '${window.currentActiveRunId || 'run-1'}', '${jsArg(fileTarget)}')" class="inline-flex items-center space-x-1 text-cyan-400 hover:text-cyan-300 underline font-mono ml-1"><i data-lucide="external-link" class="w-3 h-3"></i><span>${escapeHtml(fileTarget)}</span></button>`;
        }

        let paramSnippet = '';
        if (data.params) {
            if (data.params.CommandLine) {
                paramSnippet = `<div class="bg-black/50 p-1.5 rounded font-mono text-[11px] text-emerald-300 mt-1.5 overflow-x-auto">$ ${escapeHtml(data.params.CommandLine)}</div>`;
            } else if (data.params.Query) {
                paramSnippet = `<div class="bg-black/50 p-1.5 rounded font-mono text-[11px] text-amber-300 mt-1.5">Query: ${escapeHtml(data.params.Query)}</div>`;
            }
        }

        card.innerHTML = `
            <div class="flex items-center justify-between mb-1">
                <div class="flex items-center space-x-1.5">
                    <span class="px-1.5 py-0.2 rounded text-[10px] font-bold uppercase ${getRoleBadgeClass(role)}">${role || 'AGENT'}</span>
                    <span class="font-semibold text-amber-300 flex items-center space-x-1">
                        <i data-lucide="zap" class="w-3 h-3"></i>
                        <span>${escapeHtml(data.tool)}</span>
                    </span>
                    ${fileLink}
                </div>
                <span class="text-[10px] text-slate-500 font-mono">${timeStr}</span>
            </div>
            <div class="text-slate-300">${escapeHtml(msg)}</div>
            ${paramSnippet}
        `;
    }
    // 4. Test run results
    else if (evtType === 'TEST_RUN' || msg.includes('TEST RUNNER')) {
        const isFail = msg.includes('FAILED') || evtType === 'ERROR';
        card.className += isFail ? ' bg-rose-950/25 border-rose-500/40 text-rose-200' : ' bg-purple-950/25 border-purple-500/40 text-purple-200';
        card.innerHTML = `
            <div class="flex items-center justify-between font-semibold mb-1 ${isFail ? 'text-rose-300' : 'text-purple-300'}">
                <div class="flex items-center space-x-1.5">
                    <i data-lucide="flask-conical" class="w-3.5 h-3.5"></i>
                    <span>Pytest Sandbox Verification</span>
                </div>
                <span class="text-[10px] opacity-75 font-mono">${timeStr}</span>
            </div>
            <div class="whitespace-pre-wrap font-mono text-[11px]">${escapeHtml(msg)}</div>
        `;
    }
    // 5. Default Agent Thought / Status / Log
    else {
        card.className += ' bg-slate-900/60 border-slate-800 text-slate-300';
        const roleBadge = role ? `<span class="px-1.5 py-0.2 rounded text-[10px] font-bold uppercase mr-1.5 ${getRoleBadgeClass(role)}">${role}</span>` : '';
        card.innerHTML = `
            <div class="flex items-center justify-between mb-1 text-slate-400">
                <div class="flex items-center">${roleBadge}</div>
                <span class="text-[10px] text-slate-600 font-mono">${timeStr}</span>
            </div>
            <div class="leading-relaxed text-slate-200">${escapeHtml(msg)}</div>
        `;
    }

    list.appendChild(card);
    if (typeof lucide !== 'undefined') lucide.createIcons();

    const container = document.getElementById('studioRichStreamContainer');
    if (container) container.scrollTop = container.scrollHeight;
}

function setupCodespaceSSE(taskId) {
    const evtSource = new EventSource(`/api/events/stream?task_id=${taskId}`);
    evtSource.onmessage = function(event) {
        try {
            const data = JSON.parse(event.data);
            if (data.task_id !== taskId) return;

            appendAgyCard(data);

            // Auto-detect and open file when an agent touches a file
            if (data.data && data.data.file) {
                const autoFile = data.data.file;
                const runId = window.currentActiveRunId || 'run-1';
                openStudioFileTab(taskId, runId, encodeURIComponent(autoFile), true);
                refreshFileTree(taskId, runId);
            }

            if (data.event_type === 'STATUS_CHANGE') {
                const roleBadge = document.getElementById('codespaceRoleBadge');
                if (roleBadge && data.role) roleBadge.textContent = data.role;
            }
        } catch (e) {
            console.error('Codespace SSE error:', e);
        }
    };
    return evtSource;
}

async function loadCodespaceRichEvents(taskId, runId) {
    const list = document.getElementById('richStreamCardsList');
    if (!list) return;

    try {
        const res = await fetch(`/api/tasks/${taskId}/events?run_id=${encodeURIComponent(runId)}`);
        if (!res.ok) return;
        const events = await res.json();
        list.innerHTML = '';
        if (events && events.length > 0) {
            events.forEach(evt => appendAgyCard(evt));
            // Auto-open last modified file if no tabs are open
            if (window.studioOpenTabs.length === 0) {
                for (let i = events.length - 1; i >= 0; i--) {
                    if (events[i].data && events[i].data.file) {
                        openStudioFileTab(taskId, runId, encodeURIComponent(events[i].data.file), true);
                        break;
                    }
                }
            }
        }
    } catch (e) {
        console.debug('Failed to load past events for rich stream:', e);
    }
}

async function initCodespaceStudio(taskId, runId) {
    window.currentTaskId = taskId;
    const effectiveRunId = runId || window.currentActiveRunId || 'run-1';
    window.currentActiveRunId = effectiveRunId;
    window.studioOpenTabs = [];
    window.studioActiveTab = null;
    window.studioViewMode = 'code';
    window.studioRightView = 'rich';

    // 1. Fetch File Tree
    await refreshFileTree(taskId, effectiveRunId);

    // 2. Initialize xterm in background
    if (typeof initXtermTerminal === 'function') {
        initXtermTerminal(taskId, effectiveRunId);
    }

    // 3. Setup SSE
    setupCodespaceSSE(taskId);

    // 4. Load past events for rich stream
    loadCodespaceRichEvents(taskId, effectiveRunId);
}

function toggleBlueprintView(mode) {
    const rendered = document.getElementById('planContentRendered');
    const raw = document.getElementById('planContentRaw');
    const renderedBtn = document.getElementById('blueprintViewRenderedBtn');
    const rawBtn = document.getElementById('blueprintViewRawBtn');

    if (mode === 'raw') {
        if (rendered) rendered.classList.add('hidden');
        if (raw) {
            raw.classList.remove('hidden');
            const rawDataEl = document.getElementById('rawPlanData');
            if (rawDataEl) raw.textContent = rawDataEl.textContent;
        }
        if (renderedBtn) renderedBtn.className = 'px-2 py-0.5 rounded text-[11px] font-medium text-slate-400 hover:text-slate-200 transition';
        if (rawBtn) rawBtn.className = 'px-2 py-0.5 rounded text-[11px] font-medium bg-cyan-600 text-white transition';
    } else {
        if (rendered) rendered.classList.remove('hidden');
        if (raw) raw.classList.add('hidden');
        if (renderedBtn) renderedBtn.className = 'px-2 py-0.5 rounded text-[11px] font-medium bg-cyan-600 text-white transition';
        if (rawBtn) rawBtn.className = 'px-2 py-0.5 rounded text-[11px] font-medium text-slate-400 hover:text-slate-200 transition';
    }
}


async function refreshSandboxStats(taskId, targetRunId) {
    try {
        const runId = targetRunId || window.currentActiveRunId || 'run-1';
        const res = await fetch(`/api/sandboxes/${taskId}?run_id=${encodeURIComponent(runId)}`);
        if (!res.ok) return;
        const stats = await res.json();

        const pathEl = document.getElementById('sandboxPath');
        const diskEl = document.getElementById('sandboxDiskSize');
        const filesEl = document.getElementById('sandboxFileCount');
        const cmdEl = document.getElementById('sandboxCommandCount');
        const runBadge = document.getElementById('activeRunLabel');
        const activeRunPill = document.getElementById('activeRunPill');
        const logBtn = document.getElementById('sandboxLogBtn');

        if (pathEl && stats.workspace_path) {
            pathEl.textContent = stats.workspace_path;
            pathEl.title = stats.workspace_path;
        }
        if (diskEl && stats.size_formatted) diskEl.textContent = stats.size_formatted;
        if (filesEl && stats.file_count !== undefined) filesEl.textContent = `${stats.file_count} files`;
        if (cmdEl && stats.command_count !== undefined) cmdEl.textContent = stats.command_count;
        if (runBadge && stats.run_id) runBadge.textContent = stats.run_id;
        if (activeRunPill && stats.run_id) activeRunPill.textContent = stats.run_id;
        if (logBtn && stats.run_id) logBtn.href = `/api/tasks/${taskId}/runs/${stats.run_id}/log`;
    } catch (e) {
        console.debug('Sandbox stat refresh error:', e);
    }
}

async function pollRawLogIfActive(taskId) {
    const rawDiv = document.getElementById('sandboxRawLogs');
    if (!rawDiv || rawDiv.classList.contains('hidden')) return;

    const runId = window.currentActiveRunId || 'run-1';
    try {
        const res = await fetch(`/api/tasks/${taskId}/runs/${runId}/log`);
        if (res.ok) {
            const text = await res.text();
            if (text && rawDiv.textContent !== text) {
                rawDiv.innerHTML = `<pre class="whitespace-pre-wrap leading-relaxed">${escapeHtml(text)}</pre>`;
                rawDiv.scrollTop = rawDiv.scrollHeight;
            }
        }
    } catch (e) {}
}

function selectActiveRunView(taskId, runId) {
    window.currentActiveRunId = runId;

    // Highlight pill in top navigator
    document.querySelectorAll('[id^="runPill_"]').forEach(el => {
        el.className = 'flex items-center space-x-2 px-3 py-2 rounded-lg text-xs font-mono transition border bg-slate-900/90 border-slate-800 text-slate-400 hover:text-white hover:border-slate-700';
    });
    const targetPill = document.getElementById(`runPill_${runId}`);
    if (targetPill) {
        targetPill.className = 'flex items-center space-x-2 px-3 py-2 rounded-lg text-xs font-mono transition border bg-purple-600/20 border-purple-500 text-white shadow-md shadow-purple-950/40';
    }

    // Highlight card in right sidebar
    document.querySelectorAll('[id^="runCard_"]').forEach(el => {
        el.className = el.className.replace(/border-purple-500 bg-purple-500\/15 shadow-md shadow-purple-950\/40/g, 'border-slate-800 bg-slate-900/90');
        el.className = el.className.replace(/border-purple-500\/50 bg-purple-500\/5 shadow-md shadow-purple-950\/30/g, 'border-slate-800 bg-slate-900/90');
    });
    const targetCard = document.getElementById(`runCard_${runId}`);
    if (targetCard) {
        targetCard.className = targetCard.className.replace('border-slate-800', 'border-purple-500 bg-purple-500/15 shadow-md shadow-purple-950/40');
    }

    const runPillText = document.getElementById('activeRunPill');
    if (runPillText) runPillText.textContent = runId;

    const runBadge = document.getElementById('activeRunLabel');
    if (runBadge) runBadge.textContent = runId;

    const logBtn = document.getElementById('sandboxLogBtn');
    if (logBtn) logBtn.href = `/api/tasks/${taskId}/runs/${runId}/log`;

    // Reconnect interactive terminal to the selected run
    initXtermTerminal(taskId, runId);
    loadRunEvents(taskId, runId);
    fetchRawRunLog(taskId, runId);
    refreshSandboxStats(taskId, runId);
    refreshFileTree(taskId, runId);
}


async function loadRunEvents(taskId, runId) {
    const logsContainer = document.getElementById('terminalLogs');
    const tabEvents = document.getElementById('tabStreamEvents');
    if (!logsContainer) return;
    if (tabEvents) tabEvents.textContent = `Agent Stream (${runId})`;

    try {
        const res = await fetch(`/api/tasks/${taskId}/events?run_id=${encodeURIComponent(runId)}`);
        if (!res.ok) return;
        const events = await res.json();
        logsContainer.innerHTML = '';
        if (!events || events.length === 0) {
            logsContainer.innerHTML = `<div class="text-slate-500 italic">No agent events recorded for ${runId}.</div>`;
            return;
        }
        for (const evt of events) {
            const timeStr = new Date(evt.created_at).toTimeString().split(' ')[0];
            const div = document.createElement('div');
            div.className = 'leading-relaxed';
            let roleTag = evt.role ? `<span class="text-cyan-400 font-bold">[${evt.role}]</span> ` : '';
            let msgClass = 'text-slate-300';
            if (evt.event_type === 'ERROR') msgClass = 'text-rose-400 font-semibold';
            else if (evt.event_type === 'STEP') msgClass = 'text-amber-300 font-semibold';
            else if (evt.event_type === 'TEST_RUN') msgClass = 'text-purple-300 font-semibold';
            else if (evt.event_type === 'QUESTION') msgClass = 'text-amber-300 font-bold';
            else if (evt.event_type === 'LEARNING') msgClass = 'text-emerald-300 font-bold';
            else if (evt.event_type === 'STATUS_CHANGE') msgClass = 'text-emerald-300 font-bold';

            div.innerHTML = `<span class="text-slate-600">[${timeStr}]</span> ${roleTag}<span class="${msgClass}">${escapeHtml(evt.message)}</span>`;
            logsContainer.appendChild(div);
        }
        logsContainer.scrollTop = logsContainer.scrollHeight;
    } catch (e) {
        console.error('Failed to load events for run:', e);
    }
}

async function fetchRawRunLog(taskId, runId) {
    const rawDiv = document.getElementById('sandboxRawLogs');
    const tabRaw = document.getElementById('tabStreamRaw');
    if (tabRaw) tabRaw.textContent = `sandbox.log (${runId})`;
    if (rawDiv) {
        try {
            const res = await fetch(`/api/tasks/${taskId}/runs/${runId}/log`);
            const text = await res.text();
            rawDiv.innerHTML = `<pre class="whitespace-pre-wrap leading-relaxed">${escapeHtml(text || `No logs recorded for ${runId}.`)}</pre>`;
            rawDiv.scrollTop = rawDiv.scrollHeight;
        } catch (e) {
            rawDiv.textContent = `Error loading ${runId} log: ` + e.message;
        }
    }
}

async function viewRunLog(taskId, runId) {
    switchStreamTab('raw');
    await fetchRawRunLog(taskId, runId);
}

async function openDossierModal(taskId) {
    const modal = document.getElementById('dossierModal');
    if (!modal) return;
    modal.classList.remove('hidden');

    const readmeContent = document.getElementById('dossierReadmeContent');
    const jsonContent = document.getElementById('dossierJsonContent');

    if (readmeContent) {
        readmeContent.textContent = 'Loading README dossier...';
        try {
            const res = await fetch(`/api/tasks/${taskId}/dossier/readme`);
            const text = await res.text();
            readmeContent.textContent = text || 'No README generated yet.';
        } catch (e) {
            readmeContent.textContent = 'Error loading README: ' + e.message;
        }
    }

    if (jsonContent) {
        jsonContent.textContent = 'Loading task summary JSON...';
        try {
            const res = await fetch(`/api/tasks/${taskId}/dossier`);
            const json = await res.json();
            jsonContent.textContent = JSON.stringify(json, null, 2);
        } catch (e) {
            jsonContent.textContent = 'Error loading JSON: ' + e.message;
        }
    }
}

function closeDossierModal() {
    const modal = document.getElementById('dossierModal');
    if (modal) modal.classList.add('hidden');
}

function switchDossierTab(tab) {
    const tabReadme = document.getElementById('dossierTabReadme');
    const tabJson = document.getElementById('dossierTabJson');
    const contentReadme = document.getElementById('dossierReadmeContainer');
    const contentJson = document.getElementById('dossierJsonContainer');

    if (!contentReadme || !contentJson) return;

    if (tab === 'readme') {
        contentReadme.classList.remove('hidden');
        contentJson.classList.add('hidden');
        if (tabReadme) tabReadme.className = 'px-3 py-1 rounded text-xs font-semibold bg-indigo-600 text-white transition';
        if (tabJson) tabJson.className = 'px-3 py-1 rounded text-xs font-semibold text-slate-400 hover:text-white transition';
    } else {
        contentReadme.classList.add('hidden');
        contentJson.classList.remove('hidden');
        if (tabReadme) tabReadme.className = 'px-3 py-1 rounded text-xs font-semibold text-slate-400 hover:text-white transition';
        if (tabJson) tabJson.className = 'px-3 py-1 rounded text-xs font-semibold bg-indigo-600 text-white transition';
    }
}

async function reloadSandboxesTable() {
    const tbody = document.getElementById('sandboxesTableBody');
    if (!tbody) return;

    try {
        const res = await fetch('/api/sandboxes');
        if (!res.ok) return;
        const sandboxes = await res.json();
        if (!sandboxes || sandboxes.length === 0) return;

        let html = '';
        for (const sb of sandboxes) {
            const runBadge = sb.run_id ? `<span class="text-[10px] bg-purple-500/20 text-purple-300 px-1.5 py-0.2 rounded font-mono font-bold">${sb.run_id}</span>` : '';
            html += `
            <tr class="hover:bg-slate-800/50 transition">
                <td class="py-4 px-4">
                    <div class="flex items-center space-x-2">
                        <i data-lucide="folder-git-2" class="w-4 h-4 text-emerald-400"></i>
                        <a href="/tasks/${sb.task_id}" class="font-mono text-sm font-semibold text-white hover:text-emerald-400 transition">
                            ${sb.task_id}
                        </a>
                        ${runBadge}
                    </div>
                    <div class="text-xs text-slate-500 font-mono mt-0.5 truncate max-w-sm" title="${sb.workspace_path}">
                        ${sb.workspace_path}
                    </div>
                </td>
                <td class="py-4 px-4 whitespace-nowrap">
                    <span class="inline-flex items-center px-2 py-0.5 rounded text-xs font-medium bg-slate-800 border border-slate-700 text-slate-300">
                        ${sb.size_formatted}
                    </span>
                </td>
                <td class="py-4 px-4 whitespace-nowrap text-xs text-slate-300">
                    ${sb.file_count} files
                </td>
                <td class="py-4 px-4 whitespace-nowrap text-xs text-emerald-400 font-mono font-semibold">
                    ${sb.command_count}
                </td>
                <td class="py-4 px-4 whitespace-nowrap text-xs text-slate-400">
                    ${sb.last_modified ? sb.last_modified.substring(0, 19).replace('T', ' ') : 'N/A'}
                </td>
                <td class="py-4 px-4 whitespace-nowrap text-right space-x-2">
                    <button onclick="viewSandboxLog('${sb.task_id}')" class="bg-slate-700 hover:bg-slate-600 text-slate-200 hover:text-white text-xs px-3 py-1.5 rounded font-medium transition inline-flex items-center space-x-1">
                        <span>Logs</span>
                    </button>
                    <a href="/tasks/${sb.task_id}" class="bg-emerald-600/80 hover:bg-emerald-600 text-white text-xs px-3 py-1.5 rounded font-medium transition inline-flex items-center space-x-1">
                        <span>Task</span>
                    </a>
                    <button onclick="deleteSandbox('${sb.task_id}')" class="bg-rose-600/80 hover:bg-rose-600 text-white text-xs px-3 py-1.5 rounded font-medium transition inline-flex items-center space-x-1">
                        <span>Delete</span>
                    </button>
                </td>
            </tr>`;
        }
        tbody.innerHTML = html;
        if (window.lucide) lucide.createIcons();
    } catch (e) {
        console.debug('Sandboxes table reload error:', e);
    }
}

function setupSandboxesLiveUpdates() {
    const tbody = document.getElementById('sandboxesTableBody');
    if (!tbody) return;

    const evtSource = new EventSource('/api/events/stream');
    evtSource.onmessage = function() {
        reloadSandboxesTable();
    };

    setInterval(reloadSandboxesTable, 3000);
}

// Formalized Git Discussion Thread
function renderDiscussionComments() {
    const rawEl = document.getElementById('rawCommentsData');
    const container = document.getElementById('discussionThreadContainer');
    const countBadge = document.getElementById('discussionCountBadge');
    if (!rawEl || !container) return;

    const rawText = rawEl.textContent.trim();
    if (!rawText) {
        container.innerHTML = '<div class="text-xs text-slate-500 italic p-3 text-center">No discussion comments recorded yet.</div>';
        if (countBadge) countBadge.textContent = '0';
        return;
    }

    const lines = rawText.split('\n');
    const comments = [];
    let currentAuthor = '';
    let currentBodyLines = [];

    lines.forEach(line => {
        const match = line.match(/^- @([a-zA-Z0-9_\-\.]+):\s*(.*)$/);
        if (match) {
            if (currentAuthor) {
                comments.push({ author: currentAuthor, body: currentBodyLines.join('\n').trim() });
            }
            currentAuthor = match[1];
            currentBodyLines = [match[2]];
        } else if (currentAuthor) {
            currentBodyLines.push(line);
        }
    });
    if (currentAuthor) {
        comments.push({ author: currentAuthor, body: currentBodyLines.join('\n').trim() });
    }

    // Deduplicate identical automated bot notices
    const seenBotNotice = new Set();
    const filteredComments = comments.filter(c => {
        const isBot = c.body.includes('Issueforge Autonomous Agent');
        if (isBot) {
            const key = c.body.substring(0, 50);
            if (seenBotNotice.has(key)) return false;
            seenBotNotice.add(key);
        }
        return true;
    });

    if (countBadge) countBadge.textContent = filteredComments.length;

    let html = '';
    filteredComments.forEach((c, idx) => {
        const isBot = c.body.includes('Issueforge Autonomous Agent') || c.author.toLowerCase() === 'operator';
        const avatarBg = isBot ? 'bg-gradient-to-br from-emerald-500 to-teal-700 text-white' : 'bg-gradient-to-br from-indigo-500 to-purple-700 text-white';
        const initial = isBot ? '⚡' : c.author.substring(0, 2).toUpperCase();
        const rolePill = isBot ? '<span class="text-[9px] bg-emerald-500/20 text-emerald-300 border border-emerald-500/30 px-1.5 py-0.2 rounded font-mono font-semibold">ISSUEFORGE AGENT</span>' : '<span class="text-[9px] bg-indigo-500/20 text-indigo-300 border border-indigo-500/30 px-1.5 py-0.2 rounded font-mono font-semibold">REPORTER</span>';

        let parsedBody = escapeHtml(c.body);
        if (window.marked) {
            try {
                parsedBody = renderMarkdown(c.body);
            } catch (e) {}
        }

        html += `
        <div class="bg-slate-900/90 border border-slate-800 rounded-xl p-3.5 space-y-2 shadow-sm transition hover:border-slate-700">
            <div class="flex items-center justify-between">
                <div class="flex items-center space-x-2">
                    <div class="w-6 h-6 rounded-full flex items-center justify-center text-[10px] font-bold shadow ${avatarBg}">
                        ${escapeHtml(initial)}
                    </div>
                    <span class="text-xs font-semibold text-slate-200">@${escapeHtml(c.author)}</span>
                    ${rolePill}
                </div>
                <span class="text-[10px] text-slate-500 font-mono">#${idx + 1}</span>
            </div>
            <div class="text-xs text-slate-300 font-sans leading-relaxed prose prose-invert max-w-none text-[12px]">
                ${parsedBody}
            </div>
        </div>`;
    });

    container.innerHTML = html || '<div class="text-xs text-slate-500 italic p-3 text-center">No discussion comments recorded.</div>';
}

// Post New Discussion Comment directly to Git & Database
async function postDiscussionComment(event, taskId) {
    event.preventDefault();
    const input = document.getElementById('newCommentInput');
    const btn = document.getElementById('sendCommentBtn');
    if (!input) return;
    const text = input.value.trim();
    if (!text) return;

    if (btn) {
        btn.disabled = true;
        btn.textContent = 'Posting...';
    }

    try {
        const res = await fetch(`/api/tasks/${taskId}/comment`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ comment: text })
        });
        if (res.ok) {
            input.value = '';
            window.location.reload();
        } else {
            alert('Failed to post comment: ' + (await res.text()));
        }
    } catch (e) {
        alert('Error: ' + e.message);
    } finally {
        if (btn) {
            btn.disabled = false;
            btn.textContent = 'Send';
        }
    }
}

// Toggle Subtask Checklist item
async function toggleTaskSubtask(taskId, subtaskId) {
    try {
        const res = await fetch(`/api/tasks/${taskId}/subtasks/${subtaskId}/toggle`, {
            method: 'POST'
        });
        if (res.ok) {
            const data = await res.json();
            updateSubtasksUI(data.subtasks);
        }
    } catch (e) {
        console.error('Failed to toggle subtask:', e);
    }
}

function updateSubtasksUI(subtasks) {
    if (!subtasks || !subtasks.length) return;
    const completedCount = subtasks.filter(s => s.completed).length;
    const total = subtasks.length;
    const pct = Math.round((completedCount / total) * 100);

    const countEl = document.getElementById('subtaskCountLabel');
    if (countEl) countEl.textContent = `${completedCount}/${total} (${pct}%)`;

    const barEl = document.getElementById('subtaskProgressBar');
    if (barEl) barEl.style.width = `${pct}%`;

    subtasks.forEach(s => {
        const itemEl = document.getElementById(`subtaskItem_${s.id}`);
        const iconEl = document.getElementById(`subtaskIcon_${s.id}`);
        if (itemEl) {
            if (s.completed) {
                itemEl.classList.add('text-slate-500', 'line-through');
                itemEl.classList.remove('text-slate-200');
            } else {
                itemEl.classList.remove('text-slate-500', 'line-through');
                itemEl.classList.add('text-slate-200');
            }
        }
        if (iconEl) {
            iconEl.setAttribute('data-lucide', s.completed ? 'check-square' : 'square');
            iconEl.className = `w-3.5 h-3.5 shrink-0 mt-0.5 ${s.completed ? 'text-emerald-400' : 'text-slate-600'}`;
        }
    });
    if (window.lucide) lucide.createIcons();
}

// Global Mermaid Zoom State
window.currentMermaidScale = 1.0;

// Render Formal Plan Specification and Mermaid Diagrams
function renderPlanSpecification() {
    const rawEl = document.getElementById('rawPlanData');
    const mount = document.getElementById('planContentRendered');
    const mermaidControls = document.getElementById('mermaidControls');
    if (!rawEl || !mount) return;

    const planText = rawEl.textContent.trim();
    if (!planText) {
        mount.innerHTML = '<div class="text-xs text-slate-500 italic p-4 text-center">No architecture plan generated yet. The planner agent will create it during the PLANNING stage.</div>';
        if (mermaidControls) mermaidControls.classList.add('hidden');
        return;
    }

    if (window.marked) {
        try {
            mount.innerHTML = renderMarkdown(planText);
            
            // Render and isolate any mermaid code blocks
            let hasMermaid = false;
            mount.querySelectorAll('pre code.language-mermaid, pre code.language-graph').forEach((block, idx) => {
                hasMermaid = true;
                const parent = block.parentElement;
                const container = document.createElement('div');
                container.className = 'mermaid-container my-4 p-3 bg-slate-900/90 rounded-lg text-center overflow-x-auto border border-slate-800';
                
                const div = document.createElement('div');
                div.className = 'mermaid';
                div.id = `mermaid-chart-${idx}`;
                div.textContent = block.textContent;
                
                container.appendChild(div);
                parent.replaceWith(container);
            });

            if (hasMermaid && mermaidControls) {
                mermaidControls.classList.remove('hidden');
                mermaidControls.classList.add('flex');
            } else if (mermaidControls) {
                mermaidControls.classList.add('hidden');
            }

            if (window.mermaid) {
                mermaid.run();
            }

            // Attach 1-click copy buttons to code blocks
            mount.querySelectorAll('pre').forEach(pre => {
                if (pre.closest('.mermaid-container')) return;
                pre.classList.add('relative', 'group');
                
                const copyBtn = document.createElement('button');
                copyBtn.className = 'absolute top-2 right-2 text-[10px] px-2 py-0.5 rounded bg-slate-800 hover:bg-slate-700 text-slate-400 hover:text-slate-200 border border-slate-700/60 opacity-0 group-hover:opacity-100 transition shadow-sm select-none';
                copyBtn.textContent = 'Copy';
                copyBtn.onclick = (e) => {
                    e.stopPropagation();
                    const code = pre.querySelector('code') ? pre.querySelector('code').innerText : pre.innerText;
                    navigator.clipboard.writeText(code).then(() => {
                        copyBtn.textContent = 'Copied!';
                        copyBtn.classList.add('text-emerald-400');
                        setTimeout(() => {
                            copyBtn.textContent = 'Copy';
                            copyBtn.classList.remove('text-emerald-400');
                        }, 1500);
                    });
                };
                pre.appendChild(copyBtn);
            });

        } catch (e) {
            mount.textContent = planText;
        }
    } else {
        mount.textContent = planText;
    }
}

// Mermaid Diagram Zoom and Export Functions
function zoomMermaid(factor) {
    window.currentMermaidScale = Math.max(0.4, Math.min(3.0, (window.currentMermaidScale || 1.0) * factor));
    applyMermaidScale();
}

function resetMermaidZoom() {
    window.currentMermaidScale = 1.0;
    applyMermaidScale();
}

function applyMermaidScale() {
    document.querySelectorAll('.mermaid svg').forEach(svg => {
        svg.style.transform = `scale(${window.currentMermaidScale})`;
        svg.style.transformOrigin = 'center top';
        svg.style.transition = 'transform 0.2s ease-out';
    });
}

function exportMermaidSVG() {
    const svgEl = document.querySelector('.mermaid svg');
    if (!svgEl) {
        showToast('No Mermaid SVG found to export', 'error');
        return;
    }
    const svgData = new XMLSerializer().serializeToString(svgEl);
    const blob = new Blob([svgData], { type: 'image/svg+xml;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const downloadLink = document.createElement('a');
    downloadLink.href = url;
    downloadLink.download = 'architecture_blueprint.svg';
    document.body.appendChild(downloadLink);
    downloadLink.click();
    document.body.removeChild(downloadLink);
    URL.revokeObjectURL(url);
    showToast('Exported architecture_blueprint.svg successfully!', 'success');
}

// Quick copy helper
function copyToClipboard(text, elemId) {
    navigator.clipboard.writeText(text).then(() => {
        const el = document.getElementById(elemId);
        if (el) {
            const original = el.innerHTML;
            el.innerHTML = '<span class="text-emerald-400 font-bold">Copied!</span>';
            setTimeout(() => el.innerHTML = original, 1500);
        }
    });
}




/* ============================================================================
   Obsidian Canvas renderer — draws task_dag.canvas natively in the browser.

   The .canvas file is the pipeline's live execution DAG. Rendering it here means
   the operator does not need Obsidian installed (and it works when the dashboard
   is browsed remotely from the Jetson, where an obsidian:// link cannot resolve).
   Faithful to the JSONCanvas geometry, so it matches what Obsidian would show.
   ========================================================================== */

// Obsidian's six predefined canvas colour slots.
const CANVAS_COLORS = {
    '1': { border: '#f43f5e', bg: 'rgba(244,63,94,0.12)',   label: 'Failed'   },
    '2': { border: '#f59e0b', bg: 'rgba(245,158,11,0.12)',  label: 'Warning'  },
    '3': { border: '#eab308', bg: 'rgba(234,179,8,0.12)',   label: 'Running'  },
    '4': { border: '#10b981', bg: 'rgba(16,185,129,0.12)',  label: 'Passed'   },
    '5': { border: '#06b6d4', bg: 'rgba(6,182,212,0.12)',   label: 'Awaiting' },
    '6': { border: '#a855f7', bg: 'rgba(168,85,247,0.12)',  label: 'Review'   },
};
const CANVAS_DEFAULT = { border: '#334155', bg: 'rgba(30,41,59,0.6)', label: 'Idle' };

function canvasAnchor(node, side) {
    const cx = node.x + node.width / 2;
    const cy = node.y + node.height / 2;
    switch (side) {
        case 'top':    return { x: cx, y: node.y };
        case 'bottom': return { x: cx, y: node.y + node.height };
        case 'left':   return { x: node.x, y: cy };
        case 'right':  return { x: node.x + node.width, y: cy };
        default:       return { x: cx, y: cy };
    }
}

function canvasEdgePath(from, to, fromSide, toSide) {
    // Bezier with control points pushed out along each side's normal, so edges
    // leave and enter nodes perpendicular rather than cutting across them.
    const d = 60;
    const push = (p, side) => {
        if (side === 'right')  return { x: p.x + d, y: p.y };
        if (side === 'left')   return { x: p.x - d, y: p.y };
        if (side === 'top')    return { x: p.x, y: p.y - d };
        if (side === 'bottom') return { x: p.x, y: p.y + d };
        return p;
    };
    const c1 = push(from, fromSide);
    const c2 = push(to, toSide);
    return `M ${from.x} ${from.y} C ${c1.x} ${c1.y}, ${c2.x} ${c2.y}, ${to.x} ${to.y}`;
}

function renderCanvasNodeText(text) {
    if (window.marked) {
        try { return renderMarkdown(text); } catch (e) { /* fall through */ }
    }
    const div = document.createElement('div');
    div.textContent = text || '';
    return div.innerHTML;
}

async function renderTaskCanvas(taskId) {
    const host = document.getElementById('taskCanvasViewport');
    const empty = document.getElementById('taskCanvasEmpty');
    if (!host) return;

    let data;
    try {
        const res = await fetch(`/api/tasks/${taskId}/canvas`);
        if (!res.ok) throw new Error(`canvas unavailable (${res.status})`);
        data = await res.json();
    } catch (e) {
        host.innerHTML = '';
        if (empty) {
            empty.textContent = 'No execution canvas yet — it is created when the task starts running.';
            empty.classList.remove('hidden');
        }
        return;
    }

    const nodes = data.nodes || [];
    const edges = data.edges || [];
    if (!nodes.length) {
        host.innerHTML = '';
        if (empty) { empty.textContent = 'Canvas is empty.'; empty.classList.remove('hidden'); }
        return;
    }
    if (empty) empty.classList.add('hidden');

    // Bounding box over every node, then scale to fit the container width.
    const pad = 40;
    const minX = Math.min(...nodes.map(n => n.x)) - pad;
    const minY = Math.min(...nodes.map(n => n.y)) - pad;
    const maxX = Math.max(...nodes.map(n => n.x + n.width)) + pad;
    const maxY = Math.max(...nodes.map(n => n.y + n.height)) + pad;
    const boxW = maxX - minX;
    const boxH = maxY - minY;

    const available = host.clientWidth || host.parentElement.clientWidth || 900;
    // The 7-stage DAG is ~2460px wide. Shrinking that into a 900px column gives a 0.37
    // scale and ~4px text, which is unreadable — so auto mode keeps a legibility floor
    // and lets the container scroll horizontally instead. "Fit to width" opts into the
    // squeezed overview explicitly.
    const MIN_READABLE_SCALE = 0.8;
    let scale;
    if (window.canvasZoom) {
        scale = window.canvasZoom;
    } else if (window.canvasFitMode) {
        scale = Math.min(1, available / boxW);
    } else {
        scale = Math.max(MIN_READABLE_SCALE, Math.min(1, available / boxW));
    }

    const byId = {};
    nodes.forEach(n => { byId[n.id] = n; });

    const svgPaths = edges.map(e => {
        const a = byId[e.fromNode], b = byId[e.toNode];
        if (!a || !b) return '';
        const p1 = canvasAnchor(a, e.fromSide);
        const p2 = canvasAnchor(b, e.toSide);
        return `<path d="${canvasEdgePath(p1, p2, e.fromSide, e.toSide)}"
                      fill="none" stroke="#475569" stroke-width="2"
                      marker-end="url(#issueforgeCanvasArrow)" />`;
    }).join('');

    const nodeHtml = nodes.map(n => {
        const c = CANVAS_COLORS[n.color] || CANVAS_DEFAULT;
        const running = n.color === '3';
        return `
        <div class="absolute rounded-lg border-2 overflow-hidden shadow-lg ${running ? 'animate-pulse' : ''}"
             style="left:${n.x - minX}px; top:${n.y - minY}px; width:${n.width}px; height:${n.height}px;
                    border-color:${c.border}; background:${c.bg}; backdrop-filter: blur(2px);"
             title="${c.label}">
            <div class="h-full overflow-auto px-3 py-2 text-[14px] leading-snug text-slate-100 issueforge-canvas-node">
                ${renderCanvasNodeText(n.text)}
            </div>
        </div>`;
    }).join('');

    host.style.height = `${boxH * scale}px`;
    host.innerHTML = `
        <div style="transform: scale(${scale}); transform-origin: top left; width:${boxW}px; height:${boxH}px; position:relative;">
            <svg width="${boxW}" height="${boxH}" style="position:absolute; inset:0; pointer-events:none;"
                 viewBox="${minX} ${minY} ${boxW} ${boxH}">
                <defs>
                    <marker id="issueforgeCanvasArrow" viewBox="0 0 10 10" refX="9" refY="5"
                            markerWidth="6" markerHeight="6" orient="auto-start-reverse">
                        <path d="M 0 0 L 10 5 L 0 10 z" fill="#475569" />
                    </marker>
                </defs>
                ${svgPaths}
            </svg>
            ${nodeHtml}
        </div>`;
}

function zoomTaskCanvas(delta) {
    const current = window.canvasZoom || 1;
    window.canvasZoom = Math.min(2, Math.max(0.3, current + delta));
    window.canvasFitMode = false;
    renderTaskCanvas(window.taskIdForCanvas);
}

// Toggles between the legible default (scroll horizontally) and a squeezed whole-DAG view.
function resetTaskCanvasZoom() {
    window.canvasZoom = null;
    window.canvasFitMode = !window.canvasFitMode;
    renderTaskCanvas(window.taskIdForCanvas);
}

async function submitCanvasDirective(taskId) {
    const input = document.getElementById('canvasDirectiveInput');
    const text = (input.value || '').trim();
    if (!text) return;
    try {
        const res = await fetch(`/api/tasks/${taskId}/canvas/directive`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ text })
        });
        if (!res.ok) {
            const err = await res.json().catch(() => ({}));
            throw new Error(err.detail || res.statusText);
        }
        input.value = '';
        await renderTaskCanvas(taskId);
        alert('Directive queued.\n\nIt applies at the next planner/coder/tester turn — it does not interrupt the agent currently running.');
    } catch (e) {
        alert('Could not queue directive: ' + e.message);
    }
}
