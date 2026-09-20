(function () {
  const forms = document.querySelectorAll('[data-sync-form]');
  const buttons = document.querySelectorAll('[data-sync-button]');
  const progress = document.querySelector('[data-sync-progress]');
  const status = document.querySelector('[data-sync-status]');
  const error = document.querySelector('[data-sync-error]');
  let pollTimer = null;

  function setButtonsDisabled(disabled, activeButton) {
    buttons.forEach((button) => {
      button.disabled = disabled;
    });
    if (activeButton) activeButton.textContent = 'Syncing…';
  }

  function showProgress(message) {
    if (progress) progress.hidden = false;
    if (status) status.textContent = message || '';
  }

  function hidePreviousError() {
    if (!error) return;
    error.hidden = true;
    error.textContent = '';
  }

  function showSyncError(message) {
    if (!error) return;
    error.textContent = message || 'Sync failed.';
    error.hidden = false;
  }

  function pollSyncStatus(reloadWhenComplete) {
    if (pollTimer) window.clearTimeout(pollTimer);

    fetch('/api/sync-status', { cache: 'no-store' })
      .then((response) => {
        if (!response.ok) throw new Error(`Status request failed (${response.status})`);
        return response.json();
      })
      .then((state) => {
        showProgress(state.message || (state.running ? 'Syncing…' : ''));
        if (state.running) {
          hidePreviousError();
          setButtonsDisabled(true);
          pollTimer = window.setTimeout(() => pollSyncStatus(reloadWhenComplete), 1000);
          return;
        }

        setButtonsDisabled(false);
        if (state.error) showSyncError(state.error);
        if (reloadWhenComplete) {
          window.setTimeout(() => window.location.reload(), 250);
        }
      })
      .catch((requestError) => {
        showProgress(`Checking sync status… ${requestError.message}`);
        pollTimer = window.setTimeout(() => pollSyncStatus(reloadWhenComplete), 1000);
      });
  }

  forms.forEach((form) => {
    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      const button = form.querySelector('[data-sync-button]');
      setButtonsDisabled(true, button);
      hidePreviousError();
      showProgress('Connecting to iRStats...');

      try {
        const response = await fetch(form.action, {
          method: 'POST',
          body: new FormData(form),
          credentials: 'same-origin',
        });
        if (!response.ok) throw new Error(`Sync request failed (${response.status})`);
        const target = new URL(response.url, window.location.href);
        if (target.pathname !== '/') {
          window.location.assign(target.href);
          return;
        }
        pollSyncStatus(true);
      } catch (requestError) {
        setButtonsDisabled(false);
        showSyncError(requestError.message);
      }
    });
  });

  if (progress && progress.dataset.syncRunning === 'true') {
    setButtonsDisabled(true);
    pollSyncStatus(true);
  }

  const data = window.IRACING_WEEKLY_DATA;
  const canvas = document.getElementById('iratingChart');
  if (!canvas || !data || !window.Chart) return;

  const weeklyPoints = (data.weekly || []).filter((item) => item.end_irating !== null).map((item) => ({
    x: item.race_week_label,
    y: item.end_irating,
    delta: item.delta,
    races: item.race_count,
  }));
  const racePoints = data.everyRace || [];
  const chart = new Chart(canvas, {
    type: 'line',
    data: {
      datasets: [{
        label: 'iRating',
        data: weeklyPoints,
        borderColor: '#67e8c1',
        backgroundColor: 'rgba(103,232,193,.12)',
        pointBackgroundColor: '#67e8c1',
        pointRadius: 4,
        tension: .25,
        fill: true,
      }],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: 'index', intersect: false },
      scales: {
        x: { type: 'category', ticks: { color: '#8d9bb0' }, grid: { color: 'rgba(39,52,72,.45)' } },
        y: { ticks: { color: '#8d9bb0' }, grid: { color: 'rgba(39,52,72,.45)' } },
      },
      plugins: {
        legend: { display: false },
        tooltip: { callbacks: { label: (context) => ` ${context.parsed.y} iR · ${context.raw.delta >= 0 ? '+' : ''}${context.raw.delta ?? 'n/a'} · ${context.raw.races ?? 1} races` } },
      },
    },
  });

  document.querySelectorAll('[data-chart-mode]').forEach((button) => {
    button.addEventListener('click', () => {
      document.querySelectorAll('[data-chart-mode]').forEach((item) => item.classList.remove('active'));
      button.classList.add('active');
      const raceMode = button.dataset.chartMode === 'race';
      chart.data.datasets[0].data = raceMode ? racePoints.map((item) => ({ x: item.label, y: item.y, delta: item.delta, races: 1 })) : weeklyPoints;
      chart.options.scales.x.type = 'category';
      chart.update();
    });
  });
})();
