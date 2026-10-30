// ═══ Theme Toggle ════════════════════════════════════════════
(function() {
    const savedTheme = localStorage.getItem('theme') || 'light';
    document.documentElement.setAttribute('data-theme', savedTheme);
    updateThemeIcon(savedTheme);
})();

document.getElementById('themeToggle')?.addEventListener('click', () => {
    const current = document.documentElement.getAttribute('data-theme');
    const next = current === 'light' ? 'dark' : 'light';
    document.documentElement.setAttribute('data-theme', next);
    localStorage.setItem('theme', next);
    updateThemeIcon(next);
    showToast(`Switched to ${next} mode`, 'info');
});

function updateThemeIcon(theme) {
    const icon = document.getElementById('themeIcon');
    if (icon) icon.textContent = theme === 'dark' ? '☀️' : '🌙';
}

// ═══ Sidebar Toggle (Mobile) ═════════════════════════════════
document.getElementById('menuToggle')?.addEventListener('click', () => {
    document.querySelector('.sidebar').classList.toggle('open');
});

// ═══ Toast ═══════════════════════════════════════════════════
function showToast(message, type = 'info', duration = 3000) {
    const container = document.getElementById('toastContainer');
    if (!container) return;
    const toast = document.createElement('div');
    toast.className = `toast toast-${type}`;
    const icons = { success: '✓', error: '⚠', info: 'ℹ' };
    toast.innerHTML = `<span>${icons[type] || 'ℹ'}</span><span>${message}</span>`;
    container.appendChild(toast);
    setTimeout(() => {
        toast.style.opacity = '0';
        toast.style.transform = 'translateX(100%)';
        toast.style.transition = 'all 0.3s';
        setTimeout(() => toast.remove(), 300);
    }, duration);
}

// ═══ Health Monitoring (Sidebar footer) ══════════════════════
async function updateHealth() {
    try {
        const res = await fetch('/api/health');
        if (!res.ok) return;
        const data = await res.json();

        const cpuBar = document.getElementById('cpuBar');
        const ramBar = document.getElementById('ramBar');
        const cpuText = document.getElementById('cpuText');
        const ramText = document.getElementById('ramText');

        if (cpuBar) cpuBar.style.width = Math.min(data.cpu, 100) + '%';
        if (ramBar) ramBar.style.width = data.memory.percent + '%';
        if (cpuText) cpuText.textContent = data.cpu.toFixed(0) + '%';
        if (ramText) ramText.textContent = data.memory.percent + '%';
    } catch (e) { /* ignore */ }
}

if (document.getElementById('healthMini')) {
    updateHealth();
    setInterval(updateHealth, 5000);
}

// ═══ Auto-refresh dashboard stats every 15s ══════════════════
if (document.querySelector('.stats-grid') && !document.querySelector('form')) {
    setInterval(async () => {
        try {
            const res = await fetch('/api/stats');
            if (!res.ok) return;
            const data = await res.json();
            // Optional: update specific numbers without full reload
        } catch (e) { /* ignore */ }
    }, 15000);
}

// ═══ Confirm helper ══════════════════════════════════════════
window.confirmAction = function(message, callback) {
    if (confirm(message)) callback();
};

// ═══ Keyboard Shortcuts ══════════════════════════════════════
document.addEventListener('keydown', (e) => {
    // Ctrl+K: Focus search
    if ((e.ctrlKey || e.metaKey) && e.key === 'k') {
        e.preventDefault();
        const search = document.querySelector('input[name="number"]');
        if (search) search.focus();
    }
    // Esc: Close mobile sidebar
    if (e.key === 'Escape') {
        document.querySelector('.sidebar')?.classList.remove('open');
    }
});

console.log('%cChecker Admin v2.13', 'color: #6366f1; font-size: 16px; font-weight: bold;');
console.log('%cType "showToast(\'Hello\', \'success\')" to test toasts', 'color: #64748b; font-size: 11px;');