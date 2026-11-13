'use strict';
for (const form of document.querySelectorAll('[data-mail-selection]')) {
  const all = form.querySelector('[data-mail-select-all]');
  const checks = [...form.querySelectorAll('[name="uids"]:not(:disabled)')];
  const button = form.querySelector('button[name="action"]');
  const count = form.querySelector('[data-mail-selected-count]');
  const update = () => {
    const selected = checks.filter(check => check.checked).length;
    all.checked = checks.length > 0 && selected === checks.length;
    all.indeterminate = selected > 0 && selected < checks.length;
    button.disabled = selected === 0;
    count.textContent = `Выбрано: ${selected}`;
  };
  all.addEventListener('change', () => { for (const check of checks) check.checked = all.checked; update(); });
  for (const check of checks) check.addEventListener('change', update);
  update();
}
