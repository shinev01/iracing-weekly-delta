(function () {
  document.querySelectorAll('[data-sync-form]').forEach((form) => {
    form.addEventListener('submit', () => {
      document.querySelectorAll('[data-sync-button]').forEach((button) => {
        button.disabled = true;
      });
      const button = form.querySelector('[data-sync-button]');
      if (button) button.textContent = 'Syncing…';
      const status = document.querySelector('[data-sync-status]');
      if (status) status.textContent = 'Request sent. Waiting for the public source…';
    });
  });

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
